# Dashboard Rebuild — Static Frontend + Supabase

## Overview
Rebuild the dashboard as static HTML/JS/CSS in `web/`, querying Supabase directly via PostgREST. Same auth credentials as the extension. Deployable to Vercel with no build step.

## Scope
3 core pages + login:
- **Login** — email/password auth (same Supabase Auth as extension)
- **Dashboard** — metrics overview with date range filter
- **Emails** — grouped list + inline detail/draft editing
- **History** — pipeline run log table with expandable detail

## File Structure

```
web/
├── vercel.json               (routing config for multi-page static deploy)
├── app/
│   ├── login.html
│   ├── dashboard.html
│   ├── emails.html
│   └── history.html
├── css/
│   ├── landing.css          (unchanged)
│   └── app.css              (new — dashboard styles, reuses same design tokens)
├── js/
│   ├── landing.js           (unchanged)
│   ├── supabase-client.js   (Supabase init singleton)
│   ├── auth.js              (login/signup/logout/session guard + expiry handling)
│   ├── ui.js                (error banner, empty states, shared helpers)
│   ├── nav.js               (sidebar renderer)
│   └── pages/
│       ├── login.js
│       ├── dashboard.js
│       ├── emails.js
│       └── history.js
└── index.html               (unchanged)
```

## Technical Approach

- **No framework, no build step.** Vanilla JS with ES modules (`<script type="module">`)
- **Supabase JS v2** loaded via importmap from `esm.sh` CDN — no bundled file needed
- **Multi-page static** — each page is a separate HTML file, standard `<a href>` navigation
- **Auth** — `supabase.auth.signInWithPassword()`, session persisted in localStorage, auto-refresh
- **CSS** — new `app.css` with same design tokens as landing page (slate palette, Inter font, 12px radius). Prefix `em-` to avoid class collisions with `lp-` landing classes
- **Supabase embedded selects** for JOINs (emails → classifications, emails → drafts)

## Security & RLS Audit

The anon key is exposed client-side (standard for Supabase). Must verify all RLS is airtight.

**RLS policy checklist** (per 001_initial_schema.sql):
| Table | SELECT | INSERT | UPDATE | Dashboard needs | Status |
|---|---|---|---|---|---|
| `profiles` | ✅ uid=id | ✅ uid=id | ✅ uid=id | — | OK |
| `emails` | ✅ uid=user_id | ✅ uid=user_id | ❌ Missing | UPDATE (mark completed) | **Needs migration** |
| `classifications` | ✅ uid=user_id | — (worker only) | — | SELECT only | OK |
| `drafts` | ✅ uid=user_id | — (worker only) | ✅ uid=user_id (direct column, not join-based) | UPDATE (edit draft) | OK |
| `pipeline_runs` | ✅ uid=user_id | — (worker only) | — | SELECT only | OK |
| `conversations` | ✅ uid=user_id | ✅ uid=user_id | ✅ uid=user_id | — | OK |

**Only missing policy:** UPDATE on `emails` (for "Mark Completed").

**Hard rule:** `supabase-client.js` must only contain the anon key. Grep for service role key before shipping.

## Schema Changes Required

Combined migration `supabase/migrations/002_dashboard_policies.sql`:
```sql
-- Emails UPDATE policy (for "Mark Completed")
create policy "Users can update own emails"
  on public.emails for update using (auth.uid() = user_id);

-- Draft conflict protection flag
alter table public.drafts add column user_edited boolean default false;

-- Client-side error logging
create table public.error_logs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references public.profiles on delete cascade,
    page text,
    error text,
    stack text,
    created_at timestamptz default now()
);
alter table public.error_logs enable row level security;
create policy "Users can insert own errors"
    on public.error_logs for insert with check (auth.uid() = user_id);
```

## Architectural Decisions

### Live updates: No (v1)
Pages load data on mount. "Refresh to update" is acceptable for launch. Manual refresh button on dashboard and emails pages. Supabase Realtime subscriptions on `pipeline_runs` / `emails` can be added later without architectural changes since we're already importing the Supabase client.

### URL state: Yes
Sync filters, search, and pagination to URL params (`?page=2&q=acme&range=7d`). Read params on page load, update via `history.replaceState()` on change. Makes refresh safe, enables bookmarking filtered views. Low effort, high UX value.

### Draft editing: `<textarea>` with auto-resize, manual save
- **No contentEditable** — too inconsistent across browsers with paste/formatting. Plain `<textarea>` with CSS `resize: vertical` and JS auto-height on input.
- **Save is manual** — "Save" button with "Saved ✓" confirmation. Sets `user_edited = true` on the drafts row on first save.
- **No autosave for v1** — avoids dirty state tracking and conflict timing issues.

### Draft conflict protection
Worker could overwrite user edits on re-run. Solution:
- New column: `drafts.user_edited boolean default false`
- Dashboard sets `user_edited = true` on first save
- **Worker must check** `user_edited` before UPSERT — skip rows where `user_edited = true`
- This requires a small worker change (add `WHERE user_edited = false` or `ON CONFLICT ... DO UPDATE ... WHERE NOT user_edited`)

### Status reconciliation: Already handled
The `claim_unprocessed_emails` RPC filters `WHERE status = 'unprocessed'`. "Mark Completed" sets status to `'completed'`, so completed emails are never re-claimed by the worker. No additional work needed.

### Vercel routing
Multi-page static apps need explicit routing config. Add `web/vercel.json`:
```json
{
  "rewrites": [
    { "source": "/app/(.*)", "destination": "/app/$1" }
  ],
  "headers": [
    {
      "source": "/js/(.*)",
      "headers": [{ "key": "Content-Type", "value": "application/javascript" }]
    }
  ]
}
```
Without this, direct navigation to `/app/dashboard.html` may 404 in production. Include in Phase 1.

### Error reporting: Global handler + Supabase logging
- `window.onerror` and `window.onunhandledrejection` handlers in `ui.js`
- Log to `error_logs` table in Supabase (user_id, page, error, stack, timestamp)
- Lightweight — helps debug production issues for other users

## UX Foundations (built into Phase 1)

These cross-cutting concerns get baked into the infrastructure layer, not bolted on later:

- **Error banner** — reusable `showError(msg)` / `hideError()` in a shared `ui.js` module. Renders a dismissible red banner at the top of `.em-main`. Every Supabase query wraps in try/catch and calls this on failure.
- **Empty states** — each page handles zero-data gracefully. Dashboard: "No data yet — run the extension to sync emails." Emails: "No emails synced yet." History: "No pipeline runs recorded."
- **Loading skeletons** — CSS-only pulse animation in `app.css` (`.em-skeleton`). Placed in: dashboard metric cards, email list, history table. Each page renders skeletons first, then replaces with real data.
- **Session expiry** — `auth.js`'s `requireAuth()` calls `supabase.auth.getSession()`. If session is null or refresh fails, redirect to `login.html`. The `onAuthStateChange` listener handles mid-session expiry (e.g., user returns after hours) with the same redirect.
- **`requireAuth()` is blocking** — each page's module `await`s `requireAuth()` as its first line. No queries fire and no DOM renders until auth resolves. Prevents 401 flash.

## Implementation Order

### Phase 1: Infrastructure
1. `supabase/migrations/002_dashboard_policies.sql` — emails UPDATE policy + `user_edited` column on drafts + `error_logs` table
2. `web/vercel.json` — routing config for multi-page static app
3. `css/app.css` — layout (sidebar + main), cards, badges, tables, buttons, responsive grid, loading skeletons (`.em-skeleton` pulse), error banner, auto-resize textarea
4. `js/supabase-client.js` — `createClient()` singleton with anon key only (verify no service role key)
5. `js/auth.js` — signIn, signUp, signOut, getSession, `requireAuth()` (blocking, redirects on failure), `onAuthStateChange` listener for mid-session expiry
6. `js/ui.js` — `showError(msg)`, `hideError()`, `showEmpty(container, message)`, global `window.onerror` + `onunhandledrejection` → insert into `error_logs`, URL state helpers (`getParam`, `setParam` via `history.replaceState`)
7. `js/nav.js` — sidebar renderer (Dashboard/Emails/History links, user email, logout)
8. `app/login.html` + `js/pages/login.js` — login/signup form with toggle

### Phase 2: Dashboard
9. `app/dashboard.html` + `js/pages/dashboard.js`
   - Queries: `emails` count, `classifications` count (needs_response=true), `drafts` count, latest `pipeline_runs` row
   - Pipeline funnel from `pipeline_runs` aggregate (scanned → processed → drafts)
   - Date range filter (today/7d/30d) — synced to URL param `?range=7d`
   - Manual "Refresh" button to re-fetch data
   - 4 metric cards in CSS grid
   - Empty state: "No data yet — run the extension to sync emails"
   - Loading skeletons on all 4 cards

### Phase 3: Emails
10. `app/emails.html` + `js/pages/emails.js`
    - Primary query: `emails` with embedded `classifications(*)` and `drafts(*)`
    - Client-side grouping: Drafts Ready / Needs Response / Other / Completed
    - Email cards: sender, subject, date, action snippet, badges
    - Inline detail expand: full body, classification, draft
    - Draft editing via auto-resizing `<textarea>` + manual "Save" button → PATCH to drafts table, sets `user_edited = true`
    - "Saved" confirmation inline after successful PATCH
    - Mark completed: PATCH email status → 'completed'
    - Search filter (client-side on subject/sender) — synced to URL param `?q=`
    - **Pagination**: initial fetch 50 rows via `.range(0, 49)`, synced to `?page=`. "Load More" button appends next 50. "Showing 50 of N emails" indicator when truncated.
    - Empty state: "No emails synced yet"
    - Loading skeleton on email list

### Phase 4: History
11. `app/history.html` + `js/pages/history.js`
    - Query: `pipeline_runs` ordered by started_at desc, limit 50
    - Table: started, trigger, status, scanned, processed, drafts, duration
    - Expandable row detail: error_message, log_output
    - Empty state: "No pipeline runs recorded"
    - Loading skeleton on table

### Worker change (out of dashboard scope, but required)
12. Update worker's draft UPSERT to skip rows where `user_edited = true`
    - Without this, re-running the pipeline overwrites user-edited drafts
    - Small change in `worker/supabase_client.py` → `insert_draft()` method

## Key Supabase Queries

**Emails with classifications + drafts (paginated):**
```js
supabase.from('emails')
  .select('*, classifications(*), drafts(*)', { count: 'exact' })
  .order('received_time', { ascending: false })
  .range(offset, offset + 49)
```
`count: 'exact'` returns total rows in the response header — used for "Showing X of N" indicator.

> **Future optimization note:** Client-side grouping (Drafts Ready / Needs Response / etc.) works fine at current scale. If per-group counts are needed on the dashboard later, switch to separate filtered queries per group.

**Dashboard metrics (example — needs_response count):**
```js
supabase.from('classifications')
  .select('*', { count: 'exact', head: true })
  .eq('needs_response', true)
  .gte('created_at', dateRangeStart)
```

**Pipeline runs:**
```js
supabase.from('pipeline_runs')
  .select('*')
  .order('started_at', { ascending: false })
  .limit(50)
```

## Critical Files
- `supabase/migrations/001_initial_schema.sql` — table schemas, column names, FK relationships
- `web/css/landing.css` — design tokens to reuse (lines 6–36)
- `extension/popup.js` — auth flow reference (login/signup toggle pattern)
- `worker/supabase_client.py` — field names written by worker (must match dashboard reads)

## Verification
1. Run migration 002 in Supabase SQL editor
2. Run `app/login.html` locally (Live Server or `python -m http.server` in web/)
3. Login with the same credentials used in the extension
4. Verify dashboard loads metrics, date range filter syncs to URL `?range=`
5. Verify emails page shows emails synced by the extension
6. Test search filter persists in URL `?q=`
7. Test pagination — "Load More" appends rows, `?page=` updates
8. Test draft editing: textarea saves, "Saved" confirmation appears, `user_edited` flag set in DB
9. Test "Mark Completed" updates email status
10. Refresh each page — verify URL state restores filters/pagination
11. Test session expiry: clear localStorage, verify redirect to login (no 401 flash)
12. Test empty states: new user with no data sees placeholder messages
13. Deploy to Vercel, verify direct navigation to `/app/dashboard.html` works (no 404)
14. Check `error_logs` table for any client-side errors after using the app
