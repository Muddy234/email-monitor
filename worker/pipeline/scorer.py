"""Runtime email scorer — loads per-user scoring artifacts and scores emails.

Loads per-user scoring_parameters from DB (produced by model_trainer.py).
Applies the same multiplicative scoring pipeline at runtime, adapted for
live data shapes (signals dict, contact row, thread info dict).

Classes:
    UserScoringArtifacts: Loads per-user model artifacts from DB JSON.

Functions:
    score_email: Score a single email → (raw_score, calibrated_prob, tier, factors).
    check_triage_gate: Decide whether to gate (skip LLM) an email.
"""

import logging
import re

logger = logging.getLogger("worker.scorer")

# Subject normalization regex (mirrored from prediction_model.py)
_STRIP_PATTERNS = re.compile(
    r'\b(?:\d{1,2}[/\-]\d{1,2}[/\-]?\d{0,4}|'
    r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d+'
    r'|\d{4}|#\d+|v\d+)\b',
    re.IGNORECASE,
)

# Scoring constants
COMBINED_PENALTY_FLOOR = 0.30


def _normalize_subject(subject):
    """Normalize subject for recurring pattern matching."""
    if not subject:
        return ""
    s = re.sub(r'^(?:re|fw|fwd)\s*:\s*', '', subject, flags=re.IGNORECASE).strip()
    s = _STRIP_PATTERNS.sub('', s).strip()
    s = re.sub(r'\s+', ' ', s).lower()
    return s


class UserScoringArtifacts:
    """Loads per-user scoring parameters from DB JSON."""

    def __init__(self, params_json):
        """Initialize from a scoring_parameters JSON dict.

        Args:
            params_json: dict from scoring_parameters table (or DEFAULT_PARAMETERS).
        """
        data = params_json or {}

        meta = data.get("meta", {})
        self.version = "per-user"
        self.global_rate = meta.get("global_rate", 0.25)
        self.prior_weight = data.get("prior_weight", 3)

        lifts = data.get("lift_factors", {})
        self.boolean_lifts = lifts.get("boolean", {})
        self.msg_type_lifts = lifts.get("msg_type", {})

        # Convert dict-style bins to list-of-dicts for _lookup_bin compat
        self.recipient_multipliers = _dict_bins_to_list(
            lifts.get("recipient_bins", {})
        )
        self.depth_multipliers = _dict_bins_to_list(
            lifts.get("depth_bins", {})
        )

        # rate_x_to: model_trainer stores {tier: {to: X, not_to: Y}}
        # scorer expects {rate_bin: multiplier} for TO emails
        raw_rxt = lifts.get("rate_x_to", {})
        self.rate_x_to_interaction = _convert_rate_x_to(raw_rxt)

        self.iso_breakpoints = data.get("iso_breakpoints", [])

        triage = data.get("triage", {})
        self.hard_gate_threshold = triage.get("hard_gate_threshold", 0.01)
        self.soft_gate_threshold = triage.get("soft_gate_threshold", 0.03)

        # Recurring patterns: model_trainer stores {sender|subject: rate}
        self.recurring_patterns = {}
        for key, rate in data.get("recurring_patterns", {}).items():
            parts = key.split("|", 1)
            if len(parts) == 2:
                self.recurring_patterns[(parts[0], parts[1])] = rate

        thresholds = data.get("thresholds", {})
        self.score_cap = thresholds.get("score_cap", 0.95)
        self.score_floor = thresholds.get("score_floor", 0.01)

        logger.info(
            f"UserScoringArtifacts loaded: "
            f"global_rate={self.global_rate:.4f}, "
            f"{len(self.iso_breakpoints)} breakpoints, "
            f"{len(self.recurring_patterns)} recurring patterns"
        )


def _dict_bins_to_list(bin_dict):
    """Convert {"[1, 2)": 1.5, ...} to [{"bin": "[1, 2)", "lift": 1.5}, ...]."""
    return [{"bin": k, "lift": v} for k, v in bin_dict.items()]


def _convert_rate_x_to(raw):
    """Convert rate_x_to from model_trainer format to scorer format.

    model_trainer: {"0-5%": {"to": 1.2, "not_to": 1.0}, ...}
    scorer expects: {"low": multiplier, "med": multiplier, "high": multiplier}
    """
    # Map tier labels to scorer's rate bins
    tier_map = {"0-5%": "low", "5-15%": "low", "15-30%": "med", "30%+": "high"}
    result = {}
    for tier_label, data in raw.items():
        if isinstance(data, dict):
            rate_bin = tier_map.get(tier_label)
            if rate_bin and rate_bin not in result:
                result[rate_bin] = data.get("to", 1.0)
    return result


# ---------------------------------------------------------------------------
# Bin lookup helpers
# ---------------------------------------------------------------------------

def _parse_bin(bin_str):
    """Parse a bin string like '[1, 2)' → (1.0, 2.0)."""
    try:
        parts = bin_str.strip("[]()").split(",")
        return float(parts[0].strip()), float(parts[1].strip().rstrip(")"))
    except (ValueError, IndexError):
        return None, None


def _lookup_bin(value, bins):
    """Find the lift for a value in a list of {bin, lift} dicts."""
    for b in bins:
        lo, hi = _parse_bin(b["bin"])
        if lo is not None and lo <= value < hi:
            return b["lift"]
    # Fallback to last bin
    return bins[-1]["lift"] if bins else 1.0


def _get_rate_bin(smoothed_rate):
    """Classify smoothed rate into low/med/high."""
    if smoothed_rate is None:
        return "low"
    if smoothed_rate < 0.2:
        return "low"
    if smoothed_rate < 0.5:
        return "med"
    return "high"


def _isotonic_transform(score, breakpoints):
    """Piecewise-constant isotonic calibration lookup."""
    if not breakpoints:
        return score
    for boundary, cal_val in breakpoints:
        if score <= boundary:
            return cal_val
    return breakpoints[-1][1]


# ---------------------------------------------------------------------------
# Core scorer
# ---------------------------------------------------------------------------

def score_email(email_data, signals, contact, thread_info, artifacts):
    """Score a single email using model artifacts and runtime context.

    Args:
        email_data: dict from supabase_row_to_email_data().
        signals: dict from build_signals().
        contact: dict from contacts table (or None for unknown senders).
        thread_info: dict with thread context:
            - total_messages (int)
            - user_messages (int)
            - participation_rate (float or None)
            - user_initiated (bool)
            - hours_since_user_reply (float or None)
            - sender_events_count (int) — total emails from this sender
        artifacts: UserScoringArtifacts instance.

    Returns:
        tuple: (raw_score, calibrated_prob, confidence_tier, factors)
    """
    contact = contact or {}
    thread_info = thread_info or {}
    factors = []

    sender_email = (email_data.get("sender_email") or email_data.get("sender") or "").lower()
    subject = email_data.get("subject") or ""

    # -- Step 1: Base rate ---------------------------------------------------
    # Priority: recurring pattern → sender smoothed → global
    norm_subject = _normalize_subject(subject)
    recurring_key = (sender_email, norm_subject)
    is_recurring = recurring_key in artifacts.recurring_patterns

    sender_smoothed = None
    if is_recurring:
        base = artifacts.recurring_patterns[recurring_key]
        factors.append(f"recurring_pattern_rate={base:.3f}")
    elif contact.get("response_rate") is not None:
        # Apply Bayesian smoothing using contact's response_rate + email count
        raw_rate = contact["response_rate"]
        n_emails = contact.get("emails_per_month", 0) or 0
        if n_emails > 0:
            sender_smoothed = (
                (raw_rate * n_emails + artifacts.prior_weight * artifacts.global_rate)
                / (n_emails + artifacts.prior_weight)
            )
        else:
            sender_smoothed = artifacts.global_rate
        base = sender_smoothed
        factors.append(f"sender_rate={base:.3f}")
    else:
        base = artifacts.global_rate
        factors.append(f"global_rate={base:.3f}")

    score = base

    # -- Step 2: Rate × TO interaction ---------------------------------------
    user_in_to = signals.get("user_position") == "TO"
    user_sole_to = user_in_to and signals.get("total_recipients", 1) == 1

    rate_bin = _get_rate_bin(sender_smoothed)
    applied_to_lift = False

    if user_in_to:
        to_mult = artifacts.rate_x_to_interaction.get(rate_bin)
        if to_mult and to_mult != 0:
            score *= to_mult
            factors.append(f"rate_x_to[{rate_bin}]={to_mult:.3f}")
            applied_to_lift = True

    # -- Step 3: Boolean lifts -----------------------------------------------
    # Map signals to scorer features
    feature_map = {
        "mentions_user_name": signals.get("user_mentioned_by_name", False),
        "has_question": "?" in (email_data.get("body") or "")[:2000],
        "has_action_language": signals.get("intent_category") == "direct_request",
        "sender_is_internal": contact.get("contact_type") == "internal" or _is_internal_domain(sender_email),
        "thread_user_initiated": thread_info.get("user_initiated", False),
        "arrived_during_active_hours": True,  # runtime always within active window
        "arrived_on_active_day": True,
    }

    # Skip features handled elsewhere
    skip_features = {"user_in_to", "user_sole_to", "is_recurring"}
    for feat, lift in artifacts.boolean_lifts.items():
        if feat in skip_features:
            continue
        if feature_map.get(feat, False):
            score *= lift
            factors.append(f"{feat}={lift:.3f}")

    # -- Step 4: Recipient count multiplier ----------------------------------
    recip_count = signals.get("total_recipients", 1)
    recip_mult = _lookup_bin(recip_count, artifacts.recipient_multipliers)
    if abs(recip_mult - 1.0) > 0.01:
        score *= recip_mult
        factors.append(f"recip_mult={recip_mult:.3f}")

    # -- Step 5: Thread depth multiplier -------------------------------------
    depth = thread_info.get("total_messages", 1)
    depth_mult = _lookup_bin(depth, artifacts.depth_multipliers)
    if abs(depth_mult - 1.0) > 0.01:
        score *= depth_mult
        factors.append(f"depth_mult={depth_mult:.3f}")

    # -- Step 6: Message type lift -------------------------------------------
    msg_type = _subject_to_msg_type(subject)
    msg_lift = artifacts.msg_type_lifts.get(msg_type)
    if msg_lift and abs(msg_lift - 1.0) > 0.01:
        score *= msg_lift
        factors.append(f"msg_type[{msg_type}]={msg_lift:.3f}")

    # -- Step 7: Thread participation adjustment -----------------------------
    thread_part = thread_info.get("participation_rate")
    if thread_part is not None and depth > 2:
        if thread_part < 0.05:
            score *= 0.5
            factors.append("low_thread_participation=0.500")
        elif thread_part < 0.15 and depth <= 10:
            score *= 0.75
            factors.append("med_thread_participation=0.750")

    # -- Step 8: CC-only penalty ---------------------------------------------
    if not user_in_to and not applied_to_lift:
        if sender_smoothed is not None and sender_smoothed > 0.25:
            score *= 0.9
            factors.append("cc_only_high_sender=0.900")
        else:
            score *= 0.8
            factors.append("cc_only=0.800")

    # -- Step 9: Cold-start dampening ----------------------------------------
    sender_events = thread_info.get("sender_events_count")
    if sender_events is not None and sender_events < 3:
        score *= 0.7
        factors.append(f"cold_start({sender_events})=0.700")

    # -- Step 10: Thread recency lift ----------------------------------------
    recency_hrs = thread_info.get("hours_since_user_reply")
    if recency_hrs is not None:
        if recency_hrs < 24:
            score *= 1.5
            factors.append("thread_recency_24h=1.500")
        elif recency_hrs < 72:
            score *= 1.2
            factors.append("thread_recency_72h=1.200")

    # -- Step 11: Combined penalty floor -------------------------------------
    floor = base * COMBINED_PENALTY_FLOOR
    if score < floor:
        score = floor
        factors.append(f"penalty_floor={floor:.3f}")

    # -- Cap and floor -------------------------------------------------------
    raw_score = round(max(0.01, min(0.95, score)), 4)

    # -- Isotonic calibration ------------------------------------------------
    calibrated = round(_isotonic_transform(raw_score, artifacts.iso_breakpoints), 4)

    # -- Confidence tier -----------------------------------------------------
    if calibrated < 0.05:
        tier = "unlikely"
    elif calibrated < 0.15:
        tier = "possible"
    elif calibrated < 0.30:
        tier = "likely"
    else:
        tier = "strong"

    return raw_score, calibrated, tier, factors


# ---------------------------------------------------------------------------
# Triage gate
# ---------------------------------------------------------------------------

def check_triage_gate(calibrated_prob, thread_info, artifacts):
    """Decide whether to gate an email (skip LLM classification).

    Args:
        calibrated_prob: float from score_email().
        thread_info: dict with thread context.
        artifacts: UserScoringArtifacts instance.

    Returns:
        tuple: (should_gate: bool, reason: str or None)
    """
    thread_info = thread_info or {}

    # Hard gate: zero-participation in existing thread
    participation = thread_info.get("participation_rate")
    total_msgs = thread_info.get("total_messages", 1)
    if (participation is not None
            and participation < artifacts.hard_gate_threshold
            and total_msgs > 1):
        return True, "zero_participation_thread"

    # Soft gate: calibrated probability below threshold
    if calibrated_prob < artifacts.soft_gate_threshold:
        return True, f"low_probability({calibrated_prob:.3f})"

    return False, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_internal_domain(email_addr):
    """Check if email is from the internal domain."""
    return email_addr.endswith("@arete-collective.com")


def _subject_to_msg_type(subject):
    """Derive message type from subject prefix."""
    s = (subject or "").lower().strip()
    if s.startswith("fw:") or s.startswith("fwd:"):
        return "forward"
    if s.startswith("re:"):
        return "reply"
    return "new"
