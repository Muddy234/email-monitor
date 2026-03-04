# Supabase Integration — Extension → Supabase → Worker Pipeline

## Overview

Wire Supabase into the Chrome extension and Railway worker. The extension pushes emails to Supabase (authenticated via email/password auth), the worker processes them, and the extension listens for generated drafts via Realtime.

**Previous plan (COM→Extension migration) is complete.** This plan covers the Supabase cloud integration.

---

## Architecture

```
Extension (Chrome MV3)                     Railway Worker (existing)
  │                                           │
  ├─ content.js: capture MSAL token           │
  ├─ background.js:                           │
  │   1. Fetch emails via OWA service.svc     │
  │   2. Push to Supabase `emails` table      │
  │   3. Listen for `drafts` via Realtime     │
  │   4. Write drafts to Outlook via OWA      │
  ├─ popup.js: login form + status dashboard  │
  │                                           │
  └──── Supabase (Postgres + Auth + Realtime) ────┘
                                              │
                                  worker/main.py (exists):
                                    1. Poll for unprocessed emails
                                    2. Filter → Claude → drafts
                                    3. Write classifications + drafts
```

---

## Approach: Raw REST API (no bundler)

The extension has zero build tooling. Instead of bundling `@supabase/supabase-js`:
- **Auth**: REST calls to `/auth/v1/token`, `/auth/v1/signup`
- **PostgREST**: `fetch()` to `/rest/v1/{table}` with `apikey` + `Authorization` headers
- **Realtime**: Phoenix Channels over native WebSocket (~80 lines)
- Load via `importScripts()` in the service worker

---

## Implementation Order

### Step 1: `extension/supabase-config.js` (NEW)
- Embed `SUPABASE_URL` and `SUPABASE_ANON_KEY` as constants
- Read from `keys/supabase_url.txt` and `keys/supabase_anon_key.txt`
- Anon key is public/safe to embed in client code
- Add to `.gitignore`

### Step 2: `extension/supabase-auth.js` (NEW)
- `supabaseSignIn(email, password)` → POST `/auth/v1/token?grant_type=password`
- `supabaseSignUp(email, password)` → POST `/auth/v1/signup`
- `supabaseRefreshToken()` → POST `/auth/v1/token?grant_type=refresh_token`
- `getValidAccessToken()` → reads `chrome.storage.local`, refreshes if <5min to expiry
- `supabaseLogout()` → clears session from storage
- Session stored in `chrome.storage.local` (survives browser restarts)

### Step 3: Popup auth UI — `extension/popup.html` + `extension/popup.js` (MODIFY)
- **Login view**: email/password inputs, Login button, Sign Up link
- **Status view** (when authenticated): existing dashboard + Supabase status, user email, last sync, Sync Now button, Logout button
- Toggle between views based on `chrome.storage.local` session state

### Step 4: `extension/supabase-rest.js` (NEW)
- `supabaseRequest(path, options)` — base helper with auth headers
- `pushEmails(emails)` — POST to `/rest/v1/emails` with `Prefer: resolution=merge-duplicates` (upsert via `emails_user_ref_unique` constraint)
- `getPendingDrafts(userId)` — GET with join on emails table
- `updateDraftStatus(draftId, status, outlookDraftId)` — PATCH

### Step 5: Email sync in `extension/background.js` (MODIFY)
- New alarm `email-sync` fires every 5 minutes
- `syncEmailsToSupabase()`:
  1. Check both Exchange token AND Supabase session exist
  2. Call existing `handleGetEmails()` for FindItem
  3. Enrich each with `handleGetItem()` for body/recipients
  4. Get `user_id` from stored Supabase session
  5. Transform to Supabase row format and push via `pushEmails()`
- In-memory lock to prevent concurrent syncs
- Add `importScripts()` at top for new modules

### Step 6: `extension/manifest.json` (MODIFY)
- Add `https://*.supabase.co/*` to `host_permissions`

### Step 7: `extension/supabase-realtime.js` (NEW)
- Phoenix Channels WebSocket client
- Connect to `wss://{project}.supabase.co/realtime/v1/websocket`
- Join channel `realtime:public:drafts:user_id=eq.{userId}`
- Heartbeat every 30s
- On `INSERT` with `status=pending`:
  1. Fetch parent email's `email_ref` + sender info from Supabase
  2. Call existing `handleSaveDraft()` to create draft in Outlook
  3. Update draft status to `written` with `outlook_draft_id`
- Reconnect on close (alarm-driven, reuse existing pattern)

### Step 8: Background.js startup integration (MODIFY)
- Add `importScripts("supabase-config.js", "supabase-auth.js", "supabase-rest.js", "supabase-realtime.js")`
- On startup: restore Supabase session, start sync alarm, connect Realtime if authenticated
- Extend alarm handler for `email-sync` alarm
- Extend `getStatus` message to include Supabase state for popup

### Step 9: `.gitignore` (NEW)
- Add `keys/`, `extension/supabase-config.js`, `.env`

### Step 10: Railway deployment
- No code changes (worker already exists)
- Set env vars in Railway dashboard:
  - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
  - `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`
  - `POLL_INTERVAL_SECONDS`, `BATCH_SIZE`
  - `DRAFT_USER_NAME`, `DRAFT_USER_TITLE`

---

## Key Files

| File | Action | Purpose |
|------|--------|---------|
| `extension/supabase-config.js` | NEW | URL + anon key constants |
| `extension/supabase-auth.js` | NEW | Auth REST helpers + session management |
| `extension/supabase-rest.js` | NEW | PostgREST wrapper (push emails, update drafts) |
| `extension/supabase-realtime.js` | NEW | Phoenix Channels client for draft listener |
| `extension/background.js` | MODIFY | importScripts, sync alarm, Realtime startup |
| `extension/popup.html` | MODIFY | Login form + Supabase status section |
| `extension/popup.js` | MODIFY | Auth flow + status rendering |
| `extension/manifest.json` | MODIFY | Add `*.supabase.co` host permission |
| `.gitignore` | NEW | Protect keys + generated config |

---

## RLS Note

The `emails` table needs an `UPDATE` policy for the extension to handle upserts:

```sql
create policy "Users can update own emails"
    on public.emails for update using (auth.uid() = user_id);
```

The `drafts` table also needs the extension to read + update (to mark as `written`). Both policies already exist in the schema.

The `classifications` and `pipeline_runs` tables are worker-only (service role bypasses RLS).

---

## Verification

1. Create account via popup Sign Up → check profiles table auto-created
2. Login → verify session persists across popup close/reopen
3. Wait for sync alarm → verify emails appear in Supabase `emails` table
4. Click Sync Now → immediate push
5. Manually insert a draft row in Supabase → verify extension picks it up via Realtime and creates Outlook draft
6. Deploy worker to Railway → verify it claims unprocessed emails and writes classifications + drafts
7. End-to-end: email arrives in Outlook → extension syncs to Supabase → worker analyzes → draft appears in Outlook Drafts
