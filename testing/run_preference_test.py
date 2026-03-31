"""Run preference layer draft tests from the CLI.

Usage:
    railway run python testing/run_preference_test.py [config]

    config = 1  → Invest Heavy + Advance Heavy
    config = 2  → Conserve Light + Yield Light
    config = 3  → Invest Heavy + Yield Light (mixed)
    config = reset → Clear preference_profile to NULL
    (no args)   → Print current profile state + latest draft

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
from run_pipeline import supabase_row_to_email_data, build_config_from_profile
from pipeline.drafts import DraftGenerator
from pipeline.prompts import get_draft_prompt_template


USER_ID = "f0fe5970-dbe7-4ed2-b263-6431ba590111"
TEST_SUBJECT = "Q1 Tax Filing - Discrepancy in Revenue Recognition"

# --- Style B (Professional) + Behavior 1 (High Authority) --- constant across configs
STYLE_GUIDE = """- Tone: formal, direct, business-like
- Pleasantries: minimal — brief greeting, no small talk
- Greeting pattern: "[First Name]," or "Good morning/afternoon,"
- Sign-off pattern: "Best," or "Regards,"
- Sentence structure: short declarative sentences, no filler
- Formality: high — no contractions, no exclamation marks, title + last name for new contacts
- Response length: concise — key points only, 40-80 words typical
- Verbal habits: "Understood.", "Will do.", "Please advise.", "Confirmed."
- Punctuation: periods only, bullet points for multiple items"""

BEHAVIORAL_PROFILE = """- Decision disposition: decides — makes clear decisions, gives definitive answers
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: specific_next_step — commits to concrete actions with detail ("I will send the revised contract by Thursday")
- Scope behavior: expands_scope — proactively raises related issues or next steps the sender has not mentioned
- IF someone asks for approval → THEN grant or deny with reasoning
- IF someone presents options → THEN pick one decisively and explain why
- IF a problem is raised → THEN propose a solution and assign next steps
- IF a deadline is mentioned → THEN confirm or counter-propose with a specific date"""

# --- Three preference configurations ---
CONFIGS = {
    "1": {
        "label": "Invest Heavy + Advance Heavy",
        "preference_profile": {
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
        },
    },
    "2": {
        "label": "Conserve Light + Yield Light",
        "preference_profile": {
            "investment_orientation": {
                "category": "conserve_light",
                "description": (
                    "This user defaults to the status quo on most decisions. They "
                    "accept renewals without shopping, skip optional protections, and "
                    "take partial fixes rather than investing in full remedies. When a "
                    "problem is urgent or the cost of inaction is obvious, they will "
                    "invest — but their default is conservation. Their reasoning is "
                    "effort-driven: they weigh the hassle of acting against the cost of "
                    "not acting, and the hassle usually wins unless stakes are clearly high."
                ),
                "confidence": "high",
                "supporting_decisions": 18,
            },
            "positional_stance": {
                "category": "yield_light",
                "description": (
                    "This user accommodates by default. They follow expert guidance "
                    "without extensive independent evaluation, concede without demanding "
                    "reciprocity, and prefer collaborative resolution over "
                    "confrontation. On high-stakes matters where the cost of yielding "
                    "is clear and obvious, they will hold ground — but their default is "
                    "to go along with the recommended path."
                ),
                "confidence": "high",
                "supporting_decisions": 12,
            },
        },
    },
    "3": {
        "label": "Invest Heavy + Yield Light (mixed)",
        "preference_profile": {
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
                "category": "yield_light",
                "description": (
                    "This user accommodates by default. They follow expert guidance "
                    "without extensive independent evaluation, concede without demanding "
                    "reciprocity, and prefer collaborative resolution over "
                    "confrontation. On high-stakes matters where the cost of yielding "
                    "is clear and obvious, they will hold ground — but their default is "
                    "to go along with the recommended path."
                ),
                "confidence": "high",
                "supporting_decisions": 12,
            },
        },
    },
}

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


def set_config(db, config_num):
    """Update the test user's profile with the selected preference config."""
    cfg = CONFIGS[config_num]
    logger.info(f"Setting config {config_num}: {cfg['label']}")

    db.client.table("profiles").update({
        "writing_style_guide": STYLE_GUIDE,
        "behavioral_profile": BEHAVIORAL_PROFILE,
        "preference_profile": json.dumps(cfg["preference_profile"]),
        "preference_profiled_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", USER_ID).execute()

    logger.info("Profile updated")


def insert_test_email(db):
    """Insert a fresh copy of the test email. Returns the new email_id."""
    row = {
        "user_id": USER_ID,
        "email_ref": f"test-pref-{uuid.uuid4()}",
        "subject": TEST_SUBJECT,
        "sender": "Bobby Axelrod <bobby.axelrod5522@gmail.com>",
        "sender_name": "Bobby Axelrod",
        "sender_email": "bobby.axelrod5522@gmail.com",
        "body": TEST_EMAIL_BODY,
        "to_field": "nate.mcbride23@outlook.com",
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


def generate_draft(db, email_id):
    """Generate a draft for the test email using the current profile."""
    # Fetch email row
    row = (
        db.client.table("emails")
        .select("*")
        .eq("id", email_id)
        .single()
        .execute()
    ).data

    # Fetch profile
    profile = db.fetch_user_config(USER_ID)
    config = build_config_from_profile(profile)
    draft_gen = DraftGenerator(config, system_prompt_template=get_draft_prompt_template())

    # Build email_data
    ed = supabase_row_to_email_data(row)
    ed["_db_id"] = email_id

    # Build action_context (mirrors backfill_drafts.py)
    action_context = {
        "reason": "Revenue recognition discrepancy requiring decision on deferral and tax timing",
        "action": "Revenue recognition discrepancy requiring decision on deferral and tax timing",
        "context": "Revenue recognition discrepancy requiring decision on deferral and tax timing",
        "user_aliases": [a.lower() for a in (profile.get("user_email_aliases") or [])],
    }

    style_guide = profile.get("writing_style_guide") or ""
    if style_guide:
        action_context["style_guide"] = style_guide

    behavioral_profile = profile.get("behavioral_profile") or ""
    if behavioral_profile:
        action_context["behavioral_profile"] = behavioral_profile

    preference_profile = profile.get("preference_profile")
    if preference_profile:
        action_context["preference_profile"] = preference_profile

    # Generate
    logger.info("Generating draft...")
    cleaned, usage, thinking = draft_gen.generate_draft(ed, action_context)

    if cleaned:
        # Store draft
        db.insert_draft(email_id, USER_ID, cleaned)
        if usage:
            db.record_token_usage(USER_ID, "sonnet", "draft", usage)
        return cleaned, usage, thinking

    logger.error("Draft generation failed")
    return None, {}, None


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
        print("Usage: python testing/run_preference_test.py [1|2|3|reset]")
        sys.exit(1)

    db = SupabaseWorkerClient()
    arg = sys.argv[1] if len(sys.argv) == 2 else None

    if arg is None:
        show_status(db)
        return

    if arg == "reset":
        reset_preference(db)
        return

    if arg not in CONFIGS:
        print(f"Unknown config '{arg}'. Use 1, 2, 3, or reset.")
        sys.exit(1)

    # Run the test
    cfg = CONFIGS[arg]
    print(f"\n{'='*60}")
    print(f"  Config {arg}: {cfg['label']}")
    print(f"{'='*60}\n")

    set_config(db, arg)
    email_id = insert_test_email(db)
    cleaned, usage, thinking = generate_draft(db, email_id)

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
    else:
        print("\nDraft generation failed. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
