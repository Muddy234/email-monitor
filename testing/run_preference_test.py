"""Run full pipeline tests from the CLI.

RUN FROM PROJECT ROOT:
    railway run python testing/run_preference_test.py [run|status|reset]

    run     → Set profile, insert test email, run full pipeline (default)
    status  → Print current profile state + latest draft
    reset   → Clear preference_profile to NULL

Edit the TEST CONFIGURATION section below to change email, style guide,
behavioral profile, or preference profile before running.

Runs the full pipeline: filter → signal extraction (Haiku) → classification
→ draft generation (Sonnet). Same codepath as the production worker, just
executed synchronously for a single test email.

Environment variables required:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ANTHROPIC_API_KEY
"""

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("pref_test")

# Add worker dir to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker"))

from supabase_client import SupabaseWorkerClient
from run_pipeline import (
    supabase_row_to_email_data, build_config_from_profile,
    filter_emails, build_signals,
)
from pipeline.drafts import DraftGenerator
from pipeline.prompts import get_draft_prompt_template
from pipeline.signal_extractor import extract_signals, parse_signal_response
from pipeline.pre_process import pre_process_email, resolve_sender_tier, compute_thread_meta


# =============================================================================
# TEST CONFIGURATION — Edit everything in this section
# =============================================================================

USER_ID = "f0fe5970-dbe7-4ed2-b263-6431ba590111"

# --- Test email metadata ---
TEST_SUBJECT = "Q1 Tax Filing - Discrepancy in Revenue Recognition"
TEST_SENDER = "Bobby Axelrod <bobby.axelrod5522@gmail.com>"
TEST_SENDER_NAME = "Bobby Axelrod"
TEST_SENDER_EMAIL = "bobby.axelrod5522@gmail.com"
TEST_TO = "nate.mcbride23@outlook.com"

# --- Style guide ---
STYLE_GUIDE = """- Tone: formal, direct, business-like
- Pleasantries: minimal — brief greeting, no small talk
- Greeting pattern: "[First Name]," or "Good morning/afternoon,"
- Sign-off pattern: "Best," or "Regards,"
- Sentence structure: short declarative sentences, no filler
- Formality: high — no contractions, no exclamation marks, title + last name for new contacts
- Response length: concise — key points only, 40-80 words typical
- Verbal habits: "Understood.", "Will do.", "Please advise.", "Confirmed."
- Punctuation: periods only, bullet points for multiple items"""

# --- Behavioral profile ---
BEHAVIORAL_PROFILE = """- Decision disposition: decides — makes clear decisions, gives definitive answers
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: specific_next_step — commits to concrete actions with detail ("I will send the revised contract by Thursday")
- Scope behavior: expands_scope — proactively raises related issues or next steps the sender has not mentioned
- IF someone asks for approval → THEN grant or deny with reasoning
- IF someone presents options → THEN pick one decisively and explain why
- IF a problem is raised → THEN propose a solution and assign next steps
- IF a deadline is mentioned → THEN confirm or counter-propose with a specific date"""

# --- Preference profile ---
PREFERENCE_PROFILE = {
    "investment_orientation": {
        "category": "invest_heavy",
        "description": (
            "This user invests by default across all decision types, including "
            "low-stakes items where most people would accept good-enough. They "
            "shop alternatives rather than accepting renewals, close gaps "
            "proactively, fix problems fully rather than patching, investigate "
            "root causes, and act preemptively. Their reasoning is "
            "action-oriented — when they see a gap between current state and "
            "better state, they move to close it without waiting for the "
            "problem to force their hand."
        ),
        "confidence": "high",
        "supporting_decisions": 22,
    },
    "positional_stance": {
        "category": "advance_heavy",
        "description": (
            "This user pushes by default. They negotiate concessions, demand "
            "reciprocity when yielding ground, pressure-test expert "
            "recommendations rather than accepting them at face value, and "
            "exploit situational leverage. They challenge the easy path when a "
            "harder path offers more control over outcomes, even on lower-stakes "
            "interactions where most people would accommodate."
        ),
        "confidence": "high",
        "supporting_decisions": 16,
    },
}

# --- Test email body ---
TEST_EMAIL_BODY = (
    "Nate,\n\n"
    "While preparing your Q1 estimated tax filing, I found a discrepancy in "
    "revenue recognition. You have $142,000 in invoices marked as revenue in "
    "Q1, but $38,000 of that appears to be for services not yet delivered "
    "(contracts signed but work starts in Q2).\n\n"
    "Under accrual accounting, we should probably defer that $38,000 to Q2, "
    "which would reduce your Q1 estimated tax payment by roughly $9,500. "
    "However, if your cash flow situation favors paying more now to avoid a "
    "larger Q2 hit, we could keep it as-is.\n\n"
    "I need you to confirm: (1) whether those contracts have any deliverables "
    "completed in Q1 that would justify partial recognition, (2) your "
    "preference on timing of the tax payment, and (3) whether you want me to "
    "adjust the books now or wait until we have the full Q2 picture.\n\n"
    "Filing deadline for the estimate is April 15th, so I need a decision by "
    "April 10th.\n\n"
    "Bobby Axelrod, CPA\n"
    "Axelrod Advisory Services"
)

# =============================================================================
# END OF TEST CONFIGURATION
# =============================================================================


def set_profile(db):
    """Update the test user's profile with the configured values."""
    logger.info("Setting profile...")

    db.client.table("profiles").update({
        "writing_style_guide": STYLE_GUIDE,
        "behavioral_profile": BEHAVIORAL_PROFILE,
        "preference_profile": json.dumps(PREFERENCE_PROFILE),
        "preference_profiled_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", USER_ID).execute()

    logger.info("Profile updated")


def insert_test_email(db):
    """Insert a fresh copy of the test email. Returns the new email_id."""
    row = {
        "user_id": USER_ID,
        "email_ref": f"test-pref-{uuid.uuid4()}",
        "subject": TEST_SUBJECT,
        "sender": TEST_SENDER,
        "sender_name": TEST_SENDER_NAME,
        "sender_email": TEST_SENDER_EMAIL,
        "body": TEST_EMAIL_BODY,
        "to_field": TEST_TO,
        "folder": "Inbox",
        "importance": "Normal",
        "has_attachments": False,
        "attachment_names": [],
        "cc_field": None,
        "conversation_id": None,
        "conversation_topic": None,
        "flag_status": "NotFlagged",
        "is_read": True,
        "recipients": [],
        "received_time": datetime.now(timezone.utc).isoformat(),
        "status": "unprocessed",
    }
    result = db.client.table("emails").insert(row).execute()
    email_id = result.data[0]["id"]
    logger.info(f"Inserted test email: {email_id[:8]}")
    return email_id


def run_full_pipeline(db, email_id):
    """Run the full pipeline for a single test email.

    Same codepath as the production worker, executed synchronously:
    1. Filter (heuristic skip check)
    2. Signal extraction (Haiku)
    3. Classification (write to DB)
    4. Draft generation (Sonnet) — only if signals say draft=true
    """
    # Fetch email row
    row = (
        db.client.table("emails")
        .select("*")
        .eq("id", email_id)
        .single()
        .execute()
    ).data

    # Fetch profile + config
    profile = db.fetch_user_config(USER_ID)
    config = build_config_from_profile(profile)
    user_aliases = [a.lower() for a in (profile.get("user_email_aliases") or [])]
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    # ── Stage 1: Filter ──────────────────────────────────────────
    logger.info("Stage 1: Filtering...")
    filtered = filter_emails(db, [row], USER_ID, config)

    if not filtered:
        logger.info("Email was SKIPPED by filter. No further processing.")
        db.update_email_status(email_id, "processed")
        return None, {}, None, None

    ed = filtered[0]
    logger.info("Email passed filter")

    # ── Stage 2: Context fetch ───────────────────────────────────
    logger.info("Stage 2: Fetching context...")
    sender_email = (ed.get("sender_email") or ed.get("sender") or "").lower()
    conv_id = ed.get("conversation_id")

    # Fetch contact info
    contact = None
    try:
        contact_result = (
            db.client.table("contacts")
            .select("*")
            .eq("user_id", USER_ID)
            .eq("email", sender_email)
            .limit(1)
            .execute()
        )
        if contact_result.data:
            contact = contact_result.data[0]
    except Exception as e:
        logger.warning(f"Contact fetch failed: {e}")

    # Fetch thread emails if conversation exists
    thread_emails = []
    if conv_id:
        try:
            thread_result = (
                db.client.table("emails")
                .select("id, sender, sender_name, sender_email, body, received_time")
                .eq("conversation_id", conv_id)
                .neq("id", email_id)
                .order("received_time", desc=True)
                .limit(5)
                .execute()
            )
            thread_emails = thread_result.data or []
        except Exception as e:
            logger.warning(f"Thread fetch failed: {e}")

    # Resolve sender tier
    user_domain = None
    for alias in user_aliases:
        if "@" in alias:
            user_domain = alias.split("@")[1]
            break
    domain_tiers = {}
    try:
        dt_result = (
            db.client.table("domain_tiers")
            .select("domain, tier")
            .eq("user_id", USER_ID)
            .execute()
        )
        domain_tiers = {r["domain"]: r["tier"] for r in (dt_result.data or [])}
    except Exception:
        pass

    sender_tier = resolve_sender_tier(
        sender_email, contact, user_domain, domain_tiers
    )

    # Thread metadata
    thread_row = None
    if conv_id:
        try:
            tr_result = (
                db.client.table("threads")
                .select("*")
                .eq("conversation_id", conv_id)
                .eq("user_id", USER_ID)
                .limit(1)
                .execute()
            )
            if tr_result.data:
                thread_row = tr_result.data[0]
        except Exception:
            pass

    thread_depth, has_unanswered = compute_thread_meta(
        thread_row, sender_email, user_aliases,
        thread_emails=thread_emails,
    )

    # Pre-process body
    prior_bodies = [te["body"] for te in thread_emails if te.get("body")]
    clean_body = pre_process_email(ed, prior_bodies=prior_bodies)

    # User identity
    user_name = config.get("draft_user_name") or ""
    user_email_primary = user_aliases[0] if user_aliases else ""
    user_position = "UNKNOWN"
    to_raw = (ed.get("to_field") or "").lower()
    cc_raw = (ed.get("cc_field") or "").lower()
    for alias in user_aliases:
        if alias in to_raw:
            user_position = "TO"
            break
        if alias in cc_raw:
            user_position = "CC"
            break

    # Feedback hint
    from pipeline.signal_extractor import build_feedback_hint
    feedback_map = db.fetch_feedback_summary(USER_ID, [sender_email])
    feedback_hint = build_feedback_hint(feedback_map.get(sender_email))

    logger.info(
        f"  Context: sender_tier={sender_tier}, thread_depth={thread_depth}, "
        f"user_position={user_position}, contact={'yes' if contact else 'no'}"
    )

    # ── Stage 3: Signal extraction (Haiku) ───────────────────────
    logger.info("Stage 3: Extracting signals (Haiku)...")
    signals, signal_usage = extract_signals(
        email_body=clean_body,
        subject=ed.get("subject", ""),
        sender_name=ed.get("sender_name") or sender_email.split("@")[0],
        sender_email=sender_email,
        sender_tier=sender_tier,
        thread_depth=thread_depth,
        has_unanswered=has_unanswered,
        user_name=user_name,
        user_email=user_email_primary,
        user_position=user_position,
        to_field=ed.get("to_field") or "",
        cc_field=ed.get("cc_field") or "",
        contact_type=contact.get("contact_type", "") if contact else "",
        significance=contact.get("relationship_significance", "") if contact else "",
        api_key=api_key,
        feedback_hint=feedback_hint,
    )

    if signal_usage:
        db.record_token_usage(USER_ID, "haiku", "signals", signal_usage)

    print(f"\n--- Signals ---")
    print(f"  draft:  {signals.get('draft')}")
    print(f"  pri:    {signals.get('pri')}")
    print(f"  reason: {signals.get('reason')}")
    print(f"  mc={signals.get('mc')} ar={signals.get('ar')} "
          f"ub={signals.get('ub')} dl={signals.get('dl')} rt={signals.get('rt')}")

    # ── Stage 4: Classification (write to DB) ────────────────────
    logger.info("Stage 4: Writing classification...")
    classification = {
        "needs_response": signals.get("draft", False),
        "action": signals.get("reason", ""),
        "context": signals.get("reason", ""),
        "project": "",
        "priority": {"high": 2, "med": 1, "low": 0}.get(signals.get("pri"), 0),
    }
    db.insert_classification(email_id, USER_ID, classification)
    db.update_email_status(email_id, "processed")

    # ── Stage 5: Draft generation (Sonnet) ───────────────────────
    if not signals.get("draft"):
        logger.info("Signals say draft=false. No draft generated.")
        return None, {}, None, signals

    logger.info("Stage 5: Generating draft (Sonnet)...")
    draft_gen = DraftGenerator(config, system_prompt_template=get_draft_prompt_template())

    action_context = {
        "reason": signals.get("reason", ""),
        "action": signals.get("reason", ""),
        "context": signals.get("reason", ""),
        "user_aliases": user_aliases,
    }

    if thread_emails:
        action_context["thread_emails"] = thread_emails

    style_guide = profile.get("writing_style_guide") or ""
    if style_guide:
        action_context["style_guide"] = style_guide

    behavioral_profile = profile.get("behavioral_profile") or ""
    if behavioral_profile:
        action_context["behavioral_profile"] = behavioral_profile

    preference_profile = profile.get("preference_profile")
    if preference_profile:
        action_context["preference_profile"] = preference_profile

    if contact:
        ed["sender_contact"] = contact

    cleaned, usage, thinking = draft_gen.generate_draft(ed, action_context)

    if cleaned:
        db.insert_draft(email_id, USER_ID, cleaned)
        if usage:
            db.record_token_usage(USER_ID, "sonnet", "draft", usage)
        return cleaned, usage, thinking, signals

    logger.error("Draft generation failed")
    return None, {}, None, signals


def show_status(db):
    """Print current profile state and latest draft."""
    profile = db.fetch_user_config(USER_ID)

    pref = profile.get("preference_profile")
    io_cat = pref.get("investment_orientation", {}).get("category") if pref else None
    ps_cat = pref.get("positional_stance", {}).get("category") if pref else None

    print("\n--- Profile State ---")
    print(f"  Style guide:    {'yes' if profile.get('writing_style_guide') else 'no'}")
    print(f"  Behavioral:     {'yes' if profile.get('behavioral_profile') else 'no'}")
    print(f"  Preference:     {'yes' if pref else 'no'}")
    if pref:
        print(f"    Investment:   {io_cat}")
        print(f"    Positional:   {ps_cat}")
        print(f"    Profiled at:  {profile.get('preference_profiled_at', 'unknown')}")

    # Latest draft
    result = (
        db.client.table("drafts")
        .select("draft_body, created_at")
        .eq("user_id", USER_ID)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        d = result.data[0]
        print(f"\n--- Latest Draft ({d['created_at']}) ---")
        print(d["draft_body"])
    else:
        print("\n  No drafts found.")
    print()


def reset_preference(db):
    """Clear preference_profile to NULL."""
    db.client.table("profiles").update({
        "preference_profile": None,
        "preference_profiled_at": None,
    }).eq("id", USER_ID).execute()
    logger.info("Preference profile cleared")


def main():
    if len(sys.argv) > 2:
        print("Usage: python testing/run_preference_test.py [run|status|reset]")
        sys.exit(1)

    db = SupabaseWorkerClient()
    arg = sys.argv[1] if len(sys.argv) == 2 else "run"

    if arg == "status":
        show_status(db)
        return

    if arg == "reset":
        reset_preference(db)
        return

    if arg != "run":
        print(f"Unknown command '{arg}'. Use run, status, or reset.")
        sys.exit(1)

    # Run the test
    io_cat = PREFERENCE_PROFILE.get("investment_orientation", {}).get("category", "none")
    ps_cat = PREFERENCE_PROFILE.get("positional_stance", {}).get("category", "none")
    print(f"\n{'='*60}")
    print(f"  Preference: {io_cat} + {ps_cat}")
    print(f"{'='*60}\n")

    set_profile(db)
    email_id = insert_test_email(db)
    cleaned, usage, thinking, signals = run_full_pipeline(db, email_id)

    if cleaned:
        print(f"\n{'='*60}")
        print("  GENERATED DRAFT")
        print(f"{'='*60}\n")
        print(cleaned)
        print(f"\n{'='*60}")
        if usage:
            print(f"  Tokens — input: {usage.get('input_tokens', '?')}, "
                  f"output: {usage.get('output_tokens', '?')}")
        print(f"  Email ID: {email_id}")
        print(f"{'='*60}\n")
    elif signals:
        print(f"\nNo draft generated (draft={signals.get('draft')}). "
              "Check signals above.")
    else:
        print("\nEmail was skipped by filter. No processing occurred.")
        sys.exit(1)


if __name__ == "__main__":
    main()
