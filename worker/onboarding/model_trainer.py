"""Per-user prediction model training.

Trains a multiplicative scoring model from a user's response_events,
producing scoring_parameters JSON stored in the DB. Replaces the global
prediction_results.json with per-user artifacts.

Ported from scripts/prediction_model.py, adapted for runtime use.
"""

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean

from onboarding.stats_extraction import _parse_time, _normalize_subject

logger = logging.getLogger("worker.onboarding")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BAYESIAN_PRIOR_WEIGHT = 3
MIN_EVENTS_TO_TRAIN = 30
COMBINED_PENALTY_FLOOR = 0.30
SCORE_CAP = 0.95
SCORE_FLOOR = 0.01
SEPARATION_THRESHOLD = 0.03
PENALTY_DAMPEN_ALPHA = 0.8
LIFT_CAP_UPPER = 2.5
LIFT_CAP_LOWER = 0.7
RECURRING_CV_THRESHOLD = 0.5
MIN_CADENCE_OBSERVATIONS = 3
RETRAIN_EVENT_THRESHOLD = 100
RETRAIN_DAYS_THRESHOLD = 30

# Default parameters for users with insufficient data
DEFAULT_PARAMETERS = {
    "meta": {
        "user_id": None,
        "total_events": 0,
        "global_rate": 0.25,
        "generated_at": None,
    },
    "prior_weight": BAYESIAN_PRIOR_WEIGHT,
    "lift_factors": {
        "boolean": {},
        "msg_type": {},
        "recipient_bins": {"[1, 2)": 1.0, "[2, 4)": 1.0, "[4, 8)": 1.0,
                           "[8, 16)": 1.0, "[16, 999)": 1.0},
        "depth_bins": {"[1, 2)": 1.0, "[2, 4)": 1.0, "[4, 8)": 1.0,
                       "[8, 20)": 1.0, "[20, 999)": 1.0},
        "rate_x_to": {},
    },
    "iso_breakpoints": [],
    "recurring_patterns": {},
    "triage": {"hard_gate_threshold": 0.01, "soft_gate_threshold": 0.03},
    "thresholds": {
        "combined_penalty_floor": COMBINED_PENALTY_FLOOR,
        "score_cap": SCORE_CAP,
        "score_floor": SCORE_FLOOR,
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def train_user_model(db, user_id):
    """Train a per-user scoring model from response events.

    Returns:
        dict: scoring_parameters JSON to store in DB.
    """
    events = db.fetch_response_events(user_id)
    if len(events) < MIN_EVENTS_TO_TRAIN:
        logger.info(f"User {user_id[:8]}...: only {len(events)} events, using defaults")
        params = dict(DEFAULT_PARAMETERS)
        params["meta"] = {
            "user_id": user_id,
            "total_events": len(events),
            "global_rate": 0.25,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        db.upsert_scoring_parameters(user_id, params, emails_used=len(events))
        return params

    logger.info(f"User {user_id[:8]}...: training model on {len(events)} events")

    # Compute global rate
    total_responded = sum(1 for e in events if e.get("responded"))
    global_rate = total_responded / len(events)

    # Step 1: Feature importance
    feature_results = _analyze_features(events, global_rate)

    # Step 2: Derive lift factors
    lift_factors = _derive_lift_factors(feature_results, global_rate)

    # Step 3: Detect recurring patterns
    recurring = _detect_recurring_patterns(events)

    # Step 4: Score all events
    predictions = _score_all_events(events, lift_factors, global_rate, recurring)

    # Step 5: Fit isotonic regression
    iso_breakpoints = _fit_isotonic(predictions)

    # Step 6: Compute triage thresholds
    triage = _compute_triage(predictions, iso_breakpoints)

    params = {
        "meta": {
            "user_id": user_id,
            "total_events": len(events),
            "global_rate": round(global_rate, 4),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "prior_weight": BAYESIAN_PRIOR_WEIGHT,
        "lift_factors": lift_factors,
        "iso_breakpoints": iso_breakpoints,
        "recurring_patterns": recurring,
        "triage": triage,
        "thresholds": {
            "combined_penalty_floor": COMBINED_PENALTY_FLOOR,
            "score_cap": SCORE_CAP,
            "score_floor": SCORE_FLOOR,
        },
    }

    db.upsert_scoring_parameters(user_id, params, emails_used=len(events))
    logger.info(f"User {user_id[:8]}...: model trained, "
                f"global_rate={global_rate:.4f}, "
                f"{len(iso_breakpoints)} breakpoints, "
                f"{len(recurring)} recurring patterns")
    return params


def check_retrain_needed(db, user_id):
    """Check if a user's model needs re-training."""
    params = db.fetch_scoring_parameters(user_id)
    if not params:
        return True

    meta = params.get("meta", {})
    generated_at = meta.get("generated_at")
    if not generated_at:
        return True

    # Check days elapsed
    try:
        gen_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        days_elapsed = (datetime.now(timezone.utc) - gen_dt).days
        if days_elapsed >= RETRAIN_DAYS_THRESHOLD:
            return True
    except (ValueError, TypeError):
        return True

    # Check new events count
    new_count = db.count_response_events_since(user_id, generated_at)
    if new_count >= RETRAIN_EVENT_THRESHOLD:
        return True

    return False


# ---------------------------------------------------------------------------
# Feature importance
# ---------------------------------------------------------------------------

def _derive_event_features(ev):
    """Derive boolean features for a single event without mutating it."""
    return {
        "user_in_to": ev.get("user_position") == "TO",
        "user_sole_to": (
            ev.get("user_position") == "TO"
            and ev.get("total_recipients", 1) == 1
        ),
        "mentions_user_name": ev.get("mentions_user_name") or False,
        "has_question": ev.get("has_question") or False,
        "has_action_language": ev.get("has_action_language") or False,
        "sender_is_internal": ev.get("sender_is_internal") or False,
        "is_recurring": ev.get("is_recurring") or False,
        "thread_user_initiated": ev.get("thread_user_initiated") or False,
        "arrived_during_active_hours": ev.get("arrived_during_active_hours") or False,
        "arrived_on_active_day": ev.get("arrived_on_active_day") or False,
    }


def _analyze_features(events, global_rate):
    """Compute univariate signal strength for boolean features."""
    results = {}

    bool_features = [
        "user_in_to", "user_sole_to", "mentions_user_name",
        "has_question", "has_action_language", "sender_is_internal",
        "is_recurring", "thread_user_initiated",
        "arrived_during_active_hours", "arrived_on_active_day",
    ]

    # Derive boolean features without mutating event dicts
    derived = [_derive_event_features(ev) for ev in events]

    for feat in bool_features:
        true_idx = [i for i, d in enumerate(derived) if d.get(feat) is True]
        false_idx = [i for i, d in enumerate(derived) if d.get(feat) is False]
        if not true_idx or not false_idx:
            continue

        rate_true = sum(1 for i in true_idx if events[i]["responded"]) / len(true_idx)
        rate_false = sum(1 for i in false_idx if events[i]["responded"]) / len(false_idx)
        lift = rate_true / global_rate if global_rate > 0 else 0
        separation = rate_true - rate_false

        results[feat] = {
            "type": "boolean",
            "rate_true": round(rate_true, 4),
            "rate_false": round(rate_false, 4),
            "lift": round(lift, 3),
            "separation": round(separation, 4),
            "n_true": len(true_idx),
        }

    # Message type analysis
    msg_type_results = {}
    for mtype in ["new", "reply", "forward"]:
        mrows = [e for e in events if e.get("subject_type") == mtype]
        if not mrows:
            continue
        rate = sum(1 for r in mrows if r["responded"]) / len(mrows)
        lift = rate / global_rate if global_rate > 0 else 0
        msg_type_results[mtype] = {"rate": round(rate, 4), "lift": round(lift, 3), "n": len(mrows)}
    results["msg_type"] = msg_type_results

    # Recipient count bins
    recip_bins = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 999)]
    recip_results = {}
    for lo, hi in recip_bins:
        bin_rows = [e for e in events
                    if lo <= e.get("total_recipients", 1) < hi]
        if not bin_rows:
            continue
        rate = sum(1 for r in bin_rows if r["responded"]) / len(bin_rows)
        lift = rate / global_rate if global_rate > 0 else 0
        recip_results[f"[{lo}, {hi})"] = {"rate": round(rate, 4), "lift": round(lift, 3), "n": len(bin_rows)}
    results["recipient_bins"] = recip_results

    # Thread depth bins
    depth_bins_spec = [(1, 2), (2, 4), (4, 8), (8, 20), (20, 999)]
    depth_results = {}
    depth_dist = Counter()
    for lo, hi in depth_bins_spec:
        bin_rows = [e for e in events
                    if lo <= e.get("thread_depth", 1) < hi]
        depth_dist[f"[{lo}, {hi})"] = len(bin_rows)
        if not bin_rows:
            continue
        rate = sum(1 for r in bin_rows if r["responded"]) / len(bin_rows)
        lift = rate / global_rate if global_rate > 0 else 0
        depth_results[f"[{lo}, {hi})"] = {
            "rate": round(rate, 4), "lift": round(lift, 3), "n": len(bin_rows),
        }
    results["depth_bins"] = depth_results
    logger.info(f"Thread depth distribution: {dict(depth_dist)}")

    # Rate × TO interaction
    rate_bins = [(0, 0.05, "0-5%"), (0.05, 0.15, "5-15%"),
                 (0.15, 0.30, "15-30%"), (0.30, 1.01, "30%+")]
    # Group events by sender to get sender rates
    sender_rates = _compute_sender_rates(events)
    rate_to_results = {}
    for lo, hi, label in rate_bins:
        in_to = [e for e in events
                 if e.get("user_in_to")
                 and lo <= sender_rates.get(e["sender_email"], 0) < hi]
        not_to = [e for e in events
                  if not e.get("user_in_to")
                  and lo <= sender_rates.get(e["sender_email"], 0) < hi]
        if in_to:
            rate_in = sum(1 for r in in_to if r["responded"]) / len(in_to)
        else:
            rate_in = 0
        if not_to:
            rate_out = sum(1 for r in not_to if r["responded"]) / len(not_to)
        else:
            rate_out = 0
        rate_to_results[label] = {
            "rate_in_to": round(rate_in, 4),
            "rate_not_to": round(rate_out, 4),
            "n_in_to": len(in_to),
            "n_not_to": len(not_to),
        }
    results["rate_x_to"] = rate_to_results

    return results


def _compute_sender_rates(events):
    """Compute raw reply rate per sender."""
    by_sender = defaultdict(lambda: {"total": 0, "responded": 0})
    for ev in events:
        by_sender[ev["sender_email"]]["total"] += 1
        if ev.get("responded"):
            by_sender[ev["sender_email"]]["responded"] += 1
    return {
        s: d["responded"] / d["total"] if d["total"] > 0 else 0
        for s, d in by_sender.items()
    }


# ---------------------------------------------------------------------------
# Lift factor derivation
# ---------------------------------------------------------------------------

def _derive_lift_factors(feature_results, global_rate):
    """Derive lift factors from feature analysis."""
    # Boolean lifts — only features with significant separation
    boolean_lifts = {}
    for feat, info in feature_results.items():
        if isinstance(info, dict) and info.get("type") == "boolean":
            if abs(info.get("separation", 0)) >= SEPARATION_THRESHOLD:
                lift = info["lift"]
                # Cap lifts to [LIFT_CAP_LOWER, LIFT_CAP_UPPER]
                lift = max(LIFT_CAP_LOWER, min(LIFT_CAP_UPPER, lift))
                # Dampen reductive lifts toward 1.0
                if lift < 1.0:
                    lift = 1.0 + PENALTY_DAMPEN_ALPHA * (lift - 1.0)
                boolean_lifts[feat] = round(lift, 3)

    # Message type lifts
    msg_type_lifts = {}
    msg_data = feature_results.get("msg_type", {})
    for mtype, info in msg_data.items():
        if isinstance(info, dict) and info.get("lift"):
            lift = max(LIFT_CAP_LOWER, min(LIFT_CAP_UPPER, info["lift"]))
            if lift < 1.0:
                lift = 1.0 + PENALTY_DAMPEN_ALPHA * (lift - 1.0)
            msg_type_lifts[mtype] = round(lift, 3)

    # Recipient bin multipliers
    recipient_bins = {}
    recip_data = feature_results.get("recipient_bins", {})
    for bin_label, info in recip_data.items():
        if isinstance(info, dict) and info.get("lift"):
            lift = max(LIFT_CAP_LOWER, min(LIFT_CAP_UPPER, info["lift"]))
            if lift < 1.0:
                lift = 1.0 + PENALTY_DAMPEN_ALPHA * (lift - 1.0)
            recipient_bins[bin_label] = round(lift, 3)

    # Depth bin multipliers — computed from thread_depth on events
    depth_bins = {}
    depth_data = feature_results.get("depth_bins", {})
    for bin_label, info in depth_data.items():
        if isinstance(info, dict) and info.get("lift"):
            lift = max(LIFT_CAP_LOWER, min(LIFT_CAP_UPPER, info["lift"]))
            if lift < 1.0:
                lift = 1.0 + PENALTY_DAMPEN_ALPHA * (lift - 1.0)
            depth_bins[bin_label] = round(lift, 3)
    # Fallback to identity if no depth data (pre-migration events)
    if not depth_bins:
        depth_bins = {
            "[1, 2)": 1.0, "[2, 4)": 1.0, "[4, 8)": 1.0,
            "[8, 20)": 1.0, "[20, 999)": 1.0,
        }

    # Rate × TO interaction
    rate_x_to = {}
    rate_to_data = feature_results.get("rate_x_to", {})
    for label, info in rate_to_data.items():
        if isinstance(info, dict):
            if info.get("rate_not_to", 0) > 0:
                to_mult = info["rate_in_to"] / info["rate_not_to"]
            elif info.get("rate_in_to", 0) > 0:
                to_mult = info["rate_in_to"] / max(global_rate, 0.01)
            else:
                to_mult = 1.0
            to_capped = max(LIFT_CAP_LOWER, min(LIFT_CAP_UPPER, to_mult))
            # Dampen reductive lifts toward 1.0
            if to_capped < 1.0:
                to_capped = 1.0 + PENALTY_DAMPEN_ALPHA * (to_capped - 1.0)
            rate_x_to[label] = {
                "to": round(to_capped, 3),
                "not_to": 1.0,
            }

    return {
        "boolean": boolean_lifts,
        "msg_type": msg_type_lifts,
        "recipient_bins": recipient_bins,
        "depth_bins": depth_bins,
        "rate_x_to": rate_x_to,
    }


# ---------------------------------------------------------------------------
# Recurring pattern detection
# ---------------------------------------------------------------------------

def _detect_recurring_patterns(events):
    """Detect recurring email patterns."""
    groups = defaultdict(list)
    for ev in events:
        norm = _normalize_subject(ev.get("subject"))
        if norm:
            key = f"{ev['sender_email']}|{norm}"
            groups[key].append(ev)

    patterns = {}
    for key, group in groups.items():
        if len(group) < MIN_CADENCE_OBSERVATIONS:
            continue

        timestamps = sorted(
            t for t in (_parse_time(e.get("received_time")) for e in group) if t
        )
        if len(timestamps) < MIN_CADENCE_OBSERVATIONS:
            continue

        gaps = [
            (timestamps[i + 1] - timestamps[i]).total_seconds() / 3600
            for i in range(len(timestamps) - 1)
        ]
        if not gaps:
            continue

        mean_gap = sum(gaps) / len(gaps)
        if mean_gap <= 0:
            continue

        std_gap = (sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)) ** 0.5
        cv = std_gap / mean_gap

        if cv < RECURRING_CV_THRESHOLD:
            responded = sum(1 for e in group if e.get("responded"))
            patterns[key] = round(responded / len(group), 4)

    return patterns


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_all_events(events, lift_factors, global_rate, recurring_patterns):
    """Score all events using derived lift factors."""
    sender_stats = _compute_sender_stats(events, global_rate)
    recip_bins_parsed = _parse_bins(lift_factors.get("recipient_bins", {}))
    predictions = []

    for ev in events:
        sender = ev["sender_email"]
        stats = sender_stats.get(sender, {})
        base_rate = stats.get("smoothed_rate", global_rate)

        # Check recurring pattern
        norm = _normalize_subject(ev.get("subject"))
        pattern_key = f"{sender}|{norm}"
        if pattern_key in recurring_patterns:
            base_rate = recurring_patterns[pattern_key]

        score = base_rate

        # Derive features without mutating event dict
        feat_vals = _derive_event_features(ev)

        # Boolean lifts
        for feat, lift in lift_factors.get("boolean", {}).items():
            if feat_vals.get(feat):
                score *= lift

        # Message type lift
        msg_type = ev.get("subject_type", "new")
        msg_lift = lift_factors.get("msg_type", {}).get(msg_type, 1.0)
        score *= msg_lift

        # Recipient bin multiplier
        recip = ev.get("total_recipients", 1)
        recip_mult = _lookup_bin(recip, recip_bins_parsed)
        score *= recip_mult

        # Rate × TO interaction
        sender_rate = stats.get("raw_rate", 0)
        rate_tier = _get_rate_tier(sender_rate)
        rate_to_data = lift_factors.get("rate_x_to", {}).get(rate_tier, {})
        if feat_vals["user_in_to"]:
            score *= rate_to_data.get("to", 1.0)
        else:
            score *= rate_to_data.get("not_to", 1.0)

        # CC-only penalty (dampened toward 1.0)
        if ev.get("user_position") != "TO":
            if sender_rate > 0.25:
                score *= 1.0 + PENALTY_DAMPEN_ALPHA * (0.90 - 1.0)  # 0.92
            else:
                score *= 1.0 + PENALTY_DAMPEN_ALPHA * (0.80 - 1.0)  # 0.84

        # Cold-start dampening (dampened toward 1.0)
        sender_count = stats.get("total", 0)
        if sender_count < 3:
            score *= 1.0 + PENALTY_DAMPEN_ALPHA * (0.70 - 1.0)  # 0.76

        # Combined penalty floor
        score = max(score, base_rate * COMBINED_PENALTY_FLOOR)

        # Cap and floor
        score = max(SCORE_FLOOR, min(SCORE_CAP, score))

        predictions.append({
            "score": round(score, 6),
            "responded": ev.get("responded", False),
            "sender": sender,
        })

    return predictions


def _compute_sender_stats(events, global_rate):
    """Compute per-sender statistics for scoring."""
    by_sender = defaultdict(lambda: {"total": 0, "responded": 0})
    for ev in events:
        by_sender[ev["sender_email"]]["total"] += 1
        if ev.get("responded"):
            by_sender[ev["sender_email"]]["responded"] += 1

    stats = {}
    for sender, data in by_sender.items():
        raw_rate = data["responded"] / data["total"] if data["total"] > 0 else 0
        smoothed = (
            (data["responded"] + BAYESIAN_PRIOR_WEIGHT * global_rate)
            / (data["total"] + BAYESIAN_PRIOR_WEIGHT)
        )
        stats[sender] = {
            "total": data["total"],
            "raw_rate": round(raw_rate, 4),
            "smoothed_rate": round(smoothed, 4),
        }
    return stats


def _get_rate_tier(rate):
    """Map sender rate to tier label."""
    if rate < 0.05:
        return "0-5%"
    if rate < 0.15:
        return "5-15%"
    if rate < 0.30:
        return "15-30%"
    return "30%+"


def _parse_bins(bin_dict):
    """Pre-parse a bin dictionary into a list of (lo, hi, lift) tuples."""
    parsed = []
    for bin_label, lift in bin_dict.items():
        try:
            parts = bin_label.strip("[]()").split(",")
            lo = float(parts[0].strip())
            hi = float(parts[1].strip().rstrip(")"))
            parsed.append((lo, hi, lift))
        except (ValueError, IndexError):
            continue
    return parsed


def _lookup_bin(value, parsed_bins):
    """Lookup a value in pre-parsed bin list of (lo, hi, lift) tuples."""
    if not parsed_bins:
        return 1.0
    for lo, hi, lift in parsed_bins:
        if lo <= value < hi:
            return lift
    # Default: return last bin's lift if value exceeds all bins
    return parsed_bins[-1][2]


# ---------------------------------------------------------------------------
# Isotonic regression (PAVA)
# ---------------------------------------------------------------------------

def _fit_isotonic(predictions):
    """Fit isotonic regression via PAVA."""
    if not predictions:
        return []

    paired = sorted(
        [(p["score"], 1 if p["responded"] else 0) for p in predictions],
        key=lambda x: x[0],
    )

    scores = [s for s, _ in paired]
    labels = [y for _, y in paired]
    n = len(scores)

    # PAVA: pool adjacent violators
    blocks = [[labels[i]] for i in range(n)]
    block_scores = [[scores[i]] for i in range(n)]

    changed = True
    while changed:
        changed = False
        merged_blocks = [blocks[0]]
        merged_scores = [block_scores[0]]
        for i in range(1, len(blocks)):
            prev_mean = mean(merged_blocks[-1])
            curr_mean = mean(blocks[i])
            if curr_mean < prev_mean:
                merged_blocks[-1].extend(blocks[i])
                merged_scores[-1].extend(block_scores[i])
                changed = True
            else:
                merged_blocks.append(blocks[i])
                merged_scores.append(block_scores[i])
        blocks = merged_blocks
        block_scores = merged_scores

    # Build breakpoints: [max_score_in_block, calibrated_value]
    breakpoints = []
    for blk, blk_scores in zip(blocks, block_scores):
        cal_val = mean(blk)
        max_score = max(blk_scores)
        breakpoints.append([round(max_score, 6), round(cal_val, 4)])

    return breakpoints


def _isotonic_transform(score, breakpoints):
    """Apply isotonic calibration."""
    if not breakpoints:
        return score
    for boundary, cal_val in breakpoints:
        if score <= boundary:
            return cal_val
    return breakpoints[-1][1]


# ---------------------------------------------------------------------------
# Triage thresholds
# ---------------------------------------------------------------------------

def _compute_triage(predictions, iso_breakpoints):
    """Compute triage gate thresholds."""
    # Find the calibrated probability below which actual response rate < 5%
    if not predictions or not iso_breakpoints:
        return {"hard_gate_threshold": 0.01, "soft_gate_threshold": 0.03}

    # Sort by calibrated score
    cal_predictions = []
    for p in predictions:
        cal = _isotonic_transform(p["score"], iso_breakpoints)
        cal_predictions.append({"cal": cal, "responded": p["responded"]})

    cal_predictions.sort(key=lambda x: x["cal"])

    # Find soft gate: highest calibrated score where cumulative rate < 5%
    soft_gate = 0.03  # default
    cumulative_responded = 0
    cumulative_total = 0
    for p in cal_predictions:
        cumulative_total += 1
        if p["responded"]:
            cumulative_responded += 1
        cumulative_rate = cumulative_responded / cumulative_total
        if cumulative_rate < 0.05:
            soft_gate = p["cal"]
        else:
            break

    # Cap soft_gate to prevent over-aggressive gating when the user's
    # overall response rate is low (which pushes the threshold too high).
    soft_gate = min(soft_gate, 0.10)

    return {
        "hard_gate_threshold": 0.01,
        "soft_gate_threshold": round(soft_gate, 4),
    }
