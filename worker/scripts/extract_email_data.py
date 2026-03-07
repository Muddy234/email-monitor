"""Clarion AI Data Extraction v2.0

Extracts a normalized, message-level dataset from Outlook via COM.
Produces 6 tables: messages, threads, contacts, domains, user_profile,
response_events. Output is a single JSON file.

Threading: In-Reply-To/References primary, ConversationID fallback,
subject normalization third tier.
"""

import json
import re
import sys
import win32com.client
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

# ── Configuration ─────────────────────────────────────────────────────────

USER_CONFIG = {
    "email": "nmcbride@arete-collective.com",
    "first_name": "Nate",
    "last_name": "McBride",
    "aliases": ["Nathan"],
    "domain": "arete-collective.com",
}

WINDOW_DAYS = 60

OUTPUT_FILE = Path(__file__).parent / "email_extraction.json"

# ── Constants ─────────────────────────────────────────────────────────────

# MAPI property tags
PR_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_IN_REPLY_TO = "http://schemas.microsoft.com/mapi/proptag/0x1042001F"
PR_REFERENCES = "http://schemas.microsoft.com/mapi/proptag/0x1039001F"
PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"

# Outlook constants
OL_MAIL_ITEM = 43
OL_FOLDER_INBOX = 6
OL_FOLDER_SENT = 5

# Folders to skip when scanning for custom mail folders
SKIP_FOLDERS = {
    "deleted items", "outbox", "sent items", "drafts", "junk email",
    "sync issues", "rss feeds", "notes", "journal", "calendar",
    "contacts", "conversation action settings", "conversation history",
    "quick step settings", "externalcontacts", "eventcheckpoints",
    "tasks", "files", "archive", "social activity notifications",
    "yammer root",
}

# Automated sender patterns (checked against local part of address)
AUTOMATED_LOCAL_PARTS = {
    "no-reply", "noreply", "do-not-reply", "donotreply",
    "mailer-daemon", "postmaster", "notifications", "notification",
    "bounce", "daemon", "auto-reply", "autoreply", "no_reply",
    "alert", "alerts", "system", "admin",
}

# Action language triggers — phrase-level to avoid false positives from "please" alone
ACTION_PHRASES = re.compile(
    r'(?:'
    r'(?:please|kindly)\s+(?:review|approve|confirm|sign|send|submit|complete|update|forward|provide|let me know|advise)'
    r'|can you\b|could you\b|would you\b'
    r'|need (?:you to|your|this|the)\b'
    r'|(?:action|response|reply|approval|signature) (?:required|needed|requested)'
    r'|by (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|end of day|eod|close of business|cob)\b'
    r'|(?:asap|urgent|time.?sensitive|deadline)\b'
    r')',
    re.IGNORECASE,
)

# Subject prefix pattern for normalization
SUBJECT_PREFIX_RE = re.compile(
    r'^(?:re|fw|fwd)\s*:\s*', re.IGNORECASE,
)


# ── Outlook COM Helpers ───────────────────────────────────────────────────

# Address resolution cache: raw address -> resolved SMTP
_addr_cache = {}


def _resolve_smtp(item):
    """Resolve sender's SMTP address from a MailItem."""
    try:
        addr = (item.SenderEmailAddress or "").strip().lower()
        if addr in _addr_cache:
            return _addr_cache[addr]

        if "@" in addr and "/o=" not in addr:
            _addr_cache[addr] = addr
            return addr

        resolved = addr
        try:
            resolved = item.PropertyAccessor.GetProperty(PR_SMTP).strip().lower()
        except Exception:
            try:
                eu = item.Sender.GetExchangeUser()
                if eu and eu.PrimarySmtpAddress:
                    resolved = eu.PrimarySmtpAddress.strip().lower()
            except Exception:
                pass

        _addr_cache[addr] = resolved
        return resolved
    except Exception:
        return ""


def _resolve_recipient_addr(recip):
    """Resolve a single Recipient COM object to SMTP."""
    try:
        raw = (recip.Address or "").strip().lower()
        if raw in _addr_cache:
            return _addr_cache[raw]

        if "@" in raw and "/o=" not in raw:
            _addr_cache[raw] = raw
            return raw

        resolved = raw
        try:
            eu = recip.AddressEntry.GetExchangeUser()
            if eu and eu.PrimarySmtpAddress:
                resolved = eu.PrimarySmtpAddress.strip().lower()
        except Exception:
            try:
                resolved = recip.AddressEntry.PropertyAccessor.GetProperty(
                    PR_SMTP
                ).strip().lower()
            except Exception:
                pass

        _addr_cache[raw] = resolved
        return resolved
    except Exception:
        return ""


def _resolve_all_recipients(item):
    """Resolve all recipients to (to_list, cc_list, bcc_list)."""
    to_addrs, cc_addrs, bcc_addrs = [], [], []
    try:
        for recip in item.Recipients:
            addr = _resolve_recipient_addr(recip)
            if not addr:
                continue
            rtype = recip.Type
            if rtype == 1:
                to_addrs.append(addr)
            elif rtype == 2:
                cc_addrs.append(addr)
            elif rtype == 3:
                bcc_addrs.append(addr)
    except Exception:
        pass
    return to_addrs, cc_addrs, bcc_addrs


def _safe_prop(pa, tag):
    """Safely read a MAPI property, returning None on failure."""
    try:
        val = pa.GetProperty(tag)
        if val:
            return str(val).strip()
    except Exception:
        pass
    return None


def _to_iso(dt):
    """Convert Outlook datetime to ISO 8601 string."""
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _to_python_dt(dt):
    """Convert Outlook datetime to a naive Python datetime for sorting."""
    if dt is None:
        return datetime.min
    try:
        # pywintypes.datetime -> Python datetime
        return datetime(dt.year, dt.month, dt.day,
                        dt.hour, dt.minute, dt.second)
    except Exception:
        return datetime.min


def _fetch_from_folder(folder, cutoff_str):
    """Fetch all MailItems from a folder newer than cutoff."""
    items = folder.Items
    items.Sort("[ReceivedTime]", True)
    restricted = items.Restrict(f"[ReceivedTime] >= '{cutoff_str}'")
    results = []
    for item in restricted:
        try:
            if item.Class != OL_MAIL_ITEM:
                continue
            results.append(item)
        except Exception:
            continue
    return results


def fetch_sent_items(namespace, cutoff_str):
    """Fetch sent MailItems."""
    folder = namespace.GetDefaultFolder(OL_FOLDER_SENT)
    return _fetch_from_folder(folder, cutoff_str)


def fetch_all_received(namespace, cutoff_str):
    """Fetch received emails from Inbox + custom sibling folders."""
    inbox = namespace.GetDefaultFolder(OL_FOLDER_INBOX)
    all_items = _fetch_from_folder(inbox, cutoff_str)
    print(f"  Inbox: {len(all_items)} emails")

    try:
        parent = inbox.Parent
        for folder in parent.Folders:
            fname = folder.Name.lower()
            if fname == inbox.Name.lower() or fname in SKIP_FOLDERS:
                continue
            folder_items = _fetch_from_folder(folder, cutoff_str)
            if folder_items:
                print(f"  {folder.Name}: {len(folder_items)} emails")
                all_items.extend(folder_items)
    except Exception as e:
        print(f"  Warning: could not scan sibling folders: {e}")

    return all_items


# ── Body Processing ───────────────────────────────────────────────────────

def strip_body(body):
    """Strip signature blocks and quoted replies from email body.

    Handles Outlook-style reply headers (From:/Sent: blocks), Gmail-style
    ("On <date>, <name> wrote:"), forwarded message headers, signature
    delimiters, and legal disclaimers.
    """
    if not body:
        return ""

    lines = body.split("\n")
    clean = []
    for i, line in enumerate(lines):
        stripped = line.strip()

        # Signature delimiters
        if stripped == "--" or stripped == "-- ":
            break
        if stripped.lower().startswith("sent from my"):
            break
        if stripped.lower().startswith("sent from "):
            break

        # Outlook-style reply header: "From: <name> <email>"
        # followed by "Sent: <date>" on the next line
        if re.match(r"^from:\s+.+", stripped, re.IGNORECASE):
            # Check if next non-blank line starts with "Sent:"
            for j in range(i + 1, min(i + 3, len(lines))):
                next_stripped = lines[j].strip()
                if not next_stripped:
                    continue
                if re.match(r"^sent:\s+", next_stripped, re.IGNORECASE):
                    break  # confirmed Outlook reply block
                break
            else:
                clean.append(line)
                continue
            break  # exit outer loop — everything below is quoted

        # Gmail/Apple style: "On <date>, <name> wrote:"
        if re.match(r"^on .+ wrote:\s*$", stripped, re.IGNORECASE):
            break

        # Forwarded message headers
        if stripped.lower().startswith("---------- forwarded message"):
            break
        if stripped.lower().startswith("-----original message-----"):
            break

        # Outlook separator line (underscores)
        if re.match(r"^_{10,}$", stripped):
            # Check if next non-blank line is "From:" — if so, it's a reply block
            for j in range(i + 1, min(i + 3, len(lines))):
                next_stripped = lines[j].strip()
                if not next_stripped:
                    continue
                if re.match(r"^from:\s+", next_stripped, re.IGNORECASE):
                    break
                break
            else:
                clean.append(line)
                continue
            break

        # Legal disclaimer detection (common in corporate email)
        if re.match(
            r"^the information (transmitted|contained)",
            stripped, re.IGNORECASE,
        ):
            break
        if re.match(r"^confidentiality notice", stripped, re.IGNORECASE):
            break
        if re.match(r"^this (email|message|communication) (is |may )", stripped, re.IGNORECASE):
            break

        # Skip quoted lines
        if stripped.startswith(">"):
            continue

        clean.append(line)

    return "\n".join(clean).strip()


def has_question(body, subject):
    """Check if body or subject contains a question mark."""
    return "?" in (body or "") or "?" in (subject or "")


def has_action_language(body):
    """Check if first two sentences contain action trigger phrases."""
    if not body:
        return False
    # Extract first ~2 sentences (split on period, question mark, exclamation, or newline)
    sentences = re.split(r'[.!?\n]', body)
    first_two = " ".join(s.strip() for s in sentences[:2] if s.strip())
    return bool(ACTION_PHRASES.search(first_two))


def mentions_user_name(body):
    """Check if body mentions the user's first/last name or aliases."""
    if not body:
        return False
    body_lower = body.lower()
    names = [USER_CONFIG["first_name"].lower(), USER_CONFIG["last_name"].lower()]
    names.extend(a.lower() for a in USER_CONFIG.get("aliases", []))
    return any(name in body_lower for name in names)


def normalize_subject(subject):
    """Strip Re:/FW:/Fwd: prefixes, collapse whitespace, lowercase."""
    if not subject:
        return ""
    normalized = subject.strip()
    # Strip stacked prefixes
    prev = None
    while prev != normalized:
        prev = normalized
        normalized = SUBJECT_PREFIX_RE.sub("", normalized)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def detect_message_type(subject, in_reply_to):
    """Determine if a message is 'new', 'reply', or 'forward'."""
    subj = (subject or "").strip().lower()
    if subj.startswith(("fw:", "fwd:")):
        return "forward"
    if in_reply_to or subj.startswith("re:"):
        # Has in_reply_to -> definitely a reply
        # Has Re: but no in_reply_to -> possibly recycled subject, treat as reply
        return "reply"
    return "new"


def is_automated_sender(sender):
    """Check if sender address looks automated."""
    if not sender or "@" not in sender:
        return False
    local = sender.split("@")[0]
    return any(pat in local for pat in AUTOMATED_LOCAL_PARTS)


# ── Message Extraction ────────────────────────────────────────────────────

def extract_message(item, direction):
    """Extract a message record from an Outlook COM MailItem.

    Returns a dict matching the messages table schema, plus internal
    fields prefixed with '_' for thread reconstruction.
    """
    pa = item.PropertyAccessor

    # MAPI headers
    msg_id = _safe_prop(pa, PR_MESSAGE_ID)
    in_reply_to = _safe_prop(pa, PR_IN_REPLY_TO)
    references_raw = _safe_prop(pa, PR_REFERENCES)
    references = re.findall(r"<[^>]+>", references_raw) if references_raw else []

    # Sender
    sender = _resolve_smtp(item)
    sender_name = ""
    try:
        sender_name = item.SenderName or ""
    except Exception:
        pass

    # Recipients
    to_addrs, cc_addrs, bcc_addrs = _resolve_all_recipients(item)

    # Timestamps
    if direction == "sent":
        try:
            ts = item.SentOn
        except Exception:
            ts = item.ReceivedTime
        raw_dt = _to_python_dt(ts)
    else:
        ts = item.ReceivedTime
        raw_dt = _to_python_dt(ts)

    # Subject
    subject = ""
    try:
        subject = item.Subject or ""
    except Exception:
        pass

    # Body
    body_raw = ""
    try:
        body_raw = item.Body or ""
    except Exception:
        pass
    body_clean = strip_body(body_raw)

    # Attachments
    att_count = 0
    try:
        att_count = item.Attachments.Count
    except Exception:
        pass

    # Message type
    msg_type = detect_message_type(subject, in_reply_to)

    # Outlook-native threading (for fallback)
    outlook_conv_id = None
    try:
        outlook_conv_id = item.ConversationID
    except Exception:
        pass

    outlook_conv_topic = None
    try:
        outlook_conv_topic = item.ConversationTopic
    except Exception:
        pass

    return {
        "message_id": msg_id,
        "conversation_id": None,  # assigned during thread reconstruction
        "direction": direction,
        "sender": sender,
        "sender_name": sender_name,
        "to_recipients": to_addrs,
        "cc_recipients": cc_addrs,
        "bcc_recipients": bcc_addrs if direction == "sent" else [],
        "timestamp": _to_iso(ts),
        "subject": subject,
        "subject_normalized": normalize_subject(subject),
        "message_type": msg_type,
        "in_reply_to": in_reply_to,
        "references": references,
        "body_length": len(body_clean),
        "body_snippet": body_clean[:200] if body_clean else "",
        "has_attachments": att_count > 0,
        "attachment_count": att_count,
        "has_question": has_question(body_clean, subject),
        "has_action_language": has_action_language(body_clean),
        "mentions_user_name": mentions_user_name(body_clean),
        "recipient_count": len(to_addrs) + len(cc_addrs),
        "thread_position": None,  # assigned during thread reconstruction
        "is_automated": is_automated_sender(sender),
        # Internal fields (stripped from final output)
        "_raw_dt": raw_dt,
        "_outlook_conv_id": outlook_conv_id,
        "_outlook_conv_topic": outlook_conv_topic,
    }


def extract_all_messages(namespace, cutoff_str):
    """Extract message records from all relevant Outlook folders."""
    messages = []

    # Sent items
    print("Fetching sent items...")
    sent_items = fetch_sent_items(namespace, cutoff_str)
    print(f"  Sent Items: {len(sent_items)} emails")
    for i, item in enumerate(sent_items):
        if (i + 1) % 50 == 0:
            print(f"    Extracting sent {i + 1}/{len(sent_items)}...")
        try:
            messages.append(extract_message(item, "sent"))
        except Exception as e:
            print(f"    Warning: failed to extract sent item {i}: {e}")

    # Received items
    print("Fetching received items...")
    recv_items = fetch_all_received(namespace, cutoff_str)
    print(f"  Total received: {len(recv_items)} emails")
    for i, item in enumerate(recv_items):
        if (i + 1) % 100 == 0:
            print(f"    Extracting received {i + 1}/{len(recv_items)}...")
        try:
            messages.append(extract_message(item, "received"))
        except Exception as e:
            print(f"    Warning: failed to extract received item {i}: {e}")

    print(f"Total messages extracted: {len(messages)}")
    return messages


# ── Thread Reconstruction ─────────────────────────────────────────────────

def reconstruct_threads(messages):
    """Assign conversation_id and thread_position to each message.

    Strategy:
      1. Primary: group by shared Message-IDs via In-Reply-To / References
         using union-find on Message-IDs.
      2. Fallback: ConversationID for messages not linked by MIME headers.
      3. Third tier: normalized subject within 72h for any remaining.
    """
    # --- Union-Find ---
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Phase 1: Link messages via MIME headers
    for msg in messages:
        msg_id = msg["message_id"]
        if not msg_id:
            continue

        # Ensure this ID exists in union-find
        if msg_id not in parent:
            parent[msg_id] = msg_id

        # Link to In-Reply-To
        irt = msg["in_reply_to"]
        if irt:
            if irt not in parent:
                parent[irt] = irt
            union(msg_id, irt)

        # Link to all References
        for ref in msg["references"]:
            if ref not in parent:
                parent[ref] = ref
            union(msg_id, ref)

    # Build thread groups from union-find
    # message_id -> thread_root_id
    thread_roots = {}
    for msg in messages:
        msg_id = msg["message_id"]
        if msg_id and msg_id in parent:
            thread_roots[msg_id] = find(msg_id)

    # Group messages by their thread root
    root_to_msgs = defaultdict(list)
    unlinked = []  # messages with no MIME header links

    for msg in messages:
        msg_id = msg["message_id"]
        if msg_id and msg_id in thread_roots:
            root = thread_roots[msg_id]
            root_to_msgs[root].append(msg)
        else:
            unlinked.append(msg)

    # Phase 2: Assign conversation_ids from MIME-linked groups
    conv_counter = 0
    root_to_conv_id = {}
    # Also build lookup: outlook_conv_id -> conversation_id for fallback
    outlook_to_conv = {}

    for root, msgs in root_to_msgs.items():
        conv_counter += 1
        conv_id = f"thread_{conv_counter:04d}"
        root_to_conv_id[root] = conv_id

        for msg in msgs:
            msg["conversation_id"] = conv_id
            oc = msg["_outlook_conv_id"]
            if oc and oc not in outlook_to_conv:
                outlook_to_conv[oc] = conv_id

    # Phase 2b: Handle unlinked messages via ConversationID fallback
    still_unlinked = []
    for msg in unlinked:
        oc = msg["_outlook_conv_id"]
        if oc and oc in outlook_to_conv:
            msg["conversation_id"] = outlook_to_conv[oc]
        elif oc:
            # New thread from ConversationID
            conv_counter += 1
            conv_id = f"thread_{conv_counter:04d}"
            msg["conversation_id"] = conv_id
            outlook_to_conv[oc] = conv_id
        else:
            still_unlinked.append(msg)

    # Phase 3: Subject normalization fallback (72h window)
    if still_unlinked:
        # Group by normalized subject
        subj_groups = defaultdict(list)
        for msg in still_unlinked:
            subj_groups[msg["subject_normalized"]].append(msg)

        for subj, msgs in subj_groups.items():
            msgs.sort(key=lambda m: m["_raw_dt"])
            # Split into clusters within 72h of each other
            clusters = []
            current = [msgs[0]]
            for m in msgs[1:]:
                if (m["_raw_dt"] - current[-1]["_raw_dt"]).total_seconds() <= 72 * 3600:
                    current.append(m)
                else:
                    clusters.append(current)
                    current = [m]
            clusters.append(current)

            for cluster in clusters:
                conv_counter += 1
                conv_id = f"thread_{conv_counter:04d}"
                for msg in cluster:
                    msg["conversation_id"] = conv_id

    # Assign thread_position within each thread
    thread_msgs = defaultdict(list)
    for msg in messages:
        if msg["conversation_id"]:
            thread_msgs[msg["conversation_id"]].append(msg)

    for conv_id, msgs in thread_msgs.items():
        msgs.sort(key=lambda m: m["_raw_dt"])
        for pos, msg in enumerate(msgs, 1):
            msg["thread_position"] = pos

    linked_count = sum(1 for m in messages if m["conversation_id"])
    print(f"Thread reconstruction: {conv_counter} threads from {linked_count} messages")
    print(f"  MIME-linked groups: {len(root_to_msgs)}")
    print(f"  ConversationID fallback: {len(unlinked) - len(still_unlinked)}")
    print(f"  Subject fallback: {len(still_unlinked)}")

    return messages


# ── Table Builders ────────────────────────────────────────────────────────

def _user_emails():
    """Set of all user email addresses (primary + domain match)."""
    addrs = {USER_CONFIG["email"].lower()}
    return addrs


def _is_from_user(msg):
    """Check if a message was sent by the user."""
    return msg["direction"] == "sent"


def build_threads_table(messages):
    """Build the threads summary table."""
    user_addrs = _user_emails()
    thread_msgs = defaultdict(list)
    for msg in messages:
        thread_msgs[msg["conversation_id"]].append(msg)

    threads = []
    for conv_id, msgs in thread_msgs.items():
        msgs.sort(key=lambda m: m["_raw_dt"])

        # Participants
        participants = set()
        for msg in msgs:
            participants.add(msg["sender"])
            participants.update(msg["to_recipients"])
            participants.update(msg["cc_recipients"])
        participants.discard("")

        total = len(msgs)
        user_msgs = [m for m in msgs if _is_from_user(m)]
        user_count = len(user_msgs)

        first_ts = msgs[0]["_raw_dt"]
        last_ts = msgs[-1]["_raw_dt"]
        duration_hrs = (last_ts - first_ts).total_seconds() / 3600

        # Thread depth via In-Reply-To chain
        depth = _compute_thread_depth(msgs)

        # Messages per day
        duration_days = max(duration_hrs / 24, 0.01)
        msgs_per_day = round(total / duration_days, 2) if duration_hrs > 0 else None

        # User avg body length
        user_body_lens = [m["body_length"] for m in user_msgs if m["body_length"] > 0]
        user_avg_body = round(mean(user_body_lens), 1) if user_body_lens else None

        # User initiated?
        first_sender = msgs[0]["sender"]
        user_initiated = first_sender in user_addrs

        # User first response
        user_first_resp = None
        user_resp_latency = None
        if not user_initiated:
            for m in msgs:
                if _is_from_user(m):
                    user_first_resp = m["timestamp"]
                    user_resp_latency = round(
                        (m["_raw_dt"] - msgs[0]["_raw_dt"]).total_seconds() / 3600, 2
                    )
                    break
        elif user_count > 1:
            # User initiated, first response is their second message
            user_first_resp = user_msgs[1]["timestamp"] if len(user_msgs) > 1 else None

        # Other responders
        other_responders = set()
        for m in msgs:
            if not _is_from_user(m) and m["sender"] != first_sender:
                other_responders.add(m["sender"])

        threads.append({
            "conversation_id": conv_id,
            "subject_normalized": msgs[0]["subject_normalized"],
            "participants": sorted(participants),
            "total_messages": total,
            "user_messages": user_count,
            "user_participation_rate": round(user_count / total, 4) if total > 0 else 0,
            "first_message_ts": msgs[0]["timestamp"],
            "last_message_ts": msgs[-1]["timestamp"],
            "thread_duration_hours": round(duration_hrs, 2),
            "thread_depth": depth,
            "messages_per_day": msgs_per_day,
            "user_avg_body_length": user_avg_body,
            "user_initiated": user_initiated,
            "thread_initiator": first_sender,
            "user_first_response_ts": user_first_resp,
            "user_response_latency": user_resp_latency,
            "other_responders": sorted(other_responders),
        })

    return threads


def _compute_thread_depth(msgs):
    """Compute max reply chain depth from In-Reply-To links."""
    # Build parent map: message_id -> parent_message_id
    msg_ids_in_thread = {m["message_id"] for m in msgs if m["message_id"]}
    parent_of = {}
    for m in msgs:
        if m["message_id"] and m["in_reply_to"] and m["in_reply_to"] in msg_ids_in_thread:
            parent_of[m["message_id"]] = m["in_reply_to"]

    if not parent_of:
        # No In-Reply-To links, estimate from message count
        return len(msgs)

    # Compute depth for each message
    def depth(mid, seen=None):
        if seen is None:
            seen = set()
        if mid in seen or mid not in parent_of:
            return 1
        seen.add(mid)
        return 1 + depth(parent_of[mid], seen)

    max_depth = max(depth(mid) for mid in parent_of)
    # Include root messages that aren't in parent_of as children
    return max(max_depth, 1)


def build_contacts_table(messages, threads):
    """Build the contact profiles table."""
    user_addrs = _user_emails()

    # Index threads by conversation_id
    thread_by_id = {t["conversation_id"]: t for t in threads}

    # Per-contact accumulators
    contacts = defaultdict(lambda: {
        "sent_to_count": 0,
        "sent_cc_count": 0,
        "received_from_count": 0,
        "replied_to_count": 0,
        "reply_latencies": [],
        "forward_to_count": 0,
        "forwarded_from_count": 0,
        "threads": set(),
        "display_name": "",
        "first_ts": None,
        "last_ts": None,
        "co_recipients": Counter(),
        # For rolling windows
        "received_30d": 0,
        "replied_30d": 0,
        "received_90d": 0,
        "replied_90d": 0,
    })

    now = datetime.now()
    cutoff_30d = now - timedelta(days=30)
    cutoff_90d = now - timedelta(days=90)

    # Build reply lookup: for each received message, did the user reply?
    # Group sent messages by conversation_id for reply matching
    sent_by_conv = defaultdict(list)
    for msg in messages:
        if _is_from_user(msg):
            sent_by_conv[msg["conversation_id"]].append(msg)

    # Sort each group by timestamp
    for conv_id in sent_by_conv:
        sent_by_conv[conv_id].sort(key=lambda m: m["_raw_dt"])

    # Build reply map: inbound message_id -> user's reply message
    reply_map = _build_reply_map(messages, sent_by_conv)

    # Process each message
    for msg in messages:
        if _is_from_user(msg):
            # User sent this email
            for addr in msg["to_recipients"]:
                if addr in user_addrs:
                    continue
                c = contacts[addr]
                c["sent_to_count"] += 1
                if msg["message_type"] == "forward":
                    c["forward_to_count"] += 1
                # Track co-recipients
                all_recips = set(msg["to_recipients"] + msg["cc_recipients"]) - user_addrs - {addr}
                for co in all_recips:
                    c["co_recipients"][co] += 1

            for addr in msg["cc_recipients"]:
                if addr in user_addrs:
                    continue
                c = contacts[addr]
                c["sent_cc_count"] += 1
                if msg["message_type"] == "forward":
                    c["forward_to_count"] += 1

        else:
            # User received this email
            sender = msg["sender"]
            if sender in user_addrs or not sender:
                continue
            c = contacts[sender]
            c["received_from_count"] += 1
            c["display_name"] = msg["sender_name"] or c["display_name"]

            # Rolling windows
            if msg["_raw_dt"] >= cutoff_30d:
                c["received_30d"] += 1
            if msg["_raw_dt"] >= cutoff_90d:
                c["received_90d"] += 1

            # Check if user replied to this specific message
            if msg["message_id"] and msg["message_id"] in reply_map:
                reply_msg = reply_map[msg["message_id"]]
                c["replied_to_count"] += 1
                latency = (reply_msg["_raw_dt"] - msg["_raw_dt"]).total_seconds() / 3600
                if latency >= 0:
                    c["reply_latencies"].append(latency)

                if msg["_raw_dt"] >= cutoff_30d:
                    c["replied_30d"] += 1
                if msg["_raw_dt"] >= cutoff_90d:
                    c["replied_90d"] += 1

            # Check if user forwarded this contact's email
            if msg["conversation_id"] in sent_by_conv:
                for s in sent_by_conv[msg["conversation_id"]]:
                    if s["message_type"] == "forward" and s["_raw_dt"] > msg["_raw_dt"]:
                        c["forwarded_from_count"] += 1
                        break

        # Track threads and timestamps for all contacts in this message
        all_addrs = set()
        if _is_from_user(msg):
            all_addrs = set(msg["to_recipients"] + msg["cc_recipients"]) - user_addrs
        else:
            all_addrs = {msg["sender"]} - user_addrs

        for addr in all_addrs:
            if not addr:
                continue
            c = contacts[addr]
            c["threads"].add(msg["conversation_id"])
            if c["first_ts"] is None or msg["_raw_dt"] < c["first_ts"]:
                c["first_ts"] = msg["_raw_dt"]
            if c["last_ts"] is None or msg["_raw_dt"] > c["last_ts"]:
                c["last_ts"] = msg["_raw_dt"]

    # Build final contact records
    user_domain = USER_CONFIG["domain"].lower()
    result = []
    for email, c in contacts.items():
        if not email or "@" not in email:
            continue

        domain = email.split("@")[1]
        sent_total = c["sent_to_count"] + c["sent_cc_count"]
        recv = c["received_from_count"]
        replied = c["replied_to_count"]

        reply_rate = round(replied / recv, 4) if recv > 0 else None
        avg_latency = round(mean(c["reply_latencies"]), 2) if c["reply_latencies"] else None
        med_latency = round(median(c["reply_latencies"]), 2) if c["reply_latencies"] else None

        # Rolling rates
        rr_30 = round(c["replied_30d"] / c["received_30d"], 4) if c["received_30d"] > 0 else None
        rr_90 = round(c["replied_90d"] / c["received_90d"], 4) if c["received_90d"] > 0 else None

        # Relationship direction
        rel_dir = _relationship_direction(messages, email, user_addrs)

        # Co-recipients top 5
        co_top5 = [addr for addr, _ in c["co_recipients"].most_common(5)]

        # Detect self-send addresses (user's alternate emails used for forwarding)
        is_self = _is_self_address(email, sent_total, recv)

        result.append({
            "email": email,
            "display_name": c["display_name"],
            "domain": domain,
            "sent_to_count": c["sent_to_count"],
            "sent_cc_count": c["sent_cc_count"],
            "sent_total": sent_total,
            "received_from_count": recv,
            "replied_to_count": replied,
            "reply_rate": reply_rate,
            "avg_reply_latency_hrs": avg_latency,
            "median_reply_latency_hrs": med_latency,
            "forward_to_count": c["forward_to_count"],
            "forwarded_from_count": c["forwarded_from_count"],
            "threads_shared": len(c["threads"]),
            "last_interaction_ts": _to_iso(c["last_ts"]) if c["last_ts"] else None,
            "first_interaction_ts": _to_iso(c["first_ts"]) if c["first_ts"] else None,
            "is_internal": domain == user_domain,
            "is_self": is_self,
            "relationship_direction": rel_dir,
            "co_recipients_top5": co_top5,
            "reply_rate_30d": rr_30,
            "reply_rate_90d": rr_90,
        })

    # Sort by sent_total + received_from_count descending
    result.sort(key=lambda c: c["sent_total"] + c["received_from_count"], reverse=True)
    return result


def _build_reply_map(messages, sent_by_conv):
    """Map inbound message_id -> user's reply message.

    Priority:
      1. Direct In-Reply-To match (sent message's in_reply_to == inbound message_id)
      2. Same thread, next user message after the inbound timestamp
    """
    # Phase 1: Direct In-Reply-To matches
    reply_map = {}
    inbound_ids = {
        m["message_id"] for m in messages
        if not _is_from_user(m) and m["message_id"]
    }

    for msg in messages:
        if _is_from_user(msg) and msg["in_reply_to"] and msg["in_reply_to"] in inbound_ids:
            # Only keep the earliest reply if multiple exist
            existing = reply_map.get(msg["in_reply_to"])
            if existing is None or msg["_raw_dt"] < existing["_raw_dt"]:
                reply_map[msg["in_reply_to"]] = msg

    # Phase 2: Thread-based fallback for unmatched inbound messages
    for msg in messages:
        if _is_from_user(msg) or not msg["message_id"]:
            continue
        if msg["message_id"] in reply_map:
            continue  # already matched

        conv_id = msg["conversation_id"]
        if conv_id not in sent_by_conv:
            continue

        # Find the first user message in this thread after the inbound
        for sent_msg in sent_by_conv[conv_id]:
            if sent_msg["_raw_dt"] > msg["_raw_dt"] and sent_msg["message_type"] == "reply":
                reply_map[msg["message_id"]] = sent_msg
                break

    return reply_map


def _relationship_direction(messages, contact_email, user_addrs):
    """Determine if user is 'initiator', 'responder', or 'balanced'."""
    # Count threads where user sent first vs contact sent first
    threads = defaultdict(list)
    for msg in messages:
        if msg["sender"] == contact_email or (
            _is_from_user(msg) and contact_email in msg["to_recipients"] + msg["cc_recipients"]
        ):
            threads[msg["conversation_id"]].append(msg)

    user_initiated = 0
    contact_initiated = 0
    for conv_id, msgs in threads.items():
        msgs.sort(key=lambda m: m["_raw_dt"])
        first = msgs[0]
        if _is_from_user(first):
            user_initiated += 1
        elif first["sender"] == contact_email:
            contact_initiated += 1

    total = user_initiated + contact_initiated
    if total == 0:
        return "balanced"
    ratio = user_initiated / total
    if ratio >= 0.65:
        return "initiator"
    if ratio <= 0.35:
        return "responder"
    return "balanced"


def _is_self_address(email, sent_total, received_from_count):
    """Detect if a contact address is the user's own alternate (self-forward).

    Heuristics: address contains the user's name parts and has heavily
    one-directional traffic (lots of sent, zero or near-zero received).
    """
    user_first = USER_CONFIG["first_name"].lower()
    user_last = USER_CONFIG["last_name"].lower()
    local = email.split("@")[0].lower() if "@" in email else ""

    name_match = (user_first in local or user_last in local)
    one_directional = sent_total >= 10 and received_from_count == 0

    return name_match and one_directional


def build_domains_table(contacts):
    """Build the domain profiles table."""
    domains = defaultdict(lambda: {
        "contact_count": 0,
        "sent_total": 0,
        "received_total": 0,
        "reply_rates": [],
        "threads": set(),
        "last_ts": None,
    })

    user_domain = USER_CONFIG["domain"].lower()

    for c in contacts:
        d = domains[c["domain"]]
        d["contact_count"] += 1
        d["sent_total"] += c["sent_total"]
        d["received_total"] += c["received_from_count"]
        if c["reply_rate"] is not None:
            d["reply_rates"].append(c["reply_rate"])
        d["threads"].update(range(c["threads_shared"]))  # approximate
        if c["last_interaction_ts"]:
            if d["last_ts"] is None or c["last_interaction_ts"] > (d["last_ts"] or ""):
                d["last_ts"] = c["last_interaction_ts"]

    result = []
    for domain, d in domains.items():
        avg_rr = round(mean(d["reply_rates"]), 4) if d["reply_rates"] else None

        # Determine domain_category
        category = "unknown"
        if domain == user_domain:
            category = "internal"

        result.append({
            "domain": domain,
            "contact_count": d["contact_count"],
            "sent_total": d["sent_total"],
            "received_total": d["received_total"],
            "avg_reply_rate": avg_rr,
            "threads_shared": len(d["threads"]),
            "domain_category": category,
            "last_interaction_ts": d["last_ts"],
        })

    result.sort(key=lambda d: d["sent_total"] + d["received_total"], reverse=True)
    return result


def build_user_profile(messages):
    """Build the single user behavior profile record."""
    user_addrs = _user_emails()

    sent = [m for m in messages if _is_from_user(m)]
    received = [m for m in messages if not _is_from_user(m)]

    # Send hour distribution
    send_hours = Counter()
    send_days = Counter()
    for m in sent:
        send_hours[m["_raw_dt"].hour] += 1
        send_days[m["_raw_dt"].strftime("%A")] += 1

    # Active hours (10th to 90th percentile of sends)
    if send_hours:
        sorted_hours = sorted(send_hours.items(), key=lambda x: x[0])
        total_sends = sum(c for _, c in sorted_hours)
        cumulative = 0
        start_hour = 0
        end_hour = 23
        for hour, count in sorted_hours:
            cumulative += count
            if cumulative >= total_sends * 0.1 and start_hour == 0:
                start_hour = hour
            if cumulative >= total_sends * 0.9:
                end_hour = hour
                break
    else:
        start_hour, end_hour = 8, 17

    # Active days (days with significant volume)
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    active_days = [d for d in day_order if send_days.get(d, 0) >= max(len(sent) * 0.05, 1)]

    # Daily averages
    if sent:
        date_range = (sent[-1]["_raw_dt"] - sent[0]["_raw_dt"]).days + 1
        # Sort to get actual range
        sent_sorted = sorted(sent, key=lambda m: m["_raw_dt"])
        recv_sorted = sorted(received, key=lambda m: m["_raw_dt"])
        all_sorted = sorted(messages, key=lambda m: m["_raw_dt"])
        date_range = max((all_sorted[-1]["_raw_dt"] - all_sorted[0]["_raw_dt"]).days, 1)
    else:
        date_range = WINDOW_DAYS

    avg_daily_sent = round(len(sent) / date_range, 2)
    avg_daily_recv = round(len(received) / date_range, 2)

    # Overall reply rate
    replies = [m for m in sent if m["message_type"] == "reply"]
    forwards = [m for m in sent if m["message_type"] == "forward"]
    new_threads = [m for m in sent if m["message_type"] == "new"]

    overall_reply_rate = round(len(replies) / len(received), 4) if received else 0

    # Reply latencies (from response_events, but compute here too)
    # Quick calculation from sent replies matched to received
    reply_latencies = []
    sent_by_conv = defaultdict(list)
    for m in sent:
        sent_by_conv[m["conversation_id"]].append(m)
    for conv_id in sent_by_conv:
        sent_by_conv[conv_id].sort(key=lambda m: m["_raw_dt"])

    reply_map = _build_reply_map(messages, sent_by_conv)
    for inbound_id, reply_msg in reply_map.items():
        # Find the inbound message
        for m in messages:
            if m["message_id"] == inbound_id:
                latency = (reply_msg["_raw_dt"] - m["_raw_dt"]).total_seconds() / 3600
                if latency >= 0:
                    reply_latencies.append(latency)
                break

    avg_latency = round(mean(reply_latencies), 2) if reply_latencies else None
    med_latency = round(median(reply_latencies), 2) if reply_latencies else None

    window_start = min(m["_raw_dt"] for m in messages) if messages else datetime.now()
    window_end = max(m["_raw_dt"] for m in messages) if messages else datetime.now()

    return {
        "user_email": USER_CONFIG["email"],
        "user_name_first": USER_CONFIG["first_name"],
        "user_name_last": USER_CONFIG["last_name"],
        "user_domain": USER_CONFIG["domain"],
        "active_hours_start": start_hour,
        "active_hours_end": end_hour,
        "active_days": active_days,
        "avg_daily_sent": avg_daily_sent,
        "avg_daily_received": avg_daily_recv,
        "overall_reply_rate": overall_reply_rate,
        "avg_reply_latency_hrs": avg_latency,
        "median_reply_latency_hrs": med_latency,
        "reply_pct": round(len(replies) / len(sent), 4) if sent else 0,
        "forward_pct": round(len(forwards) / len(sent), 4) if sent else 0,
        "new_thread_pct": round(len(new_threads) / len(sent), 4) if sent else 0,
        "analysis_window_start": _to_iso(window_start),
        "analysis_window_end": _to_iso(window_end),
        "total_sent": len(sent),
        "total_received": len(received),
    }


def build_response_events(messages):
    """Build the response events (labeled training set) table."""
    user_addrs = _user_emails()

    # Prep: sent messages by conversation
    sent_by_conv = defaultdict(list)
    for msg in messages:
        if _is_from_user(msg):
            sent_by_conv[msg["conversation_id"]].append(msg)
    for conv_id in sent_by_conv:
        sent_by_conv[conv_id].sort(key=lambda m: m["_raw_dt"])

    reply_map = _build_reply_map(messages, sent_by_conv)

    # All messages by conversation for other_replies_before_user
    msgs_by_conv = defaultdict(list)
    for msg in messages:
        msgs_by_conv[msg["conversation_id"]].append(msg)
    for conv_id in msgs_by_conv:
        msgs_by_conv[conv_id].sort(key=lambda m: m["_raw_dt"])

    automated_excluded = 0
    events = []
    for msg in messages:
        if _is_from_user(msg):
            continue  # only inbound emails

        sender = msg["sender"]
        if not sender or sender in user_addrs:
            continue

        # Skip automated senders — they aren't actionable for draft prediction
        if msg["is_automated"]:
            automated_excluded += 1
            continue

        # User in TO?
        user_in_to = any(a in msg["to_recipients"] for a in user_addrs)
        user_in_cc = any(a in msg["cc_recipients"] for a in user_addrs)
        if not user_in_to and not user_in_cc:
            # User wasn't in TO or CC — might be via distribution list or BCC
            # Still include as a response event
            pass

        user_sole_to = user_in_to and len(msg["to_recipients"]) == 1

        # Response info
        responded = msg["message_id"] is not None and msg["message_id"] in reply_map
        response_msg = reply_map.get(msg["message_id"]) if msg["message_id"] else None

        response_message_id = None
        response_latency = None
        response_type = None

        if responded and response_msg:
            response_message_id = response_msg["message_id"]
            latency = (response_msg["_raw_dt"] - msg["_raw_dt"]).total_seconds() / 3600
            response_latency = round(latency, 2) if latency >= 0 else None

            # Determine response type
            if response_msg["message_type"] == "forward":
                response_type = "forward"
            else:
                # reply vs reply_all
                orig_recips = set(msg["to_recipients"] + msg["cc_recipients"]) - user_addrs - {sender}
                resp_recips = set(response_msg["to_recipients"] + response_msg["cc_recipients"]) - user_addrs
                if orig_recips and orig_recips & resp_recips:
                    response_type = "reply_all"
                else:
                    response_type = "reply"

        # Other replies before user — count distinct people, not messages
        other_replies = 0
        if msg["conversation_id"]:
            conv_msgs = msgs_by_conv[msg["conversation_id"]]
            deadline = response_msg["_raw_dt"] if response_msg else max(
                m["_raw_dt"] for m in messages
            )
            other_senders = set()
            for m in conv_msgs:
                if (m["_raw_dt"] > msg["_raw_dt"]
                        and m["_raw_dt"] <= deadline
                        and not _is_from_user(m)
                        and m["sender"] != sender):
                    other_senders.add(m["sender"])
            other_replies = len(other_senders)

        # Latency cap at 168h (7 days) — beyond this is thread revival, not real response
        is_thread_revival = False
        if response_latency is not None and response_latency > 168:
            is_thread_revival = True
            response_latency = 168.0

        events.append({
            "inbound_message_id": msg["message_id"],
            "sender": sender,
            "sender_domain": sender.split("@")[1] if "@" in sender else "",
            "conversation_id": msg["conversation_id"],
            "timestamp": msg["timestamp"],
            "user_in_to": user_in_to,
            "recipient_count": msg["recipient_count"],
            "user_sole_to": user_sole_to,
            "has_question": msg["has_question"],
            "has_action_language": msg["has_action_language"],
            "mentions_user_name": msg["mentions_user_name"],
            "body_length": msg["body_length"],
            "has_attachments": msg["has_attachments"],
            "thread_depth_at_receipt": msg["thread_position"],
            "other_replies_before_user": other_replies,
            "responded": responded,
            "response_message_id": response_message_id,
            "response_latency_hrs": response_latency,
            "response_type": response_type,
            "is_thread_revival": is_thread_revival,
        })

    return events, automated_excluded


# ── Data Quality Validation ───────────────────────────────────────────────

def validate_extraction(messages, threads, contacts, domains, user_profile, response_events):
    """Run validation checks and print warnings."""
    warnings = []

    sent_count = sum(1 for m in messages if m["direction"] == "sent")
    recv_count = sum(1 for m in messages if m["direction"] == "received")

    # Reconciliation: message counts match user_profile
    if sent_count != user_profile["total_sent"]:
        warnings.append(
            f"RECONCILIATION: sent count mismatch: messages={sent_count}, "
            f"user_profile={user_profile['total_sent']}"
        )
    if recv_count != user_profile["total_received"]:
        warnings.append(
            f"RECONCILIATION: received count mismatch: messages={recv_count}, "
            f"user_profile={user_profile['total_received']}"
        )

    # Reconciliation: replied_to_count vs response_events
    total_replied_contacts = sum(c["replied_to_count"] for c in contacts)
    total_responded_events = sum(1 for e in response_events if e["responded"])
    if total_replied_contacts != total_responded_events:
        warnings.append(
            f"RECONCILIATION: replied count mismatch: contacts sum={total_replied_contacts}, "
            f"response_events responded={total_responded_events}"
        )

    # Reconciliation: response_message_ids exist in messages
    sent_msg_ids = {m["message_id"] for m in messages if m["direction"] == "sent" and m["message_id"]}
    for e in response_events:
        if e["responded"] and e["response_message_id"]:
            if e["response_message_id"] not in sent_msg_ids:
                warnings.append(
                    f"RECONCILIATION: response_message_id {e['response_message_id'][:40]}... "
                    f"not found in sent messages"
                )

    # Reconciliation: thread count matches distinct conversation_ids
    msg_conv_ids = {m["conversation_id"] for m in messages if m["conversation_id"]}
    thread_conv_ids = {t["conversation_id"] for t in threads}
    if msg_conv_ids != thread_conv_ids:
        diff = msg_conv_ids.symmetric_difference(thread_conv_ids)
        warnings.append(
            f"RECONCILIATION: thread count mismatch: {len(diff)} conversation_ids differ"
        )

    # Address normalization: no uppercase
    for m in messages:
        if m["sender"] != m["sender"].lower():
            warnings.append(f"NORMALIZATION: uppercase sender: {m['sender']}")
            break

    # Address normalization: duplicate contacts
    contact_emails = [c["email"] for c in contacts]
    dupes = [e for e, cnt in Counter(contact_emails).items() if cnt > 1]
    if dupes:
        warnings.append(f"NORMALIZATION: duplicate contacts: {dupes}")

    # Reasonableness: reply rate <= 100%
    for c in contacts:
        if c["reply_rate"] is not None and c["reply_rate"] > 1.0:
            warnings.append(
                f"REASONABLENESS: reply rate > 100% for {c['email']}: {c['reply_rate']}"
            )

    # Reasonableness: response latency >= 0
    for e in response_events:
        if e["response_latency_hrs"] is not None and e["response_latency_hrs"] < 0:
            warnings.append(
                f"REASONABLENESS: negative response latency for {e['inbound_message_id']}"
            )

    # Reasonableness: top 10 by sent_total should have received_from_count > 0
    top_by_sent = sorted(contacts, key=lambda c: c["sent_total"], reverse=True)[:10]
    for c in top_by_sent:
        if c["sent_total"] > 10 and c["received_from_count"] == 0:
            warnings.append(
                f"REASONABLENESS: top contact {c['email']} has {c['sent_total']} sent "
                f"but 0 received — possible thread matching issue"
            )

    # Print results
    print(f"\n{'='*60}")
    print(f"VALIDATION: {len(warnings)} warnings")
    print(f"{'='*60}")
    if warnings:
        for w in warnings:
            print(f"  WARNING: {w}")
    else:
        print("  All checks passed.")
    print()

    return warnings


# ── Output ────────────────────────────────────────────────────────────────

def strip_internal_fields(messages):
    """Remove internal fields (prefixed with _) from message dicts."""
    cleaned = []
    for msg in messages:
        cleaned.append({k: v for k, v in msg.items() if not k.startswith("_")})
    return cleaned


# ── Main ──────────────────────────────────────────────────────────────────

def run_extraction():
    """Main entry point: extract, build tables, validate, output JSON."""
    print("Clarion AI Data Extraction v2.0")
    print(f"Window: {WINDOW_DAYS} days")
    print(f"User: {USER_CONFIG['email']}")
    print()

    # Connect to Outlook
    outlook = win32com.client.Dispatch("Outlook.Application")
    namespace = outlook.GetNamespace("MAPI")

    cutoff = (datetime.now() - timedelta(days=WINDOW_DAYS)).strftime("%m/%d/%Y")

    # Step 1: Extract all messages
    print("-- Step 1: Message Extraction --")
    messages = extract_all_messages(namespace, cutoff)
    if not messages:
        print("ERROR: No messages extracted. Aborting.")
        sys.exit(1)

    # Step 2: Thread reconstruction
    print("\n-- Step 2: Thread Reconstruction --")
    messages = reconstruct_threads(messages)

    # Step 3: Build tables
    print("\n-- Step 3: Building Tables --")

    print("  Building threads...")
    threads = build_threads_table(messages)
    print(f"    {len(threads)} threads")

    print("  Building contacts...")
    contacts = build_contacts_table(messages, threads)
    print(f"    {len(contacts)} contacts")

    print("  Building domains...")
    domains = build_domains_table(contacts)
    print(f"    {len(domains)} domains")

    print("  Building user profile...")
    user_profile = build_user_profile(messages)

    print("  Building response events...")
    response_events, automated_excluded = build_response_events(messages)
    responded_count = sum(1 for e in response_events if e["responded"])
    print(f"    {len(response_events)} events ({responded_count} responded)")
    print(f"    {automated_excluded} automated senders excluded")

    # Step 4: Validate
    print("\n-- Step 4: Validation --")
    warnings = validate_extraction(
        messages, threads, contacts, domains, user_profile, response_events
    )

    # Step 5: Output
    print("-- Step 5: Writing Output --")
    sent_count = sum(1 for m in messages if m["direction"] == "sent")
    recv_count = sum(1 for m in messages if m["direction"] == "received")

    # Count self-sent excluded from contacts
    self_contacts = [c for c in contacts if c.get("is_self")]
    self_sent_excluded = sum(c["sent_total"] for c in self_contacts)

    output = {
        "meta": {
            "extraction_date": datetime.now().isoformat(),
            "window_start": user_profile["analysis_window_start"],
            "window_end": user_profile["analysis_window_end"],
            "window_days": WINDOW_DAYS,
            "total_messages_processed": len(messages),
            "sent_count": sent_count,
            "received_count": recv_count,
            "thread_count": len(threads),
            "contact_count": len(contacts),
            "domain_count": len(domains),
            "response_event_count": len(response_events),
            "automated_excluded": automated_excluded,
            "self_sent_excluded": self_sent_excluded,
            "validation_warnings": len(warnings),
            "validation_details": warnings,
            "extraction_version": "2.1",
        },
        "messages": strip_internal_fields(messages),
        "threads": threads,
        "contacts": contacts,
        "domains": domains,
        "user_profile": user_profile,
        "response_events": response_events,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"  Written to {OUTPUT_FILE}")
    print(f"  File size: {OUTPUT_FILE.stat().st_size / 1024:.0f} KB")

    # Summary
    print(f"\n{'='*60}")
    print("EXTRACTION SUMMARY")
    print(f"{'='*60}")
    print(f"  Messages:        {len(messages)} ({sent_count} sent, {recv_count} received)")
    print(f"  Threads:         {len(threads)}")
    print(f"  Contacts:        {len(contacts)}")
    print(f"  Domains:         {len(domains)}")
    print(f"  Response Events: {len(response_events)} ({responded_count} responded, "
          f"{len(response_events) - responded_count} no response)")
    print(f"  Reply Rate:      {user_profile['overall_reply_rate']:.1%}")
    if user_profile['avg_reply_latency_hrs']:
        print(f"  Avg Latency:     {user_profile['avg_reply_latency_hrs']:.1f} hrs")
    print(f"  Warnings:        {len(warnings)}")


if __name__ == "__main__":
    run_extraction()
