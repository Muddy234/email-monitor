-- Lock each user to a single Outlook email account.
-- First sync writes the email; subsequent syncs verify it matches.

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS connected_outlook_email text;
