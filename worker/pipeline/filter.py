"""Rule-based email classifier."""


class EmailFilter:
    """Rule-based email classifier: important, skip, or maybe."""

    def __init__(self, config):
        self.blacklist_senders = [s.lower() for s in config.get("filter_blacklist_senders", [])]
        self.blacklist_subjects = [s.lower() for s in config.get("filter_blacklist_subject_patterns", [])]
        self.whitelist_senders = [s.lower() for s in config.get("filter_whitelist_senders", [])]
        self.whitelist_domains = [d.lower() for d in config.get("filter_whitelist_domains", [])]
        self.project_keywords = [k.lower() for k in config.get("filter_project_keywords", [])]
        self.auto_important = [p.lower() for p in config.get("filter_auto_important_patterns", ["/o=exchangelabs/"])]
        self.direct_recipient = config.get("filter_direct_recipient", "").lower()

    def classify(self, email_data):
        """Classify an email as 'important', 'skip', or 'maybe'."""
        sender = (email_data.get("sender", "") or "").lower()
        sender_name = (email_data.get("sender_name", "") or "").lower()
        subject = (email_data.get("subject", "") or "").lower()
        body_preview = (email_data.get("body", "") or "")[:500].lower()

        # --- BLACKLIST CHECK (fast reject) ---
        for bl_sender in self.blacklist_senders:
            if bl_sender in sender:
                return "skip"

        for bl_pattern in self.blacklist_subjects:
            if bl_pattern in subject:
                return "skip"

        # --- WHITELIST SENDER CHECK (fast accept) ---
        for wl_sender in self.whitelist_senders:
            if wl_sender in sender or wl_sender in sender_name:
                return "important"

        for wl_domain in self.whitelist_domains:
            if wl_domain in sender:
                return "important"

        # --- PROJECT KEYWORD CHECK (subject + body preview) ---
        combined_text = subject + " " + body_preview
        for keyword in self.project_keywords:
            if keyword in combined_text:
                return "important"

        # --- AUTO-IMPORTANT PATTERNS CHECK ---
        for pattern in self.auto_important:
            if pattern in sender:
                return "important"

        # --- DEFAULT ---
        return "maybe"
