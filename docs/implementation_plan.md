# Clarion AI — UI Implementation Plan

> Derived from the completed feature questionnaire. This plan covers the extension popup and web dashboard redesign. Backend/worker changes are noted where required.

---

## Design Principles (from questionnaire)

1. **Invisible until useful** — Clarion works in the background. UI exists for review, not management.
2. **Simplicity is non-negotiable** — Every element must be intuitive to a non-technical user. If it needs explaining, rethink it.
3. **Action-oriented** — Only surface things the user needs to do something about. Everything else gets out of the way.
4. **One thing done well** — Draft relevant, tone-specific responses that feel like the user wrote them.

---

## Phase 1: Extension Popup Redesign

### Goal
Transform the popup from a status panel into a concise "proof of work" + navigation hub. One headline, no clutter.

### Layout (top to bottom)

1. **Connection indicator** — Small dot (green/red) + "Connected" or "Reconnect" text. Top-right corner, minimal footprint.

2. **Headline stat** — Single, prominent, contextual number. Logic:
   - If drafts > 0 → "You have **3 drafts** ready to review" (links to Outlook drafts)
   - Else if needs_response > 0 → "**2 emails** need your attention" (links to dashboard emails page)
   - Else → "All caught up" or "**47 emails** handled this week" (proof of value)
   - Only one headline shown at a time, prioritized in the order above.

3. **Quick stats row** — Two or three small secondary stats below the headline:
   - Emails processed (today or this week)
   - Drafts generated (today or this week)
   - These reinforce "I'm working" without being the focus.

4. **Deep links** — Contextual navigation buttons:
   - "View Drafts" → Outlook drafts folder (reuse existing tab logic)
   - "View Notable" → Dashboard emails page filtered to Notable
   - "Open Dashboard" → Dashboard home
   - Only show links that are relevant (e.g., hide "View Drafts" if draft count is 0).

5. **Feedback button** — "Give Feedback" → Opens dashboard feedback/emails page in new tab.

6. **Footer** — User email + Logout (with existing two-step confirmation).

### Error/Failure Handling
- If sync fails or token expires, replace the headline with an error state: "Outlook connection lost — open Outlook to reconnect"
- Badge on extension icon: red dot for errors (current behavior).

### What to Remove
- Setup checklist (move to dashboard onboarding flow or show only on first install)
- "Sync Now" button (auto-sync is sufficient; keep only for onboarding)

### Technical Notes
- Popup queries Supabase REST for counts (drafts where status='pending', classifications where needs_response=true)
- Headline logic is pure client-side conditional rendering
- Deep links use `chrome.tabs.create()` or existing Outlook tab reuse logic
- Weekly stats: query `pipeline_runs` aggregated by date range, or count emails with `received_time` in range

---

## Phase 2: Dashboard Home Redesign

### Goal
Answer "what do I need to deal with right now?" first, then provide context and insights.

### Layout (top to bottom)

#### Section 1: Status Banner
- Single sentence, dynamically generated:
  - "You have **4 drafts** to review and **2 emails** that need attention"
  - "All caught up — Clarion processed **31 emails** this week"
  - "**1 urgent email** from Sarah Chen needs your response"
- Clickable segments: "4 drafts" links to Emails page (Drafts group), "2 emails" links to Emails page (Notable group).

#### Section 2: Actionable Email Cards
- Condensed preview cards for emails requiring user action (drafts ready + needs_response without draft).
- Each card shows: sender name, subject line, suggested action (one line), priority badge if P1-P3.
- Click on any card → navigates to Emails page with that email expanded.
- Cap at ~5-8 cards. If more exist, show count + "View all on Emails page" link.
- Empty state: "Nothing needs your attention right now."

#### Section 3: Insights Panel
- **Time-range filter** (keep existing 1d/7d/30d toggle).
- **Metrics row** (rethought):
  - "Drafts Ready" (keep) — count of pending drafts
  - "Emails Processed" (rethink of "Emails Synced") — emphasize what Clarion did, not raw sync count
  - "Response Rate" or "Emails Handled" — percentage or count of emails Clarion handled without user intervention
- **Trend charts** (new):
  - Email volume over time (bar or line chart, by day/week)
  - Drafts generated over time
  - Top senders needing attention (table: sender name, email count, avg priority)
- **Weekly summary card** (new):
  - "This week: Clarion processed 47 emails, drafted 12 responses, 3 still need your attention"

#### What to Remove
- Pipeline funnel visualization (move to Dev Tools)
- Latest pipeline run status card (move to Dev Tools; errors surface via popup badge)

### Technical Notes
- Status banner: single query joining `emails` → `classifications` → `drafts`, filtered to actionable states
- Actionable cards: same query, limited to 8, ordered by priority desc then received_time desc
- Trend charts: aggregate query on `emails` grouped by date + `pipeline_runs` for draft counts
- Top senders: aggregate query on `emails` joined with `classifications` where needs_response=true, grouped by sender
- Consider a lightweight charting library (Chart.js or similar) for trends

---

## Phase 3: Emails Page Redesign

### Goal
Replace the 4-group structure with an action-oriented layout. Only show what the user needs to act on.

### Email Groups (3 categories)

#### 1. Drafts
- **What**: Emails where a draft has been generated and is in Outlook.
- **User action**: Go to Outlook, review the draft, send it.
- **DB filter**: `drafts.status = 'pending'` or draft exists with `outlook_draft_id` set.
- **Card contents**: Sender name, subject, suggested action, draft preview (collapsed by default, expandable).
- **Actions on card**:
  - "View in Outlook" → navigate to Outlook drafts
  - "Mark Done" → set email status to completed
  - Thumbs up/down feedback (see Phase 5)

#### 2. Notable
- **What**: Emails Clarion determined are worth the user reading, but don't need a response.
- **User action**: Read for awareness, then dismiss.
- **DB filter**: `classifications.needs_response = false` AND meets a "notable" threshold (not junk/auto-skipped). See derivation logic below.
- **Card contents**: Sender name, subject, context/reason (from classification), priority badge.
- **Actions on card**:
  - "Mark Done" / "Dismiss"
  - "Actually, draft a reply" → triggers draft generation for this email
  - Thumbs up/down feedback

#### 3. All Emails (secondary view)
- **What**: Full email list for the last 30 days. Searchable, filterable.
- **Access**: Tab or toggle below the main groups, collapsed by default. Not the primary view.
- **Purpose**: "I know I got an email about X last week" — lookup, not triage.
- **Features**: Search by sender/subject (existing), filter by date range.

### "Notable" Derivation Logic
An email is "notable" if:
- `needs_response = false` (no draft generated)
- AND at least one of:
  - `response_events.pri IN ('med', 'high')`
  - `response_events.mc = true` (financial/legal consequence)
  - `response_events.sender_tier IN ('C', 'I')` (critical or internal sender)
  - `response_events.rt != 'none'` (some response type detected, even if not user-targeted)
- This uses existing signals — no schema change needed.

### Email Card Design
- **Always visible**: Sender name, subject line, time received (relative), priority badge (if high)
- **Expandable**: Full email body, suggested action, draft preview, classification reasoning, feedback controls
- **Contact context**: If contact exists in `contacts` table, show small label (e.g., "Lender · 8 emails/month")

### Draft Generation Controls
When user clicks "Draft a Reply" on a Notable or unhandled email:
- **Default action**: Generate immediately with AI defaults
- **Expandable "Advanced" section**:
  - Text field: "Any instructions?" (e.g., "Decline politely", "Mention Tuesday call")
  - Tone selector: dropdown or pills (Professional / Casual / Brief / Detailed)
  - "Generate" button

### Bulk Actions
- "Mark All Done" button per group (Drafts, Notable)
- Confirmation before executing

### What to Remove
- "Other" group
- "Completed" group (accessible only in "All Emails" view)
- Draft editing in dashboard (user edits in Outlook)
- "Needs Response" group name (replaced by Drafts and Notable)

### DB Changes Required
- **`dismissed` email status**: New enum value on `emails.status` for emails the user dismisses from Notable, to distinguish from `completed` (Clarion processed it).
- **Notable derivation**: Query logic only, joins `classifications` + `response_events`. No new columns.

---

## Phase 4: Per-Contact Settings & Context

### Goal
Show contact context on email cards and allow VIP/preference configuration.

### Contact Context on Email Cards
- When expanding an email card, show a small contact badge:
  - Name, organization, contact type (Lender, Internal, etc.)
  - Email frequency (e.g., "~12/month")
  - VIP indicator if flagged
- Data source: `contacts` table, joined by sender email.

### Contact Settings (modal or dedicated page)
- Accessible from: email card contact badge (click to manage), or a "Contacts" link in sidebar.
- Per-contact options:
  - **VIP flag** — Boosts priority for all emails from this sender
  - **Auto-draft preference**: Always draft / Never draft / Use AI judgment (default)
  - **Custom priority override**: Force High / Medium / Low for this sender
- Stored in `contacts` table (new columns).

### DB Changes
- `contacts.is_vip` (boolean, default false)
- `contacts.draft_preference` (enum: `always` / `never` / `auto`, default `auto`)
- `contacts.priority_override` (enum: `high` / `med` / `low` / null, default null)

### Worker Impact
- Signal extraction step reads contact preferences before classification
- If `draft_preference = 'always'` → force `draft=true` regardless of signals
- If `draft_preference = 'never'` → force `draft=false`
- If `is_vip = true` → boost priority to `high`

---

## Phase 5: Feedback System

### Goal
Let users correct misclassifications with minimal friction. Collect data for future model improvement.

### UX Flow
1. Each email card (Drafts and Notable) shows a small thumbs-up / thumbs-down icon pair.
2. **Thumbs up**: Records positive signal. No further UI. Toast: "Thanks for the feedback."
3. **Thumbs down**: Expands a dropdown with correction options:
   - "This doesn't need a response" (shown on Drafts cards)
   - "This actually needs a response" (shown on Notable cards)
   - "Priority is wrong" → show High/Med/Low selector
   - "Draft tone/content was off" (shown on Drafts cards)
   - "Other" → small text input
4. Submit → records feedback, toast confirmation, collapses dropdown.

### Popup Integration
- "Give Feedback" button in popup opens dashboard Emails page.

### DB Changes
- **New table: `feedback`**
  - `id` (uuid), `user_id`, `email_id`
  - `feedback_type` (enum: `positive`, `negative`)
  - `correction_category` (enum: `no_response_needed`, `response_needed`, `wrong_priority`, `draft_quality`, `other`)
  - `correction_value` (text, nullable)
  - `created_at` (timestamptz)

### Worker Impact (future, not required for initial build)
- Feedback data can inform per-user prompt adjustments or signal threshold tuning.
- Initial phase: just collect and store.

---

## Phase 6: Analytics & Insights

### Goal
Give users visibility into email patterns and Clarion's value.

### Dashboard Insights Panel (built as part of Phase 2, Section 3)
- **Email volume chart**: Emails received per day over selected time range (bar chart)
- **Drafts generated chart**: Drafts created per day (overlay or separate)
- **Top senders table**: Sender name, email count, avg priority — top 10, sortable
- **Weekly summary**: "This week: 47 emails processed, 12 drafts generated, avg priority 4.2"

### Dedicated Analytics Page (lower priority, optional)
- Deeper breakdowns by sender, project, priority level
- Response time trends
- Classification accuracy (if feedback data exists)
- Accessible from sidebar nav

### Technical Notes
- All data derivable from existing tables: `emails`, `classifications`, `response_events`, `contacts`, `pipeline_runs`
- Charting: Chart.js (lightweight, no build step needed for vanilla JS project)
- Queries: Supabase `.rpc()` for aggregates or client-side aggregation for small datasets

---

## Phase 7: Email Threading

### Goal
Show full conversation context, not just individual emails.

### Implementation
- Group emails by `conversation_id` on the Emails page
- Thread indicator on email cards: small "3 messages" badge
- Expandable thread view: vertical timeline of messages (sender, timestamp, body snippet)
- Current email highlighted in the thread

### Technical Notes
- `conversations` table already stores message arrays per `conversation_id`
- Join `emails.conversation_id` → `conversations.conversation_id` on the client
- Thread view is read-only

---

## Phase 8: Mobile Responsiveness

### Goal
Dashboard works on phone/tablet browsers.

### Approach
- Responsive CSS breakpoints on all dashboard pages
- Sidebar collapses to bottom nav or hamburger on mobile
- Email cards stack vertically, full-width
- Charts resize to container width
- Breakpoints: 375px (phone), 768px (tablet), 1024px+ (desktop)
- Vanilla CSS media queries — no framework change needed
- Should be done after all UI changes are stable to avoid rework

---

## Phase 9: Pipeline & Dev Tools Cleanup

### Goal
Hide developer-facing features behind a toggle.

### Changes
- Add "Developer Mode" toggle in user settings/profile
- Dev Tools nav item only appears when developer mode is enabled
- Pipeline History page moves under Dev Tools
- Pipeline funnel visualization moves from dashboard to Dev Tools
- Error surfacing: pipeline failures show as red badge on extension icon (existing behavior)

---

## Implementation Order

| Priority | Phase | Rationale |
|---|---|---|
| 1 | Phase 3: Emails Page | Core UX — fixes the biggest pain point (confusing categories) |
| 2 | Phase 2: Dashboard Home | Depends on new email grouping logic from Phase 3 |
| 3 | Phase 1: Extension Popup | Independent, can be done in parallel with Phase 2 |
| 4 | Phase 5: Feedback System | Lightweight, adds to email cards built in Phase 3 |
| 5 | Phase 4: Contact Settings | Builds on email card contact context from Phase 3 |
| 6 | Phase 6: Analytics | Additive — enhances dashboard from Phase 2 |
| 7 | Phase 7: Email Threading | Data already exists, UI addition to Phase 3 cards |
| 8 | Phase 9: Dev Tools Cleanup | Low effort, low urgency |
| 9 | Phase 8: Mobile Responsiveness | Do after all UI changes are stable |

---

## DB Changes Summary

| Change | Type | Table |
|---|---|---|
| `is_vip` (boolean) | New column | `contacts` |
| `draft_preference` (enum: always/never/auto) | New column | `contacts` |
| `priority_override` (enum: high/med/low/null) | New column | `contacts` |
| `feedback` | New table | — |
| `dismissed` status value | New enum value | `emails.status` |
| "Notable" derivation | Query logic only | No schema change |

## Worker Changes Summary

| Change | Phase |
|---|---|
| Read `contacts.is_vip`, `draft_preference`, `priority_override` during signal extraction | Phase 4 |
| Apply VIP boost and draft preference overrides to classification output | Phase 4 |
| Store feedback data (future: use for retraining) | Phase 5 |

## Files Likely Affected

**Extension:**
- `extension/popup.html` — Redesigned layout
- `extension/popup.js` — Headline logic, deep links, stats queries
- `extension/popup.css` — New styles

**Dashboard:**
- `web/app/dashboard.html` — Status banner, actionable cards, insights layout
- `web/js/pages/dashboard.js` — New queries, chart rendering, summary logic
- `web/app/emails.html` — New group structure, feedback controls, threading UI
- `web/js/pages/emails.js` — Grouping logic, Notable derivation, bulk actions, draft generation controls
- `web/css/app.css` — Responsive breakpoints, new component styles
- `web/js/nav.js` — Dev Tools visibility toggle

**New files:**
- `web/js/components/feedback.js` — Feedback widget (thumbs up/down + dropdown)
- `web/js/components/charts.js` — Chart rendering helpers

**Worker:**
- `worker/run_pipeline.py` — Read contact preferences during processing
- `worker/signal_extractor.py` — Apply VIP/draft overrides post-extraction
