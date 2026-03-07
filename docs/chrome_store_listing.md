# Chrome Web Store Listing — Clarion AI

## Extension Name
Clarion AI — AI Email Assistant

## Short Description (132 char max)
AI-powered email assistant that reads, prioritizes, and drafts replies for your Outlook inbox. Save hours every morning.

## Detailed Description
Clarion AI bridges your Outlook inbox with AI-powered analysis and draft generation.

**How it works:**
1. Install the extension and sign in with your Clarion AI account.
2. Open Outlook Web (outlook.live.com, outlook.office365.com, or outlook.cloud.microsoft).
3. The extension syncs your inbox every 5 minutes to the Clarion AI backend.
4. AI analyzes your emails for priority, intent, and required actions.
5. Draft replies appear in your Outlook Drafts folder — ready for your review.

**Features:**
- Automatic email sync from Outlook Web
- AI-powered email analysis and prioritization
- Smart draft reply generation
- Web dashboard for reviewing results
- Works with outlook.live.com, outlook.office365.com, and outlook.cloud.microsoft

**Privacy first:**
- Your Outlook session token never leaves your browser
- All data encrypted in transit (HTTPS/WSS)
- Row-level security ensures you only see your own data
- No data is sold or shared with third parties

## Category
Productivity

## Language
English

---

## Permission Justifications (required by Chrome Web Store)

### alarms
"Used to schedule periodic email sync every 5 minutes between Outlook and the Clarion AI backend."

### storage
"Used to persist user authentication session, Outlook token state, and last sync timestamp across browser restarts."

### Host permission: *://outlook.cloud.microsoft/*
"Required to access the Outlook Web API (service.svc) to read emails and save draft replies on the user's behalf."

### Host permission: *://outlook.live.com/*
"Required to access the Outlook Web API for personal Microsoft accounts (outlook.live.com)."

### Host permission: *://outlook.office365.com/*
"Required to access the Outlook Web API for Microsoft 365 business accounts."

### Host permission: *://outlook.office.com/*
"Required to access Outlook Web to capture the user's existing session token from localStorage."

### Host permission: https://*.supabase.co/*
"Required to communicate with the Clarion AI backend (Supabase) for authentication, email storage, and real-time draft delivery."

---

## Privacy Policy URL
https://clarion-ai.app/privacy.html

## Single Purpose Description
"This extension syncs emails from Outlook Web to the Clarion AI service for AI-powered analysis and draft reply generation."
