# Implementation Plan: Supabase Bridge + Contact Intelligence Matrix

Two features need to be built to close the gap between the extension (which fetches emails from Outlook) and the Supabase-backed worker (which processes them). Additionally, the Contact Intelligence Matrix — already partially coded in the worker — needs a proper Supabase table and a build pipeline.

---

## Part A: Extension → Supabase Bridge

**Problem:** The extension fetches emails from Outlook via OWA but only relays them over a local WebSocket to `localhost:5000`. It has zero Supabase code — no auth, no REST calls, no Realtime listener. Emails never reach the `emails` table.

### A1. Supabase config — `extension/supabase-config.js` (NEW)
- Export `SUPABASE_URL` and `SUPABASE_ANON_KEY` as constants.
- Values read at build time from `keys/supabase_url.txt` and `keys/supabase_anon_key.txt` (anon key is safe to embed in client code).
- Add `keys/` and `extension/supabase-config.js` to `.gitignore`.

### A2. Auth module — `extension/supabase-auth.js` (NEW)
- `supabaseSignIn(email, password)` — POST to `/auth/v1/token?grant_type=password`.
- `supabaseSignUp(email, password)` — POST to `/auth/v1/signup`.
- `supabaseRefreshToken()` — POST to `/auth/v1/token?grant_type=refresh_token`.
- `getValidAccessToken()` — reads session from `chrome.storage.local`, auto-refreshes if <5 min to expiry.
- `supabaseLogout()` — clears stored session.
- Session (access_token, refresh_token, user, expires_at) persisted in `chrome.storage.local`.

### A3. REST module — `extension/supabase-rest.js` (NEW)
- `supabaseRequest(path, options)` — base fetch wrapper injecting `apikey` + `Authorization: Bearer <token>` headers.
- `pushEmails(emails)` — POST to `/rest/v1/emails` with `Prefer: resolution=merge-duplicates` (upserts on `emails_user_ref_unique`).
- `getPendingDrafts(userId)` — GET `/rest/v1/drafts?user_id=eq.{userId}&status=eq.pending`.
- `updateDraftStatus(draftId, status, outlookDraftId)` — PATCH `/rest/v1/drafts?id=eq.{draftId}`.

### A4. Realtime module — `extension/supabase-realtime.js` (NEW)
- Phoenix Channels WebSocket client (~80 lines, no library needed).
- Connect to `wss://{project}.supabase.co/realtime/v1/websocket`.
- Subscribe to `realtime:public:drafts:user_id=eq.{userId}`.
- On `INSERT` with `status=pending`:
  1. Fetch parent email's `email_ref` + sender from Supabase.
  2. Call existing `handleSaveDraft()` to create the draft in Outlook.
  3. PATCH draft status to `written` with the `outlook_draft_id`.
- 30s heartbeat, auto-reconnect on close (alarm-driven).

### A5. Popup auth UI — `extension/popup.html` + `popup.js` (MODIFY)
- Add a **login view**: email + password inputs, Login button, Sign Up link.
- Add a **status view** (post-auth): user email, Supabase connection status, last sync time, "Sync Now" button, Logout button.
- Toggle views based on session presence in `chrome.storage.local`.

### A6. Background.js integration (MODIFY)
- Add `importScripts("supabase-config.js", "supabase-auth.js", "supabase-rest.js", "supabase-realtime.js")` at top.
- New `email-sync` alarm (every 5 min):
  1. Verify both Exchange token AND Supabase session exist.
  2. Call existing `handleGetEmails()` for FindItem.
  3. Enrich each email with `handleGetItem()` for body/recipients.
  4. Transform to Supabase row format (map `email_ref`, `sender_email`, `body`, `recipients`, etc.).
  5. Push via `pushEmails()`.
- In-memory lock to prevent overlapping syncs.
- On startup: restore Supabase session, start sync alarm, connect Realtime if authenticated.
- Extend `getStatus` message response to include Supabase state for popup.

### A7. Manifest update — `extension/manifest.json` (MODIFY)
- Add `"https://*.supabase.co/*"` to `host_permissions`.

### A8. RLS policy fix — new migration `002_email_update_policy.sql`
- The `emails` table needs an UPDATE policy for upserts to work:
  ```sql
  create policy "Users can update own emails"
      on public.emails for update using (auth.uid() = user_id);
  ```

---

## Part B: Contact Intelligence Matrix in Supabase

**Problem:** The worker's `analyzer.py` and `drafts.py` already consume `sender_contact` and `recipient_contacts` fields, but there's no table to store contacts and no pipeline to build them. The strategy doc references SQLite, which doesn't exist in the Supabase architecture.

### B1. Contacts table — new migration `003_contacts_table.sql`

```sql
create table public.contacts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles on delete cascade,
    email text not null,
    name text,
    organization text,
    role text,
    expertise_areas text[] default '{}',
    contact_type text default 'unknown',
    -- e.g. internal, external_vendor, external_legal, external_lender
    emails_per_month integer default 0,
    response_rate numeric(5,2) default 0.0,
    common_co_recipients text[] default '{}',
    last_profiled_at timestamptz,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),

    constraint contacts_user_email_unique unique (user_id, email)
);

create index idx_contacts_user on public.contacts (user_id);
create index idx_contacts_email on public.contacts (user_id, email);

alter table public.contacts enable row level security;

create policy "Users can read own contacts"
    on public.contacts for select using (auth.uid() = user_id);
create policy "Users can update own contacts"
    on public.contacts for update using (auth.uid() = user_id);
create policy "Users can insert own contacts"
    on public.contacts for insert with check (auth.uid() = user_id);
create policy "Users can delete own contacts"
    on public.contacts for delete using (auth.uid() = user_id);
```

### B2. Contact profiler — `worker/pipeline/contact_profiler.py` (NEW)

Scheduled job (or on-demand) that builds/refreshes the contact matrix for a user:

1. **Scan** — Query the last 30 days of emails from `public.emails` for the user.
2. **Rank** — Count sender frequency, select top 25 contacts by volume.
3. **Aggregate** — For each top contact, collect:
   - All subject lines they appear on.
   - Email body snippets (first 200 chars) from their most recent 5 emails.
   - Co-recipient lists from their emails.
   - Sent Items where the user replied to them (for response rate).
4. **Profile** — Send aggregated data to Claude (Haiku for cost efficiency) in a single batch prompt:
   - "Given these email patterns, infer organization, role, expertise areas, contact type, and relationship to the user."
   - Returns structured JSON per contact.
5. **Store** — Upsert results into `public.contacts` via the Supabase service-role client.

### B3. Contact lookup in the worker pipeline — `worker/pipeline/analyzer.py` + `run_pipeline.py` (MODIFY)

Before analyzing an email batch:
1. Query `public.contacts` for the user's full contact directory.
2. For each email, look up `sender_email` and all `recipients[].address` against the directory.
3. Inject `sender_contact` and `recipient_contacts` into the email data dict.
4. The existing signal-building code in `analyzer.py:211-240` already handles the rest — no changes needed there.

### B4. Contact refresh trigger — `worker/main.py` (MODIFY)

Add a lightweight check at the start of each poll cycle:
- If the user's `contacts` table has `last_profiled_at` older than 30 days (or is empty), trigger the profiler before processing emails.
- This keeps the matrix fresh without a separate cron job.

### B5. Supabase client methods — `worker/supabase_client.py` (MODIFY)

Add methods to `SupabaseWorkerClient`:
- `get_contacts(user_id)` — SELECT all contacts for a user.
- `upsert_contacts(user_id, contacts_list)` — bulk upsert to `public.contacts`.
- `get_contact_staleness(user_id)` — check `last_profiled_at` for the user.

---

## Implementation Order

| Step | Task | Depends on |
|------|------|-----------|
| 1 | A8: Email UPDATE RLS policy migration | — |
| 2 | A1: `supabase-config.js` | — |
| 3 | A2: `supabase-auth.js` | A1 |
| 4 | A3: `supabase-rest.js` | A1, A2 |
| 5 | A7: Manifest host_permissions update | — |
| 6 | A5: Popup auth UI | A2 |
| 7 | A6: Background.js sync integration | A2, A3, A4 |
| 8 | A4: `supabase-realtime.js` | A1, A2 |
| 9 | B1: Contacts table migration | — |
| 10 | B5: Supabase client contact methods | B1 |
| 11 | B2: Contact profiler | B1, B5 |
| 12 | B3: Contact lookup in pipeline | B5 |
| 13 | B4: Contact refresh trigger | B2, B3 |

Steps 1, 2, 5, and 9 can be done in parallel. Steps 3–8 (Part A) and 10–13 (Part B) can proceed in parallel after their respective prerequisites.

---

## Verification Checklist

### Part A — Supabase Bridge
- [ ] Sign up via popup → `profiles` row auto-created
- [ ] Login → session persists across popup close/reopen
- [ ] Sync alarm fires → emails appear in Supabase `emails` table with status `unprocessed`
- [ ] Click "Sync Now" → immediate push
- [ ] Worker claims unprocessed emails → writes `classifications` + `drafts`
- [ ] Extension picks up new draft via Realtime → Outlook draft created
- [ ] End-to-end: flagged email in Outlook → extension syncs → worker analyzes → draft appears in Outlook Drafts

### Part B — Contact Matrix
- [ ] Contact profiler runs on empty contacts table → builds profiles for top 25 senders
- [ ] `contacts` table populated with name, org, role, contact_type, stats
- [ ] Analyzer signals include sender/recipient contact profiles
- [ ] Draft tone adjusts based on contact_type (e.g., formal for external_legal)
- [ ] Auto-refresh triggers when profiles are >30 days old
- [ ] Manual refresh available via dashboard (future)
