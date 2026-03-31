"""Insert prepared test emails into Supabase to trigger the drafting pipeline.

Usage:
    python insert_test_emails.py <user_id> <emails.json>

Environment variables required:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY

JSON format — array of objects:
[
  {
    "subject": "Q1 Tax Filing - Discrepancy",
    "sender_name": "Bobby Axelrod",
    "sender_email": "bobby.axelrod5522@gmail.com",
    "body": "Nate, while preparing your Q1...",
    "to_field": "nate.mcbride23@outlook.com",
    "folder": "Inbox",
    "importance": "Normal",
    "has_attachments": false,
    "cc_field": null,
    "conversation_id": null,
    "conversation_topic": null
  }
]

Only "subject", "sender_email", and "body" are required.
Defaults are applied for everything else.
"""

import json
import sys
import os
import uuid
from datetime import datetime, timezone

from supabase import create_client


DEFAULTS = {
    "sender_name": "",
    "to_field": "",
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
}

REQUIRED_FIELDS = {"subject", "sender_email", "body"}


def validate_email(email: dict, index: int) -> list[str]:
    missing = REQUIRED_FIELDS - email.keys()
    if missing:
        return [f"Email [{index}]: missing required fields: {', '.join(sorted(missing))}"]
    return []


def build_row(email: dict, user_id: str) -> dict:
    row = {**DEFAULTS, **email}
    row["user_id"] = user_id
    row["email_ref"] = f"test-{uuid.uuid4()}"
    row["sender"] = f"{row['sender_name']} <{row['sender_email']}>"
    row["received_time"] = datetime.now(timezone.utc).isoformat()
    row["status"] = "unprocessed"
    return row


def main():
    if len(sys.argv) != 3:
        print("Usage: python insert_test_emails.py <user_id> <emails.json>")
        sys.exit(1)

    user_id = sys.argv[1]
    json_path = sys.argv[2]

    # Validate UUID format
    try:
        uuid.UUID(user_id)
    except ValueError:
        print(f"Error: '{user_id}' is not a valid UUID")
        sys.exit(1)

    # Load emails
    with open(json_path, "r", encoding="utf-8") as f:
        emails = json.load(f)

    if not isinstance(emails, list):
        print("Error: JSON must be an array of email objects")
        sys.exit(1)

    # Validate all emails before inserting any
    errors = []
    for i, email in enumerate(emails):
        errors.extend(validate_email(email, i))
    if errors:
        print("Validation errors:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    # Connect
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    client = create_client(url, key)

    # Verify user exists
    profile = (
        client.table("profiles")
        .select("id, user_email_primary, onboarding_status")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    if not profile.data:
        print(f"Error: No profile found for user_id '{user_id}'")
        sys.exit(1)

    p = profile.data
    print(f"Target user: {p.get('user_email_primary', 'unknown')} "
          f"(onboarding: {p.get('onboarding_status', 'unknown')})")

    # Insert
    rows = [build_row(email, user_id) for email in emails]
    result = client.table("emails").insert(rows).execute()

    print(f"Inserted {len(result.data)} email(s) with status='unprocessed'")
    for row in result.data:
        print(f"  - [{row['id'][:8]}] {row['subject']}")

    print("\nThe worker will pick these up on its next polling cycle (~45s).")


if __name__ == "__main__":
    main()
