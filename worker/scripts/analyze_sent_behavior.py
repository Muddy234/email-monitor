"""Analyze sent email behavior from local Outlook via COM.

Pulls sent items directly from Outlook desktop, computes behavioral
signals, saves results to a local JSON file.

Usage:
    python scripts/analyze_sent_behavior.py
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import win32com.client


# ---------------------------------------------------------------------------
# Outlook COM helpers
# ---------------------------------------------------------------------------

# Outlook folder constants
OL_FOLDER_INBOX = 6
OL_FOLDER_SENT = 5


def get_outlook():
    """Connect to running Outlook instance."""
    try:
        return win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    except Exception as e:
        print(f"ERROR: Could not connect to Outlook. Is it running?\n{e}")
        sys.exit(1)


def _resolve_smtp(item, field="sender"):
    """Resolve the SMTP email address from an Exchange item.

    Outlook COM often returns X500/Exchange DNs instead of SMTP addresses
    for internal contacts. This uses PropertyAccessor to get the real SMTP.
    """
    try:
        if field == "sender":
            addr = (item.SenderEmailAddress or "").lower()
            if "@" in addr and "/o=" not in addr:
                return addr
            # Try PropertyAccessor for SMTP
            try:
                PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
                return item.PropertyAccessor.GetProperty(PR_SMTP).lower()
            except Exception:
                pass
            # Try Sender.GetExchangeUser
            try:
                sender = item.Sender
                if sender:
                    eu = sender.GetExchangeUser()
                    if eu:
                        return eu.PrimarySmtpAddress.lower()
            except Exception:
                pass
            return addr
    except Exception:
        return ""


def _resolve_recipients(item):
    """Resolve SMTP addresses for all TO and CC recipients."""
    to_addrs = []
    cc_addrs = []
    try:
        for recip in item.Recipients:
            try:
                addr = (recip.Address or "").lower()
                # If it's an Exchange DN, resolve SMTP
                if "/o=" in addr or "@" not in addr:
                    try:
                        eu = recip.AddressEntry.GetExchangeUser()
                        if eu and eu.PrimarySmtpAddress:
                            addr = eu.PrimarySmtpAddress.lower()
                    except Exception:
                        # Try PropertyAccessor
                        try:
                            PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
                            addr = recip.AddressEntry.PropertyAccessor.GetProperty(PR_SMTP).lower()
                        except Exception:
                            pass

                # recip.Type: 1=TO, 2=CC, 3=BCC
                if recip.Type == 1:
                    to_addrs.append(addr)
                elif recip.Type == 2:
                    cc_addrs.append(addr)
            except Exception:
                continue
    except Exception:
        pass
    return to_addrs, cc_addrs


def _fetch_from_folder(folder, cutoff_str):
    """Pull mail items from a single Outlook folder."""
    emails = []
    try:
        items = folder.Items
        items.Sort("[SentOn]", True)
        items = items.Restrict(f"[SentOn] >= '{cutoff_str}'")
    except Exception:
        return emails

    for item in items:
        try:
            if item.Class != 43:  # olMail = 43
                continue

            sender_email = _resolve_smtp(item, field="sender")
            to_addrs, cc_addrs = _resolve_recipients(item)

            emails.append({
                "subject": item.Subject or "",
                "sender_email": sender_email,
                "sender_name": item.SenderName or "",
                "to_field": "; ".join(to_addrs),
                "cc_field": "; ".join(cc_addrs),
                "sent_time": item.SentOn.isoformat() if item.SentOn else None,
                "received_time": item.ReceivedTime.isoformat() if item.ReceivedTime else None,
                "body": (item.Body or "")[:5000],
                "has_attachments": item.Attachments.Count > 0,
                "attachment_count": item.Attachments.Count,
                "conversation_id": item.ConversationID or "",
                "conversation_topic": item.ConversationTopic or "",
                "importance": item.Importance,  # 0=Low, 1=Normal, 2=High
            })
        except Exception:
            continue

    return emails


# Folders to skip when scanning for received mail
_SKIP_FOLDERS = {
    "deleted items", "outbox", "sent items", "drafts", "junk email",
    "sync issues", "rss feeds", "notes", "journal", "calendar",
    "contacts", "conversation action settings", "conversation history",
    "quick step settings", "externalcontacts", "eventcheckpoints",
    "tasks", "files", "archive", "social activity notifications",
    "yammer root",
}


def fetch_folder_emails(namespace, folder_id, days=30):
    """Pull emails from an Outlook folder for the last N days."""
    folder = namespace.GetDefaultFolder(folder_id)
    cutoff_str = (datetime.now() - timedelta(days=days)).strftime("%m/%d/%Y")
    return _fetch_from_folder(folder, cutoff_str)


def fetch_all_received_emails(namespace, days=30):
    """Pull received emails from Inbox AND custom mail folders.

    Outlook rules can move emails to sibling folders (e.g., 'Tyler',
    'Becky'). This scans all non-system folders to catch those.
    """
    cutoff_str = (datetime.now() - timedelta(days=days)).strftime("%m/%d/%Y")
    inbox = namespace.GetDefaultFolder(OL_FOLDER_INBOX)

    # Start with the Inbox itself
    all_emails = _fetch_from_folder(inbox, cutoff_str)
    print(f"  Inbox: {len(all_emails)} emails")

    # Scan sibling folders (same parent as Inbox)
    try:
        parent = inbox.Parent
        for folder in parent.Folders:
            fname = folder.Name.lower()
            if fname == inbox.Name.lower():
                continue  # Already scanned
            if fname in _SKIP_FOLDERS:
                continue
            folder_emails = _fetch_from_folder(folder, cutoff_str)
            if folder_emails:
                print(f"  {folder.Name}: {len(folder_emails)} emails")
                all_emails.extend(folder_emails)
    except Exception as e:
        print(f"  Warning: could not scan sibling folders: {e}")

    return all_emails


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Email address extraction
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def extract_emails_from_field(text):
    """Extract email addresses from a TO/CC field.

    Outlook COM returns semicolon-separated display names, not always
    email addresses. Try regex first, fall back to splitting on ';'.
    """
    if not text:
        return []
    found = EMAIL_RE.findall(text.lower())
    if found:
        return found
    # Fallback: split on semicolons and clean up
    return [name.strip().lower() for name in text.split(";") if name.strip()]


def get_domain(email):
    try:
        return email.split("@")[1].lower()
    except (IndexError, AttributeError):
        return "unknown"


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def analyze_recipient_frequency(sent_emails):
    """1. Who the user writes to — TO count, CC count, and total touches."""
    to_counter = Counter()
    cc_counter = Counter()
    for email in sent_emails:
        for addr in extract_emails_from_field(email.get("to_field")):
            to_counter[addr] += 1
        for addr in extract_emails_from_field(email.get("cc_field")):
            cc_counter[addr] += 1

    # Merge into total touches
    all_addrs = set(to_counter) | set(cc_counter)
    combined = {}
    for addr in all_addrs:
        combined[addr] = {
            "to": to_counter.get(addr, 0),
            "cc": cc_counter.get(addr, 0),
            "total": to_counter.get(addr, 0) + cc_counter.get(addr, 0),
        }
    sorted_combined = sorted(combined.items(), key=lambda x: x[1]["total"], reverse=True)
    return {addr: counts for addr, counts in sorted_combined[:50]}


def analyze_recipient_domains(sent_emails):
    """2. Which orgs get the most outbound email (TO + CC)."""
    domain_counter = Counter()
    for email in sent_emails:
        for addr in extract_emails_from_field(email.get("to_field")):
            domain_counter[get_domain(addr)] += 1
        for addr in extract_emails_from_field(email.get("cc_field")):
            domain_counter[get_domain(addr)] += 1
    return dict(domain_counter.most_common(30))


def analyze_to_vs_cc_usage(sent_emails):
    """3. Per-recipient: how often they're in TO vs CC."""
    usage = defaultdict(lambda: {"to": 0, "cc": 0})
    for email in sent_emails:
        for addr in extract_emails_from_field(email.get("to_field")):
            usage[addr]["to"] += 1
        for addr in extract_emails_from_field(email.get("cc_field")):
            usage[addr]["cc"] += 1
    sorted_usage = sorted(usage.items(), key=lambda x: x[1]["to"] + x[1]["cc"], reverse=True)
    return {addr: counts for addr, counts in sorted_usage[:30]}


def analyze_co_recipients(sent_emails):
    """4. Which recipients appear together — project/team signals."""
    pair_counter = Counter()
    for email in sent_emails:
        all_recip = set(extract_emails_from_field(email.get("to_field")))
        all_recip.update(extract_emails_from_field(email.get("cc_field")))
        recip_list = sorted(all_recip)
        for i in range(len(recip_list)):
            for j in range(i + 1, len(recip_list)):
                pair_counter[(recip_list[i], recip_list[j])] += 1
    return [
        {"pair": list(pair), "count": count}
        for pair, count in pair_counter.most_common(20)
    ]


def analyze_new_vs_repeat_recipients(sent_emails, prior_sent_emails=None):
    """5. How often does user email someone new vs repeat.

    Uses prior_sent_emails (older sends outside the analysis window) to
    establish a baseline of known contacts. Without this, every recipient
    is "new" the first time they appear in the window, making the metric
    trivially equal to the unique count.
    """
    # Build set of previously-known recipients from older sent emails
    known = set()
    if prior_sent_emails:
        for email in prior_sent_emails:
            for addr in extract_emails_from_field(email.get("to_field")):
                known.add(addr)

    seen_this_window = set()
    new_count = 0
    repeat_count = 0

    for email in sorted(sent_emails, key=lambda e: e.get("sent_time") or ""):
        for addr in extract_emails_from_field(email.get("to_field")):
            if addr in known or addr in seen_this_window:
                repeat_count += 1
            else:
                new_count += 1
            seen_this_window.add(addr)

    total = new_count + repeat_count
    return {
        "new_recipients": new_count,
        "repeat_sends": repeat_count,
        "repeat_rate": round(repeat_count / total, 4) if total > 0 else 0,
        "unique_recipients_in_window": len(seen_this_window),
        "known_from_prior": len(known),
        "truly_new": len(seen_this_window - known),
    }


def analyze_send_time_hours(sent_emails):
    """6. Hour-of-day distribution for sends."""
    hour_counts = Counter()
    for email in sent_emails:
        t = parse_time(email.get("sent_time"))
        if t:
            hour_counts[t.hour] += 1
    return dict(sorted(hour_counts.items()))


def analyze_send_time_days(sent_emails):
    """7. Day-of-week distribution for sends."""
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_counts = Counter()
    for email in sent_emails:
        t = parse_time(email.get("sent_time"))
        if t:
            day_counts[day_names[t.weekday()]] += 1
    return dict(day_counts)


def analyze_response_latency(sent_emails, received_emails):
    """8-10. Time between received email and user's reply."""
    received_by_conv = defaultdict(list)
    for r in received_emails:
        conv_id = r.get("conversation_id")
        if conv_id:
            received_by_conv[conv_id].append(r)

    latencies = []
    latency_by_recipient = defaultdict(list)

    for sent in sent_emails:
        conv_id = sent.get("conversation_id")
        if not conv_id or conv_id not in received_by_conv:
            continue

        sent_time = parse_time(sent.get("sent_time"))
        if not sent_time:
            continue

        # Find most recent received in this thread before the sent
        best_match = None
        best_gap = None

        for r in received_by_conv[conv_id]:
            r_time = parse_time(r.get("received_time") or r.get("sent_time"))
            if r_time and r_time < sent_time:
                gap = (sent_time - r_time).total_seconds() / 3600
                if gap < 168 and (best_gap is None or gap < best_gap):
                    best_gap = gap
                    best_match = r

        if best_match and best_gap is not None:
            latencies.append(best_gap)
            sender = (best_match.get("sender_email") or "").lower()
            if sender:
                latency_by_recipient[sender].append(best_gap)

    per_recipient = {}
    for addr, times in latency_by_recipient.items():
        per_recipient[addr] = {
            "avg_hours": round(sum(times) / len(times), 2),
            "min_hours": round(min(times), 2),
            "max_hours": round(max(times), 2),
            "count": len(times),
        }
    sorted_recip = sorted(per_recipient.items(), key=lambda x: x[1]["count"], reverse=True)

    overall = {}
    if latencies:
        latencies.sort()
        overall = {
            "avg_hours": round(sum(latencies) / len(latencies), 2),
            "median_hours": round(latencies[len(latencies) // 2], 2),
            "p90_hours": round(latencies[int(len(latencies) * 0.9)], 2),
            "min_hours": round(min(latencies), 2),
            "max_hours": round(max(latencies), 2),
            "total_matched_replies": len(latencies),
        }

    return {
        "overall": overall,
        "by_recipient": dict(sorted_recip[:20]),
    }


def analyze_burst_patterns(sent_emails, session_gap_minutes=30):
    """9. Does user send in clusters or spread throughout the day.

    A session is a sequence of sends with no gap longer than
    session_gap_minutes (default 30). Sessions with avg >2.5 emails
    are classified as "burst", otherwise "spread".
    """
    times = []
    for email in sent_emails:
        t = parse_time(email.get("sent_time"))
        if t:
            times.append(t)
    times.sort()

    if len(times) < 2:
        return {"sessions": 0, "avg_per_session": 0, "pattern": "insufficient_data"}

    sessions = []
    current_session = [times[0]]

    for i in range(1, len(times)):
        gap = (times[i] - times[i - 1]).total_seconds() / 60
        if gap > session_gap_minutes:
            sessions.append(current_session)
            current_session = [times[i]]
        else:
            current_session.append(times[i])
    sessions.append(current_session)

    sizes = [len(s) for s in sessions]
    avg_size = sum(sizes) / len(sizes)
    multi_sessions = [s for s in sizes if s > 1]

    return {
        "session_gap_minutes": session_gap_minutes,
        "total_sessions": len(sessions),
        "avg_emails_per_session": round(avg_size, 1),
        "avg_emails_per_multi_session": round(sum(multi_sessions) / len(multi_sessions), 1) if multi_sessions else 0,
        "max_session_size": max(sizes),
        "single_email_sessions": sum(1 for s in sizes if s == 1),
        "multi_email_sessions": len(multi_sessions),
        "pattern": "burst" if avg_size > 2.5 else "spread",
    }


def analyze_subject_tokens(sent_emails):
    """11-12. Keywords and bigrams in subjects of threads user engaged with."""
    stop_words = {
        "re", "fw", "fwd", "the", "a", "an", "and", "or", "but", "in", "on",
        "at", "to", "for", "of", "with", "by", "from", "is", "are", "was",
        "were", "be", "been", "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "can", "this",
        "that", "these", "those", "it", "its", "i", "you", "we", "they",
        "he", "she", "my", "your", "our", "their", "me", "us", "him", "her",
        "not", "no", "so", "if", "up", "out", "about", "just", "get", "got",
        "hi", "hello", "hey", "thanks", "thank", "please", "ok", "okay",
    }

    token_counter = Counter()
    bigram_counter = Counter()

    for email in sent_emails:
        subject = (email.get("subject") or "").lower()
        subject = re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", subject, flags=re.IGNORECASE)
        tokens = re.findall(r"[a-z][a-z0-9]*(?:'[a-z]+)?", subject)
        meaningful = [t for t in tokens if t not in stop_words and len(t) >= 3]

        for token in meaningful:
            token_counter[token] += 1
        for i in range(len(meaningful) - 1):
            bigram_counter[f"{meaningful[i]} {meaningful[i + 1]}"] += 1

    return {
        "top_tokens": dict(token_counter.most_common(40)),
        "top_bigrams": dict(bigram_counter.most_common(30)),
    }


def analyze_reply_vs_forward_vs_new(sent_emails):
    """13. Reply vs forward vs new thread ratio."""
    reply = 0
    forward = 0
    new_thread = 0

    for email in sent_emails:
        subject = (email.get("subject") or "").lower().strip()
        if subject.startswith("re:"):
            reply += 1
        elif subject.startswith(("fw:", "fwd:")):
            forward += 1
        else:
            new_thread += 1

    total = reply + forward + new_thread
    return {
        "reply": reply,
        "forward": forward,
        "new_thread": new_thread,
        "reply_pct": round(reply / total, 4) if total else 0,
        "forward_pct": round(forward / total, 4) if total else 0,
        "new_thread_pct": round(new_thread / total, 4) if total else 0,
    }


def analyze_thread_depth(sent_emails, received_emails):
    """14-15. Thread depth and re-engagement rate."""
    conv_counts = defaultdict(lambda: {"received": 0, "sent": 0})
    for r in received_emails:
        conv_id = r.get("conversation_id")
        if conv_id:
            conv_counts[conv_id]["received"] += 1
    for s in sent_emails:
        conv_id = s.get("conversation_id")
        if conv_id:
            conv_counts[conv_id]["sent"] += 1

    depths = []
    re_engaged = 0
    total_threads = 0

    for conv_id, counts in conv_counts.items():
        if counts["sent"] > 0:
            total_threads += 1
            depth = counts["received"] + counts["sent"]
            depths.append(depth)
            if counts["sent"] > 1:
                re_engaged += 1

    return {
        "avg_thread_depth": round(sum(depths) / len(depths), 1) if depths else 0,
        "max_thread_depth": max(depths) if depths else 0,
        "threads_engaged": total_threads,
        "re_engaged_threads": re_engaged,
        "re_engagement_rate": round(re_engaged / total_threads, 4) if total_threads else 0,
    }


def analyze_original_senders(sent_emails, received_emails):
    """16-18. Who sent the email that triggered the user's reply."""
    received_by_conv = defaultdict(list)
    for r in received_emails:
        conv_id = r.get("conversation_id")
        if conv_id:
            received_by_conv[conv_id].append(r)

    trigger_counter = Counter()
    trigger_domain_counter = Counter()

    for sent in sent_emails:
        subject = (sent.get("subject") or "").lower()
        if not subject.startswith("re:"):
            continue

        conv_id = sent.get("conversation_id")
        if not conv_id or conv_id not in received_by_conv:
            continue

        sent_time = parse_time(sent.get("sent_time"))
        if not sent_time:
            continue

        best = None
        for r in received_by_conv[conv_id]:
            r_time = parse_time(r.get("received_time") or r.get("sent_time"))
            if r_time and r_time < sent_time:
                if best is None or r_time > parse_time(best.get("received_time") or best.get("sent_time")):
                    best = r

        if best:
            sender = (best.get("sender_email") or "").lower()
            if sender:
                trigger_counter[sender] += 1
                trigger_domain_counter[get_domain(sender)] += 1

    return {
        "trigger_senders": dict(trigger_counter.most_common(30)),
        "trigger_domains": dict(trigger_domain_counter.most_common(15)),
    }


def analyze_reply_all_vs_reply(sent_emails):
    """20. Reply-all vs reply direct patterns."""
    reply_direct = 0
    reply_all = 0

    for sent in sent_emails:
        subject = (sent.get("subject") or "").lower()
        if not subject.startswith("re:"):
            continue

        sent_cc = extract_emails_from_field(sent.get("cc_field"))
        sent_to = extract_emails_from_field(sent.get("to_field"))

        if len(sent_to) > 1 or sent_cc:
            reply_all += 1
        else:
            reply_direct += 1

    total = reply_direct + reply_all
    return {
        "reply_direct": reply_direct,
        "reply_all": reply_all,
        "reply_all_pct": round(reply_all / total, 4) if total else 0,
    }


def analyze_cc_modifications(sent_emails, received_emails, user_addrs):
    """21-22. Does user add/remove people from CC when replying.

    Excludes user's own addresses and the original sender (who naturally
    moves from sender to recipient in a reply) to avoid false positives.
    """
    received_by_conv = defaultdict(list)
    for r in received_emails:
        conv_id = r.get("conversation_id")
        if conv_id:
            received_by_conv[conv_id].append(r)

    added_cc = 0
    removed_cc = 0
    unchanged = 0

    for sent in sent_emails:
        subject = (sent.get("subject") or "").lower()
        if not subject.startswith("re:"):
            continue

        conv_id = sent.get("conversation_id")
        if not conv_id or conv_id not in received_by_conv:
            continue

        sent_time = parse_time(sent.get("sent_time"))
        if not sent_time:
            continue

        best = None
        for r in received_by_conv[conv_id]:
            r_time = parse_time(r.get("received_time") or r.get("sent_time"))
            if r_time and r_time < sent_time:
                if best is None or r_time > parse_time(best.get("received_time") or best.get("sent_time")):
                    best = r

        if not best:
            continue

        # Build exclusion set: user's own addresses + original sender
        exclude = set(user_addrs)
        orig_sender = (best.get("sender_email") or "").lower()
        if orig_sender:
            exclude.add(orig_sender)

        orig_recipients = set(extract_emails_from_field(best.get("to_field")))
        orig_recipients.update(extract_emails_from_field(best.get("cc_field")))
        orig_recipients -= exclude

        sent_recipients = set(extract_emails_from_field(sent.get("to_field")))
        sent_recipients.update(extract_emails_from_field(sent.get("cc_field")))
        sent_recipients -= exclude

        new_adds = sent_recipients - orig_recipients
        removals = orig_recipients - sent_recipients

        if new_adds:
            added_cc += 1
        if removals:
            removed_cc += 1
        if not new_adds and not removals:
            unchanged += 1

    return {
        "replies_with_cc_added": added_cc,
        "replies_with_cc_removed": removed_cc,
        "replies_unchanged": unchanged,
    }


def analyze_attachment_behavior(sent_emails):
    """23. How often does user attach files in outbound emails."""
    with_attachments = sum(1 for e in sent_emails if e.get("has_attachments"))
    total = len(sent_emails)
    return {
        "with_attachments": with_attachments,
        "without_attachments": total - with_attachments,
        "attachment_rate": round(with_attachments / total, 4) if total else 0,
    }


def analyze_forward_targets(sent_emails):
    """24. When user forwards, who do they forward to — delegation map.

    Returns both per-recipient counts (who gets forwarded to most) and
    per-email count (how many emails were forwarded total).
    """
    forward_targets = Counter()
    forward_emails = 0
    for email in sent_emails:
        subject = (email.get("subject") or "").lower()
        if subject.startswith(("fw:", "fwd:")):
            forward_emails += 1
            for addr in extract_emails_from_field(email.get("to_field")):
                forward_targets[addr] += 1
    return {
        "forward_emails": forward_emails,
        "per_recipient": dict(forward_targets.most_common(20)),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_analysis():
    print("Connecting to Outlook...")
    namespace = get_outlook()

    print("Fetching sent emails (last 30 days)...")
    sent = fetch_folder_emails(namespace, OL_FOLDER_SENT, days=30)
    print(f"Found {len(sent)} sent emails")

    if not sent:
        print("ERROR: No sent emails found in the last 30 days.")
        sys.exit(1)

    print("Fetching prior sent emails (31-120 days ago, for contact baseline)...")
    all_sent_120 = fetch_folder_emails(namespace, OL_FOLDER_SENT, days=120)
    # Filter to only the older emails (not in the 30-day window)
    cutoff_30 = (datetime.now() - timedelta(days=30)).isoformat()
    prior_sent = [e for e in all_sent_120 if (e.get("sent_time") or "") < cutoff_30]
    print(f"Found {len(prior_sent)} prior sent emails (31-120 days)")

    print("Fetching received emails (inbox + custom folders)...")
    received = fetch_all_received_emails(namespace, days=30)
    print(f"Found {len(received)} received emails total")

    # Detect user's own email addresses from sent items
    user_addrs = set()
    for s in sent:
        addr = (s.get("sender_email") or "").lower()
        if addr and "@" in addr:
            user_addrs.add(addr)
    print(f"Detected user addresses: {user_addrs}")

    # Run all analyses
    print(f"\n{'='*60}")
    print("RUNNING BEHAVIORAL ANALYSIS")
    print(f"{'='*60}\n")

    results = {}

    # --- Who the user writes to ---
    print("Analyzing: recipient frequency...")
    results["recipient_frequency"] = analyze_recipient_frequency(sent)

    print("Analyzing: recipient domains...")
    results["recipient_domains"] = analyze_recipient_domains(sent)

    print("Analyzing: TO vs CC usage...")
    results["to_vs_cc_usage"] = analyze_to_vs_cc_usage(sent)

    print("Analyzing: co-recipient clusters...")
    results["co_recipient_clusters"] = analyze_co_recipients(sent)

    print("Analyzing: new vs repeat recipients...")
    results["new_vs_repeat"] = analyze_new_vs_repeat_recipients(sent, prior_sent)

    # --- When the user writes ---
    print("Analyzing: send time (hours)...")
    results["send_hours"] = analyze_send_time_hours(sent)

    print("Analyzing: send time (days)...")
    results["send_days"] = analyze_send_time_days(sent)

    print("Analyzing: response latency...")
    results["response_latency"] = analyze_response_latency(sent, received)

    print("Analyzing: burst patterns...")
    results["burst_patterns"] = analyze_burst_patterns(sent)

    # --- What the user engages with ---
    print("Analyzing: subject tokens...")
    results["subject_tokens"] = analyze_subject_tokens(sent)

    print("Analyzing: reply vs forward vs new...")
    results["reply_forward_new"] = analyze_reply_vs_forward_vs_new(sent)

    print("Analyzing: thread depth...")
    results["thread_depth"] = analyze_thread_depth(sent, received)

    # --- Who triggered the response ---
    print("Analyzing: original senders (triggers)...")
    results["trigger_senders"] = analyze_original_senders(sent, received)

    # --- How the user responds ---
    print("Analyzing: reply-all vs reply...")
    results["reply_all_vs_reply"] = analyze_reply_all_vs_reply(sent)

    print("Analyzing: CC modifications...")
    results["cc_modifications"] = analyze_cc_modifications(sent, received, user_addrs)

    print("Analyzing: attachment behavior...")
    results["attachment_behavior"] = analyze_attachment_behavior(sent)

    print("Analyzing: forward targets...")
    results["forward_targets"] = analyze_forward_targets(sent)

    # --- Meta ---
    results["_meta"] = {
        "sent_count": len(sent),
        "received_count": len(received),
        "analysis_date": datetime.now().isoformat(),
        "window_days": 30,
    }

    # Print summary
    print(f"\n{'='*60}")
    print("RESULTS SUMMARY")
    print(f"{'='*60}\n")

    print(f"Sent emails analyzed: {len(sent)}")
    print(f"Received emails (for matching): {len(received)}")

    print(f"\n--- TOP 10 RECIPIENTS (total touches = TO + CC) ---")
    for addr, counts in list(results["recipient_frequency"].items())[:10]:
        print(f"  {addr}: {counts['total']} total (TO: {counts['to']}, CC: {counts['cc']})")

    print(f"\n--- TOP 5 DOMAINS ---")
    for domain, count in list(results["recipient_domains"].items())[:5]:
        print(f"  {domain}: {count} emails")

    print(f"\n--- SEND TIME PATTERN ---")
    hours = results["send_hours"]
    if hours:
        peak_hour = max(hours, key=lambda k: hours[k])
        print(f"  Peak hour: {peak_hour}:00 ({hours[peak_hour]} emails)")
    days = results["send_days"]
    if days:
        peak_day = max(days, key=lambda k: days[k])
        print(f"  Peak day: {peak_day} ({days[peak_day]} emails)")

    print(f"\n--- RESPONSE LATENCY ---")
    lat = results["response_latency"].get("overall", {})
    if lat:
        print(f"  Avg: {lat.get('avg_hours', '?')} hours")
        print(f"  Median: {lat.get('median_hours', '?')} hours")
        print(f"  Matched replies: {lat.get('total_matched_replies', 0)}")
    else:
        print(f"  No reply matches found")

    print(f"\n--- BURST PATTERN (session gap: {results['burst_patterns'].get('session_gap_minutes', 30)} min) ---")
    bp = results["burst_patterns"]
    print(f"  Pattern: {bp.get('pattern', '?')}")
    print(f"  Total sessions: {bp.get('total_sessions', '?')}")
    print(f"  Avg emails/session (all): {bp.get('avg_emails_per_session', '?')}")
    print(f"  Avg emails/session (multi only): {bp.get('avg_emails_per_multi_session', '?')}")
    print(f"  Single-email sessions: {bp.get('single_email_sessions', '?')}")
    print(f"  Multi-email sessions: {bp.get('multi_email_sessions', '?')}")

    print(f"\n--- REPLY / FORWARD / NEW ---")
    rfn = results["reply_forward_new"]
    print(f"  Replies: {rfn['reply']} ({rfn['reply_pct']:.0%})")
    print(f"  Forwards: {rfn['forward']} ({rfn['forward_pct']:.0%})")
    print(f"  New threads: {rfn['new_thread']} ({rfn['new_thread_pct']:.0%})")

    print(f"\n--- TOP TRIGGER SENDERS ---")
    triggers = results["trigger_senders"].get("trigger_senders", {})
    for addr, count in list(triggers.items())[:10]:
        print(f"  {addr}: triggered {count} replies")

    print(f"\n--- TOP SUBJECT KEYWORDS ---")
    tokens = results["subject_tokens"].get("top_tokens", {})
    for token, count in list(tokens.items())[:10]:
        print(f"  \"{token}\": {count}")

    print(f"\n--- NEW vs REPEAT RECIPIENTS ---")
    nvr = results["new_vs_repeat"]
    print(f"  Known contacts from prior 90 days: {nvr['known_from_prior']}")
    print(f"  Truly new recipients this window: {nvr['truly_new']}")
    print(f"  Repeat sends: {nvr['repeat_sends']}")
    print(f"  Repeat rate: {nvr['repeat_rate']:.0%}")

    print(f"\n--- THREAD BEHAVIOR ---")
    td = results["thread_depth"]
    print(f"  Threads engaged: {td['threads_engaged']}")
    print(f"  Re-engagement rate: {td['re_engagement_rate']:.0%}")
    print(f"  Avg thread depth: {td['avg_thread_depth']}")

    print(f"\n--- CC MODIFICATIONS ---")
    ccm = results["cc_modifications"]
    print(f"  Added CC: {ccm['replies_with_cc_added']}")
    print(f"  Removed CC: {ccm['replies_with_cc_removed']}")
    print(f"  Unchanged: {ccm['replies_unchanged']}")

    print(f"\n--- FORWARD TARGETS ---")
    ft = results["forward_targets"]
    print(f"  Forward emails: {ft['forward_emails']}")
    for addr, count in list(ft["per_recipient"].items())[:5]:
        print(f"  {addr}: {count} forwards received")

    # Save to local JSON
    output_path = os.path.join(os.path.dirname(__file__), "behavior_results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results saved to: {output_path}")

    return results


if __name__ == "__main__":
    run_analysis()
