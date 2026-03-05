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

## Part C: Writing Style Guide from Sent Email Analysis

**Problem:** The draft generator now instructs Claude to "sound like the user," but it has no data about how the user actually writes. We need to analyze the user's sent emails to build a writing style guide that the draft prompt can reference.

### C1. Sent emails sync — `extension/background.js` (MODIFY)

The extension already has `handleGetSentItems()` but only uses it for response rate estimation. Extend the sync alarm (A6) to also push sent emails:
- Fetch last 30 days of Sent Items via OWA `FindItem` on the `sentitems` folder.
- Push to `public.emails` with `folder='Sent Items'` so the worker can distinguish them.
- Only sync sent items during the initial profile build and monthly refresh — not every 5-min cycle. Use a `last_sent_sync_at` timestamp in `chrome.storage.local`.

### C2. Writing style columns — add to `profiles` table migration or new migration `004_writing_style.sql`

```sql
alter table public.profiles
    add column writing_style_guide text,
    add column style_profiled_at timestamptz,
    add column style_sample_count integer default 0;
```

- `writing_style_guide` — the generated natural-language style guide (fed directly into the draft prompt).
- `style_profiled_at` — when it was last generated.
- `style_sample_count` — how many sent emails were analyzed.

### C3. Style profiler — `worker/pipeline/style_profiler.py` (NEW)

Analyzes the user's sent emails and produces a writing style guide:

1. **Sample** — Query `public.emails` where `folder='Sent Items'` and `user_id=X`, last 30 days. Select up to 50 emails, stratified:
   - ~20 short replies (under 100 words) — captures quick acknowledgment style.
   - ~20 substantive replies (100–500 words) — captures detailed response style.
   - ~10 initiated emails (no `In-Reply-To` / `conversation_id` with only 1 message) — captures how the user starts conversations.
2. **Extract patterns** — Send sampled emails to Claude (Haiku) in a structured prompt:
   - Greeting patterns: "Hi [Name]," vs "Hey [Name]," vs no greeting
   - Sign-off patterns: "Best," vs "Thanks," vs "Best regards,"
   - Sentence structure: average length, use of contractions, bullet points vs prose
   - Formality spectrum: casual internal vs formal external (compare by recipient domain)
   - Common phrases and filler words the user relies on
   - How the user handles requests: direct ("I'll handle this") vs delegating ("Can you loop in...")
   - Punctuation habits: exclamation marks, ellipses, em dashes
3. **Generate guide** — Send the extracted patterns to Claude (Sonnet) with instructions to produce a concise writing style guide (target: 300–500 words) that another AI can follow to mimic the user's voice.
4. **Store** — Update `profiles.writing_style_guide`, `style_profiled_at`, `style_sample_count`.

### C4. Inject style guide into draft generation — `worker/pipeline/drafts.py` (MODIFY)

In `_build_draft_prompt()`:
- Accept an optional `writing_style_guide` string (passed from `run_pipeline.py`).
- If present, append a `WRITING STYLE GUIDE` block to the prompt before the "Generate the reply" instruction.

### C5. Pipeline integration — `worker/run_pipeline.py` (MODIFY)

Before generating drafts:
1. Fetch `profiles.writing_style_guide` for the user.
2. Pass it into `draft_generator.generate_draft()` via `action_context["writing_style_guide"]`.

### C6. Style refresh trigger — `worker/main.py` (MODIFY)

Add a check similar to B4's contact staleness:
- If `profiles.style_profiled_at` is null or older than 30 days, and there are sufficient sent emails (>20), trigger the style profiler before processing the email batch.

---

## Part D: Email Behavior Statistics

**Problem:** Response rates, thread participation, and timing patterns are computed on-the-fly during analysis but never persisted. This means (a) the same expensive queries run repeatedly, (b) there's no historical trend data, and (c) a future dashboard has nothing to display.

### D1. Statistics tables — new migration `005_email_statistics.sql`

Two tables: one for per-contact stats (enriches B1's contacts table), one for user-level aggregate stats.

```sql
-- Per-contact interaction statistics (extends contacts table)
alter table public.contacts
    add column avg_response_time_hours numeric(8,2),
    add column last_interaction_at timestamptz,
    add column user_initiates_pct numeric(5,2) default 0.0,
    add column typical_email_length integer default 0;

-- User-level aggregate statistics
create table public.user_statistics (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles on delete cascade,
    period_start date not null,
    period_end date not null,
    -- Volume
    emails_received integer default 0,
    emails_sent integer default 0,
    emails_needing_response integer default 0,
    drafts_generated integer default 0,
    drafts_accepted integer default 0,
    drafts_edited integer default 0,
    -- Timing
    avg_response_time_hours numeric(8,2),
    median_response_time_hours numeric(8,2),
    busiest_hour integer,            -- 0-23 UTC
    busiest_day_of_week integer,     -- 0=Mon, 6=Sun
    -- Accuracy
    needs_response_accuracy numeric(5,2),  -- % of needs_response=true that user actually replied to
    false_positive_rate numeric(5,2),       -- % flagged as needs_response that user ignored
    false_negative_rate numeric(5,2),       -- % not flagged that user replied to anyway
    -- Metadata
    computed_at timestamptz default now(),

    constraint user_stats_period_unique unique (user_id, period_start, period_end)
);

alter table public.user_statistics enable row level security;

create policy "Users can read own statistics"
    on public.user_statistics for select using (auth.uid() = user_id);
```

### D2. Statistics computation — `worker/pipeline/stats_computer.py` (NEW)

Runs after each email processing batch (or on a daily schedule):

1. **Per-contact stats** — For each contact in `public.contacts`:
   - `avg_response_time_hours`: average time between receiving their email and the user's reply (from sent items with matching `conversation_id`).
   - `last_interaction_at`: most recent email to or from this contact.
   - `user_initiates_pct`: % of threads with this contact that the user started.
   - `typical_email_length`: average word count of the user's replies to this contact.
   - Update `response_rate` (already on the contacts table) with fresh data.

2. **User-level aggregates** — Compute for the current 7-day and 30-day rolling windows:
   - Volume counts from `emails` and `drafts` tables.
   - Timing stats from sent email timestamps vs received email timestamps in the same conversation.
   - `busiest_hour` and `busiest_day_of_week` from sent email distribution.

3. **Accuracy tracking** — Compare pipeline decisions against actual user behavior:
   - `needs_response_accuracy`: of emails where `classifications.needs_response=true`, how many did the user actually reply to (matched by checking if a sent email exists in the same conversation after the classification)?
   - `false_positive_rate`: flagged as needing response, but no reply sent within 72 hours.
   - `false_negative_rate`: not flagged, but the user replied anyway.
   - This feedback loop is critical for tuning the analysis prompt over time.

### D3. Stats in the analysis pipeline — `worker/pipeline/analyzer.py` (MODIFY)

Replace the on-the-fly response rate computation with a lookup against persisted `contacts.response_rate`. This is faster and consistent across runs.

### D4. Pipeline integration — `worker/run_pipeline.py` (MODIFY)

After `process_email_batch()` completes:
1. Call `stats_computer.update_contact_stats(user_id)` to refresh per-contact metrics.
2. Periodically (once per day) call `stats_computer.compute_user_aggregates(user_id)` for rolling window stats.

### D5. Supabase client methods — `worker/supabase_client.py` (MODIFY)

Add methods:
- `get_user_statistics(user_id, period)` — fetch aggregate stats.
- `upsert_user_statistics(user_id, stats)` — insert/update rolling window stats.
- `update_contact_stats(user_id, contact_email, stats)` — update per-contact metrics.
- `get_sent_emails_for_conversations(user_id, conversation_ids)` — fetch sent items for response time calculation.

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
| 14 | C2: Writing style columns migration | — |
| 15 | C1: Sent emails sync in extension | A6 |
| 16 | C3: Style profiler | C1, C2 |
| 17 | C4: Inject style guide into drafts | C3 |
| 18 | C5: Pipeline integration for style | C4 |
| 19 | C6: Style refresh trigger | C3 |
| 20 | D1: Statistics tables migration | B1 |
| 21 | D5: Supabase client stats methods | D1 |
| 22 | D2: Statistics computation | D1, D5 |
| 23 | D3: Stats in analyzer (replace on-the-fly) | D2, B3 |
| 24 | D4: Pipeline integration for stats | D2 |

Steps 1, 2, 5, 9, and 14 can be done in parallel. Parts A (3–8), B (10–13), C (15–19), and D (20–24) can proceed in parallel after their respective prerequisites. Part C depends on A6 (sent email sync). Part D depends on B1 (contacts table).

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

### Part C — Writing Style Guide
- [ ] Extension syncs sent emails to Supabase with `folder='Sent Items'`
- [ ] `profiles.writing_style_guide` column exists
- [ ] Style profiler samples 50 sent emails and generates a style guide
- [ ] Style guide is injected into draft generation prompt
- [ ] Generated drafts sound noticeably like the user, not like a generic assistant
- [ ] Style auto-refreshes when >30 days old
- [ ] Style profiler handles users with <20 sent emails gracefully (skips or uses minimal guide)

### Part D — Email Behavior Statistics
- [ ] `user_statistics` table captures rolling 7-day and 30-day aggregates
- [ ] Per-contact stats (avg response time, initiation %, typical length) populate on `contacts`
- [ ] Accuracy tracking compares `needs_response` predictions vs actual user replies
- [ ] `false_positive_rate` and `false_negative_rate` computed and stored
- [ ] Analyzer uses persisted `contacts.response_rate` instead of on-the-fly computation
- [ ] Stats update after each batch processing run
- [ ] User aggregates compute daily
