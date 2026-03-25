"""Rule-based email classifier."""


class EmailFilter:
    """Rule-based email classifier: important, skip, or maybe.

    classify() returns (classification, reason) tuples for debuggability.
    """

    def __init__(self, config):
        # Blacklist checks match against email address only (not display name).
        # This is intentional — display names aren't unique, so blacklisting
        # by name would produce false positives ("John" catching all Johns).
        self.blacklist_senders = [s.lower() for s in config.get("filter_blacklist_senders", [])]
        self.blacklist_subjects = [s.lower() for s in config.get("filter_blacklist_subject_patterns", [])]
        self.whitelist_senders = [s.lower() for s in config.get("filter_whitelist_senders", [])]
        self.whitelist_domains = [d.lower() for d in config.get("filter_whitelist_domains", [])]
        # Project keywords use substring matching against subject + first 500
        # chars of body. Multi-word phrases ("thomas ranch") are safe; short
        # single-word keywords risk false positives (e.g. "bank" in "bankrupt").
        self.project_keywords = [k.lower() for k in config.get("filter_project_keywords", [])]
        self.auto_important = [p.lower() for p in config.get("filter_auto_important_patterns", [])]

    def classify(self, email_data):
        """Classify an email as 'important', 'skip', or 'maybe'.

        Returns:
            tuple: (classification, reason) — e.g. ("skip", "blacklist_sender: noreply@")
        """
        sender = (email_data.get("sender", "") or "").lower()
        sender_name = (email_data.get("sender_name", "") or "").lower()
        subject = (email_data.get("subject", "") or "").lower()
        body_preview = (email_data.get("body", "") or "")[:500].lower()

        # --- EXCHANGE SYSTEM MESSAGE CHECK (infrastructure skip) ---
        # Matches Exchange internal routing headers (e.g. /o=exchangelabs/)
        # that appear in the sender field. These are system-generated messages,
        # not human-sent email.
        if "/o=exchangelabs/" in sender:
            return ("skip", "exchange_system_message")

        # --- BLACKLIST CHECK (fast reject) ---
        for bl_sender in self.blacklist_senders:
            if bl_sender in sender:
                return ("skip", f"blacklist_sender: {bl_sender}")

        for bl_pattern in self.blacklist_subjects:
            if bl_pattern in subject:
                return ("skip", f"blacklist_subject: {bl_pattern}")

        # --- WHITELIST SENDER CHECK (fast accept) ---
        for wl_sender in self.whitelist_senders:
            if wl_sender in sender or wl_sender in sender_name:
                return ("important", f"whitelist_sender: {wl_sender}")

        for wl_domain in self.whitelist_domains:
            if wl_domain in sender:
                return ("important", f"whitelist_domain: {wl_domain}")

        # --- PROJECT KEYWORD CHECK (subject + body preview) ---
        combined_text = subject + " " + body_preview
        for keyword in self.project_keywords:
            if keyword in combined_text:
                return ("important", f"project_keyword: {keyword}")

        # --- AUTO-IMPORTANT PATTERNS CHECK ---
        for pattern in self.auto_important:
            if pattern in sender:
                return ("important", f"auto_important: {pattern}")

        # --- DEFAULT ---
        return ("maybe", "no_rule_matched")
