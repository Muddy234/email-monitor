"""Clarion AI -- Prediction Model (v3.2 — final)

Loads email_extraction.json, fixes response fan-out labeling, re-labels
forwards, applies zero/near-zero participation hard gate, runs feature
importance analysis, builds a base-rate scorer with interaction-aware lifts,
backtests with rolling-window evaluation, and calibrates via isotonic regression.

Changes from v3.1:
  - Fix recurring pattern double-penalty (is_recurring in skip_features)
  - Add cold-start dampening (0.7x for senders with <3 prior events)
  - Recalibrate all output to isotonic scale (calibrated = primary)
  - Add per-sender demotion counts to fan-out debug output

Changes from v3.0 (v3.1):
  - Reduce Bayesian prior_weight from 5 to 3
  - Soften CC-only multiplier: 0.80 base, 0.90 if sender rate > 0.25
  - Condition thread participation penalty on depth (no med penalty for deep)
  - Add thread recency lift (per-thread last user reply tracking)
  - Add combined penalty floor (prevent penalty stacking from crushing scores)
  - Replace Platt scaling with isotonic regression (PAVA)
  - Add Gina rate investigation debug output

Usage:
    python prediction_model.py                       # full pipeline
    python prediction_model.py --phase 1             # feature analysis only
    python prediction_model.py --phase 2             # scorer only
    python prediction_model.py --phase 3             # backtest only
"""

import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median, mean

# -- Config -------------------------------------------------------------------

DATA_FILE = Path(__file__).parent / "email_extraction.json"
OUTPUT_FILE = Path(__file__).parent / "prediction_results.json"

BAYESIAN_PRIOR_WEIGHT = 3           # shrinkage toward global rate (was 5)
COMBINED_PENALTY_FLOOR = 0.30       # min score as fraction of base rate
LATENCY_CAP_HRS = 168               # 7 days -- beyond = thread revival
RECURRING_CV_THRESHOLD = 0.5        # coefficient of variation for cadence
MIN_CADENCE_OBSERVATIONS = 3        # min occurrences for recurring pattern

# Decision thresholds (conservative launch values)
THRESHOLD_DRAFT = 0.65              # >= this: auto-generate draft
THRESHOLD_FLAG = 0.45               # >= this: flag as "likely needs response"

# Zero-participation gate threshold
ZERO_PARTICIPATION_THRESHOLD = 0.01  # gate threads with participation < this

# Triage: soft gate (LLM bypass) -- will be set from backtest data
# Events below this raw score get classified "no response needed" without LLM
TRIAGE_ACTUAL_RATE_CUTOFF = 0.05    # bypass if actual rate < 5%


# =============================================================================
# STEP 0: DATA LOADING
# =============================================================================

def load_data(path=DATA_FILE):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"Loaded {path.name}: {data['meta']['total_messages_processed']} messages, "
          f"{data['meta']['response_event_count']} events")
    return data


# =============================================================================
# STEP 1: FIX FAN-OUT LABELING
# =============================================================================

def fix_fanout_labeling(events):
    """Fix response fan-out: only the most recent inbound message before each
    user reply gets responded=true."""
    by_response = defaultdict(list)
    for ev in events:
        if ev.get("responded") and ev.get("response_message_id"):
            by_response[ev["response_message_id"]].append(ev)

    demoted = 0
    demoted_by_sender = Counter()
    for response_id, group in by_response.items():
        if len(group) <= 1:
            continue
        group.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
        for ev in group[1:]:
            ev["responded"] = False
            ev["_demoted_from_fanout"] = True
            demoted += 1
            demoted_by_sender[ev["sender"]] += 1

    total = len(events)
    responded = sum(1 for e in events if e.get("responded"))

    print(f"Fan-out fix: demoted {demoted} events from responded=true")
    print(f"  Unique responses: {len(by_response)}")
    print(f"  Top demoted senders:")
    for sender, count in demoted_by_sender.most_common(10):
        print(f"    {sender}: {count}")
    print(f"  After fix: {responded}/{total} responded = {responded/total:.4f}")
    return events


# =============================================================================
# STEP 2: RE-LABEL FORWARDS + ENRICH FEATURES
# =============================================================================

def relabel_events(data):
    """Re-label forwards, enrich with time features and inbound message_type."""
    events = data["response_events"]
    messages_by_id = {m["message_id"]: m for m in data["messages"] if m.get("message_id")}
    user_profile = data["user_profile"]

    active_start = user_profile.get("active_hours_start", 8)
    active_end = user_profile.get("active_hours_end", 18)

    relabeled = 0
    forwarded_count = 0

    for ev in events:
        ev["user_forwarded"] = False
        if ev.get("responded") and ev.get("response_type") == "forward":
            ev["responded"] = False
            ev["user_forwarded"] = True
            relabeled += 1
            forwarded_count += 1
        elif ev.get("response_type") == "forward":
            ev["user_forwarded"] = True
            forwarded_count += 1

        ts = ev.get("timestamp")
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hour = dt.hour
                weekday = dt.strftime("%A")

                ev["arrived_hour"] = hour
                ev["arrived_weekday"] = weekday
                ev["arrived_during_active_hours"] = active_start <= hour < active_end
                ev["arrived_on_active_day"] = weekday in (
                    user_profile.get("active_days") or
                    ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
                )

                if ev["arrived_during_active_hours"] and ev["arrived_on_active_day"]:
                    ev["hours_until_active_window"] = 0.0
                elif not ev["arrived_during_active_hours"] and ev["arrived_on_active_day"]:
                    if hour < active_start:
                        ev["hours_until_active_window"] = float(active_start - hour)
                    else:
                        ev["hours_until_active_window"] = float(24 - hour + active_start)
                else:
                    ev["hours_until_active_window"] = float(24 - hour + active_start)
            except (ValueError, TypeError):
                ev["arrived_during_active_hours"] = None
                ev["hours_until_active_window"] = None

        inbound_msg = messages_by_id.get(ev.get("inbound_message_id"))
        if inbound_msg:
            ev["inbound_message_type"] = inbound_msg.get("message_type", "unknown")
        else:
            ev["inbound_message_type"] = "unknown"

    responded = sum(1 for e in events if e.get("responded"))
    print(f"Forward re-label: {relabeled} forwards removed from responded=true")
    print(f"  After forwards: {responded}/{len(events)} responded = "
          f"{responded/len(events):.4f}")
    return events


# =============================================================================
# STEP 3: ZERO/NEAR-ZERO PARTICIPATION HARD GATE
# =============================================================================

def apply_zero_participation_gate(events, threads):
    """Remove events in threads where user participation rate is near zero.

    Gates threads with participation_rate < 0.01 (not just exactly 0).
    This catches the [0, 0.001) bin (0% actual response rate) plus any
    barely-above-zero threads.
    """
    threads_by_id = {t["conversation_id"]: t for t in threads}

    scorable = []
    gated = 0
    gated_responded = 0

    for ev in events:
        thread = threads_by_id.get(ev.get("conversation_id"), {})
        participation = thread.get("user_participation_rate")

        if (participation is not None
                and participation < ZERO_PARTICIPATION_THRESHOLD
                and thread.get("total_messages", 1) > 1):
            gated += 1
            if ev.get("responded"):
                gated_responded += 1
            continue

        scorable.append(ev)

    responded = sum(1 for e in scorable if e.get("responded"))
    print(f"Zero-participation gate (< {ZERO_PARTICIPATION_THRESHOLD}): "
          f"removed {gated} events ({gated_responded} responded)")
    print(f"  Scorable pool: {responded}/{len(scorable)} = "
          f"{responded/len(scorable):.4f}")
    return scorable, gated


# =============================================================================
# STEP 4: RECOMPUTE REPLY RATES
# =============================================================================

def recompute_reply_rates(events, contacts, domains, user_profile):
    """Recompute sender reply rates on the corrected event set."""

    sender_received = Counter()
    sender_replied = Counter()

    for ev in events:
        sender = ev["sender"]
        sender_received[sender] += 1
        if ev.get("responded"):
            sender_replied[sender] += 1

    global_replied = sum(sender_replied.values())
    global_received = sum(sender_received.values())
    global_rate = global_replied / max(global_received, 1)

    contacts_by_email = {c["email"]: c for c in contacts}
    for sender, count in sender_received.items():
        c = contacts_by_email.get(sender)
        if c:
            c["reply_rate_clean"] = sender_replied[sender] / count if count > 0 else 0
            c["received_in_events"] = count
            c["replied_in_events"] = sender_replied[sender]
            c["reply_rate_smoothed"] = (
                (sender_replied[sender] + BAYESIAN_PRIOR_WEIGHT * global_rate)
                / (count + BAYESIAN_PRIOR_WEIGHT)
            )

    domain_received = Counter()
    domain_replied = Counter()
    for ev in events:
        domain_received[ev["sender_domain"]] += 1
        if ev.get("responded"):
            domain_replied[ev["sender_domain"]] += 1

    domains_by_name = {d["domain"]: d for d in domains}
    for domain, count in domain_received.items():
        d = domains_by_name.get(domain)
        if d:
            d["reply_rate_clean"] = domain_replied[domain] / count if count > 0 else 0

    user_profile["overall_reply_rate_clean"] = global_rate
    user_profile["_global_rate"] = global_rate

    print(f"Reply rates recomputed: global={global_rate:.4f} "
          f"({global_replied}/{global_received}), prior_weight={BAYESIAN_PRIOR_WEIGHT}")
    return global_rate


# =============================================================================
# STEP 5: RECURRING PATTERN DETECTION
# =============================================================================

_STRIP_PATTERNS = re.compile(
    r'\b(?:\d{1,2}[/\-]\d{1,2}[/\-]?\d{0,4}|'
    r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d+'
    r'|\d{4}|#\d+|v\d+)\b',
    re.IGNORECASE,
)

def _normalize_subject(subject):
    if not subject:
        return ""
    s = re.sub(r'^(?:re|fw|fwd)\s*:\s*', '', subject, flags=re.IGNORECASE).strip()
    s = _STRIP_PATTERNS.sub('', s).strip()
    s = re.sub(r'\s+', ' ', s).lower()
    return s


def detect_recurring_patterns(events, contacts):
    """Group events by sender + normalized subject, detect cadence."""

    groups = defaultdict(list)
    for ev in events:
        norm = _normalize_subject(ev.get("_subject", ""))
        key = (ev["sender"], norm)
        if norm:
            groups[key].append(ev)

    patterns = {}
    for (sender, norm_subject), group_events in groups.items():
        if len(group_events) < MIN_CADENCE_OBSERVATIONS:
            continue

        timestamps = sorted(
            datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
            for e in group_events if e.get("timestamp")
        )
        if len(timestamps) < MIN_CADENCE_OBSERVATIONS:
            continue

        gaps = [
            (timestamps[i+1] - timestamps[i]).total_seconds() / 3600
            for i in range(len(timestamps) - 1)
        ]
        if not gaps:
            continue

        mean_gap = mean(gaps)
        if mean_gap <= 0:
            continue

        std_gap = (sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)) ** 0.5
        cv = std_gap / mean_gap

        if cv < RECURRING_CV_THRESHOLD:
            responded_count = sum(1 for e in group_events if e.get("responded"))
            pattern_rate = responded_count / len(group_events)
            patterns[(sender, norm_subject)] = {
                "sender": sender,
                "normalized_subject": norm_subject,
                "count": len(group_events),
                "mean_gap_hours": round(mean_gap, 1),
                "cv": round(cv, 3),
                "pattern_reply_rate": round(pattern_rate, 4),
                "responded": responded_count,
            }

    for ev in events:
        norm = _normalize_subject(ev.get("_subject", ""))
        key = (ev["sender"], norm)
        pat = patterns.get(key)
        if pat:
            ev["is_recurring"] = True
            ev["recurring_reply_rate"] = pat["pattern_reply_rate"]
        else:
            ev["is_recurring"] = False
            ev["recurring_reply_rate"] = None

    print(f"Recurring patterns: {len(patterns)} detected, "
          f"covering {sum(p['count'] for p in patterns.values())} events")
    return patterns


# =============================================================================
# STEP 6: BUILD ANALYSIS TABLE
# =============================================================================

def build_analysis_table(events, contacts, threads, domains, user_profile):
    """Join events with contacts, threads, domains into flat rows."""

    contacts_by_email = {c["email"]: c for c in contacts}
    threads_by_id = {t["conversation_id"]: t for t in threads}
    domains_by_name = {d["domain"]: d for d in domains}

    rows = []
    for ev in events:
        c = contacts_by_email.get(ev["sender"], {})
        t = threads_by_id.get(ev.get("conversation_id"), {})
        d = domains_by_name.get(ev.get("sender_domain"), {})

        row = {
            "responded": ev.get("responded", False),
            "user_forwarded": ev.get("user_forwarded", False),
            "sender": ev["sender"],
            "sender_domain": ev.get("sender_domain", ""),
            "conversation_id": ev.get("conversation_id"),
            "timestamp": ev.get("timestamp"),
            "user_in_to": ev.get("user_in_to", False),
            "user_sole_to": ev.get("user_sole_to", False),
            "recipient_count": ev.get("recipient_count", 1),
            "has_question": ev.get("has_question", False),
            "has_action_language": ev.get("has_action_language", False),
            "mentions_user_name": ev.get("mentions_user_name", False),
            "body_length": ev.get("body_length", 0),
            "has_attachments": ev.get("has_attachments", False),
            "thread_depth_at_receipt": ev.get("thread_depth_at_receipt", 1),
            "other_replies_before_user": ev.get("other_replies_before_user", 0),
            "response_latency_hrs": ev.get("response_latency_hrs"),
            "is_thread_revival": ev.get("is_thread_revival", False),
            "inbound_message_type": ev.get("inbound_message_type", "unknown"),
            "arrived_hour": ev.get("arrived_hour"),
            "arrived_during_active_hours": ev.get("arrived_during_active_hours"),
            "hours_until_active_window": ev.get("hours_until_active_window"),
            "arrived_on_active_day": ev.get("arrived_on_active_day"),
            "is_recurring": ev.get("is_recurring", False),
            "recurring_reply_rate": ev.get("recurring_reply_rate"),
            "sender_reply_rate": c.get("reply_rate_clean"),
            "sender_reply_rate_smoothed": c.get("reply_rate_smoothed"),
            "sender_reply_rate_30d": c.get("reply_rate_30d"),
            "sender_is_internal": c.get("is_internal", False),
            "sender_is_self": c.get("is_self", False),
            "sender_sent_total": c.get("sent_total", 0),
            "sender_received_from": c.get("received_from_count", 0),
            "sender_threads_shared": c.get("threads_shared", 0),
            "thread_participation_rate": t.get("user_participation_rate"),
            "thread_total_messages": t.get("total_messages", 1),
            "thread_user_initiated": t.get("user_initiated", False),
            "thread_duration_hours": t.get("thread_duration_hours", 0),
            "thread_messages_per_day": t.get("messages_per_day"),
            "domain_reply_rate": d.get("reply_rate_clean", d.get("avg_reply_rate")),
            "domain_contact_count": d.get("contact_count", 0),
            "_subject": ev.get("_subject", ""),
        }
        rows.append(row)

    print(f"Analysis table: {len(rows)} rows, {len(rows[0])} features")
    return rows


# =============================================================================
# PHASE 1: FEATURE IMPORTANCE ANALYSIS
# =============================================================================

def analyze_feature_importance(rows):
    """Compute univariate signal strength for each candidate feature."""

    total = len(rows)
    total_responded = sum(1 for r in rows if r["responded"])
    baseline = total_responded / total if total > 0 else 0

    print(f"\n{'='*70}")
    print(f"PHASE 1: FEATURE IMPORTANCE ANALYSIS")
    print(f"{'='*70}")
    print(f"Baseline response rate: {baseline:.4f} ({total_responded}/{total})")

    results = {}

    # -- Boolean features -----------------------------------------------------
    bool_features = [
        "user_in_to", "user_sole_to", "mentions_user_name",
        "has_question", "has_action_language", "has_attachments",
        "sender_is_internal", "arrived_during_active_hours",
        "arrived_on_active_day", "is_recurring", "thread_user_initiated",
    ]

    print(f"\n--- Boolean Features ---")
    print(f"{'Feature':<30} {'N_true':>7} {'Rate_T':>7} {'Rate_F':>7} "
          f"{'Lift':>6} {'Sep':>7}")
    print("-" * 70)

    for feat in bool_features:
        true_rows = [r for r in rows if r.get(feat) is True]
        false_rows = [r for r in rows if r.get(feat) is False]
        if not true_rows or not false_rows:
            continue

        rate_true = sum(1 for r in true_rows if r["responded"]) / len(true_rows)
        rate_false = sum(1 for r in false_rows if r["responded"]) / len(false_rows)
        lift = rate_true / baseline if baseline > 0 else 0
        separation = rate_true - rate_false

        results[feat] = {
            "type": "boolean",
            "n_true": len(true_rows),
            "n_false": len(false_rows),
            "rate_true": round(rate_true, 4),
            "rate_false": round(rate_false, 4),
            "lift": round(lift, 3),
            "separation": round(separation, 4),
        }
        print(f"{feat:<30} {len(true_rows):>7} {rate_true:>7.4f} {rate_false:>7.4f} "
              f"{lift:>6.3f} {separation:>+7.4f}")

    # -- Continuous features (binned) -----------------------------------------
    bin_configs = {
        "sender_reply_rate_smoothed": [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.01],
        "thread_participation_rate": [0, 0.001, 0.05, 0.1, 0.2, 0.3, 0.5, 1.01],
        "recipient_count": [1, 2, 3, 5, 8, 999],
        "thread_depth_at_receipt": [1, 2, 4, 7, 15, 999],
        "body_length": [0, 100, 500, 2000, 999999],
        "hours_until_active_window": [0, 0.01, 4, 12, 999],
    }

    print(f"\n--- Continuous Features (Binned) ---")

    for feat, bins in bin_configs.items():
        print(f"\n  {feat}:")
        print(f"  {'Bin':<20} {'N':>6} {'Rate':>7} {'Lift':>6}")

        feat_results = []
        for i in range(len(bins) - 1):
            lo, hi = bins[i], bins[i+1]
            bin_rows = [
                r for r in rows
                if r.get(feat) is not None and lo <= r[feat] < hi
            ]
            if not bin_rows:
                continue

            rate = sum(1 for r in bin_rows if r["responded"]) / len(bin_rows)
            lift = rate / baseline if baseline > 0 else 0
            label = f"[{lo}, {hi})"
            feat_results.append({
                "bin": label, "n": len(bin_rows),
                "rate": round(rate, 4), "lift": round(lift, 3),
            })
            print(f"  {label:<20} {len(bin_rows):>6} {rate:>7.4f} {lift:>6.3f}")

        results[feat] = {"type": "binned", "bins": feat_results}

    # -- Inbound message_type -------------------------------------------------
    print(f"\n--- Inbound Message Type ---")
    print(f"  {'Type':<15} {'N':>6} {'Rate':>7} {'Lift':>6}")

    msg_type_results = []
    for mtype in ["new", "reply", "forward", "unknown"]:
        mrows = [r for r in rows if r.get("inbound_message_type") == mtype]
        if not mrows:
            continue
        rate = sum(1 for r in mrows if r["responded"]) / len(mrows)
        lift = rate / baseline if baseline > 0 else 0
        msg_type_results.append({
            "type": mtype, "n": len(mrows),
            "rate": round(rate, 4), "lift": round(lift, 3),
        })
        print(f"  {mtype:<15} {len(mrows):>6} {rate:>7.4f} {lift:>6.3f}")

    results["inbound_message_type"] = {"type": "categorical", "values": msg_type_results}

    # -- Feature interactions -------------------------------------------------
    print(f"\n--- Key Feature Interactions ---")
    interactions = _compute_interactions(rows, baseline)
    results["interactions"] = interactions

    # -- Rank by separation ---------------------------------------------------
    print(f"\n--- Feature Ranking (by absolute separation) ---")
    ranked = []
    for feat, info in results.items():
        if info.get("type") == "boolean":
            ranked.append((feat, abs(info["separation"]), info["separation"]))

    ranked.sort(key=lambda x: x[1], reverse=True)
    print(f"{'Rank':>4} {'Feature':<30} {'|Sep|':>7} {'Sep':>7}")
    for i, (feat, abs_sep, sep) in enumerate(ranked, 1):
        marker = " ***" if abs_sep >= 0.03 else ""
        print(f"{i:>4} {feat:<30} {abs_sep:>7.4f} {sep:>+7.4f}{marker}")

    return results, baseline


def _compute_interactions(rows, baseline):
    """Test key feature pairs for interaction effects."""

    interactions = {}

    # reply_rate (binned) x user_in_to
    print(f"\n  sender_reply_rate_smoothed x user_in_to:")
    print(f"  {'Rate Bin':<20} {'in_to':>7} {'not_to':>7} {'Delta':>7}")

    rate_bins = [(0, 0.2, "low"), (0.2, 0.5, "med"), (0.5, 1.01, "high")]
    interaction_data = []

    for lo, hi, label in rate_bins:
        in_to = [r for r in rows if r.get("user_in_to") and
                 r.get("sender_reply_rate_smoothed") is not None and
                 lo <= r["sender_reply_rate_smoothed"] < hi]
        not_to = [r for r in rows if not r.get("user_in_to") and
                  r.get("sender_reply_rate_smoothed") is not None and
                  lo <= r["sender_reply_rate_smoothed"] < hi]

        rate_in = sum(1 for r in in_to if r["responded"]) / max(len(in_to), 1)
        rate_out = sum(1 for r in not_to if r["responded"]) / max(len(not_to), 1)
        delta = rate_in - rate_out

        interaction_data.append({
            "rate_bin": label, "n_in_to": len(in_to), "n_not_to": len(not_to),
            "rate_in_to": round(rate_in, 4), "rate_not_to": round(rate_out, 4),
            "delta": round(delta, 4),
        })
        print(f"  {label:<20} {rate_in:>7.4f} {rate_out:>7.4f} {delta:>+7.4f}")

    interactions["reply_rate_x_user_in_to"] = interaction_data

    # user_in_to x recipient_count
    print(f"\n  user_in_to x recipient_count:")
    print(f"  {'Recip Bin':<20} {'in_to':>7} {'not_to':>7}")

    recip_bins = [(1, 2, "sole"), (2, 5, "small"), (5, 999, "large")]
    recip_interaction = []

    for lo, hi, label in recip_bins:
        in_to = [r for r in rows if r.get("user_in_to") and
                 lo <= r.get("recipient_count", 1) < hi]
        not_to = [r for r in rows if not r.get("user_in_to") and
                  lo <= r.get("recipient_count", 1) < hi]

        rate_in = sum(1 for r in in_to if r["responded"]) / max(len(in_to), 1)
        rate_out = sum(1 for r in not_to if r["responded"]) / max(len(not_to), 1)

        recip_interaction.append({
            "recip_bin": label, "n_in_to": len(in_to), "n_not_to": len(not_to),
            "rate_in_to": round(rate_in, 4), "rate_not_to": round(rate_out, 4),
        })
        print(f"  {label:<20} {rate_in:>7.4f} {rate_out:>7.4f}")

    interactions["user_in_to_x_recipient_count"] = recip_interaction

    # Internal x user_in_to
    print(f"\n  sender_is_internal x user_in_to:")
    combos = [
        ("int+to", True, True), ("int+cc", True, False),
        ("ext+to", False, True), ("ext+cc", False, False),
    ]
    internal_interaction = []
    for label, is_int, in_to in combos:
        subset = [r for r in rows if r.get("sender_is_internal") == is_int
                  and r.get("user_in_to") == in_to]
        rate = sum(1 for r in subset if r["responded"]) / max(len(subset), 1)
        internal_interaction.append({
            "combo": label, "n": len(subset), "rate": round(rate, 4),
        })
        print(f"  {label:<15} n={len(subset):>5}  rate={rate:.4f}")

    interactions["internal_x_user_in_to"] = internal_interaction

    return interactions


# =============================================================================
# PHASE 2: BUILD SCORER
# =============================================================================

def derive_lift_factors(feature_results, baseline):
    """Compute lift factors from Phase 1 results."""

    lifts = {}
    for feat, info in feature_results.items():
        if info.get("type") == "boolean" and abs(info.get("separation", 0)) >= 0.03:
            lifts[feat] = info["lift"]

    # Derive inbound_message_type lifts
    msg_type_info = feature_results.get("inbound_message_type", {})
    msg_type_lifts = {}
    for entry in msg_type_info.get("values", []):
        msg_type_lifts[entry["type"]] = entry["lift"]

    # Derive recipient_count bin multipliers
    recip_info = feature_results.get("recipient_count", {})
    recip_multipliers = []
    if recip_info.get("bins"):
        for b in recip_info["bins"]:
            recip_multipliers.append({
                "bin": b["bin"], "lift": b["lift"],
            })

    # Derive thread_depth peaked multipliers
    depth_info = feature_results.get("thread_depth_at_receipt", {})
    depth_multipliers = []
    if depth_info.get("bins"):
        for b in depth_info["bins"]:
            depth_multipliers.append({
                "bin": b["bin"], "lift": b["lift"],
            })

    # Derive reply_rate x user_in_to interaction
    interaction_data = feature_results.get("interactions", {}).get(
        "reply_rate_x_user_in_to", [])
    rate_to_interaction = {}
    for entry in interaction_data:
        # Compute the TO lift multiplier for each rate bin
        # This is the ratio of in_to rate to not_to rate
        if entry["rate_not_to"] > 0:
            to_multiplier = entry["rate_in_to"] / entry["rate_not_to"]
        else:
            to_multiplier = entry["rate_in_to"] / max(baseline, 0.01)
        rate_to_interaction[entry["rate_bin"]] = round(to_multiplier, 3)

    print(f"\n{'='*70}")
    print(f"PHASE 2: SCORER")
    print(f"{'='*70}")
    print(f"Boolean lift factors (separation >= 0.03):")
    for feat, lift in sorted(lifts.items(), key=lambda x: abs(x[1] - 1), reverse=True):
        direction = "UP" if lift > 1 else "DN"
        print(f"  {feat:<30} {direction} {lift:.3f}")

    print(f"\nMessage type lifts:")
    for mtype, lift in msg_type_lifts.items():
        print(f"  {mtype:<15} {lift:.3f}")

    print(f"\nRecipient count multipliers:")
    for rm in recip_multipliers:
        print(f"  {rm['bin']:<20} {rm['lift']:.3f}")

    print(f"\nThread depth multipliers (peaked):")
    for dm in depth_multipliers:
        print(f"  {dm['bin']:<20} {dm['lift']:.3f}")

    print(f"\nReply rate x TO interaction multipliers:")
    for bin_label, mult in rate_to_interaction.items():
        print(f"  {bin_label:<10} TO mult = {mult:.3f}")

    return {
        "boolean": lifts,
        "msg_type": msg_type_lifts,
        "recipient": recip_multipliers,
        "depth": depth_multipliers,
        "rate_x_to": rate_to_interaction,
    }


def _get_rate_bin(smoothed_rate):
    """Classify a smoothed rate into low/med/high for interaction lookup."""
    if smoothed_rate is None:
        return "low"
    if smoothed_rate < 0.2:
        return "low"
    if smoothed_rate < 0.5:
        return "med"
    return "high"


def _get_recipient_multiplier(recip_count, recip_multipliers, baseline):
    """Lookup recipient count multiplier from bins."""
    if not recip_multipliers:
        return 1.0
    # Parse bins and find matching one
    for rm in recip_multipliers:
        bin_str = rm["bin"]  # e.g. "[1, 2)"
        try:
            parts = bin_str.strip("[]()").split(",")
            lo = float(parts[0].strip())
            hi = float(parts[1].strip().rstrip(")"))
            if lo <= recip_count < hi:
                return rm["lift"]
        except (ValueError, IndexError):
            continue
    # Default: last bin if count is very high
    if recip_multipliers:
        return recip_multipliers[-1]["lift"]
    return 1.0


def _get_depth_multiplier(depth, depth_multipliers):
    """Lookup thread depth multiplier from bins."""
    if not depth_multipliers:
        return 1.0
    for dm in depth_multipliers:
        bin_str = dm["bin"]
        try:
            parts = bin_str.strip("[]()").split(",")
            lo = float(parts[0].strip())
            hi = float(parts[1].strip().rstrip(")"))
            if lo <= depth < hi:
                return dm["lift"]
        except (ValueError, IndexError):
            continue
    if depth_multipliers:
        return depth_multipliers[-1]["lift"]
    return 1.0


def score_email(event_row, all_lifts, contacts_lookup, domains_lookup,
                user_profile, global_rate, thread_recency_hours=None,
                sender_events_so_far=None):
    """Score a single email row. Returns (probability, tier, factors).

    Scoring layers:
    1. Base rate: recurring pattern > sender smoothed > domain > global
    2. Reply_rate x user_in_to interaction (conditional TO lift)
    3. Boolean lifts (excluding features handled elsewhere)
    4. Recipient count multiplier (continuous bin lookup)
    5. Thread depth peaked multiplier
    6. Inbound message type lift
    7. Thread participation adjustment (depth-conditioned)
    8. CC-only penalty (conditional on sender rate)
    9. Cold-start dampening
    10. Thread recency lift
    11. Combined penalty floor
    """

    lifts = all_lifts["boolean"]
    msg_type_lifts = all_lifts.get("msg_type", {})
    recip_multipliers = all_lifts.get("recipient", [])
    depth_multipliers = all_lifts.get("depth", [])
    rate_to_interaction = all_lifts.get("rate_x_to", {})

    factors = []

    # -- Step 1: Base probability ---------------------------------------------
    sender = event_row["sender"]
    sender_domain = event_row.get("sender_domain", "")

    contact = contacts_lookup.get(sender)
    domain = domains_lookup.get(sender_domain)

    sender_smoothed = None
    if event_row.get("is_recurring") and event_row.get("recurring_reply_rate") is not None:
        base = event_row["recurring_reply_rate"]
        factors.append(f"recurring_pattern_rate={base:.3f}")
    elif contact and contact.get("reply_rate_smoothed") is not None:
        base = contact["reply_rate_smoothed"]
        sender_smoothed = base
        factors.append(f"sender_rate={base:.3f}")
    elif domain and domain.get("reply_rate_clean") is not None:
        base = domain["reply_rate_clean"]
        factors.append(f"domain_rate={base:.3f}")
    else:
        base = global_rate
        factors.append(f"global_rate={base:.3f}")

    score = base

    # -- Step 2: Reply rate x TO interaction ----------------------------------
    # Instead of flat TO/sole_to lifts, use the conditional interaction
    rate_bin = _get_rate_bin(sender_smoothed)
    applied_to_lift = False

    if event_row.get("user_in_to") or event_row.get("user_sole_to"):
        to_mult = rate_to_interaction.get(rate_bin)
        if to_mult and to_mult != 0:
            score *= to_mult
            factors.append(f"rate_x_to[{rate_bin}]={to_mult:.3f}")
            applied_to_lift = True

    # -- Step 3: Boolean lifts (skip features handled elsewhere) ---------------
    # user_in_to/user_sole_to: handled by rate_x_to interaction (step 2)
    # is_recurring: base rate already uses pattern-specific rate (step 1)
    skip_features = {"user_in_to", "user_sole_to", "is_recurring"}
    for feat, lift in lifts.items():
        if feat in skip_features:
            continue
        if event_row.get(feat) is True:
            score *= lift
            factors.append(f"{feat}={lift:.3f}")

    # -- Step 4: Recipient count multiplier -----------------------------------
    recip = event_row.get("recipient_count", 1)
    recip_mult = _get_recipient_multiplier(recip, recip_multipliers, global_rate)
    if abs(recip_mult - 1.0) > 0.01:
        score *= recip_mult
        factors.append(f"recip_mult={recip_mult:.3f}")

    # -- Step 5: Thread depth peaked multiplier -------------------------------
    depth = event_row.get("thread_depth_at_receipt", 1)
    depth_mult = _get_depth_multiplier(depth, depth_multipliers)
    if abs(depth_mult - 1.0) > 0.01:
        score *= depth_mult
        factors.append(f"depth_mult={depth_mult:.3f}")

    # -- Step 6: Inbound message type lift ------------------------------------
    msg_type = event_row.get("inbound_message_type", "unknown")
    msg_lift = msg_type_lifts.get(msg_type)
    if msg_lift and abs(msg_lift - 1.0) > 0.01:
        score *= msg_lift
        factors.append(f"msg_type[{msg_type}]={msg_lift:.3f}")

    # -- Step 7: Thread participation adjustment (depth-conditioned) ----------
    thread_part = event_row.get("thread_participation_rate")
    thread_depth = event_row.get("thread_depth_at_receipt", 1)
    if thread_part is not None and thread_depth > 2:
        if thread_part < 0.05:
            score *= 0.5
            factors.append("low_thread_participation=0.500")
        elif thread_part < 0.15 and thread_depth <= 10:
            # Only penalize medium participation in shallow threads;
            # in deep threads (>10), 10-15% participation is real engagement
            score *= 0.75
            factors.append("med_thread_participation=0.750")

    # -- Step 8: CC-only penalty (conditional) --------------------------------
    if not event_row.get("user_in_to") and not applied_to_lift:
        sender_rate = event_row.get("sender_reply_rate_smoothed")
        if sender_rate is not None and sender_rate > 0.25:
            score *= 0.9
            factors.append("cc_only_high_sender=0.900")
        else:
            score *= 0.8
            factors.append("cc_only=0.800")

    # -- Step 9: Cold-start dampening -----------------------------------------
    if sender_events_so_far is not None and sender_events_so_far < 3:
        score *= 0.7
        factors.append(f"cold_start({sender_events_so_far})=0.700")

    # -- Step 10: Thread recency lift -----------------------------------------
    if thread_recency_hours is not None:
        if thread_recency_hours < 24:
            score *= 1.5
            factors.append("thread_recency_24h=1.500")
        elif thread_recency_hours < 72:
            score *= 1.2
            factors.append("thread_recency_72h=1.200")

    # -- Step 11: Combined penalty floor --------------------------------------
    # Prevent penalty stacking from crushing scores below recoverable levels
    floor = base * COMBINED_PENALTY_FLOOR
    if score < floor:
        score = floor
        factors.append(f"penalty_floor={floor:.3f}")

    # -- Cap and floor --------------------------------------------------------
    score = max(0.01, min(0.95, score))

    # -- Determine tier -------------------------------------------------------
    if score >= THRESHOLD_DRAFT:
        tier = "high"
    elif score >= THRESHOLD_FLAG:
        tier = "medium"
    else:
        tier = "low"

    return round(score, 4), tier, factors


# =============================================================================
# PHASE 3: ROLLING BACKTEST
# =============================================================================

def rolling_backtest(rows, all_lifts, contacts, domains, user_profile,
                     global_rate):
    """Backtest with rolling-window computation to avoid temporal leakage.

    For each event at time T, the scorer only uses data from events
    before T to compute sender rates.
    """

    print(f"\n{'='*70}")
    print(f"PHASE 3: ROLLING BACKTEST")
    print(f"{'='*70}")

    sorted_rows = sorted(rows, key=lambda r: r.get("timestamp") or "")

    contacts_lookup = {c["email"]: c for c in contacts}
    domains_lookup = {d["domain"]: d for d in domains}

    # Determine temporal boundaries
    timestamps = [r.get("timestamp") for r in sorted_rows if r.get("timestamp")]
    if timestamps:
        first_dt = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
        last_dt = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
        total_days = max((last_dt - first_dt).days, 1)
        split_date = first_dt.isoformat()
        end_date = last_dt.isoformat()
    else:
        total_days = 60
        split_date = "unknown"
        end_date = "unknown"

    print(f"\nTemporal range: {split_date[:10]} to {end_date[:10]} ({total_days} days)")
    print(f"Rolling window: each event scored using only prior data (temporal holdout)")
    print(f"Daily volume denominator: {total_days} days")

    rolling_sender_received = Counter()
    rolling_sender_replied = Counter()
    rolling_total_received = 0
    rolling_total_replied = 0

    # Track per-thread last user reply timestamp for thread recency lift
    thread_last_user_reply = {}

    predictions = []

    for row in sorted_rows:
        sender = row["sender"]
        conv_id = row.get("conversation_id")

        # Compute rolling rate BEFORE counting this event
        sender_recv = rolling_sender_received[sender]
        sender_repl = rolling_sender_replied[sender]
        total_recv = rolling_total_received

        rolling_global = (rolling_total_replied / max(total_recv, 1)
                          if total_recv > 0 else global_rate)

        # Temporary contact with rolling rates
        rolling_contact = dict(contacts_lookup.get(sender, {}))
        if sender_recv > 0:
            rolling_contact["reply_rate_smoothed"] = (
                (sender_repl + BAYESIAN_PRIOR_WEIGHT * rolling_global)
                / (sender_recv + BAYESIAN_PRIOR_WEIGHT)
            )
        else:
            rolling_contact["reply_rate_smoothed"] = None

        temp_contacts = {sender: rolling_contact}

        # Compute thread recency: hours since user last replied in this thread
        thread_recency_hours = None
        if conv_id and conv_id in thread_last_user_reply and row.get("timestamp"):
            try:
                event_dt = datetime.fromisoformat(
                    row["timestamp"].replace("Z", "+00:00"))
                last_reply_dt = thread_last_user_reply[conv_id]
                delta_hrs = (event_dt - last_reply_dt).total_seconds() / 3600
                if delta_hrs >= 0:
                    thread_recency_hours = delta_hrs
            except (ValueError, TypeError):
                pass

        score, tier, factors = score_email(
            row, all_lifts, temp_contacts, domains_lookup,
            user_profile, rolling_global, thread_recency_hours,
            sender_events_so_far=sender_recv,
        )

        predictions.append({
            "score": score,
            "tier": tier,
            "responded": row["responded"],
            "sender": sender,
            "factors": factors,
            "sender_events_so_far": sender_recv,
            "timestamp": row.get("timestamp"),
            "_subject": row.get("_subject", ""),
            "_row": row,
        })

        # Update rolling counts AFTER scoring
        rolling_sender_received[sender] += 1
        rolling_total_received += 1
        if row.get("responded"):
            rolling_sender_replied[sender] += 1
            rolling_total_replied += 1
            # Track last reply time per thread
            if conv_id and row.get("timestamp"):
                try:
                    reply_dt = datetime.fromisoformat(
                        row["timestamp"].replace("Z", "+00:00"))
                    thread_last_user_reply[conv_id] = reply_dt
                except (ValueError, TypeError):
                    pass

    # -- Metrics at various thresholds ----------------------------------------
    total_responded = sum(1 for p in predictions if p["responded"])
    pool_base_rate = total_responded / len(predictions) if predictions else 0

    print(f"\nTotal predictions: {len(predictions)}")
    print(f"Pool base rate: {pool_base_rate:.4f}")

    thresholds_to_test = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40,
                          0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]

    # -- Raw score metrics (internal reference) --------------------------------
    print(f"\n--- Raw Score Metrics (internal) ---")
    print(f"{'Thresh':>6} {'Prec':>6} {'Recall':>7} {'FPR':>6} "
          f"{'TP':>5} {'FP':>5} {'FN':>5} {'TN':>5} {'ActR<':>6}")

    raw_metric_results = []

    for threshold in thresholds_to_test:
        tp = sum(1 for p in predictions if p["score"] >= threshold and p["responded"])
        fp = sum(1 for p in predictions if p["score"] >= threshold and not p["responded"])
        fn = sum(1 for p in predictions if p["score"] < threshold and p["responded"])
        tn = sum(1 for p in predictions if p["score"] < threshold and not p["responded"])

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        fpr = fp / (fp + tn) if (fp + tn) > 0 else 0

        below_count = fn + tn
        actual_rate_below = fn / below_count if below_count > 0 else 0

        raw_metric_results.append({
            "threshold": threshold,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "fpr": round(fpr, 4),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "actual_rate_below": round(actual_rate_below, 4),
        })

        print(f"{threshold:>6.2f} {precision:>6.3f} {recall:>7.3f} {fpr:>6.3f} "
              f"{tp:>5} {fp:>5} {fn:>5} {tn:>5} {actual_rate_below:>6.1%}")

    # -- Calibration analysis (raw) -------------------------------------------
    calibration = _calibration_analysis(predictions)

    # -- Isotonic regression --------------------------------------------------
    iso_breakpoints = _fit_isotonic_regression(predictions)

    # -- Calibrated metrics (primary output) ----------------------------------
    cal_metric_results = []
    calibrated_predictions = predictions  # fallback
    triage_matrix = {}
    llm_bypass_threshold_cal = 0.03  # default calibrated

    if iso_breakpoints is not None:
        calibrated_predictions = []
        for p in predictions:
            cal_score = _isotonic_transform(p["score"], iso_breakpoints)
            calibrated_predictions.append({**p, "calibrated_score": cal_score})

        cal_thresholds = [0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15,
                          0.20, 0.25, 0.30, 0.35, 0.40]

        print(f"\n{'='*70}")
        print(f"PRIMARY METRICS (Calibrated Scale)")
        print(f"{'='*70}")
        print(f"{'Thresh':>6} {'Prec':>6} {'Recall':>7} {'FPR':>6} "
              f"{'TP':>5} {'FP':>5} {'FN':>5} {'TN':>5} {'D/day':>6} "
              f"{'DismR':>6} {'ActR':>6} {'vBase':>6}")

        for threshold in cal_thresholds:
            tp = sum(1 for p in calibrated_predictions
                     if p["calibrated_score"] >= threshold and p["responded"])
            fp = sum(1 for p in calibrated_predictions
                     if p["calibrated_score"] >= threshold and not p["responded"])
            fn = sum(1 for p in calibrated_predictions
                     if p["calibrated_score"] < threshold and p["responded"])
            tn = sum(1 for p in calibrated_predictions
                     if p["calibrated_score"] < threshold and not p["responded"])

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            fpr = fp / (fp + tn) if (fp + tn) > 0 else 0
            drafts_per_day = (tp + fp) / total_days
            dismiss_rate = fp / (tp + fp) if (tp + fp) > 0 else 0

            above_count = tp + fp
            actual_rate_above = tp / above_count if above_count > 0 else 0
            lift_vs_base = precision / pool_base_rate if pool_base_rate > 0 else 0

            below_count = fn + tn
            actual_rate_below = fn / below_count if below_count > 0 else 0

            cal_metric_results.append({
                "threshold": threshold,
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "fpr": round(fpr, 4),
                "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                "drafts_per_day": round(drafts_per_day, 1),
                "dismiss_rate": round(dismiss_rate, 4),
                "actual_rate_above": round(actual_rate_above, 4),
                "actual_rate_below": round(actual_rate_below, 4),
                "lift_vs_base": round(lift_vs_base, 3),
            })

            marker = ""
            if precision >= 0.70 and recall >= 0.40 and fpr <= 0.05:
                marker = " * MEETS TARGETS"

            print(f"{threshold:>6.2f} {precision:>6.3f} {recall:>7.3f} {fpr:>6.3f} "
                  f"{tp:>5} {fp:>5} {fn:>5} {tn:>5} {drafts_per_day:>6.1f} "
                  f"{dismiss_rate:>6.1%} {actual_rate_above:>6.1%} "
                  f"{lift_vs_base:>5.2f}x{marker}")

        # -- Triage on calibrated scale ---------------------------------------
        llm_bypass_threshold_cal = 0.03  # default
        for m in cal_metric_results:
            if m["actual_rate_below"] < TRIAGE_ACTUAL_RATE_CUTOFF:
                llm_bypass_threshold_cal = m["threshold"]
                break

        print(f"\n--- Triage Layer (Calibrated Scale) ---")
        print(f"  Hard gate: threads with participation < {ZERO_PARTICIPATION_THRESHOLD}")
        print(f"  Soft gate (LLM bypass): calibrated score < {llm_bypass_threshold_cal}")
        print(f"    (actual response rate below this threshold is "
              f"< {TRIAGE_ACTUAL_RATE_CUTOFF:.0%})")

        # Find the triage matrix on calibrated scale
        bypass_m = None
        for m in cal_metric_results:
            if abs(m["threshold"] - llm_bypass_threshold_cal) < 0.001:
                bypass_m = m
                break

        if bypass_m:
            bypassed = bypass_m["fn"] + bypass_m["tn"]
            llm_eligible = bypass_m["tp"] + bypass_m["fp"]
            lost_tp = bypass_m["fn"]
            llm_pool_base = (bypass_m["tp"] / llm_eligible
                             if llm_eligible > 0 else 0)

            triage_matrix = {
                "bypass_threshold_calibrated": llm_bypass_threshold_cal,
                "events_bypassed": bypassed,
                "events_to_llm": llm_eligible,
                "true_positives_lost": lost_tp,
                "true_positives_lost_pct": round(
                    lost_tp / max(total_responded, 1), 4),
                "llm_pool_base_rate": round(llm_pool_base, 4),
                "overall_base_rate": round(pool_base_rate, 4),
                "base_rate_lift": round(llm_pool_base / pool_base_rate, 3)
                                 if pool_base_rate > 0 else 0,
            }

            print(f"\n--- Triage Confusion Matrix "
                  f"(calibrated threshold={llm_bypass_threshold_cal}) ---")
            print(f"  Events bypassed (no LLM): {bypassed}")
            print(f"  Events sent to LLM:       {llm_eligible}")
            print(f"  True positives lost:      {lost_tp} "
                  f"({triage_matrix['true_positives_lost_pct']:.1%} "
                  f"of all positives)")
            print(f"  LLM pool base rate:       {llm_pool_base:.4f} "
                  f"(vs overall {pool_base_rate:.4f}, "
                  f"{triage_matrix['base_rate_lift']:.2f}x lift)")

        # Calibrated score ceiling
        max_cal = max(p["calibrated_score"] for p in calibrated_predictions)
        print(f"\n  Calibrated score ceiling: {max_cal:.4f}")
        print(f"  (This is the model's honest maximum confidence)")

    # -- Calibration plot data ------------------------------------------------
    calibration_plot = _calibration_plot_data(predictions, iso_breakpoints)

    # -- False negative analysis -----------------------------------------------
    fn_analysis = _collect_false_negatives(predictions)

    # -- Misclassification analysis -------------------------------------------
    _analyze_misclassifications(predictions, THRESHOLD_DRAFT)

    # -- Score distribution (raw) ---------------------------------------------
    print(f"\n--- Raw Score Distribution ---")
    score_buckets = Counter()
    for p in predictions:
        bucket = int(p["score"] * 10) / 10
        score_buckets[bucket] += 1

    for bucket in sorted(score_buckets.keys()):
        bar = "#" * (score_buckets[bucket] // 10)
        print(f"  {bucket:.1f}: {score_buckets[bucket]:>5} {bar}")

    # -- Calibrated score distribution ----------------------------------------
    if iso_breakpoints is not None:
        print(f"\n--- Calibrated Score Distribution ---")
        cal_buckets = Counter()
        for p in calibrated_predictions:
            bucket = round(int(p["calibrated_score"] * 20) / 20, 2)
            cal_buckets[bucket] += 1
        for bucket in sorted(cal_buckets.keys()):
            bar = "#" * (cal_buckets[bucket] // 10)
            print(f"  {bucket:.2f}: {cal_buckets[bucket]:>5} {bar}")

    return {
        "predictions": predictions,
        "metrics_calibrated": cal_metric_results,
        "metrics_raw": raw_metric_results,
        "calibration_deciles": calibration,
        "calibration_plot": calibration_plot,
        "iso_breakpoints": iso_breakpoints,
        "triage_matrix": triage_matrix,
        "llm_bypass_threshold_calibrated": llm_bypass_threshold_cal,
        "false_negatives": fn_analysis,
        "temporal": {
            "start_date": split_date[:10] if split_date != "unknown" else "unknown",
            "end_date": end_date[:10] if end_date != "unknown" else "unknown",
            "total_days": total_days,
        },
    }


def _calibration_analysis(predictions):
    """Bucket predictions into deciles, compare predicted vs actual rates."""

    print(f"\n--- Calibration Analysis (Deciles) ---")
    print(f"  {'Decile':<10} {'N':>5} {'Avg Pred':>9} {'Actual':>8} {'Gap':>7}")

    sorted_preds = sorted(predictions, key=lambda p: p["score"])
    n = len(sorted_preds)
    decile_size = max(n // 10, 1)

    calibration = []
    for i in range(10):
        start = i * decile_size
        end = start + decile_size if i < 9 else n
        bucket = sorted_preds[start:end]
        if not bucket:
            continue

        avg_pred = mean(p["score"] for p in bucket)
        actual_rate = sum(1 for p in bucket if p["responded"]) / len(bucket)
        gap = actual_rate - avg_pred

        calibration.append({
            "decile": i + 1,
            "n": len(bucket),
            "avg_predicted": round(avg_pred, 4),
            "actual_rate": round(actual_rate, 4),
            "gap": round(gap, 4),
        })
        print(f"  D{i+1:<9} {len(bucket):>5} {avg_pred:>9.4f} {actual_rate:>8.4f} "
              f"{gap:>+7.4f}")

    if calibration:
        mae = mean(abs(d["gap"]) for d in calibration)
        print(f"\n  Mean absolute calibration error: {mae:.4f}")

    return calibration


def _fit_isotonic_regression(predictions):
    """Fit isotonic regression via PAVA (pool adjacent violators algorithm).

    Returns a list of (score_boundary, calibrated_value) breakpoints that
    define a piecewise-constant monotonic calibration function.
    Preserves rank ordering without forcing a sigmoid shape.
    """
    if not predictions:
        return None

    # Sort by raw score
    paired = sorted(
        [(p["score"], 1 if p["responded"] else 0) for p in predictions],
        key=lambda x: x[0],
    )

    scores = [s for s, _ in paired]
    labels = [y for _, y in paired]
    n = len(scores)

    # PAVA: pool adjacent violators
    # Start with each point as its own block
    blocks = [[labels[i]] for i in range(n)]
    block_scores = [[scores[i]] for i in range(n)]

    # Merge blocks that violate monotonicity
    changed = True
    while changed:
        changed = False
        merged_blocks = [blocks[0]]
        merged_scores = [block_scores[0]]
        for i in range(1, len(blocks)):
            prev_mean = mean(merged_blocks[-1])
            curr_mean = mean(blocks[i])
            if curr_mean < prev_mean:
                # Violation: merge with previous
                merged_blocks[-1].extend(blocks[i])
                merged_scores[-1].extend(block_scores[i])
                changed = True
            else:
                merged_blocks.append(blocks[i])
                merged_scores.append(block_scores[i])
        blocks = merged_blocks
        block_scores = merged_scores

    # Build breakpoints: (max_score_in_block, calibrated_value)
    breakpoints = []
    for blk, blk_scores in zip(blocks, block_scores):
        cal_val = mean(blk)
        max_score = max(blk_scores)
        breakpoints.append((max_score, round(cal_val, 4)))

    print(f"\n--- Isotonic Regression (PAVA) ---")
    print(f"  {len(breakpoints)} breakpoints from {n} predictions")

    # Show calibration after isotonic
    cal_scores = [_isotonic_transform(s, breakpoints) for s in scores]
    print(f"  Calibrated score range: [{min(cal_scores):.4f}, {max(cal_scores):.4f}]")

    decile_size = max(n // 10, 1)
    cal_paired = sorted(zip(cal_scores, labels), key=lambda x: x[0])

    print(f"  {'Decile':<10} {'Avg Cal':>9} {'Actual':>8} {'Gap':>7}")
    iso_mae_parts = []
    for i in range(10):
        start = i * decile_size
        end = start + decile_size if i < 9 else n
        bucket = cal_paired[start:end]
        if not bucket:
            continue
        avg_cal = mean(s for s, _ in bucket)
        actual = sum(y for _, y in bucket) / len(bucket)
        gap = actual - avg_cal
        iso_mae_parts.append(abs(gap))
        print(f"  D{i+1:<9} {avg_cal:>9.4f} {actual:>8.4f} {gap:>+7.4f}")

    if iso_mae_parts:
        print(f"\n  Isotonic MAE: {mean(iso_mae_parts):.4f}")

    return breakpoints


def _isotonic_transform(score, breakpoints):
    """Apply isotonic calibration: piecewise-constant lookup."""
    if not breakpoints:
        return score
    for boundary, cal_val in breakpoints:
        if score <= boundary:
            return cal_val
    # Above all breakpoints: use last value
    return breakpoints[-1][1]


def _calibration_plot_data(predictions, iso_breakpoints):
    """Generate calibration plot data: 20 equally-spaced bins of raw score
    with predicted vs actual, plus isotonic-calibrated predicted."""

    n_bins = 20
    bin_width = 1.0 / n_bins

    plot_data = []
    for i in range(n_bins):
        lo = i * bin_width
        hi = (i + 1) * bin_width
        bucket = [p for p in predictions if lo <= p["score"] < hi]
        if not bucket:
            continue

        avg_raw = mean(p["score"] for p in bucket)
        actual_rate = sum(1 for p in bucket if p["responded"]) / len(bucket)

        avg_calibrated = None
        if iso_breakpoints is not None:
            avg_calibrated = round(
                _isotonic_transform(avg_raw, iso_breakpoints), 4)

        plot_data.append({
            "bin": f"[{lo:.2f}, {hi:.2f})",
            "n": len(bucket),
            "avg_raw_score": round(avg_raw, 4),
            "actual_rate": round(actual_rate, 4),
            "isotonic_calibrated": avg_calibrated,
        })

    print(f"\n--- Calibration Plot Data (20 bins) ---")
    print(f"  {'Bin':<15} {'N':>5} {'Raw':>7} {'Actual':>8} {'Isotonic':>8}")
    for d in plot_data:
        iso_str = f"{d['isotonic_calibrated']:.4f}" if d['isotonic_calibrated'] else "     N/A"
        print(f"  {d['bin']:<15} {d['n']:>5} {d['avg_raw_score']:>7.4f} "
              f"{d['actual_rate']:>8.4f} {iso_str:>8}")

    return plot_data


def _collect_false_negatives(predictions, critical_threshold=0.10, broad_threshold=0.50):
    """Collect false negatives (model scored low, user actually replied).

    Returns two tiers:
    - critical: score < critical_threshold (would be bypassed entirely by triage)
    - broad: score < broad_threshold (model leaned negative)

    Both sorted by score ascending (worst misses first).
    """
    all_fn = [
        p for p in predictions
        if p["responded"] and p["score"] < broad_threshold
    ]
    all_fn.sort(key=lambda p: p["score"])

    def _build_fn_record(p):
        row = p.get("_row", {})
        return {
            "sender": p["sender"],
            "subject": p.get("_subject", ""),
            "timestamp": p.get("timestamp"),
            "score": p["score"],
            "factors": p["factors"],
            "sender_events_so_far": p.get("sender_events_so_far", 0),
            # Key features that influenced the score
            "features": {
                "user_in_to": row.get("user_in_to"),
                "user_sole_to": row.get("user_sole_to"),
                "recipient_count": row.get("recipient_count"),
                "has_question": row.get("has_question"),
                "has_action_language": row.get("has_action_language"),
                "mentions_user_name": row.get("mentions_user_name"),
                "sender_is_internal": row.get("sender_is_internal"),
                "sender_reply_rate_smoothed": round(row["sender_reply_rate_smoothed"], 4)
                    if row.get("sender_reply_rate_smoothed") is not None else None,
                "thread_participation_rate": round(row["thread_participation_rate"], 4)
                    if row.get("thread_participation_rate") is not None else None,
                "thread_depth_at_receipt": row.get("thread_depth_at_receipt"),
                "inbound_message_type": row.get("inbound_message_type"),
                "is_recurring": row.get("is_recurring"),
                "thread_user_initiated": row.get("thread_user_initiated"),
            },
        }

    critical = [_build_fn_record(p) for p in all_fn if p["score"] < critical_threshold]
    broad = [_build_fn_record(p) for p in all_fn]

    # Summary stats
    critical_senders = Counter(p["sender"] for p in all_fn if p["score"] < critical_threshold)
    broad_senders = Counter(p["sender"] for p in all_fn)

    print(f"\n--- False Negative Analysis ---")
    print(f"  Critical (score < {critical_threshold}): {len(critical)} events")
    if critical_senders:
        print(f"  Top critical FN senders:")
        for sender, count in critical_senders.most_common(10):
            print(f"    {sender}: {count}")

    print(f"  Broad (score < {broad_threshold}): {len(broad)} events")
    if broad_senders:
        print(f"  Top broad FN senders:")
        for sender, count in broad_senders.most_common(10):
            print(f"    {sender}: {count}")

    # Feature patterns across FNs
    if broad:
        in_to_pct = sum(1 for r in broad if r["features"].get("user_in_to")) / len(broad)
        question_pct = sum(1 for r in broad if r["features"].get("has_question")) / len(broad)
        internal_pct = sum(1 for r in broad if r["features"].get("sender_is_internal")) / len(broad)
        cold_start = sum(1 for r in broad if r["sender_events_so_far"] < 3) / len(broad)
        print(f"\n  Broad FN feature patterns:")
        print(f"    user_in_to:       {in_to_pct:.1%}")
        print(f"    has_question:     {question_pct:.1%}")
        print(f"    sender_internal:  {internal_pct:.1%}")
        print(f"    cold_start (<3):  {cold_start:.1%}")

    return {
        "critical_threshold": critical_threshold,
        "broad_threshold": broad_threshold,
        "critical_count": len(critical),
        "broad_count": len(broad),
        "critical": critical,
        "broad": broad,
    }


def _analyze_misclassifications(predictions, threshold):
    """Examine false positives and false negatives."""

    fp = [p for p in predictions if p["score"] >= threshold and not p["responded"]]
    fn = [p for p in predictions if p["score"] < threshold and p["responded"]]

    print(f"\n--- Misclassifications at threshold={threshold} ---")
    print(f"  False Positives: {len(fp)}")
    if fp:
        fp_senders = Counter(p["sender"] for p in fp)
        print(f"  Top FP senders:")
        for sender, count in fp_senders.most_common(10):
            print(f"    {sender}: {count}")
        cold_start_fp = sum(1 for p in fp if p.get("sender_events_so_far", 0) < 3)
        print(f"  Cold-start FPs (< 3 prior events): {cold_start_fp} "
              f"({cold_start_fp/max(len(fp),1)*100:.0f}%)")

    print(f"\n  False Negatives: {len(fn)}")
    if fn:
        fn_senders = Counter(p["sender"] for p in fn)
        print(f"  Top FN senders:")
        for sender, count in fn_senders.most_common(10):
            print(f"    {sender}: {count}")
        cold_start_fn = sum(1 for p in fn if p.get("sender_events_so_far", 0) < 3)
        print(f"  Cold-start FNs (< 3 prior events): {cold_start_fn} "
              f"({cold_start_fn/max(len(fn),1)*100:.0f}%)")


def _debug_sender_rates(events, contacts, global_rate):
    """Print rate details for high-volume senders to verify smoothing."""
    contacts_by_email = {c["email"]: c for c in contacts}

    sender_received = Counter()
    sender_replied = Counter()
    for ev in events:
        sender_received[ev["sender"]] += 1
        if ev.get("responded"):
            sender_replied[ev["sender"]] += 1

    # Show top senders by volume
    top_senders = sender_received.most_common(15)
    print(f"\n--- Sender Rate Debug (top 15 by volume) ---")
    print(f"  {'Sender':<40} {'Recv':>5} {'Repl':>5} {'Raw':>6} "
          f"{'Smoothed':>8} {'AllTime':>7}")

    for sender, recv in top_senders:
        repl = sender_replied[sender]
        raw_rate = repl / recv if recv > 0 else 0
        c = contacts_by_email.get(sender, {})
        smoothed = c.get("reply_rate_smoothed")
        all_time = c.get("reply_rate", c.get("avg_reply_rate"))

        smoothed_str = f"{smoothed:.4f}" if smoothed is not None else "  N/A"
        alltime_str = f"{all_time:.4f}" if all_time is not None else "  N/A"

        print(f"  {sender:<40} {recv:>5} {repl:>5} {raw_rate:>6.4f} "
              f"{smoothed_str:>8} {alltime_str:>7}")

    print(f"  (global_rate={global_rate:.4f}, prior_weight={BAYESIAN_PRIOR_WEIGHT})")


# =============================================================================
# MAIN
# =============================================================================

def main():
    phase = None
    if "--phase" in sys.argv:
        idx = sys.argv.index("--phase")
        if idx + 1 < len(sys.argv):
            phase = int(sys.argv[idx + 1])

    # Load
    data = load_data()

    # Enrich events with subject (for recurring detection)
    messages_by_id = {m["message_id"]: m for m in data["messages"] if m.get("message_id")}
    for ev in data["response_events"]:
        inbound = messages_by_id.get(ev.get("inbound_message_id"))
        ev["_subject"] = inbound.get("subject", "") if inbound else ""

    # -- Data corrections -----------------------------------------------------
    print(f"\n--- Data Corrections ---")

    orig_responded = sum(1 for e in data["response_events"] if e.get("responded"))
    print(f"Original: {orig_responded}/{len(data['response_events'])} responded "
          f"= {orig_responded/len(data['response_events']):.4f}")

    fix_fanout_labeling(data["response_events"])
    events = relabel_events(data)
    events, gated_count = apply_zero_participation_gate(events, data["threads"])

    global_rate = recompute_reply_rates(
        events, data["contacts"], data["domains"], data["user_profile"]
    )

    # -- Debug: investigate high-volume sender rates after fan-out fix --------
    _debug_sender_rates(events, data["contacts"], global_rate)

    patterns = detect_recurring_patterns(events, data["contacts"])

    rows = build_analysis_table(
        events, data["contacts"], data["threads"],
        data["domains"], data["user_profile"]
    )

    # Phase 1
    if phase is None or phase == 1:
        feature_results, baseline = analyze_feature_importance(rows)

    if phase == 1:
        return

    # Phase 2
    if phase is None or phase >= 2:
        if phase == 2:
            feature_results, baseline = analyze_feature_importance(rows)
        all_lifts = derive_lift_factors(feature_results, baseline)

    if phase == 2:
        return

    # Phase 3
    if phase is None or phase >= 3:
        if phase == 3:
            feature_results, baseline = analyze_feature_importance(rows)
            all_lifts = derive_lift_factors(feature_results, baseline)

        bt_results = rolling_backtest(
            rows, all_lifts, data["contacts"], data["domains"],
            data["user_profile"], global_rate,
        )

        # -- Save results -----------------------------------------------------
        output = {
            "meta": {
                "version": "3.2",
                "extraction_file": str(DATA_FILE),
                "run_date": datetime.now().isoformat(),
                "total_events_original": len(data["response_events"]),
                "total_events_scored": len(rows),
                "events_gated_zero_participation": gated_count,
                "zero_participation_threshold": ZERO_PARTICIPATION_THRESHOLD,
                "global_reply_rate": round(global_rate, 4),
                "baseline_rate": round(baseline, 4),
                "bayesian_prior_weight": BAYESIAN_PRIOR_WEIGHT,
                "recurring_patterns_count": len(patterns),
                "temporal_holdout": True,
                "temporal_start": bt_results["temporal"]["start_date"],
                "temporal_end": bt_results["temporal"]["end_date"],
                "temporal_days": bt_results["temporal"]["total_days"],
            },
            "lift_factors": all_lifts,
            "feature_analysis": {
                k: v for k, v in feature_results.items()
                if k != "interactions"
            },
            "interactions": feature_results.get("interactions", {}),
            "backtest_metrics_raw": bt_results["metrics_raw"],
            "backtest_metrics_calibrated": bt_results["metrics_calibrated"],
            "calibration_deciles": bt_results["calibration_deciles"],
            "calibration_plot": bt_results["calibration_plot"],
            "calibration": {
                "method": "isotonic_regression",
                "breakpoint_count": len(bt_results["iso_breakpoints"])
                    if bt_results["iso_breakpoints"] else 0,
            },
            "iso_breakpoints": [
                list(bp) for bp in bt_results["iso_breakpoints"]
            ] if bt_results["iso_breakpoints"] else [],
            "triage": {
                "hard_gate_threshold": ZERO_PARTICIPATION_THRESHOLD,
                "llm_bypass_threshold_calibrated": bt_results["llm_bypass_threshold_calibrated"],
                "actual_rate_cutoff": TRIAGE_ACTUAL_RATE_CUTOFF,
                "confusion_matrix": bt_results["triage_matrix"],
            },
            "false_negatives": bt_results["false_negatives"],
            "recurring_patterns": list(patterns.values()),
            "thresholds": {
                "draft": THRESHOLD_DRAFT,
                "flag": THRESHOLD_FLAG,
            },
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)

        print(f"\nResults saved to {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
