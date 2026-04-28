# State Management Refactor — Implementation Guide

**Date:** 2026-04-24
**Status:** Ready for final review
**Revision:** v4 — reconciled with actual codebase state. Shifts migration numbers (037+), corrects drafts/emails value maps (actual values differ from v3 assumptions), fixes `emails.updated_at` / `pipeline_runs.updated_at` backfill (columns don't exist), adds §3.4 reality check.
**Scope:** Unify status vocabulary, add `state_entered_at`, collapse onboarding state, retire `pipeline_stage`, persist extension state, separate retry-able failures from terminal skips.
**Goal:** Establish a consistent state substrate that a future reaper/self-healing process can reason about reliably.

### Locked Pre-Flight Decisions (v3)

| Decision | Choice | Rationale |
|---|---|---|
| 1. Drafts split status + delivery_state | **A — Split** | Clean separation; reaper only watches `status` for generation |
| 2. partial_failure as boolean flag | **A — Boolean** | `has_partial_failures` on `done` runs |
| 3. Onboarding substage column | **A — `onboarding_stage text`** | Queryable without JSON ops |
| 4. `emails.status='onboarding'` | **A — `skipped` + `deferred_reason='onboarding'`** | Preserves unified vocabulary |
| 5. `error` vs `failed` on emails | **Collapse to `failed`** | `last_error` preserves detail |
| 6. `failure_count` in trigger | **Yes, in trigger. Budget = 3.** | Can't be bypassed by direct writes |
| 7. Deployment window | **Deferred to ops** | Not a design question |

### Scope Locked — Iteration 1: Drafts Reaper

This iteration ships only what the drafts reaper needs. Onboarding keeps its
existing `_recover_stuck_onboarding` path unchanged.

| Option | In scope? | Tables affected |
|---|---|---|
| A. Unified vocabulary | **Yes** | `emails`, `drafts`, `pipeline_runs` |
| B. `state_entered_at` + `failure_count` + `last_error` | **Yes** | All status-bearing tables (additive columns only on `profiles`) |
| F. Failed/skipped split + `delivery_state` on drafts | **Yes** | `drafts` |
| C. Collapse onboarding's 12-state | **Deferred** | `profiles.onboarding_status` unchanged |
| D. Retire `pipeline_stage` | **Deferred** | `profiles.pipeline_stage` unchanged |
| E. Extension state persistence | **Ship independently, first** | none (extension only) |

**Sections marked `[DEFERRED — iteration 2+]`** in this document stay in the
plan as future-work reference. Do not execute them in this iteration.

**Explicitly NOT in this iteration:**
- Onboarding 12-state collapse (`profiles.onboarding_status` remapping in Phase 3)
- `_recover_stuck_onboarding` deletion in Phase 4
- `pipeline_stage` column drop (migration 042)
- `profiles_onboarding_status_check` CHECK constraint in Phase 5

---

## Table of Contents

1. [Design Principles](#1-design-principles)
2. [Target State Model](#2-target-state-model)
3. [Current-State Inventory](#3-current-state-inventory)
4. [Pre-Flight Decisions (BLOCKING)](#4-pre-flight-decisions-blocking)
5. [Phased Rollout](#5-phased-rollout)
6. [Phase 1 — Additive Foundations](#6-phase-1--additive-foundations)
7. [Phase 2 — Dual-Read Compatibility](#7-phase-2--dual-read-compatibility)
8. [Phase 3 — Data Migration](#8-phase-3--data-migration)
9. [Phase 4 — Writer Flip](#9-phase-4--writer-flip)
10. [Phase 5 — Compat Removal](#10-phase-5--compat-removal)
11. [Phase 6 — Extension State Persistence](#11-phase-6--extension-state-persistence)
12. [Risk Register & Rollback Plans](#12-risk-register--rollback-plans)
13. [Testing Strategy](#13-testing-strategy)
14. [Post-Refactor: Reaper Integration](#14-post-refactor-reaper-integration)
15. [Appendix A — File Manifest](#15-appendix-a--file-manifest)
16. [Appendix B — Migration Templates](#16-appendix-b--migration-templates)
17. [Recommended Scope Trim for First Ship](#17-recommended-scope-trim-for-first-ship)
18. [Clock Policy — state_entered_at vs updated_at](#18-clock-policy--state_entered_at-vs-updated_at)
19. [system_config — Reaper Kill Switch](#19-system_config--reaper-kill-switch)
20. [Error Sanitization & last_error RLS](#20-error-sanitization--last_error-rls)
21. [Staging Environment — Schema Clone + Synthetic Data](#21-staging-environment--schema-clone--synthetic-data)

---

## 1. Design Principles

1. **One vocabulary for lifecycle state.** All transient-to-terminal state uses the same words, enabling generic tooling (reaper, monitoring, dashboards).
2. **Time-bound every intermediate state.** Every row in a non-terminal state has a `state_entered_at` so "stuck" is computable without heuristics.
3. **Explicit over implicit.** Row-presence checks become explicit status reads.
4. **Preserve diagnostic fidelity.** Collapsing state must not erase *why* something is in that state.
5. **Never big-bang.** Every phase is individually deployable and reversible until Phase 3.
6. **Stripe is the source of truth for `subscriptions.status`.** Out of scope.

---

## 2. Target State Model

### 2.1 Unified Lifecycle Vocabulary

```
pending → active → done
              ↘   failed    (retry candidate)
              ↘   skipped   (terminal non-error)
```

| Value | Meaning | Terminal? | Retry-eligible? |
|---|---|---|---|
| `pending` | Queued, not yet started | No | N/A |
| `active` | In progress | No | N/A |
| `done` | Completed successfully | Yes | No |
| `failed` | Failed, retry permitted within budget | Yes | Yes |
| `skipped` | Intentionally not processed (noise, unsupported, deferred) | Yes | No |

### 2.2 Per-Table Target

| Table | Current values (grep-verified) | Target values | Notes |
|---|---|---|---|
| `emails.status` | `unprocessed, processing, processed, onboarding, error, completed, dismissed` | `pending, active, done, failed, skipped` | Mapping in §3.4. `completed`/`dismissed` are dashboard user actions — not currently in v3 plan |
| `drafts.status` | `pending, written, deleted` (no `failed`/`sent`/`edited`/`obsolete` are ever written) | `pending, active, done, failed, skipped` + `delivery_state` | Split: `status` = generation lifecycle, `delivery_state` = Outlook state. `draft_deleted` boolean subsumed into `delivery_state='user_deleted'` |
| `pipeline_runs.status` | `running, completed, failed, partial_failure` | `pending, active, done, failed` + `has_partial_failures bool` | Partial failure becomes a boolean on `done` runs |
| `profiles.onboarding_status` | 10 intermediate values (`running` never written) | `pending, active, done, failed, skipped` + `onboarding_stage text` | **DEFERRED to iteration 2+** — stays unchanged in iteration 1 |
| `profiles.pipeline_stage` | `idle, gathering, analyzing, drafting` | **DROPPED** — derive from `pipeline_runs` | **DEFERRED to iteration 2+** |
| `subscriptions.status` | Stripe values | **UNCHANGED** | Out of scope |

### 2.3 New Columns

Every status-bearing table gets:

```sql
state_entered_at timestamptz NOT NULL DEFAULT now()
```

Additional columns introduced:

| Table | Column | Purpose |
|---|---|---|
| `emails` | `deferred_reason text NULL` | Why a row is `skipped` (`noise`, `onboarding`, `no_body`, `unsupported`) |
| `drafts` | `delivery_state text` | `not_delivered`, `delivered`, `edited_in_outlook`, `sent`, `stale` — decouples Outlook push state from generation |
| `pipeline_runs` | `has_partial_failures boolean` | True when some emails in the run failed |
| `profiles` | `onboarding_stage text` | Diagnostic substage when `onboarding_status='active'` |
| `profiles` | `onboarding_stage_entered_at timestamptz` | For substage-level stuck detection |
| All above | `state_entered_at timestamptz` | Time current status was entered |
| All above | `failure_count integer DEFAULT 0` | For retry-budget enforcement |
| All above | `last_error text NULL` | Last error message, for diagnostics |

---

## 3. Current-State Inventory

### 3.1 Database

| Field | Table | Default | Index | Constraint |
|---|---|---|---|---|
| `status` | `emails` | `'unprocessed'` | Yes (`idx_emails_status`, `idx_emails_user_status`) | None |
| `status` | `drafts` | `'pending'` | Yes (`idx_drafts_user_status`) | None |
| `status` | `pipeline_runs` | `'running'` | None | None |
| `onboarding_status` | `profiles` | `NULL` | Partial (on `onboarding_completed_at`) | None |
| `pipeline_stage` | `profiles` | `'idle'` | None | None |
| `status` | `subscriptions` | `'inactive'` | None | None |

### 3.2 RPCs Depending on Status

- `claim_unprocessed_emails(p_user_id, p_limit)` — atomic claim with `FOR UPDATE SKIP LOCKED`
- `find_stale_drafts()` — filters on `status IN ('written', 'pending')`

### 3.3 Code Surface Summary

| Component | File | Approximate call sites |
|---|---|---|
| Worker | `worker/supabase_client.py` | 15 methods |
| Worker | `worker/run_pipeline.py` | 10 call sites |
| Worker | `worker/onboarding/runner.py` | 12 status writes |
| Worker | `worker/main.py` | 3 call sites |
| Extension | `extension/supabase-realtime.js` | 1 realtime filter, 3 status checks |
| Extension | `extension/background.js` | 4 in-memory globals |
| Web | `web/js/pages/emails.js` | 6 call sites |
| Web | `web/js/pages/history.js` | 1 display mapping |
| Web | `web/js/components/trace-renderers.js` | 2 checks |
| Edge fn | `supabase/functions/generate-draft/index.ts` | 1 subscription check (OUT OF SCOPE) |

### 3.4 Reality Check — Grep-Verified Current Values (v4)

A full code scan confirmed that several assumptions in v1–v3 were wrong.
This section is the **authoritative value inventory** — every value below
is grep-verified to be either written or read in the current codebase.

**Next free migration number:** `037`. Existing: `...031, 032_profile_counts,
032_draft_quality_issues, 033_draft_thread_summary_and_preference_text, 035_drop_calibration, 036_personality_blurb`.
(Migration 034 was used and rolled back — number not reusable.)

**Missing columns (breaks v1–v3 backfill assumptions):**

| Table | Column | Status | Impact |
|---|---|---|---|
| `emails` | `updated_at` | **DOES NOT EXIST** | Phase 1 backfill must use `created_at` only |
| `pipeline_runs` | `updated_at` | **DOES NOT EXIST** | Phase 1 backfill uses `finished_at` / `started_at` |
| `drafts` | `updated_at` | Exists | OK |

**emails.status — actual values:**

| Value | Written by | Read by |
|---|---|---|
| `unprocessed` | Worker (initial), dashboard restore (`emails.js:725`) | Worker claim RPC, dashboard |
| `processing` | Worker claim RPC | Worker reset stuck |
| `processed` | Worker (`run_pipeline.py:225, 331, 337, 1093`), dashboard restore | `isCompleted` **does NOT include this** |
| `onboarding` | Worker (`supabase_client.py:210`, `run_pipeline.py:145`) | Worker skip query |
| `error` | Worker (`run_pipeline.py:1523`) | — |
| `completed` | Dashboard (`emails.js:558, 692`), extension sent-items (`background.js:1253`) | `isCompleted` ✓ |
| `dismissed` | Dashboard (`emails.js:558, 692`) | `isCompleted` ✓ |

> **Pre-existing inconsistency:** worker writes `processed`; dashboard writes `completed`/`dismissed`.
> `isCompleted` only checks `completed`/`dismissed` — so worker-processed emails do **not** appear
> "completed" in the dashboard UI. This is **not fixed** in iteration 1 (reaper doesn't touch emails
> beyond vocabulary mapping). See §4 Decision 5b below.

**drafts.status — actual values (grep-verified):**

| Value | Written by | Read by |
|---|---|---|
| `pending` | Worker (`supabase_client.py:297`) | Extension realtime filter, dashboard |
| `written` | Extension only (`supabase-realtime.js:205`) | Extension, `find_stale_drafts`, popup |
| `deleted` | Extension only (`supabase-realtime.js:364`, **together** with `draft_deleted=true` on line 363) | Extension, dashboard, popup |
| `failed` | **Never written** in current code | — |
| `sent` / `edited` / `obsolete` | **Never written.** Values in v3 plan were incorrect assumptions | — |

**drafts booleans:**
- `draft_deleted boolean default false` — always written together with `status='deleted'` (redundant today)
- `user_edited boolean default false` — prevents worker overwrite

**pipeline_runs.status — actual values:**
- Written: `running` (default), `completed`, `partial_failure`, `failed`. Matches v3 assumption.

**profiles.onboarding_status — actual values (grep-verified):**
- Written in `worker/onboarding/runner.py` (docstring lines 4–19): `collecting, statistics, persisting, extracting, synthesizing, style_guide, training, complete, complete_partial, failed`
- Also: `pending` (initial), `starting` (entry point)
- **`running` is never written** — v3's `LegacyOnboardingStatus.RUNNING` constant is dead. Remove.
- **Gap:** if runner crashes mid-phase, the row is stuck in the intermediate stage name; `_recover_stuck_onboarding` owns detection today. Not changing in iteration 1.

### 3.5 Consequences for this Plan

1. **All migration numbers shift.** `033→037, 034→038, 035→039, 036→040, 037→041, 038→042, 039→043, 039a→043a, 039b→043b, 040→044, 041→045`.
2. **`emails.updated_at` references removed** from migration backfills and pre-flight steps. Add column first or rely on `created_at`.
3. **`pipeline_runs.updated_at` references removed.**
4. **Drafts value map:** `pending → pending`, `written → done` (with `delivery_state='delivered'`), `deleted → skipped` (with `delivery_state` TBD — see §4 Decision 1b). Drop `sent/edited/obsolete` mappings.
5. **Emails value map:** add `completed → done`, `dismissed → skipped` (with `deferred_reason='user_dismissed'`).
6. **Legacy constants:** remove `LegacyOnboardingStatus.RUNNING`, `LegacyDraftStatus.{EDITED,SENT,OBSOLETE}`. Add `LegacyDraftStatus.DELETED`, `LegacyEmailStatus.{COMPLETED,DISMISSED}`.

---

## 4. Pre-Flight Decisions (BLOCKING)

**These must be resolved before Phase 1 begins. Each has an owner decision required.**

### Decision 1: Drafts — split `status` and `delivery_state`?
- **Option A (LOCKED):** Split. `status` tracks generation (`pending`→`active`→`done`/`failed`); `delivery_state` tracks Outlook.
  - **Pro:** Clean separation; reaper only watches `status` for work generation; delivery is a separate lifecycle.
  - **Con:** One more column; requires migrating `written` and `deleted` into new column.

### Decision 1b: `drafts.draft_deleted` boolean — retire or keep? (NEW in v4)

Today, `draft_deleted=true` is always set together with `status='deleted'`
(extension `supabase-realtime.js:363–364`). They're redundant.

- **Option A (recommended):** Subsume into `delivery_state`. Post-migration:
  - `status='skipped'` + `delivery_state='user_deleted'` replaces both the old `status='deleted'` AND `draft_deleted=true`.
  - Keep `draft_deleted` column during Phase 2 dual-read (so `20_draft_cleanup.sql`'s RPC `find_stale_drafts` still works). Drop in Phase 5.
- **Option B:** Keep `draft_deleted` boolean indefinitely; just migrate `status`.
  - **Con:** Perpetuates duplicated signal; reaper has to know both paths.

**Locked:** Option A. `delivery_state` values: `not_delivered, delivered, user_deleted, sent, edited_in_outlook, stale`.
`sent` / `edited_in_outlook` / `stale` are **reserved for future writes** — no existing rows use them.

### Decision 2: `partial_failure` — separate state or boolean flag?
- **Option A (recommended):** Boolean `has_partial_failures` on `done` runs.
- **Option B:** Keep as distinct status (`partial`).
- **Impact:** Affects `history.js` badge display. Option A requires frontend update to render `done + partial_failures` as "Partial" badge.

### Decision 3: Onboarding substage — secondary column or JSONB?
- **Option A (recommended):** `onboarding_stage text` column.
  - Enum-like values matching current intermediate states (`collecting`, `extracting`, `synthesizing`, etc.)
  - Queryable without JSON ops.
- **Option B:** `onboarding_meta jsonb` with arbitrary substage info.
- Option A is sufficient and simpler.

### Decision 4: `emails.status='onboarding'` handling
- **Option A (recommended):** Migrate to `status='skipped'` + `deferred_reason='onboarding'`. Reaper can un-skip these when onboarding completes via a specific query.
- **Option B:** Keep `onboarding` as a distinct value (breaks unified vocabulary).
- Option A preserves the refactor's core benefit.

### Decision 5: `error` status on emails
- Current code writes `status='error'` at `run_pipeline.py:1523` (exception handler).
- `status='failed'` is **not** currently used on `emails` (grep confirms). The v3 assumption was wrong.
- **Decision:** Map `error → failed`. Use `last_error` column to preserve detail.

### Decision 5b: emails.status `completed` and `dismissed` (NEW in v4)

The dashboard writes `completed` (user marks email handled) and `dismissed`
(user dismisses from notable list). Worker writes `processed` after generation.
`isCompleted` in the dashboard only treats `completed`/`dismissed` as complete,
not `processed` — a pre-existing inconsistency.

- **Locked mapping for iteration 1:**
  - `completed` → `done` (user-terminal success, same as worker `processed`)
  - `dismissed` → `skipped` + `deferred_reason='user_dismissed'`
- **Post-migration,** both worker and dashboard write `done` for "this email is finished"; the `isCompleted` check collapses to a single value.
- **Not fixing** the pre-migration dashboard bug (worker-processed emails not appearing as "complete" in the UI) — the migration itself fixes it by unifying values.

### Decision 6: Retry budget per entity
- Reaper needs to know "how many attempts have we made."
- **Proposal:** `failure_count integer DEFAULT 0` on each status-bearing table, incremented on every `→ failed` transition.
- **Budget:** Start with 3. Configurable per entity type.

### Decision 7: Deployment window
- Phase 3 requires low-traffic window.
- **Recommended:** Friday evening or weekend, with DB snapshot taken immediately before.

---

## 5. Phased Rollout

```
Phase 1: Additive       → Deploy (safe, reversible)
Phase 2: Dual-read      → Deploy (safe, reversible)
Phase 3: Data migration → Single transaction (DANGER ZONE)
Phase 4: Writer flip    → Deploy (safe if Phase 3 succeeded)
Phase 5: Compat removal → Deploy (cosmetic cleanup)
Phase 6: Extension state → Deploy anytime (independent)
```

| Phase | Reversible? | Requires DB migration? | Requires coordinated deploy? |
|---|---|---|---|
| 1 | Yes (drop columns) | Yes (additive) | No |
| 2 | Yes (revert code) | No | No (worker/extension/web independent) |
| 3 | Hard (requires reverse migration) | Yes (data rewrite) | Yes (stop workers first) |
| 4 | Yes (revert code, Phase 2 compat still in place) | No | No |
| 5 | Yes (restore compat code) | Optional (CHECK constraints) | No |
| 6 | Yes (revert code) | No | No |

---

## 6. Phase 1 — Additive Foundations

**Goal:** Lay infrastructure without changing any behavior.

**Pre-requisites:** All Pre-Flight Decisions resolved.

### 6.1 Create status constants module

**New file: `worker/status.py`**

```python
"""Status constants and enum-like classes for state management.

DO NOT use raw string literals for status values. Always import from here.
"""

# Unified lifecycle vocabulary
class Status:
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

    TRANSIENT = {PENDING, ACTIVE}
    TERMINAL = {DONE, FAILED, SKIPPED}
    RETRYABLE = {FAILED}

    # Note: pipeline_runs uses a 4-state subset of this vocabulary:
    # {active, done, failed} (and conceptually pending, though runs are
    # created directly as 'active' — no queued stage). 'skipped' never
    # applies to a pipeline_run. Use Status.* constants for writes; the
    # CHECK constraint on pipeline_runs enforces the 4-state subset.


# Legacy status values (used during Phase 2 dual-read) — v4 corrections
class LegacyEmailStatus:
    UNPROCESSED = "unprocessed"
    PROCESSING = "processing"
    PROCESSED = "processed"
    ONBOARDING = "onboarding"
    ERROR = "error"
    COMPLETED = "completed"    # dashboard user action
    DISMISSED = "dismissed"    # dashboard user action
    # NOTE: no 'failed' — emails table never has status='failed' today


class LegacyDraftStatus:
    PENDING = "pending"
    WRITTEN = "written"    # extension: pushed to Outlook successfully
    DELETED = "deleted"    # extension: user deleted via extension (also sets draft_deleted=true)
    # NOTE: no 'failed'/'edited'/'sent'/'obsolete' — never written today


class LegacyPipelineRunStatus:
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL_FAILURE = "partial_failure"


class LegacyOnboardingStatus:
    # 10 intermediate values actually written (no 'running')
    PENDING = "pending"
    STARTING = "starting"
    COLLECTING = "collecting"
    STATISTICS = "statistics"
    PERSISTING = "persisting"
    EXTRACTING = "extracting"
    SYNTHESIZING = "synthesizing"
    STYLE_GUIDE = "style_guide"
    TRAINING = "training"
    COMPLETE = "complete"
    COMPLETE_PARTIAL = "complete_partial"
    FAILED = "failed"
    # NOTE: 'running' was in v3 plan but is never written in any code path.


# Mapping from legacy to new (used by Phase 3 migration and dual-read logic)
EMAIL_LEGACY_TO_NEW = {
    "unprocessed": Status.PENDING,
    "processing": Status.ACTIVE,
    "processed": Status.DONE,
    "completed": Status.DONE,       # dashboard user "mark complete"
    "error": Status.FAILED,
    "onboarding": Status.SKIPPED,   # + deferred_reason='onboarding'
    "dismissed": Status.SKIPPED,    # + deferred_reason='user_dismissed'
}

DRAFT_LEGACY_TO_NEW = {
    "pending": Status.PENDING,
    "written": Status.DONE,         # + delivery_state='delivered'
    "deleted": Status.SKIPPED,      # + delivery_state='user_deleted' (draft_deleted bool retired)
}

PIPELINE_RUN_LEGACY_TO_NEW = {
    "running": Status.ACTIVE,
    "completed": Status.DONE,
    "failed": Status.FAILED,
    "partial_failure": Status.DONE,  # + has_partial_failures=true
}

ONBOARDING_LEGACY_TO_NEW = {
    "pending": Status.PENDING,
    "starting": Status.ACTIVE,
    "collecting": Status.ACTIVE,
    "statistics": Status.ACTIVE,
    "persisting": Status.ACTIVE,
    "extracting": Status.ACTIVE,
    "synthesizing": Status.ACTIVE,
    "style_guide": Status.ACTIVE,
    "training": Status.ACTIVE,
    "complete": Status.DONE,
    "complete_partial": Status.DONE,  # + has_partial_failures
    "failed": Status.FAILED,
}

# Onboarding substage preserved in onboarding_stage column
ONBOARDING_SUBSTAGE_MAP = {
    "starting": "starting",
    "collecting": "collecting",
    "statistics": "statistics",
    "persisting": "persisting",
    "extracting": "extracting",
    "synthesizing": "synthesizing",
    "style_guide": "style_guide",
    "training": "training",
}
```

**New file: `extension/status.js`** (ES module)

```javascript
// Mirror of worker/status.py for extension-side code
export const Status = {
  PENDING: "pending",
  ACTIVE: "active",
  DONE: "done",
  FAILED: "failed",
  SKIPPED: "skipped",
};

export const DeliveryState = {
  NOT_DELIVERED: "not_delivered",
  DELIVERED: "delivered",
  USER_DELETED: "user_deleted",       // replaces draft_deleted boolean
  // Reserved for future writes (not in iteration 1):
  EDITED_IN_OUTLOOK: "edited_in_outlook",
  SENT: "sent",
  STALE: "stale",
};

// Legacy (for dual-read in Phase 2) — v4 corrected
export const LegacyDraftStatus = {
  PENDING: "pending",
  WRITTEN: "written",
  DELETED: "deleted",
};
```

**New file: `web/js/status.js`** — Mirror for dashboard code.

### 6.2 Refactor code to use constants (NO VALUE CHANGES)

Replace every hardcoded status string literal with a constant import. **Do not change any values yet.**

Example:

```python
# Before
.update({"status": "processed"})

# After
from worker.status import LegacyEmailStatus
.update({"status": LegacyEmailStatus.PROCESSED})
```

**Files to update:**
- All files in Appendix A.

**Validation:** `grep` for remaining hardcoded status strings. After this step there should be near-zero.

### 6.3 Migration: add `state_entered_at` columns

**New migration file: `supabase/migrations/037_add_state_entered_at.sql`**

```sql
-- Add state_entered_at to every status-bearing table
-- Nullable initially to avoid default-now() issue on existing rows

alter table public.emails
  add column if not exists state_entered_at timestamptz,
  add column if not exists failure_count integer default 0 not null,
  add column if not exists last_error text,
  add column if not exists deferred_reason text;

alter table public.drafts
  add column if not exists state_entered_at timestamptz,
  add column if not exists failure_count integer default 0 not null,
  add column if not exists last_error text,
  add column if not exists delivery_state text;

alter table public.pipeline_runs
  add column if not exists state_entered_at timestamptz,
  add column if not exists failure_count integer default 0 not null,
  add column if not exists last_error text,
  add column if not exists has_partial_failures boolean default false not null;

alter table public.profiles
  add column if not exists onboarding_state_entered_at timestamptz,
  add column if not exists onboarding_stage text,
  add column if not exists onboarding_stage_entered_at timestamptz,
  add column if not exists onboarding_failure_count integer default 0 not null,
  add column if not exists onboarding_last_error text;

-- Backfill from best available timestamp.
-- NOTE (v4): emails and pipeline_runs have NO updated_at column.
-- emails:         created_at only
-- drafts:         created_at + updated_at
-- pipeline_runs:  started_at + finished_at
update public.emails
  set state_entered_at = coalesce(created_at, now())
  where state_entered_at is null;

update public.drafts
  set state_entered_at = coalesce(updated_at, created_at, now())
  where state_entered_at is null;

update public.pipeline_runs
  set state_entered_at = coalesce(finished_at, started_at, now())
  where state_entered_at is null;

update public.profiles
  set onboarding_state_entered_at = coalesce(onboarding_started_at, created_at, now())
  where onboarding_state_entered_at is null
    and onboarding_status is not null;

-- Enforce NOT NULL after backfill
alter table public.emails
  alter column state_entered_at set not null,
  alter column state_entered_at set default now();

alter table public.drafts
  alter column state_entered_at set not null,
  alter column state_entered_at set default now();

alter table public.pipeline_runs
  alter column state_entered_at set not null,
  alter column state_entered_at set default now();
```

### 6.4 Trigger: auto-update `state_entered_at` on status change

**Migration: `supabase/migrations/038_state_entered_at_triggers.sql`**

```sql
-- Generic trigger that updates state_entered_at and maintains failure_count
-- whenever status changes. Keeping failure_count in the trigger (not a helper)
-- means every code path writing status=failed gets counted correctly.
create or replace function public.bump_state_entered_at()
returns trigger
language plpgsql
as $$
begin
  if new.status is distinct from old.status then
    new.state_entered_at := now();
    -- Increment failure_count on any transition INTO failed (from any other state)
    if new.status = 'failed' and old.status is distinct from 'failed' then
      new.failure_count := coalesce(old.failure_count, 0) + 1;
    end if;
    -- Reset failure_count when transitioning away from failed to a non-failed state
    if old.status = 'failed' and new.status <> 'failed' then
      new.failure_count := 0;
    end if;
  end if;
  return new;
end;
$$;

create trigger emails_bump_state
  before update on public.emails
  for each row
  execute function public.bump_state_entered_at();

create trigger drafts_bump_state
  before update on public.drafts
  for each row
  execute function public.bump_state_entered_at();

create trigger pipeline_runs_bump_state
  before update on public.pipeline_runs
  for each row
  execute function public.bump_state_entered_at();

-- Profiles trigger: tracks onboarding_status on profiles
create or replace function public.bump_onboarding_state_entered_at()
returns trigger
language plpgsql
as $$
begin
  if new.onboarding_status is distinct from old.onboarding_status then
    new.onboarding_state_entered_at := now();
    if new.onboarding_status = 'failed' and old.onboarding_status is distinct from 'failed' then
      new.onboarding_failure_count := coalesce(old.onboarding_failure_count, 0) + 1;
    end if;
    if old.onboarding_status = 'failed' and new.onboarding_status <> 'failed' then
      new.onboarding_failure_count := 0;
    end if;
  end if;
  if new.onboarding_stage is distinct from old.onboarding_stage then
    new.onboarding_stage_entered_at := now();
  end if;
  return new;
end;
$$;

create trigger profiles_bump_onboarding_state
  before update on public.profiles
  for each row
  execute function public.bump_onboarding_state_entered_at();
```

### 6.5 Indexes on new columns

**Migration: `supabase/migrations/039_state_indexes.sql`**

```sql
-- Reaper query support
create index if not exists idx_emails_state_entered
  on public.emails (status, state_entered_at)
  where status in ('pending', 'active', 'processing', 'unprocessed');  -- legacy + new

create index if not exists idx_drafts_state_entered
  on public.drafts (status, state_entered_at)
  where status in ('pending', 'active');

create index if not exists idx_pipeline_runs_state_entered
  on public.pipeline_runs (status, state_entered_at)
  where status in ('pending', 'active', 'running');

create index if not exists idx_profiles_onboarding_state_entered
  on public.profiles (onboarding_status, onboarding_state_entered_at)
  where onboarding_status not in ('complete', 'complete_partial', 'failed', 'done', 'skipped')
    or onboarding_status is null;
```

### 6.6 Phase 1 Deploy

1. Apply migrations 037, 038, 039 (sequential)
2. Deploy worker with constants refactor
3. Deploy extension with constants refactor
4. Deploy web with constants refactor

**Verification:**
- `select count(*) from emails where state_entered_at is null;` returns 0
- `grep -r '"unprocessed"\|"processing"\|"processed"\|"written"' worker/ extension/ web/` returns near-zero (comments and legacy constants excepted)
- Worker still functional (no behavior change)

**Rollback:** Drop the new columns and triggers. Revert code (trivial since no value changes).

---

## 7. Phase 2 — Dual-Read Compatibility

**Goal:** Every reader accepts both old and new vocabulary. **Writers still emit old values.**

### 7.1 Query helpers

Add helper functions that encapsulate the OR-matching of legacy and new values.

**In `worker/supabase_client.py`:**

```python
def _email_pending_filter(query):
    """emails in pending state (legacy: unprocessed; new: pending)"""
    return query.in_("status", ["unprocessed", "pending"])

def _email_active_filter(query):
    return query.in_("status", ["processing", "active"])

def _email_done_filter(query):
    return query.in_("status", ["processed", "done"])

def _email_failed_filter(query):
    return query.in_("status", ["failed", "error"])

def _draft_pending_filter(query):
    return query.in_("status", ["pending"])

def _draft_done_filter(query):
    return query.in_("status", ["written", "done"])
```

Replace all `.eq("status", X)` and `.in_("status", [...])` calls with these helpers.

### 7.2 RPC dual-read

`claim_unprocessed_emails` RPC needs updating to accept either value.

> ⚠ **Preserve full RPC body.** `CREATE OR REPLACE FUNCTION` replaces the entire body, not a diff. Extract current production definition with `pg_dump --schema-only --function public.claim_unprocessed_emails` before modifying. The abbreviated snippet below shows only the status-filter change; the rest of the function (return column list, ordering, existing logic) must be copied verbatim.

**Migration: `supabase/migrations/040_rpc_dual_read.sql`**

```sql
create or replace function public.claim_unprocessed_emails(
  p_user_id uuid,
  p_limit int
)
returns table (
  id bigint,
  -- ... existing columns
)
language plpgsql
security definer
as $$
begin
  return query
  with claimed as (
    update public.emails e
    set status = 'processing',  -- still write legacy value in Phase 2
        state_entered_at = now()
    where e.id in (
      select id from public.emails
      where user_id = p_user_id
        and status in ('unprocessed', 'pending')  -- DUAL-READ
      order by received_at asc
      limit p_limit
      for update skip locked
    )
    returning e.*
  )
  select * from claimed;
end;
$$;
```

`find_stale_drafts`:

```sql
create or replace function public.find_stale_drafts(...)
...
where d.status in ('written', 'pending', 'done');  -- DUAL-READ
...
```

### 7.3 Extension realtime filter

**`extension/supabase-realtime.js:302`** — The REST query filter must match *both* values. Since `status=eq.X` only supports one value, switch to either:

- **Option A:** Use `in` filter: `status=in.(pending)` — but during Phase 2 `pending` already matches both legacy drafts (current value) and will match new (same string). For drafts, `pending` is stable across both.
- **Option B:** Subscribe without status filter and filter client-side.

For drafts, `pending` is unchanged, so the existing filter continues to work. No change needed in Phase 2.

For other tables where the pending value is changing (emails: `unprocessed` → `pending`), if the extension subscribes to them (it doesn't currently), dual-read would need `status=in.(unprocessed,pending)`.

### 7.4 Web dashboard dual-read

**`web/js/pages/history.js:49-58`:**

```javascript
function renderRunStatus(run) {
  const status = run.status;
  if (status === "completed" || status === "done") {
    return run.has_partial_failures ? "Partial" : "Completed";
  }
  if (status === "partial_failure") return "Partial";  // legacy
  if (status === "failed") return "Failed";
  if (status === "running" || status === "active") return "Running";
  return status;
}
```

**`web/js/components/trace-renderers.js`:**

```javascript
// Replace: email.status === "unprocessed"
// With:    ["unprocessed", "pending"].includes(email.status)

// Replace: email.status === "processing" || email.status === "unprocessed"
// With:    ["processing", "active", "unprocessed", "pending"].includes(email.status)
```

**`web/js/pages/emails.js`:** Same treatment.

### 7.5 Phase 2 Deploy

1. Apply migration 040
2. Deploy worker (readers accept both)
3. Deploy web (readers accept both)
4. Extension: no change required (drafts `pending` is stable)

**Verification:**
- Worker continues to pick up `status='unprocessed'` emails
- Dashboard continues to render correctly
- No new behavior

**Rollback:** Revert code. Migration 040 is backward-compatible (the dual-read RPC still picks up legacy rows).

---

## 8. Phase 3 — Data Migration

**Goal:** Rewrite all existing rows to new vocabulary in a single transaction.

**⚠ DANGER ZONE — REQUIRES DEPLOYMENT WINDOW**

### 8.1 Pre-flight for Phase 3

Before running:

**Stability and verification:**
- [ ] Confirm Phase 1 & 2 deployed and stable for at least 24 hours
- [ ] Confirm no code reads a status value exclusively in its legacy form (see Grep Audit Checklist below)
- [ ] Full dry-run of migration 041 against a staging DB cloned from production (`supabase db dump --data-only` → restore → run migration → validate). Catches trigger/schema/RPC issues cheaply.

**Window preparation:**
- [ ] Schedule deployment window (low-traffic, e.g., Friday evening)
- [ ] Take DB snapshot via Supabase dashboard
- [ ] Announce brief maintenance window if any user-facing impact expected

**Pre-migration data prep (run against production before stopping worker):**
- [ ] Reset in-flight emails so they re-claim cleanly post-migration:
  ```sql
  -- emails has no updated_at column; use state_entered_at from Phase 1.
  -- Phase 1 backfills state_entered_at from created_at, then the trigger
  -- maintains it on every status change. Rows stuck in 'processing' for
  -- >5 min are safe to reset without racing the worker's current batch.
  update public.emails set status = 'unprocessed'
    where status = 'processing'
      and state_entered_at < now() - interval '5 minutes';
  ```

**Stop the world:**
- [ ] Stop Railway worker (`railway down` or scale to 0). Confirm no active pipeline runs.
- [ ] Drain realtime subscriptions to prevent NOTIFY spam on bulk UPDATE. Either:
  - Drop the affected tables from the publication temporarily:
    ```sql
    alter publication supabase_realtime drop table public.emails;
    alter publication supabase_realtime drop table public.drafts;
    alter publication supabase_realtime drop table public.pipeline_runs;
    alter publication supabase_realtime drop table public.profiles;
    ```
  - Or pause the `realtime` Supabase service from the dashboard.
  - (Re-enable after migration in the post-migration step.)

**Grep Audit Checklist** (run before scheduling window):
- [ ] `grep -rn '"unprocessed"\|"processing"\|"processed"\|"onboarding"\|"error"' worker/ extension/ web/` — should return only constants file after Phase 1
- [ ] `grep -rn '"written"\|"obsolete"\|"edited"\|"sent"' extension/ web/ worker/` — audit every drafts.status consumer
- [ ] `grep -rn '"running"\|"partial_failure"\|"completed"' worker/ web/` — audit pipeline_runs consumers
- [ ] `grep -rn '"collecting"\|"statistics"\|"persisting"\|"extracting"\|"synthesizing"\|"style_guide"\|"training"\|"complete"\|"complete_partial"\|"starting"' worker/ web/` — onboarding substages
- [ ] `grep -rn 'pipeline_stage' worker/ web/ extension/` — every reader/writer, ensure all removed or derived
- [ ] `grep -rn 'status=eq\.\|status=in\.' extension/` — every realtime filter, confirm dual-read compatible
- [ ] `grep -rn '\.eq("status"\|\.in_("status"' worker/` — every Python filter call
- [ ] `grep -rn 'delivery_state\|has_partial_failures\|deferred_reason' web/` — confirm consumers updated where legacy status meanings split into new columns
- [ ] `grep -rn '\.status ===\|\.status ==\|\.status !==\|\.status !=' web/ extension/` — every JS equality check

### 8.2 Migration: rewrite data

**Migration: `supabase/migrations/041_unify_status_vocabulary.sql`**

> ⚠ **Critical:** This migration must disable the `bump_state_entered_at` trigger while running. Without this, every bulk UPDATE would reset `state_entered_at` to `now()` on every row, destroying the Phase 1 backfill and causing the reaper (once deployed) to see every row as "just entered its state" simultaneously.

> ⚠ **RPC bodies in this migration are shown in abbreviated form (`...`) for readability. When writing the actual migration, use `pg_dump --schema-only --function claim_unprocessed_emails` (etc.) to extract the current production RPC definitions verbatim, then modify only the status-filter clauses. A partial `CREATE OR REPLACE FUNCTION` silently truncates the function body.**

```sql
begin;

-- Fail fast if the migration can't make progress. Without these, a contended
-- lock could pin the transaction open for hours before we notice.
set local statement_timeout = '10min';
set local lock_timeout = '30s';

-- Disable bump_state_entered_at triggers for the duration of this migration.
-- Without this, every row's state_entered_at gets overwritten to now(),
-- destroying the diagnostic substrate we set up in Phase 1.
alter table public.emails disable trigger emails_bump_state;
alter table public.drafts disable trigger drafts_bump_state;
alter table public.pipeline_runs disable trigger pipeline_runs_bump_state;
alter table public.profiles disable trigger profiles_bump_onboarding_state;

-- Wipe legacy failure_count values. The trigger didn't maintain these before
-- this phase, so whatever is in the columns is unreliable. Start from 0 and
-- let the new trigger be the sole writer going forward. Cheaper and less
-- error-prone than widening the trigger to infer counts from legacy values.
update public.emails set failure_count = 0;
update public.drafts set failure_count = 0;
update public.pipeline_runs set failure_count = 0;
update public.profiles set onboarding_failure_count = 0
  where onboarding_failure_count is not null;

-- ===== EMAILS =====
-- Actual legacy values observed in production (v4 reconciliation):
--   unprocessed, processing, processed, onboarding, error, completed, dismissed
-- 'failed' is NOT written by any current code path, so no remap needed for it.

-- Reset any stragglers still in 'processing' (worker should have been stopped
-- before this migration; pre-flight step resets most, but catch any that
-- arrived between pre-flight and worker stop).
update public.emails set status = 'unprocessed' where status = 'processing';

-- Preserve 'onboarding' via deferred_reason
update public.emails
  set deferred_reason = 'onboarding',
      status = 'skipped'
  where status = 'onboarding';

-- Preserve dashboard 'dismissed' user action via deferred_reason
update public.emails
  set deferred_reason = 'user_dismissed',
      status = 'skipped'
  where status = 'dismissed';

-- Standard vocabulary mapping
update public.emails set status = 'pending' where status = 'unprocessed';
-- (no rows should have status='processing' at this point)
-- Both worker-written 'processed' and dashboard-written 'completed' collapse to 'done'.
update public.emails set status = 'done' where status in ('processed', 'completed');
update public.emails set status = 'failed' where status = 'error';

-- Validation
do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.emails
  where status not in ('pending', 'active', 'done', 'failed', 'skipped');
  if bad_count > 0 then
    raise exception 'Found % emails with invalid status after migration', bad_count;
  end if;
end $$;

-- ===== DRAFTS =====
-- Actual legacy values observed in production (v4 reconciliation):
--   pending, written, deleted
-- 'sent', 'edited', 'obsolete', 'failed' are NOT written by any current code path.
-- The 'deleted' status is always accompanied by draft_deleted=true;
-- this migration subsumes that boolean into delivery_state='user_deleted'.

-- 'written' → done + delivered
update public.drafts set delivery_state = 'delivered', status = 'done'
  where status = 'written';

-- 'deleted' → skipped + user_deleted (retires the draft_deleted boolean)
update public.drafts set delivery_state = 'user_deleted', status = 'skipped'
  where status = 'deleted';

-- 'pending' is unchanged in name; ensure delivery_state default for any row
-- that lacks one (pending rows are not_delivered until the extension writes).
update public.drafts
  set delivery_state = coalesce(delivery_state, 'not_delivered');

-- Validation
do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.drafts
  where status not in ('pending', 'active', 'done', 'failed', 'skipped');
  if bad_count > 0 then
    raise exception 'Found % drafts with invalid status after migration', bad_count;
  end if;
end $$;

-- ===== PIPELINE_RUNS =====

update public.pipeline_runs set status = 'active' where status = 'running';
update public.pipeline_runs set has_partial_failures = true, status = 'done'
  where status = 'partial_failure';
update public.pipeline_runs set status = 'done' where status = 'completed';
-- 'failed' unchanged

do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.pipeline_runs
  where status not in ('pending', 'active', 'done', 'failed');
  if bad_count > 0 then
    raise exception 'Found % pipeline_runs with invalid status after migration', bad_count;
  end if;
end $$;

-- ===== PROFILES (ONBOARDING) =====   [DEFERRED — iteration 2+]
-- The block below remaps profiles.onboarding_status into the unified
-- vocabulary (scope option C). It is DEFERRED for iteration 1 (drafts-only
-- reaper). DO NOT include this block in migration 041 for this iteration.
-- _recover_stuck_onboarding continues to own onboarding stuck-detection
-- until a later iteration.
--
-- Kept here verbatim for the next iteration; wrap in /* ... */ or delete
-- when copying this migration body for iteration 1.
/*
-- Preserve substage in onboarding_stage
update public.profiles
  set onboarding_stage = onboarding_status
  where onboarding_status in (
    'starting', 'collecting', 'statistics', 'persisting',
    'extracting', 'synthesizing', 'style_guide', 'training'
  );

-- Map to unified vocabulary
update public.profiles set onboarding_status = 'active'
  where onboarding_status in (
    'starting', 'collecting', 'statistics', 'persisting',
    'extracting', 'synthesizing', 'style_guide', 'training'
  );

-- Partial-failure onboarding: encode in onboarding_stage instead of a boolean.
-- Avoids adding a `has_partial_failures` column to profiles.
update public.profiles
  set onboarding_stage = 'completed_with_partial_failures',
      onboarding_status = 'done'
  where onboarding_status = 'complete_partial';

update public.profiles set onboarding_status = 'done' where onboarding_status = 'complete';
-- 'pending' and 'failed' unchanged
-- NULL unchanged (means onboarding not yet started)

do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.profiles
  where onboarding_status is not null
    and onboarding_status not in ('pending', 'active', 'done', 'failed', 'skipped');
  if bad_count > 0 then
    raise exception 'Found % profiles with invalid onboarding_status', bad_count;
  end if;
end $$;
*/
-- End DEFERRED onboarding block.

-- ===== UPDATE RPC TO NEW VOCABULARY =====

create or replace function public.claim_unprocessed_emails(
  p_user_id uuid,
  p_limit int
)
returns table (...)
language plpgsql
security definer
as $$
begin
  return query
  with claimed as (
    update public.emails e
    set status = 'active',
        state_entered_at = now()
    where e.id in (
      select id from public.emails
      where user_id = p_user_id
        and status = 'pending'
      order by received_at asc
      limit p_limit
      for update skip locked
    )
    returning e.*
  )
  select * from claimed;
end;
$$;

create or replace function public.find_stale_drafts(...)
...
where d.status in ('pending', 'done')
  and d.delivery_state in ('not_delivered', 'delivered');

-- Re-enable triggers now that bulk updates are complete
alter table public.emails enable trigger emails_bump_state;
alter table public.drafts enable trigger drafts_bump_state;
alter table public.pipeline_runs enable trigger pipeline_runs_bump_state;
alter table public.profiles enable trigger profiles_bump_onboarding_state;

commit;

-- AFTER the transaction commits, re-add tables to the realtime publication
-- (must be outside the transaction; publication DDL cannot be transactional
-- together with DML on the same tables in all cases):
-- alter publication supabase_realtime add table public.emails;
-- alter publication supabase_realtime add table public.drafts;
-- alter publication supabase_realtime add table public.pipeline_runs;
-- alter publication supabase_realtime add table public.profiles;
```

### 8.3 Post-migration verification

```sql
-- No legacy values remain
select status, count(*) from public.emails group by status;
-- Should show only: pending, active, done, failed, skipped

select status, count(*) from public.drafts group by status;
select status, count(*) from public.pipeline_runs group by status;
select onboarding_status, count(*) from public.profiles group by onboarding_status;

-- deferred_reason populated correctly
select deferred_reason, count(*) from public.emails
  where status = 'skipped' group by deferred_reason;

-- delivery_state populated correctly
select delivery_state, count(*) from public.drafts group by delivery_state;
```

### 8.4 Restart worker

```bash
railway up
# Confirm worker picks up pending emails under new vocabulary
```

### 8.5 Rollback plan for Phase 3

**Reverse migration: `supabase/rollbacks/041_unify_status_vocabulary_rollback.sql`** (kept OUTSIDE the `supabase/migrations/` directory so Supabase CLI does not auto-apply it. Only applied manually via `supabase db execute --file <path>` if rollback is triggered.)

```sql
-- Only run if Phase 3 needs reverting
begin;

-- Emails: restore legacy vocabulary (v4: covers actual production values)
update public.emails set status = 'onboarding'
  where status = 'skipped' and deferred_reason = 'onboarding';
update public.emails set status = 'dismissed'
  where status = 'skipped' and deferred_reason = 'user_dismissed';
update public.emails set status = 'unprocessed' where status = 'pending';
update public.emails set status = 'processing' where status = 'active';
-- Forward migration collapsed worker 'processed' + dashboard 'completed' → 'done'.
-- Choose the safer legacy restore ('processed'); the dashboard treats both
-- as terminal-success anyway. Flag if stricter fidelity is needed.
update public.emails set status = 'processed' where status = 'done';
update public.emails set status = 'error' where status = 'failed';

-- Drafts: restore (v4: only 'written' and 'deleted' ever existed)
update public.drafts set status = 'written'
  where status = 'done' and delivery_state = 'delivered';
update public.drafts set status = 'deleted', draft_deleted = true
  where status = 'skipped' and delivery_state = 'user_deleted';

-- Pipeline_runs
update public.pipeline_runs set status = 'running' where status = 'active';
update public.pipeline_runs set status = 'partial_failure' where status = 'done' and has_partial_failures;
update public.pipeline_runs set status = 'completed' where status = 'done';

-- Profiles
update public.profiles set onboarding_status = 'complete_partial'
  where onboarding_status = 'done'
    and onboarding_stage = 'completed_with_partial_failures';
update public.profiles set onboarding_status = onboarding_stage
  where onboarding_status = 'active' and onboarding_stage is not null
    and onboarding_stage <> 'completed_with_partial_failures';
update public.profiles set onboarding_status = 'complete' where onboarding_status = 'done';

-- Restore original RPC bodies (copy from original migrations)

commit;
```

**Trigger for rollback:** Unexpected worker errors, UI failures, data corruption detected in first hour after deploy.

---

## 9. Phase 4 — Writer Flip

**Goal:** All writers emit only new vocabulary. Legacy values never produced again.

### 9.1 Worker writer updates

Replace all `LegacyEmailStatus.X` references with `Status.X`.

**Example:** `worker/run_pipeline.py:331`
```python
# Before (Phase 2)
.update_email_status(db_id, LegacyEmailStatus.PROCESSED)

# After (Phase 4)
.update_email_status(db_id, Status.DONE)
```

**Full file list:** see Appendix A — same files as Phase 1 but updating values now.

### 9.2 Onboarding runner

`worker/onboarding/runner.py` writes status at 12 points. Each becomes:

```python
# Before
db.update_onboarding_status(user_id, "collecting")

# After
db.update_onboarding_status(
  user_id,
  status=Status.ACTIVE,
  stage="collecting",
)
```

Requires extending `update_onboarding_status()` to accept `stage` parameter:

```python
def update_onboarding_status(self, user_id, status, stage=None, **kwargs):
    payload = {"onboarding_status": status}
    if stage is not None:
        payload["onboarding_stage"] = stage
    payload.update(kwargs)
    ...
```

### 9.2b Remove `_recover_stuck_onboarding`   `[DEFERRED — iteration 2+]`

Deferred to the iteration that brings onboarding into the unified model
(scope option C). Until then, `_recover_stuck_onboarding` remains the sole
onboarding stuck-detection path. The drafts reaper does not touch
`profiles.onboarding_status`.

Once the unified reaper (Section 14) becomes the sole stuck-detection path
for onboarding, the per-subsystem recovery function in
`worker/onboarding/runner.py` (`_recover_stuck_onboarding`) becomes a
duplicate driver that can fight the reaper over the same rows.

```python
# worker/onboarding/runner.py
# DELETE: _recover_stuck_onboarding(...)
# Superseded by the unified reaper in worker/reaper.py.
# Onboarding stuck-detection now flows through the same
# state_entered_at + failure_count path as every other entity type.
```

Search for any caller and replace with a no-op (the reaper will pick up stuck
`profiles` rows via `onboarding_state_entered_at`).

### 9.3 Retire `pipeline_stage`   `[DEFERRED — iteration 2+]`

Remove all `db.set_pipeline_stage(...)` calls from worker:
- `worker/main.py:324, 335`
- `worker/run_pipeline.py:901, 1327`
- `worker/supabase_client.py:470-479` (the method itself)

**Migration: `supabase/migrations/042_drop_pipeline_stage.sql`**

```sql
alter table public.profiles drop column if exists pipeline_stage;
```

**Pre-check:** Grep `web/` for any consumer. If found, update those to derive from `pipeline_runs.status` instead:

```javascript
// Before: profile.pipeline_stage === "drafting"
// After:  latestPipelineRun?.status === "active"
```

**Also check realtime and DB-side consumers** before dropping the column:

```sql
-- Is the column still in the realtime publication?
select pubname, schemaname, tablename, attnames
  from pg_publication_tables
  where schemaname = 'public'
    and tablename = 'profiles';
-- If attnames is NULL the publication tracks all columns (includes pipeline_stage).
-- If attnames lists columns explicitly, check whether pipeline_stage is in the list.

-- Any trigger, view, function, or RLS policy referencing the column?
select 'view' as kind, schemaname||'.'||viewname as name from pg_views
  where definition ilike '%pipeline_stage%'
union all
select 'function', n.nspname||'.'||p.proname
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
  where pg_get_functiondef(p.oid) ilike '%pipeline_stage%'
union all
select 'policy', schemaname||'.'||tablename||'.'||policyname from pg_policies
  where qual::text ilike '%pipeline_stage%' or with_check::text ilike '%pipeline_stage%';
```

If the realtime publication explicitly lists columns, update it before
dropping the column so the publication doesn't error out:

```sql
alter publication supabase_realtime
  set table public.profiles (id, user_id, onboarding_status, onboarding_stage /* …other columns, minus pipeline_stage… */);
```

### 9.4 Extension writer updates

`extension/supabase-realtime.js:355`:
```javascript
// Before
if (draft.status === "written") { ... }
await updateDraftStatus(draftId, "written", ...)

// After
if (draft.delivery_state === DeliveryState.DELIVERED) { ... }
await updateDraftStatus(draftId, Status.DONE, DeliveryState.DELIVERED, ...)
```

Schema for `updateDraftStatus` needs expanding to write both columns atomically.

### 9.5 Web dashboard writer updates

`web/js/pages/emails.js:590, 725`:
```javascript
// Before
.update({ status: "unprocessed" })
.update({ status: "processed" })

// After
.update({ status: Status.PENDING })
.update({ status: Status.DONE })
```

### 9.6 Phase 4 Deploy

1. Deploy worker
2. Deploy extension (coordinate with Chrome Web Store release)
3. Deploy web
4. Apply migration 042 (drop `pipeline_stage`)

**Verification:**
- No new rows with legacy status values
- `select status, count(*) from emails where updated_at > now() - interval '1 hour' group by status;` shows only new vocabulary

**Rollback:** Revert code. DB still has Phase 2 dual-read compat so legacy reads still work.

---

## 10. Phase 5 — Compat Removal

**Goal:** Remove dual-read branches, add CHECK constraints.

**⚠ Timing gate — Chrome Web Store lag:** Phase 5 must NOT begin until the legacy-version tail of the extension is confirmed small. CWS review can take 3–7 days and stretch to 2+ weeks. Auto-updates are staggered, so users can lag further. Remove dual-read too early and older-extension users break silently.

### 10.0 Extension version telemetry (prerequisite for Phase 5)

Added in Phase 4 extension release. Every write the extension makes includes its version:

```javascript
// extension/background.js
const EXTENSION_VERSION = chrome.runtime.getManifest().version;

await supabase.from("drafts").update({
  status: Status.DONE,
  delivery_state: DeliveryState.DELIVERED,
  written_by_extension_version: EXTENSION_VERSION,  // NEW
}).eq("id", draftId);
```

Add column in a minor migration alongside Phase 4:

```sql
alter table public.drafts
  add column if not exists written_by_extension_version text;
```

Monitoring query before scheduling Phase 5:

```sql
-- Share of drafts written in the last 7 days by extension version
select
  written_by_extension_version,
  count(*) as rows,
  round(100.0 * count(*) / sum(count(*)) over (), 2) as pct
from public.drafts
where updated_at > now() - interval '7 days'
  and written_by_extension_version is not null
group by 1
order by 2 desc;
```

**Gate:** Do not proceed to Phase 5 until the most recent (Phase 4+) extension version accounts for > 99% of activity over a rolling 7-day window, *and* no draft has been written by a pre-Phase-4 version for at least 14 days.



### 10.1 Remove legacy constants from code

Delete `LegacyEmailStatus`, `LegacyDraftStatus`, etc. from `worker/status.py`.

Remove all `_email_pending_filter`-style dual-read helpers; replace with direct `.eq("status", Status.PENDING)`.

### 10.2 Remove dual-read from web dashboard

```javascript
// Before
["unprocessed", "pending"].includes(email.status)

// After
email.status === Status.PENDING
```

### 10.3 Add CHECK constraints

**Migration: `supabase/migrations/043_status_check_constraints.sql`**

```sql
alter table public.emails
  add constraint emails_status_check
    check (status in ('pending', 'active', 'done', 'failed', 'skipped'));

alter table public.drafts
  add constraint drafts_status_check
    check (status in ('pending', 'active', 'done', 'failed', 'skipped'));

alter table public.drafts
  add constraint drafts_delivery_state_check
    check (delivery_state in (
      'not_delivered', 'delivered', 'user_deleted',
      'edited_in_outlook', 'sent', 'stale'
    ));
-- 'user_deleted' subsumes the legacy draft_deleted boolean (Decision 1b).
-- 'edited_in_outlook', 'sent', 'stale' are reserved for iteration 2+
-- (delivery outcome detection) and have no writers in iteration 1.

alter table public.pipeline_runs
  add constraint pipeline_runs_status_check
    check (status in ('pending', 'active', 'done', 'failed'));

-- [DEFERRED — iteration 2+] profiles.onboarding_status CHECK constraint.
-- Not added in iteration 1 because onboarding still uses the 12-state
-- legacy vocabulary. Add this in the iteration that ships scope option C.
-- alter table public.profiles
--   add constraint profiles_onboarding_status_check
--     check (onboarding_status is null
--       or onboarding_status in ('pending', 'active', 'done', 'failed', 'skipped'));
```

### 10.4 Clean up indexes

Drop the Phase 1 transitional indexes that included legacy values, recreate with tightened predicates.

**Must run OUTSIDE a transaction** — `CREATE INDEX CONCURRENTLY` cannot be in a transaction, and the dropped index leaves the table unindexed briefly. Put each statement in its own migration file or use `supabase db execute` individually.

```sql
-- File: supabase/migrations/043a_drop_transitional_indexes.sql
drop index concurrently if exists idx_emails_state_entered;
drop index concurrently if exists idx_drafts_state_entered;
drop index concurrently if exists idx_pipeline_runs_state_entered;
drop index concurrently if exists idx_profiles_onboarding_state_entered;
```

```sql
-- File: supabase/migrations/043b_recreate_state_indexes.sql
create index concurrently idx_emails_state_entered
  on public.emails (status, state_entered_at)
  where status in ('pending', 'active');

create index concurrently idx_drafts_state_entered
  on public.drafts (status, state_entered_at)
  where status in ('pending', 'active');

create index concurrently idx_pipeline_runs_state_entered
  on public.pipeline_runs (status, state_entered_at)
  where status in ('pending', 'active');

create index concurrently idx_profiles_onboarding_state_entered
  on public.profiles (onboarding_status, onboarding_state_entered_at)
  where onboarding_status in ('pending', 'active');
```

Note: Supabase CLI migration runner defaults to wrapping each file in a transaction. Either disable that behavior for these files, or run them manually via dashboard SQL editor.

### 10.5 Phase 5 Deploy

Deploy code + migration.

**Verification:** CHECK constraint violation on any attempt to write legacy values.

**Rollback:** Drop constraints, restore legacy constants and dual-read helpers.

---

## 11. Phase 6 — Extension State Persistence

**Independent of DB refactor. Can ship at any time after Phase 1.**

### 11.1 Variables to migrate

In `extension/background.js`, currently in-memory:

| Variable | Line | Current scope | Target storage |
|---|---|---|---|
| `isSyncing` | ~42 | Module-global | `chrome.storage.session`, key `syncing:{userId}` |
| `hasCompletedFolderSync` | ~45 | Module-global | `chrome.storage.session`, key `folderSyncComplete:{userId}` |
| `outlookTabId` | ~47 | Module-global | `chrome.storage.session`, key `outlookTabId` (global) |
| `currentUserId` | ~48 | Module-global | `chrome.storage.session`, key `currentUserId` |

Already persisted (no change):
- `token` → `chrome.storage.session` key `exchangeToken`
- Per-user `lastSyncTime, foldersCache, connectedOutlookEmail` → `chrome.storage.local` key `sync:{userId}`

### 11.2 Implementation pattern

```javascript
// Thin wrappers that read/write session storage
async function getSyncLock(userId) {
  const key = `syncing:${userId}`;
  const data = await chrome.storage.session.get(key);
  return data[key] === true;
}

async function setSyncLock(userId, value) {
  const key = `syncing:${userId}`;
  if (value) await chrome.storage.session.set({ [key]: true });
  else await chrome.storage.session.remove(key);
}

async function clearStaleSyncLocks() {
  // Call on SW startup — any sync lock older than X minutes is stale
  const all = await chrome.storage.session.get(null);
  const staleCutoff = Date.now() - 5 * 60 * 1000;
  const keysToRemove = [];
  for (const [k, v] of Object.entries(all)) {
    if (k.startsWith("syncing:") && v.timestamp < staleCutoff) {
      keysToRemove.push(k);
    }
  }
  if (keysToRemove.length) await chrome.storage.session.remove(keysToRemove);
}
```

Store as `{value: true, timestamp: Date.now()}` rather than bare booleans, so stale-lock detection works.

### 11.3 Call site updates

Every existing `if (isSyncing) return` becomes `if (await getSyncLock(currentUserId)) return`.
Every `isSyncing = true` becomes `await setSyncLock(currentUserId, true)`.

**Risk:** Async reads introduce race conditions that synchronous in-memory globals didn't have. Mitigate by:
- Reading lock, checking, and setting lock in one function (not across an `await` boundary where possible)
- Using an atomic compare-and-swap if Chrome supports it (it doesn't natively — workaround: use a serialization pattern via a single async queue)

### 11.4 SW startup restoration

On service worker activation:

```javascript
chrome.runtime.onStartup.addListener(async () => {
  await clearStaleSyncLocks();
  await restoreToken();
  // Other init
});

// Also on install
chrome.runtime.onInstalled.addListener(async () => {
  await clearStaleSyncLocks();
});
```

### 11.5 Phase 6 Deploy

Extension release only. No DB changes.

**Rollback:** Revert code.

---

## 12. Risk Register & Rollback Plans

| Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|
| Worker hardcoded string literal missed in refactor | Medium | Medium | Grep validation (Section 8.1); dual-read Phase 2 catches it |
| Extension realtime filter breaks after migration | High | Critical (drafts stop pushing) | Drafts `pending` is stable across migration; verify. For any other table, use dual-read filters. |
| RPC signature break during Phase 3 | Medium | High | Use `pg_dump --schema-only --function` to preserve full body; verify worker restarts cleanly |
| Onboarding substage info lost | Low | Medium | `onboarding_stage` column preserves it; backfill in Phase 3 |
| `pipeline_stage` drop breaks frontend | Low | Medium | Pre-check: grep `web/` for readers before Phase 4 |
| Existing invalid status rows in DB | Low | Medium | Audit query in Phase 1; reject migration if found |
| Phase 3 migration exceeds lock timeout | Low | High | Staging dry-run (Section 8.1) validates timing; run in staged batches if table > 1M rows |
| Worker polling during Phase 3 creates partial state | High | High | Pre-flight reset `processing→unprocessed`, then stop worker before migration |
| Extension state persistence introduces race condition | Medium | Medium | Serialize access via async queue; comprehensive testing before release |
| Chrome Web Store release delay blocks Phase 5 | High | Medium | Extension version telemetry gates Phase 5; expect 2+ week window between Phase 4 and Phase 5 |
| Bulk UPDATE triggers reset `state_entered_at` during Phase 3 | Certain if unmitigated | Critical (reaper substrate destroyed) | Disable triggers in migration 041 transaction; re-enable before commit |
| Realtime NOTIFY spam during Phase 3 bulk UPDATE | High | Medium | Drop tables from `supabase_realtime` publication before migration; re-add after |
| Direct `status='failed'` writes miss `failure_count` increment | Medium | Medium | Increment moved into trigger; helper no longer correctness-critical |
| `profiles.has_partial_failures` schema mismatch | Was present in v1; eliminated in v2 | — | Substage now encodes partial-failure state |
| Migration number collision (041 + 041_rollback) | Certain if unmitigated | Medium | Rollback moved to `supabase/rollbacks/` directory |
| Orphaned `active` emails post-migration (no worker polling them) | Certain if unmitigated | High | Pre-flight resets `processing→unprocessed`; migration 041 re-asserts as safety net |

### Rollback decision tree

```
Phase 1/2/4/5/6 issue detected
  → Revert code deploy
  → No DB action needed (schema is additive/compat)

Phase 3 issue detected within first hour
  → Run rollback migration 041_rollback.sql
  → Revert worker to Phase 2 state
  → Investigate

Phase 3 issue detected after 24h+
  → Data corruption risk — DO NOT roll back blindly
  → Triage: fix forward with targeted update
  → Restore from snapshot if corruption is systemic
```

---

## 13. Testing Strategy

### 13.1 Phase 1 tests

- [ ] Migrations apply cleanly to dev DB
- [ ] `state_entered_at` is populated for all existing rows
- [ ] Trigger fires on status update
- [ ] No hardcoded status literals remain (grep audit)
- [ ] Worker runs unchanged (smoke test: complete one email cycle)

### 13.2 Phase 2 tests

- [ ] Worker can read legacy `unprocessed` rows via dual-read filter
- [ ] Dashboard renders both legacy and new status values correctly
- [ ] RPC `claim_unprocessed_emails` claims rows with status IN (unprocessed, pending)

### 13.3 Phase 3 tests (staging first)

- [ ] Migration runs in < 60s on staging DB
- [ ] All validation DO blocks pass
- [ ] Post-migration counts match pre-migration (no data loss)
- [ ] Worker restarts and claims pending emails successfully
- [ ] Extension pushes drafts under new vocabulary
- [ ] Dashboard renders all existing data
- [ ] Onboarding substage preserved for active users

### 13.4 Phase 4 tests

- [ ] No new rows with legacy status values (monitor for 24h)
- [ ] Extension writes `status=done` + `delivery_state=delivered` on draft push
- [ ] Pipeline runs complete with `status=done` and `has_partial_failures=true/false`

### 13.5 Phase 5 tests

- [ ] CHECK constraints reject legacy values
- [ ] All legacy constants removed from codebase

### 13.6 Phase 6 tests

- [ ] Kill SW mid-sync; restart; confirm sync lock released after 5m
- [ ] Two rapid sync triggers don't double-execute
- [ ] Token survives SW restart
- [ ] `outlookTabId` restored correctly on SW startup

---

## 14. Post-Refactor: Reaper Integration

After this refactor, the reaper design becomes trivial:

```sql
-- Find all stuck entities in one query
select 'emails' as entity, id, status, state_entered_at
  from public.emails
  where status in ('pending', 'active')
    and state_entered_at < now() - interval '10 minutes'
    and failure_count < 3
union all
select 'drafts', id, status, state_entered_at
  from public.drafts
  where status in ('pending', 'active')
    and state_entered_at < now() - interval '10 minutes'
    and failure_count < 3
union all
select 'pipeline_runs', id::text, status, state_entered_at
  from public.pipeline_runs
  where status = 'active'
    and state_entered_at < now() - interval '30 minutes'
union all
select 'profiles', user_id::text, onboarding_status, onboarding_state_entered_at
  from public.profiles
  where onboarding_status = 'active'
    and onboarding_state_entered_at < now() - interval '30 minutes'
    and onboarding_failure_count < 3;
```

The reaper's action logic:

```
For each stuck row:
  - If failure_count < budget: transition back to 'pending' (retry)
  - If failure_count >= budget: transition to 'failed' with last_error set
  - If in 'active' with no recent heartbeat: transition to 'pending'
```

No special cases per entity type — the unified vocabulary eliminates the need for per-table logic.

---

## 15. Appendix A — File Manifest

### Files touched in Phase 1 (constants refactor)

**Worker:**
- `worker/status.py` (NEW)
- `worker/supabase_client.py` (lines 62-82, 88-114, 116-143, 168-213, 260-318, 348-375, 455-479)
- `worker/run_pipeline.py` (lines 145, 220, 225, 331, 337, 862-865, 901, 1093, 1327, 1498-1516, 1523-1528)
- `worker/onboarding/runner.py` (lines 74, 87, 101, 118, 148, 200, 220, 272, 305, 347, 519, 544-549, 561)
- `worker/main.py` (lines 209-231, 254-261, 324, 335)

**Extension:**
- `extension/status.js` (NEW)
- `extension/supabase-realtime.js` (lines 133, 302, 355)
- `extension/background.js` (status-related call sites only; state persistence in Phase 6)

**Web:**
- `web/js/status.js` (NEW)
- `web/js/pages/emails.js` (lines 561, 590, 600, 696, 725, 729)
- `web/js/pages/history.js` (lines 49-58)
- `web/js/components/trace-renderers.js` (lines 91, 193)

**Migrations:**
- `supabase/migrations/037_add_state_entered_at.sql` (NEW)
- `supabase/migrations/038_state_entered_at_triggers.sql` (NEW)
- `supabase/migrations/039_state_indexes.sql` (NEW)

### Files touched in Phase 2

- Worker: add query helpers in `supabase_client.py`
- Web: dual-read in `emails.js`, `history.js`, `trace-renderers.js`
- Migrations:
  - `supabase/migrations/040_rpc_dual_read.sql` (NEW)

### Files touched in Phase 3

- Migrations:
  - `supabase/migrations/041_unify_status_vocabulary.sql` (NEW)
  - `supabase/rollbacks/041_unify_status_vocabulary_rollback.sql` (NEW, not applied unless rollback)

### Files touched in Phase 4 (iteration 1)

- `worker/supabase_client.py` — flip draft/email/pipeline_run writers from legacy to new vocabulary
- `worker/run_pipeline.py` — writer flips only
- `worker/main.py` — writer flips only
- `extension/supabase-realtime.js`, `extension/background.js` — draft writer updates + `written_by_extension_version`
- `web/js/pages/emails.js`, `web/js/pages/history.js`, `web/js/components/trace-renderers.js`

**DEFERRED to iteration 2+** (not touched in iteration 1):
- `worker/onboarding/runner.py` — `_recover_stuck_onboarding` stays
- `worker/supabase_client.py` — `set_pipeline_stage` stays (no-op writes still valid)
- `supabase/migrations/042_drop_pipeline_stage.sql`

### Files touched in Phase 5 (iteration 1)

- `worker/status.py`: remove legacy classes **except** `LegacyOnboardingStatus` (retained until iteration 2)
- `worker/supabase_client.py`: remove draft/email/pipeline_run dual-read helpers
- Web files: remove dual-read arrays for drafts/emails/pipeline_runs
- Migrations:
  - `supabase/migrations/043_status_check_constraints.sql` — `emails`, `drafts`, `pipeline_runs` only (NEW)
  - `supabase/migrations/043a_drop_transitional_indexes.sql` (NEW)
  - `supabase/migrations/043b_recreate_state_indexes.sql` (NEW)

Onboarding-related dual-read and CHECK constraints are retained for iteration 2.

### Files touched in Phase 6

- `extension/background.js`: state persistence functions + call sites
- No migrations

---

## 16. Appendix B — Migration Templates

### Status transition pattern (for use in future code)

```python
# worker/status.py
def transition_to(db, entity_type, entity_id, new_status, error=None):
    """
    Unified status transition helper.
    Writes status + state_entered_at + (optional) last_error in one update.
    Increments failure_count when transitioning to 'failed'.
    """
    payload = {"status": new_status}
    if new_status == Status.FAILED:
        payload["last_error"] = error
        # failure_count incremented by trigger or separate RPC
    # state_entered_at handled by trigger
    return db.table(entity_type).update(payload).eq("id", entity_id).execute()
```

### Reaper scaffold (post-refactor)

**Placement (locked, iteration 1):** `run_reaper(db)` is called **once per
loop iteration, as the final step**, after all per-user processing has
completed. No startup invocation. No mid-cycle invocation. Single call site
in `worker/main.py`.

**Shutdown awareness:** the driver checks `_shutdown` between entity types
(emails → drafts → pipeline_runs) so a SIGTERM during reaper exits within a
few seconds rather than blocking on the full sweep.

**Null-column handling (locked):**
- `state_entered_at IS NULL` → row is skipped by the RPC and the count is
  logged. Backfill is a separate task if the count is non-zero on prod.
- `failure_count IS NULL` → treated as `0` via `COALESCE` in the retry-budget
  check.

```python
# worker/reaper.py
from worker.status import Status

STUCK_TIMEOUTS = {
    "emails":        timedelta(minutes=10),
    "drafts":        timedelta(minutes=10),
    "pipeline_runs": timedelta(minutes=60),  # accommodates Anthropic Batches API
    "profiles":      timedelta(minutes=30),  # onboarding (iteration 2)
}

RETRY_BUDGET = 3

def run_reaper(db):
    if not reaper_enabled():
        return
    stuck = db.rpc("find_stuck_entities").execute()
    for row in stuck.data:
        if _shutdown:
            return
        if (row.get("failure_count") or 0) >= RETRY_BUDGET:
            transition_to(db, row["entity"], row["id"], Status.FAILED,
                          error=f"Exceeded retry budget ({RETRY_BUDGET})")
        else:
            transition_to(db, row["entity"], row["id"], Status.PENDING)
```

**`pipeline_runs` timeout rationale:** classification + draft batches via
the Anthropic Batches API can each take several minutes. On a wide window
or slow batch, total time can approach 30 min. 60 min gives headroom while
still catching genuinely crashed runs. Revisit if false positives surface.

---

## 17. Scope Trim — Iteration 1 (Drafts Reaper Only)

The full refactor (A–F) is large. Iteration 1 is scoped to what the drafts
reaper needs. Onboarding and `pipeline_stage` retirement are explicitly
deferred.

**Locked decision (iteration 1):** primary reaper driver is **drafts**.
Onboarding keeps `_recover_stuck_onboarding` running until a later iteration
brings it into the unified model.

**Locked decision (iteration 1) — reaper placement:**
- Reaper runs **once per cycle, at the end**, after all per-user processing
  completes. No startup invocation. No mid-cycle calls.
- Legacy `reset_stuck_processing` (call site `worker/main.py:286`,
  definition `worker/supabase_client.py:123`) is **removed in the same
  commit** that introduces `worker/reaper.py`.
- Accepted tradeoff: after worker restart, orphaned `active` rows aged past
  their timeout during downtime wait one full cycle (~45–90s) before
  recovery. Acceptable for single-placement clarity.

**Locked decision (iteration 1) — kill switch default:**
- The `system_config.reaper_enabled` row is seeded `'true'::jsonb` in
  migration 044. Reaper ships enabled; disable via SQL if issues surface.

**Locked decision (iteration 1) — null-column handling:**
- `find_stuck_entities` RPC skips rows where `state_entered_at IS NULL` and
  returns the skipped count for logging. Worker logs it once per tick. A
  non-zero count signals a backfill is needed.
- Retry-budget check uses `COALESCE(failure_count, 0)` so legacy rows
  without the column populated retry up to `RETRY_BUDGET` times.

**Locked decision (iteration 1) — `pipeline_runs` timeout:**
- 60 minutes (not 30) to accommodate Anthropic Batches API runtime.
  Revisit if a real false positive is observed.

### 17.1 Iteration 1 ship: A + B + F (drafts fully covered)

| Option | Included? | Why |
|---|---|---|
| A. Unified vocabulary (`pending/active/done/failed/skipped`) | **Yes** | Core substrate; every other piece assumes it |
| B. `state_entered_at` + `failure_count` + `last_error` on every table | **Yes** | Makes the reaper implementable |
| F. Failed/skipped split + `delivery_state` on drafts | **Yes** | Without this, drafts can't use the new vocabulary coherently |
| C. Collapse onboarding's 12-state to `active + onboarding_stage` | **Defer** | Onboarding has its own recovery path today; touching it is a separate risk |
| D. Retire `pipeline_stage` | **Defer** | Derives from `pipeline_runs`; pure cleanup |
| E. Extension state persistence (`chrome.storage.session`) | **Ship first, independently** | No DB coupling; no dependency on A/B/F |

### 17.2 Why this cut

- **Drafts are the acute pain point.** Most of the "self-healing" gap users
  notice is drafts that don't push or drafts that never get generated. A+B+F
  gives the reaper everything it needs to detect and retry those.
- **Onboarding (C) is self-contained.** Today it has `_recover_stuck_onboarding`
  doing its own thing. Leaving onboarding on legacy vocabulary for one
  iteration is survivable. Delete `_recover_stuck_onboarding` **only when
  onboarding joins the unified model** — not before, or we lose its recovery
  path without a replacement.
- **`pipeline_stage` (D) is cosmetic.** Readable from `pipeline_runs` already.
  Defer until A+B+F is stable.
- **Extension state (E) is orthogonal.** Shipping it first de-risks the worker
  refactor: fewer moving parts in the DB-coupled phases.

### 17.3 Dependencies between the deferred items

- **C depends on B.** Onboarding collapse needs `onboarding_state_entered_at`,
  which Phase 1 (scope B) provides. So C is a clean follow-up after A+B+F.
- **D depends on nothing (it's pure removal).** Can ship any time after
  Phase 4 writer flip.
- **`_recover_stuck_onboarding` removal is coupled to C, not A+B+F.** Keep it
  running until the unified reaper owns onboarding.

### 17.4 Iteration 2+ follow-ups

Tracked for later, not executed in iteration 1:

- **C (onboarding collapse):** migrate the 12 legacy onboarding states into
  `active + onboarding_stage`. At the same time, delete
  `_recover_stuck_onboarding` (§9.2b) and add the
  `profiles_onboarding_status_check` constraint.
- **D (retire `pipeline_stage`):** derive from `pipeline_runs.status` in the
  dashboard, then drop the column (§9.3, migration 042).
- **Onboarding publication check:** re-run the `pg_publication_tables` audit
  from §9.3 against `profiles` before iteration 2 migrations.

---

## 18. Clock Policy — state_entered_at vs updated_at

Two timestamp columns exist on status-bearing tables and serve distinct
purposes. This section is the canonical reference.

### 18.1 Policy

| Column | Semantics | Who writes it |
|---|---|---|
| `state_entered_at` | The **lifecycle clock**. "When did this row enter its current `status`?" | `bump_state_entered_at` trigger — status-change only |
| `updated_at` | The **any-field clock**. "When was any column on this row last modified?" | Application code on every write; or a generic updated_at trigger |

**Consequences:**
- A status change bumps **both**. `updated_at` always moves when
  `state_entered_at` moves.
- A `last_error`-only write, `failure_count`-only write, or any other
  non-status write bumps **only** `updated_at`.
- Stuck detection **must use `state_entered_at`**, not `updated_at`. Using
  `updated_at` gives false negatives — a reaper's own heartbeat write would
  reset the clock.
- Partial-failure bookkeeping writes (`has_partial_failures = true`) that
  don't change `status` bump only `updated_at`.

### 18.2 Grep audit — `updated_at` used as a stuck-proxy

Find every call site that filters on `updated_at` for stuck detection and
migrate to `state_entered_at`:

```
grep -rn "updated_at.*interval\|updated_at.*now()\|WHERE.*updated_at <" worker/ supabase/
```

Likely candidates to review and migrate:
- `reset_stuck_emails` / `reset_stuck_processing` (migration `006`)
- `find_stale_drafts` RPC (migration `020`) — already uses updated_at-ish
  heuristics
- `_recover_stuck_onboarding` (being deleted in Phase 4)
- Any dashboard query in `web/js/pages/history.js`

Keep `updated_at` where the query genuinely wants "last modified" semantics
(e.g., "show me rows touched in the last hour").

---

## 19. system_config — Reaper Kill Switch

The reaper (and any other autonomous agent we add later) needs a fast kill
switch that doesn't require a redeploy. An env var would require a restart;
a feature flag service would add a new dependency. Pick the smallest thing
that works: a table.

### 19.1 Schema

**Migration: `supabase/migrations/046_system_config.sql`** (ships with reaper,
not with Phase 1–5; numbered after the existing `045_lock_unified_vocabulary`):

```sql
create table if not exists public.system_config (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

-- Service-role only. No anon/authenticated access.
alter table public.system_config enable row level security;

-- No policies created — RLS enabled with no policies means
-- only service-role (which bypasses RLS) can read/write.

insert into public.system_config (key, value) values
  ('reaper_enabled', 'true'::jsonb),
  ('reaper_paused_until', 'null'::jsonb)
on conflict (key) do nothing;
```

### 19.2 Worker-side cache (60s TTL)

Reading a row on every reaper tick is silly and creates an N+1 query pattern
if the reaper is running on a schedule. Cache it:

```python
# worker/config.py
import time
from worker.supabase_client import db

_cache = {}
_CACHE_TTL_SECONDS = 60

def get_config(key, default=None):
    """Read a system_config value with 60s TTL.

    Changes to system_config take up to 60s to propagate to the worker.
    This is the intentional trade-off for not hammering the DB.
    """
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL_SECONDS:
        return cached["value"]
    row = db.table("system_config").select("value").eq("key", key).maybe_single().execute()
    value = row.data["value"] if row.data else default
    _cache[key] = {"value": value, "fetched_at": now}
    return value

def reaper_enabled() -> bool:
    if get_config("reaper_enabled", True) is not True:
        return False
    paused_until = get_config("reaper_paused_until")
    if paused_until:
        from datetime import datetime, timezone
        if datetime.now(timezone.utc) < datetime.fromisoformat(paused_until):
            return False
    return True
```

### 19.3 Operator workflow

```sql
-- Kill the reaper immediately
update public.system_config
  set value = 'false'::jsonb, updated_at = now()
  where key = 'reaper_enabled';

-- Or: pause for 1 hour (auto-resumes)
update public.system_config
  set value = to_jsonb((now() + interval '1 hour')::text),
      updated_at = now()
  where key = 'reaper_paused_until';

-- Resume
update public.system_config
  set value = 'true'::jsonb, updated_at = now()
  where key = 'reaper_enabled';
update public.system_config
  set value = 'null'::jsonb, updated_at = now()
  where key = 'reaper_paused_until';
```

### 19.4 Why not an env var

- Env var changes require a Railway redeploy (~60–90s of worker downtime).
- Env vars can't express "pause for 1 hour, then resume." A paused_until
  timestamp can.
- Future autonomous agents share the same table; no per-agent env var sprawl.

---

## 20. Error Sanitization & last_error RLS

`last_error` is useful for diagnostics but is **not** safe to expose to
anon/authenticated reads. Stack traces leak file paths and, occasionally,
secrets accidentally logged into exceptions.

### 20.1 Sanitize before writing

```python
# worker/status.py
import re

_SECRET_KEY_RE = re.compile(r'(password|token|secret|api[-_]?key|bearer)\s*[:=]\s*\S+', re.I)
_PATH_RE = re.compile(r'[A-Za-z]:[\\/][^\s,)\]]*|/[\w./-]+\.py')

def sanitize_error(exc: Exception | str, max_len: int = 500) -> str:
    """Strip paths, credentials, and multi-line stack traces from error strings.

    Use this on every path that writes `last_error`. Keeps the DB clear of
    leaked secrets, and keeps the last_error column rendering tidy on the
    dashboard.
    """
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    msg = msg.splitlines()[0] if msg else ""
    msg = _SECRET_KEY_RE.sub(r'\1=<redacted>', msg)
    msg = _PATH_RE.sub('<path>', msg)
    return msg[:max_len]
```

All `last_error` writes go through this helper:

```python
from worker.status import sanitize_error, transition_to, Status

try:
    ...
except Exception as exc:
    transition_to(db, "emails", email_id, Status.FAILED,
                  error=sanitize_error(exc))
```

### 20.2 RLS — `last_error` service-role only

Option A (preferred): column-level grant.

**Migration: `supabase/migrations/048_last_error_rls.sql`**
(numbered after `046_system_config.sql` and `047_find_stuck_entities.sql`):

```sql
-- Revoke column-level access from anon and authenticated.
revoke select (last_error) on public.emails from anon, authenticated;
revoke select (last_error) on public.drafts from anon, authenticated;
revoke select (last_error) on public.pipeline_runs from anon, authenticated;
revoke select (onboarding_last_error) on public.profiles from anon, authenticated;
```

The dashboard (which uses anon or authenticated role) will not see `last_error`.
If a user-facing error summary is needed, expose a **sanitized, categorized**
signal (e.g., `error_category text` with values like `auth_failed`,
`network_timeout`, `llm_refused`) rather than raw message text.

### 20.3 `failure_count` and `onboarding_stage` stay readable

These are safe to expose — no PII, no secret leakage risk. They drive the
dashboard's "this onboarding has been stuck in `extracting` for 20 minutes"
UX, which is legitimately useful.

---

## 21. Staging Environment — Schema Clone + Synthetic Data

A true prod data clone is heavy, expensive, and leaks user data into a
less-hardened environment. A schema-only clone with synthetic data is the
pragmatic compromise for dry-running Phase 3.

### 21.1 One-time setup

```bash
# Dump prod schema (no data, no RLS policies if your local tooling can't replay them)
supabase db dump --schema-only > /tmp/prod_schema.sql

# Create a staging project (or reuse a dev branch)
supabase link --project-ref <staging-ref>

# Apply schema
psql $STAGING_DB_URL < /tmp/prod_schema.sql
```

### 21.2 Synthetic data generator

**New file: `scripts/seed_staging.py`** (ships with the plan, not with prod).

Purpose: populate staging with rows that exercise every legacy status value
and every target value simultaneously, plus edge cases (rows with NULL
`updated_at`, rows with partial_failure, rows mid-onboarding).

Rough skeleton:

```python
# scripts/seed_staging.py
"""Populate staging DB with synthetic data covering every legacy status
value and every target value. Uses no production data.

Usage: STAGING_DB_URL=... python scripts/seed_staging.py
"""
import os, random, uuid
from datetime import datetime, timezone, timedelta
from supabase import create_client

sb = create_client(os.environ["STAGING_URL"], os.environ["STAGING_SERVICE_KEY"])

FIXTURES = {
    # v4: only fixture legacy values that actually appear in production.
    "emails.unprocessed":   {"count": 50, "status": "unprocessed"},
    "emails.processing":    {"count": 10, "status": "processing"},
    "emails.processed":     {"count": 200, "status": "processed"},
    "emails.completed":     {"count": 40, "status": "completed"},    # dashboard user action
    "emails.dismissed":     {"count": 10, "status": "dismissed"},    # dashboard user action
    "emails.error":         {"count": 3, "status": "error"},
    "emails.onboarding":    {"count": 30, "status": "onboarding"},
    "drafts.pending":       {"count": 10, "status": "pending"},
    "drafts.written":       {"count": 40, "status": "written"},
    "drafts.deleted":       {"count": 5, "status": "deleted", "draft_deleted": True},
    "pipeline_runs.running":       {"count": 2, "status": "running"},
    "pipeline_runs.completed":     {"count": 50, "status": "completed"},
    "pipeline_runs.partial":       {"count": 5, "status": "partial_failure"},
    "pipeline_runs.failed":        {"count": 3, "status": "failed"},
    # Onboarding coverage across all 12 states
    # ...
}
# Generate rows with varied state_entered_at timestamps after Phase 1
# (include stale rows and fresh rows to exercise reaper thresholds).
# Note: emails/pipeline_runs have no updated_at; only drafts does.
```

### 21.3 Dry-run workflow

```bash
# 1. Seed
python scripts/seed_staging.py

# 2. Apply Phases 1 & 2 migrations
supabase db push --include-all  # or specific files

# 3. Apply Phase 3 migration
psql $STAGING_DB_URL < supabase/migrations/041_unify_status_vocabulary.sql

# 4. Run validation queries from Section 8.3
# 5. Re-seed + repeat until clean
```

### 21.4 What staging does NOT validate

- **Production-scale timing.** A synthetic seed of ~10k rows runs the
  migration in seconds. Production may have millions. Combine staging dry-run
  with a row-count estimate and the `statement_timeout` from Section 8.2.
- **Realistic contention.** Staging has no concurrent workers. The `lock_timeout`
  in migration 041 is the production safety net for lock waits.
- **Stripe / realtime external surfaces.** Out of scope for this refactor.

---

**End of document.**

Review this plan and resolve the 7 Pre-Flight Decisions in Section 4 before beginning Phase 1.
