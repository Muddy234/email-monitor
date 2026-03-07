"""Probe MAPI headers on a sample of Outlook emails.

Checks availability of MIME threading headers (Message-ID, In-Reply-To,
References) alongside Exchange-native threading properties (ConversationID,
ConversationIndex) across internal and external senders.
"""

import win32com.client
from datetime import datetime, timedelta

# MAPI property tags for MIME headers
PR_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_IN_REPLY_TO = "http://schemas.microsoft.com/mapi/proptag/0x1042001F"
PR_REFERENCES = "http://schemas.microsoft.com/mapi/proptag/0x1039001F"

OL_MAIL_ITEM = 43


def probe_headers():
    outlook = win32com.client.Dispatch("Outlook.Application")
    ns = outlook.GetNamespace("MAPI")
    inbox = ns.GetDefaultFolder(6)  # Inbox
    sent = ns.GetDefaultFolder(5)  # Sent Items

    cutoff = (datetime.now() - timedelta(days=30)).strftime("%m/%d/%Y")

    # Sample from inbox (mix of internal/external)
    print("=== INBOX SAMPLES ===\n")
    _probe_folder(inbox, cutoff, limit=10)

    # Sample from sent items
    print("\n=== SENT ITEMS SAMPLES ===\n")
    _probe_folder(sent, cutoff, limit=5)

    # Sample from custom folders (Tyler, Becky if they exist)
    try:
        parent = inbox.Parent
        for folder in parent.Folders:
            fname = folder.Name.lower()
            if fname in ("tyler", "becky"):
                print(f"\n=== {folder.Name.upper()} FOLDER SAMPLES ===\n")
                _probe_folder(folder, cutoff, limit=3)
    except Exception as e:
        print(f"Could not scan custom folders: {e}")


def _probe_folder(folder, cutoff_str, limit=10):
    items = folder.Items
    items.Sort("[ReceivedTime]", True)  # newest first
    items = items.Restrict(f"[ReceivedTime] >= '{cutoff_str}'")

    count = 0
    for item in items:
        if count >= limit:
            break
        try:
            if item.Class != OL_MAIL_ITEM:
                continue
        except Exception:
            continue

        count += 1
        sender = _get_sender(item)
        domain = sender.split("@")[1] if "@" in sender else "unknown"
        is_internal = domain == "arete-collective.com"
        subject = (item.Subject or "")[:60]

        print(f"--- Email {count} ---")
        print(f"  From:     {sender}")
        print(f"  Type:     {'INTERNAL' if is_internal else 'EXTERNAL'}")
        print(f"  Subject:  {subject}")
        print(f"  Date:     {item.ReceivedTime}")

        # MIME headers via PropertyAccessor
        pa = item.PropertyAccessor
        msg_id = _safe_get(pa, PR_MESSAGE_ID)
        in_reply_to = _safe_get(pa, PR_IN_REPLY_TO)
        references = _safe_get(pa, PR_REFERENCES)

        print(f"  Message-ID:    {_summarize(msg_id)}")
        print(f"  In-Reply-To:   {_summarize(in_reply_to)}")
        print(f"  References:    {_summarize(references)}")

        # Exchange-native threading
        conv_id = getattr(item, "ConversationID", None)
        conv_index = _safe_get(pa, "http://schemas.microsoft.com/mapi/proptag/0x0071001F")
        # Fallback: try binary ConversationIndex
        if not conv_index:
            conv_index = _safe_get_binary(pa, "http://schemas.microsoft.com/mapi/proptag/0x00710102")

        print(f"  ConversationID:    {_summarize(conv_id)}")
        print(f"  ConversationIndex: {_summarize(conv_index, max_len=40)}")

        # Also check ConversationTopic (normalized subject)
        conv_topic = getattr(item, "ConversationTopic", None)
        print(f"  ConversationTopic: {_summarize(conv_topic)}")
        print()


def _get_sender(item):
    try:
        addr = (item.SenderEmailAddress or "").lower()
        if "@" in addr and "/o=" not in addr:
            return addr
        try:
            PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
            return item.PropertyAccessor.GetProperty(PR_SMTP).lower()
        except Exception:
            pass
        try:
            eu = item.Sender.GetExchangeUser()
            if eu:
                return eu.PrimarySmtpAddress.lower()
        except Exception:
            pass
        return addr
    except Exception:
        return ""


def _safe_get(pa, prop_tag):
    try:
        val = pa.GetProperty(prop_tag)
        if val:
            return str(val).strip()
    except Exception:
        pass
    return None


def _safe_get_binary(pa, prop_tag):
    try:
        val = pa.GetProperty(prop_tag)
        if val:
            return val.hex() if isinstance(val, bytes) else str(val)[:80]
    except Exception:
        pass
    return None


def _summarize(val, max_len=70):
    if val is None:
        return "NOT PRESENT"
    val = str(val).strip()
    if not val:
        return "EMPTY"
    if len(val) > max_len:
        return val[:max_len] + "..."
    return val


if __name__ == "__main__":
    probe_headers()
