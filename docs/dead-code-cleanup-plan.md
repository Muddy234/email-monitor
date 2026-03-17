# Dead Code Cleanup — Pre-Chrome Store Audit

## Corrections from Verification
- `handleGetItem()` in background.js — IS called by `handleGetItemBatch()` → NOT dead
- Console.log in supabase-rest.js:62 — IS behind `DEBUG` flag → NOT unconditional
- `hideError()` in web/js/ui.js — only defined there (line 31), never referenced in any HTML onclick or JS import → confirmed dead

---

## Step 1 — Worker: Remove dead enriched pipeline (~260 lines)

**File: `worker/run_pipeline.py`**
- Delete `process_user_batch_enriched()` (lines 837–1094)
- Delete helper `_score_and_gate()` — only called by enriched pipeline
- Delete helper `_enrich_batch()` — only called by enriched pipeline
- Remove dead imports (lines 20–21):
  - `from pipeline.scorer import UserScoringArtifacts, score_email, check_triage_gate`
  - `from pipeline.enrichment import assemble_enrichment`

**File: `worker/main.py`**
- Line 29: remove `process_user_batch_enriched` from the import

---

## Step 2 — Web: Remove dead subscription/paywall code

**Confirmed dead, not pre-built.** Commit `3f3b2d2` ("Move subscription to Account page, remove paywall overlay from all pages") explicitly removed all `requireSubscription()` calls from every page.

**How billing works now:** The Account page calls `getSubscription()` to check status, `isSubscriptionActive()` / `isGrandfathered()` to gate features, and `startCheckout()` / `openPortal()` for Stripe redirects. `pollForActivation()` handles the post-checkout callback. These are all still live and stay untouched. The old approach injected a blocking `showPaywall()` overlay on every page — that overlay and its helpers are the dead code.

**File: `web/js/subscription.js`**
- Delete `showPaywall()` (lines 118–152)
- Delete `showPastDueBanner()` (lines 157–184)
- Delete `showCheckoutSpinner()` (lines 189–204)
- Delete `requireSubscription()` (lines 211–253)
- Keep: `getSubscription()`, `isSubscriptionActive()`, `isGrandfathered()`, `callEdgeFunction()`, `startCheckout()`, `openPortal()`, `pollForActivation()`

**File: `web/css/app.css`**
- Delete orphaned paywall CSS classes:
  - `.em-paywall-backdrop` (line ~1650)
  - `.em-paywall-card` (line ~1661)
  - `.em-paywall-price` (line ~1685)
  - `.em-paywall-cta` (line ~1698)
  - `.em-paywall-hint` (line ~1704)
  - `.em-spinner` (line ~1712) — only used by paywall
  - `.em-past-due-banner` (line ~1727)

---

## Step 3 — Web: Remove dead `hideError()` export

Grep across all file types (`web/`) confirms `hideError` appears only at its definition in `web/js/ui.js:31`. No HTML onclick, no inline handlers, no JS imports.

**File: `web/js/ui.js`**
- Delete `hideError()` (lines 31–34)

---

## Step 4 — DEFERRED: Deduplicate Supabase credentials in login.js

~~Remove hardcoded `SUPABASE_URL`/`SUPABASE_ANON_KEY` from login.js, import from supabase-client.js.~~

**Deferred until after Chrome Store submission.** This is a refactor, not dead code removal. Changing how login.js resolves credentials touches the auth critical path. Not worth the risk right before release — do it in the next maintenance pass.

---

## Step 5 — Extension: Remove dead functions

**File: `extension/popup.js`**
- Delete `showStatusError()` (lines 26–31) — never called
- Delete `hideStatusError()` (lines 33–37) — never called

**File: `extension/background.js`**
- Delete `MAX_CATCHUP_DAYS` constant (line 27) — unused
- Delete `handleUnflagEmail()` (lines 437–477) — no message type routes to it. Verified: the `onMessage` listener (lines 767–809) dispatches exactly 5 types (`token_update`, `getStatus`, `supabaseSessionChanged`, `syncNow`). No `"unflagEmail"` case exists anywhere — the function is fully orphaned with no dispatch stub to clean up.

---

## Step 6 — Extension: Clean temp artifacts

The `tmpclaude-*-cwd` directories are already matched by `.gitignore` (`tmpclaude-*` pattern) — they're untracked and won't appear in the Chrome Store package. **No git action needed.** Delete them locally for hygiene only.

Also found `tmpclaude-*` dirs inside `supabase/migrations/` — same situation (gitignored). Delete locally.

**File: `extension/icons/generate_icons.py`** — build tool, not runtime. **Already excluded** from Chrome Store zip: `build_extension.py` uses a whitelist (`INCLUDE` list) that only adds specific files — `generate_icons.py` is not in the list. No action needed.

---

## Step 7 — Migrations: Add clarifying comments to duplicate-numbered files

Renumbering risks breaking applied migration state on existing deploys. Instead, add a comment to the top of each duplicate-numbered file explaining the conflict and why it's safe.

**Files to annotate:**
- `003_heartbeat_columns.sql` — add: `-- NOTE: Shares 003 prefix with 003_onboarding_schema.sql. Both are idempotent (IF NOT EXISTS). Safe to run in any order.`
- `003_onboarding_schema.sql` — same note
- `006_reset_stuck_emails.sql` — add: `-- NOTE: Shares 006 prefix with 006_trace_scoring_columns.sql. Both are idempotent.`
- `006_trace_scoring_columns.sql` — same note
- `007_profile_display_name.sql` — add: `-- NOTE: Shares 007 prefix with 007_response_events_features.sql. Both are idempotent.`
- `007_response_events_features.sql` — same note
- `008_contacts_total_received.sql` — add: `-- NOTE: Column total_received already added in 005. This migration is a no-op due to IF NOT EXISTS.`
- `009_contacts_missing_columns.sql` — add: `-- NOTE: Columns already added in 005. This migration is a no-op due to IF NOT EXISTS.`

---

## Verification

1. **Extension popup** — load in Chrome, test login/signup/phone-verify flow
2. **Web login** — test signup + phone verify at `clarion-ai.app/app/login.html`
3. **Web billing** — confirm Account page subscription functions still work (untouched)
4. **Worker** — `python main.py` starts without import errors
5. **Console audit** — all 27 console statements in `extension/` are behind `DEBUG` flag (verified via grep). Two progress logs in `supabase-rest.js:62` and `background.js:429` are gated by `if (DEBUG && ...)`. No unguarded output.
6. **`git diff --stat`** — confirm only targeted deletions, no functional changes
