# Comprehensive Email Draft Testing Plan

**Sender:** bobby.axelrod5522@gmail.com
**Recipient (test account):** nate.mcbride23@outlook.com
**Goal:** Measure the impact of style and behavior guides on draft quality by comparing guided responses against a no-guide control baseline.

---

## 1. Industries (5)

| # | Industry | Simulated Sender Role |
|---|----------|-----------------------|
| 1 | Real Estate | Agent / Broker |
| 2 | Insurance | Agent / Account Manager |
| 3 | Ecommerce | Customer Support Rep |
| 4 | Legal | Attorney / Paralegal |
| 5 | Professional Services | Consultant / CPA |

---

## 2. Style Guides (3 + Control)

Each style guide will be set in `profiles.writing_style_guide` before running that test batch.

### Control — NULL (No Style Guide)
Set `profiles.writing_style_guide` to `NULL`. The system falls back to:
- Claude's default voice
- Context-based tone adjustment (formal for attorneys, casual for colleagues)
- Default sign-off: "Best regards,"
- No archetype hint is suppressed — it still flows through from signal extraction

### Style A — Friendly / Warm / High-Word-Count
```
WRITING STYLE GUIDE:
- Tone: casual, warm, conversational
- Pleasantries: warm — open with a personalized greeting, close with well-wishes
- Greeting pattern: "Hey [First Name]," or "Hi [First Name]!"
- Sign-off pattern: "Thanks so much!", "Looking forward to hearing from you!", "Have a great day!"
- Sentence structure: longer, flowing sentences with connecting phrases ("by the way", "also wanted to mention")
- Formality: low — contractions encouraged, first-name basis, exclamation points OK
- Response length: thorough — address all points, add context, 150-250 words typical
- Verbal habits: "absolutely", "sounds great", "happy to help", "no worries"
- Punctuation: liberal use of exclamation marks, occasional ellipses for conversational tone
```

### Style B — Professional / Terse / Low-Word-Count
```
WRITING STYLE GUIDE:
- Tone: formal, direct, business-like
- Pleasantries: minimal — brief greeting, no small talk
- Greeting pattern: "[First Name]," or "Good morning/afternoon,"
- Sign-off pattern: "Best," or "Regards,"
- Sentence structure: short declarative sentences, no filler
- Formality: high — no contractions, no exclamation marks, title + last name for new contacts
- Response length: concise — key points only, 40-80 words typical
- Verbal habits: "Understood.", "Will do.", "Please advise.", "Confirmed."
- Punctuation: periods only, bullet points for multiple items
```

### Style C — Balanced / Neutral / Mid-Word-Count
```
WRITING STYLE GUIDE:
- Tone: professional but approachable
- Pleasantries: standard — greeting + brief closing
- Greeting pattern: "Hi [First Name],"
- Sign-off pattern: "Thanks," or "Best regards,"
- Sentence structure: medium length, mix of simple and compound sentences
- Formality: moderate — contractions OK, first-name basis, no exclamation marks
- Response length: moderate — address main points with brief context, 80-150 words typical
- Verbal habits: "sounds good", "let me know", "I'll take care of it"
- Punctuation: standard — periods, occasional dashes for asides
```

---

## 3. Behavior Guides (3 + Control)

Each behavior guide will be set in `profiles.behavioral_profile` before running that test batch.

### Control — NULL (No Behavior Guide)
Set `profiles.behavioral_profile` to `NULL`. The system falls back to:
- Archetype hint from signal extraction (e.g., "Expected response type: decision_needed") injected into context
- Model's own judgment via the 8-step thinking framework, with no IF-THEN rules
- Neutral behavior on all four dimensions (decision disposition, completeness, commitment, scope)

### Behavior 1 — High Authority / Decision-Maker
```
BEHAVIORAL PROFILE:
- Decision disposition: decides — makes clear decisions, gives definitive answers
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: specific_next_step — commits to concrete actions with detail ("I'll send the revised contract by Thursday")
- Scope behavior: expands_scope — proactively raises related issues or next steps the sender hasn't mentioned
- IF someone asks for approval → THEN grant or deny with reasoning
- IF someone presents options → THEN pick one decisively and explain why
- IF a problem is raised → THEN propose a solution and assign next steps
- IF a deadline is mentioned → THEN confirm or counter-propose with a specific date
```

### Behavior 2 — Low Authority / Deferential
```
BEHAVIORAL PROFILE:
- Decision disposition: defers — avoids committing, routes decisions upward or back to sender
- Response completeness: key_point_only — addresses the most important item, leaves rest
- Commitment pattern: vague_forward — references future action without specifics ("I'll look into this")
- Scope behavior: stays_narrow — responds only to what was explicitly asked
- IF someone asks for approval → THEN say you need to check with your team/manager
- IF someone presents options → THEN ask for their recommendation instead of choosing
- IF a problem is raised → THEN acknowledge and say you'll escalate or follow up
- IF a deadline is mentioned → THEN say you'll do your best but can't guarantee
```

### Behavior 3 — Moderate Authority / Collaborative
```
BEHAVIORAL PROFILE:
- Decision disposition: proposes_solution — identifies the issue and offers a specific fix for consideration
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: conditional_decision — commits contingent on a stated condition ("If the numbers check out, let's move forward")
- Scope behavior: adds_context — provides relevant unrequested info without going off-topic
- IF someone asks for approval → THEN give conditional approval with caveats or questions
- IF someone presents options → THEN analyze trade-offs and recommend one with rationale
- IF a problem is raised → THEN diagnose with targeted questions before proposing a fix
- IF a deadline is mentioned → THEN confirm feasibility and flag any dependencies
```

---

## 4. Inbound Test Emails

All sent FROM bobby.axelrod5522@gmail.com TO nate.mcbride23@outlook.com.
Each email is action-oriented and requires a substantive draft response.

### Email 1 — Real Estate Agent

**Subject:** Offer on 742 Evergreen Terrace - Need Your Response

**Body:**
Hi Nate,

We received a competing offer on 742 Evergreen Terrace this morning. The new offer is $485,000, all cash, 14-day close. Your client's current offer is $475,000 with FHA financing and a 30-day close.

The seller is leaning toward the cash offer but is willing to give your client until 5pm tomorrow to respond. Do you want to advise your client to increase their offer, waive any contingencies, or let this one go?

Also, I have two other properties hitting the market next week that might work — a 3BR in Oakdale for $460K and a 4BR fixer in Riverside for $425K. Want me to schedule showings?

Let me know how you'd like to proceed.

Bobby Axelrod
Axelrod Realty Group

---

### Email 2 — Insurance Agent

**Subject:** Policy Renewal - Premium Increase & Coverage Gap

**Body:**
Nate,

Your commercial property policy (Policy #CP-2024-8817) is up for renewal on April 15th. The carrier is proposing a 22% premium increase due to claims history and market conditions, bringing the annual premium from $18,400 to $22,450.

I also noticed during the review that your current policy doesn't include business interruption coverage, which I'd strongly recommend given your property type. Adding it would be roughly another $3,200/year.

I need to know by end of week: (1) should I shop this to other carriers to see if we can beat the renewal price, (2) do you want to add the business interruption coverage, and (3) are there any changes to the property or operations I should know about before I submit?

Thanks,
Bobby Axelrod
Axelrod Insurance Group

---

### Email 3 — Ecommerce Customer Support

**Subject:** Bulk Order #EM-90421 - Wrong Items Shipped & Event Deadline

**Body:**
Hi Nate,

I'm reaching out regarding bulk order #EM-90421 placed on March 20th. The customer received 500 units of the navy polo (SKU NV-200) instead of the 500 black polos (SKU BK-200) they ordered. They have a corporate event on April 8th and absolutely need the correct items by then.

We have two options: (1) expedited reshipping of the correct SKU via 2-day air at our cost, or (2) offering a 30% discount if they keep the navy and we ship the black as a separate order on standard timeline.

The customer is upset and has threatened a chargeback. How should we handle this? Also, should I flag this to the warehouse team as a pick/pack error for the incident report?

Bobby Axelrod
Customer Operations, Axelrod Commerce

---

### Email 4 — Attorney

**Subject:** Discovery Deadline Extension - Opposing Counsel Requesting 30 Days

**Body:**
Nate,

Opposing counsel in the Henderson matter (Case No. 2024-CV-3391) has requested a 30-day extension on the discovery deadline, citing volume of documents and key personnel being unavailable. Current deadline is April 14th; they're asking to push to May 14th.

My concern is that this delays our deposition schedule and could push the trial date. On the other hand, denying it might create goodwill issues with the judge since the request seems reasonable on its face.

I need your direction on three things: (1) agree to the extension, oppose it, or propose a shorter compromise (e.g., 15 days), (2) whether we should condition agreement on them producing their privilege log by the original deadline, and (3) if we should use this as leverage to lock in our preferred deposition dates.

Please advise at your earliest convenience.

Bobby Axelrod, Esq.
Axelrod & Associates, LLP

---

### Email 5 — Professional Services (CPA/Consultant)

**Subject:** Q1 Tax Filing - Discrepancy in Revenue Recognition

**Body:**
Nate,

While preparing your Q1 estimated tax filing, I found a discrepancy in revenue recognition. You have $142,000 in invoices marked as revenue in Q1, but $38,000 of that appears to be for services not yet delivered (contracts signed but work starts in Q2).

Under accrual accounting, we should probably defer that $38,000 to Q2, which would reduce your Q1 estimated tax payment by roughly $9,500. However, if your cash flow situation favors paying more now to avoid a larger Q2 hit, we could keep it as-is.

I need you to confirm: (1) whether those contracts have any deliverables completed in Q1 that would justify partial recognition, (2) your preference on timing of the tax payment, and (3) whether you want me to adjust the books now or wait until we have the full Q2 picture.

Filing deadline for the estimate is April 15th, so I need a decision by April 10th.

Bobby Axelrod, CPA
Axelrod Advisory Services

---

## 5. Test Permutation Matrix

**Control group:** 5 emails × NULL style × NULL behavior = **5 control tests**
**Guided group:** 5 emails × 3 styles × 3 behaviors = **45 guided tests**
**Total: 50 tests**

### 5a. Control Group (Baseline — No Guides)

Run these FIRST to establish the baseline for each industry.

| Test # | Industry | Email | Style Guide | Behavior Guide |
|--------|----------|-------|-------------|----------------|
| C1 | Real Estate | Email 1 | NULL | NULL |
| C2 | Insurance | Email 2 | NULL | NULL |
| C3 | Ecommerce | Email 3 | NULL | NULL |
| C4 | Legal | Email 4 | NULL | NULL |
| C5 | Prof Services | Email 5 | NULL | NULL |

### 5b. Guided Group (Style × Behavior Permutations)

| Test # | Industry | Email | Style Guide | Behavior Guide |
|--------|----------|-------|-------------|----------------|
| 1 | Real Estate | Email 1 | A — Friendly | 1 — High Authority |
| 2 | Real Estate | Email 1 | A — Friendly | 2 — Low Authority |
| 3 | Real Estate | Email 1 | A — Friendly | 3 — Moderate Authority |
| 4 | Real Estate | Email 1 | B — Professional | 1 — High Authority |
| 5 | Real Estate | Email 1 | B — Professional | 2 — Low Authority |
| 6 | Real Estate | Email 1 | B — Professional | 3 — Moderate Authority |
| 7 | Real Estate | Email 1 | C — Balanced | 1 — High Authority |
| 8 | Real Estate | Email 1 | C — Balanced | 2 — Low Authority |
| 9 | Real Estate | Email 1 | C — Balanced | 3 — Moderate Authority |
| 10 | Insurance | Email 2 | A — Friendly | 1 — High Authority |
| 11 | Insurance | Email 2 | A — Friendly | 2 — Low Authority |
| 12 | Insurance | Email 2 | A — Friendly | 3 — Moderate Authority |
| 13 | Insurance | Email 2 | B — Professional | 1 — High Authority |
| 14 | Insurance | Email 2 | B — Professional | 2 — Low Authority |
| 15 | Insurance | Email 2 | B — Professional | 3 — Moderate Authority |
| 16 | Insurance | Email 2 | C — Balanced | 1 — High Authority |
| 17 | Insurance | Email 2 | C — Balanced | 2 — Low Authority |
| 18 | Insurance | Email 2 | C — Balanced | 3 — Moderate Authority |
| 19 | Ecommerce | Email 3 | A — Friendly | 1 — High Authority |
| 20 | Ecommerce | Email 3 | A — Friendly | 2 — Low Authority |
| 21 | Ecommerce | Email 3 | A — Friendly | 3 — Moderate Authority |
| 22 | Ecommerce | Email 3 | B — Professional | 1 — High Authority |
| 23 | Ecommerce | Email 3 | B — Professional | 2 — Low Authority |
| 24 | Ecommerce | Email 3 | B — Professional | 3 — Moderate Authority |
| 25 | Ecommerce | Email 3 | C — Balanced | 1 — High Authority |
| 26 | Ecommerce | Email 3 | C — Balanced | 2 — Low Authority |
| 27 | Ecommerce | Email 3 | C — Balanced | 3 — Moderate Authority |
| 28 | Legal | Email 4 | A — Friendly | 1 — High Authority |
| 29 | Legal | Email 4 | A — Friendly | 2 — Low Authority |
| 30 | Legal | Email 4 | A — Friendly | 3 — Moderate Authority |
| 31 | Legal | Email 4 | B — Professional | 1 — High Authority |
| 32 | Legal | Email 4 | B — Professional | 2 — Low Authority |
| 33 | Legal | Email 4 | B — Professional | 3 — Moderate Authority |
| 34 | Legal | Email 4 | C — Balanced | 1 — High Authority |
| 35 | Legal | Email 4 | C — Balanced | 2 — Low Authority |
| 36 | Legal | Email 4 | C — Balanced | 3 — Moderate Authority |
| 37 | Prof Services | Email 5 | A — Friendly | 1 — High Authority |
| 38 | Prof Services | Email 5 | A — Friendly | 2 — Low Authority |
| 39 | Prof Services | Email 5 | A — Friendly | 3 — Moderate Authority |
| 40 | Prof Services | Email 5 | B — Professional | 1 — High Authority |
| 41 | Prof Services | Email 5 | B — Professional | 2 — Low Authority |
| 42 | Prof Services | Email 5 | B — Professional | 3 — Moderate Authority |
| 43 | Prof Services | Email 5 | C — Balanced | 1 — High Authority |
| 44 | Prof Services | Email 5 | C — Balanced | 2 — Low Authority |
| 45 | Prof Services | Email 5 | C — Balanced | 3 — Moderate Authority |

---

## 6. Test Execution Process

### Prerequisites

- **Chrome extension** running and authenticated against `nate.mcbride23@outlook.com`
- **Railway worker** deployed and polling (or ready to run manually)
- **Supabase dashboard** access with service-role permissions
- **Gmail access** to bobby.axelrod5522@gmail.com for sending test emails
- **User ID** for the test Outlook account (query: `SELECT id FROM profiles WHERE email = 'nate.mcbride23@outlook.com'`)

### Phase 1 — Control Baseline (Tests C1-C5)

**Step 1: Clear profile guides**

Set both guides to NULL so the system uses only its defaults.

```sql
-- Run in Supabase SQL Editor
UPDATE profiles
SET writing_style_guide = NULL,
    behavioral_profile = NULL
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

Verify:
```sql
SELECT id, writing_style_guide, behavioral_profile
FROM profiles
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```
Both columns should return `null`.

**Step 2: Send all 5 test emails**

From bobby.axelrod5522@gmail.com, send each email from Section 4 to nate.mcbride23@outlook.com. Use the exact subject lines and body text. Send all 5 — one per industry.

**Step 3: Wait for pipeline processing**

The extension syncs new emails to Supabase with `status = 'unprocessed'`. The Railway worker picks them up on its next poll cycle:
1. Extension fetches new inbox emails via OWA API → upserts to `emails` table (status defaults to `unprocessed`)
2. Worker claims unprocessed emails → runs signal extraction → classifies → generates drafts
3. Draft is inserted to `drafts` table with `status = 'pending'`
4. Extension's Realtime listener picks up the pending draft → writes it to Outlook Drafts folder via `CreateItem` (MessageDisposition: SaveOnly)

Monitor progress:
```sql
-- Check email ingestion
SELECT id, subject, status, received_time
FROM emails
WHERE user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
  AND sender ILIKE '%bobby.axelrod%'
ORDER BY received_time DESC
LIMIT 5;

-- Check draft generation
SELECT d.id, e.subject, d.status, d.created_at
FROM drafts d
JOIN emails e ON e.id = d.email_id
WHERE d.user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
ORDER BY d.created_at DESC
LIMIT 5;
```

Wait until all 5 emails show `status = 'completed'` in the emails table and all 5 drafts exist.

**Step 4: Record control email IDs**

Save the 5 email IDs — you'll need them for Phase 2 backfill runs.
```sql
SELECT e.id AS email_id, e.subject
FROM emails e
WHERE e.user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
  AND e.sender ILIKE '%bobby.axelrod%'
ORDER BY e.received_time DESC
LIMIT 5;
```

**Step 5: Collect control drafts**

Pull each draft body for scoring:
```sql
SELECT e.subject, d.draft_body
FROM drafts d
JOIN emails e ON e.id = d.email_id
WHERE d.user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
  AND e.sender ILIKE '%bobby.axelrod%'
ORDER BY e.received_time DESC;
```

Paste each draft into the corresponding control slot (C1-C5) in the per-industry result files under `testing/test_emails/`.

Drafts also appear in Outlook's Drafts folder as reply drafts to the original emails.

---

### Phase 2 — Guided Tests (Tests 1-45)

Each test iteration follows this loop: **update guides → delete old draft → regenerate → collect**.

You do NOT need to re-send the emails. The same 5 emails are re-drafted with different guide combos.

**Step 1: Update profile guides**

For each style/behavior combo, update the profile. Use the exact guide text from Sections 2 and 3 (without the `WRITING STYLE GUIDE:` / `BEHAVIORAL PROFILE:` header — the system adds these automatically).

```sql
-- Example: Style A (Friendly) + Behavior 1 (High Authority)
UPDATE profiles
SET writing_style_guide = '- Tone: casual, warm, conversational
- Pleasantries: warm — open with a personalized greeting, close with well-wishes
- Greeting pattern: "Hey [First Name]," or "Hi [First Name]!"
- Sign-off pattern: "Thanks so much!", "Looking forward to hearing from you!", "Have a great day!"
- Sentence structure: longer, flowing sentences with connecting phrases ("by the way", "also wanted to mention")
- Formality: low — contractions encouraged, first-name basis, exclamation points OK
- Response length: thorough — address all points, add context, 150-250 words typical
- Verbal habits: "absolutely", "sounds great", "happy to help", "no worries"
- Punctuation: liberal use of exclamation marks, occasional ellipses for conversational tone',
    behavioral_profile = '- Decision disposition: decides — makes clear decisions, gives definitive answers
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: specific_next_step — commits to concrete actions with detail ("I will send the revised contract by Thursday")
- Scope behavior: expands_scope — proactively raises related issues or next steps the sender has not mentioned
- IF someone asks for approval → THEN grant or deny with reasoning
- IF someone presents options → THEN pick one decisively and explain why
- IF a problem is raised → THEN propose a solution and assign next steps
- IF a deadline is mentioned → THEN confirm or counter-propose with a specific date'
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

Verify the update:
```sql
SELECT writing_style_guide, behavioral_profile
FROM profiles
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

**Step 2: Delete existing drafts for the 5 test emails**

The backfill script skips emails that already have a draft. Delete old drafts first:
```sql
DELETE FROM drafts
WHERE user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
  AND email_id IN (
    '<EMAIL_1_ID>',
    '<EMAIL_2_ID>',
    '<EMAIL_3_ID>',
    '<EMAIL_4_ID>',
    '<EMAIL_5_ID>'
  );
```

Also delete any corresponding Outlook drafts from the Drafts folder (these become stale). The extension's `sweepPendingDrafts` will write new ones once regenerated.

**Step 3: Regenerate drafts via backfill**

Update `worker/backfill_drafts.py` with the 5 test email IDs:
```python
TARGET_EMAIL_IDS = [
    "<EMAIL_1_ID>",  # Real Estate
    "<EMAIL_2_ID>",  # Insurance
    "<EMAIL_3_ID>",  # Ecommerce
    "<EMAIL_4_ID>",  # Legal
    "<EMAIL_5_ID>",  # Professional Services
]
```

Run the backfill:
```powershell
# Via Railway (production env vars)
railway run python worker/backfill_drafts.py

# Or locally with env vars set
python worker/backfill_drafts.py
```

The script:
1. Fetches the email rows from Supabase
2. Reads the current `writing_style_guide` and `behavioral_profile` from the profile
3. Builds `action_context` with the guides injected
4. Calls `DraftGenerator.generate_draft()` with the same prompt template used in production
5. Inserts the new draft via `db.insert_draft()`

**Step 4: Verify new drafts**

```sql
SELECT e.subject, d.draft_body, d.created_at, d.status
FROM drafts d
JOIN emails e ON e.id = d.email_id
WHERE d.user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
  AND e.sender ILIKE '%bobby.axelrod%'
ORDER BY d.created_at DESC;
```

All 5 should have new `created_at` timestamps and `status = 'pending'`.

**Step 5: Collect drafts and record results**

Pull the draft bodies and paste them into the corresponding test slots in the per-industry result files.

**Step 6: Repeat for all 9 combos**

Cycle through all style/behavior permutations:

| Iteration | Style | Behavior | Tests Covered |
|-----------|-------|----------|---------------|
| 1 | A (Friendly) | 1 (High Authority) | 1, 10, 19, 28, 37 |
| 2 | A (Friendly) | 2 (Low Authority) | 2, 11, 20, 29, 38 |
| 3 | A (Friendly) | 3 (Moderate Authority) | 3, 12, 21, 30, 39 |
| 4 | B (Professional) | 1 (High Authority) | 4, 13, 22, 31, 40 |
| 5 | B (Professional) | 2 (Low Authority) | 5, 14, 23, 32, 41 |
| 6 | B (Professional) | 3 (Moderate Authority) | 6, 15, 24, 33, 42 |
| 7 | C (Balanced) | 1 (High Authority) | 7, 16, 25, 34, 43 |
| 8 | C (Balanced) | 2 (Low Authority) | 8, 17, 26, 35, 44 |
| 9 | C (Balanced) | 3 (Moderate Authority) | 9, 18, 27, 36, 45 |

For each iteration: repeat Steps 1-5 above with the appropriate guide text.

---

### Quick Reference — Guide Update SQL Templates

**Style A (Friendly):**
```sql
UPDATE profiles SET writing_style_guide = '- Tone: casual, warm, conversational
- Pleasantries: warm — open with a personalized greeting, close with well-wishes
- Greeting pattern: "Hey [First Name]," or "Hi [First Name]!"
- Sign-off pattern: "Thanks so much!", "Looking forward to hearing from you!", "Have a great day!"
- Sentence structure: longer, flowing sentences with connecting phrases ("by the way", "also wanted to mention")
- Formality: low — contractions encouraged, first-name basis, exclamation points OK
- Response length: thorough — address all points, add context, 150-250 words typical
- Verbal habits: "absolutely", "sounds great", "happy to help", "no worries"
- Punctuation: liberal use of exclamation marks, occasional ellipses for conversational tone'
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

**Style B (Professional):**
```sql
UPDATE profiles SET writing_style_guide = '- Tone: formal, direct, business-like
- Pleasantries: minimal — brief greeting, no small talk
- Greeting pattern: "[First Name]," or "Good morning/afternoon,"
- Sign-off pattern: "Best," or "Regards,"
- Sentence structure: short declarative sentences, no filler
- Formality: high — no contractions, no exclamation marks, title + last name for new contacts
- Response length: concise — key points only, 40-80 words typical
- Verbal habits: "Understood.", "Will do.", "Please advise.", "Confirmed."
- Punctuation: periods only, bullet points for multiple items'
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

**Style C (Balanced):**
```sql
UPDATE profiles SET writing_style_guide = '- Tone: professional but approachable
- Pleasantries: standard — greeting + brief closing
- Greeting pattern: "Hi [First Name],"
- Sign-off pattern: "Thanks," or "Best regards,"
- Sentence structure: medium length, mix of simple and compound sentences
- Formality: moderate — contractions OK, first-name basis, no exclamation marks
- Response length: moderate — address main points with brief context, 80-150 words typical
- Verbal habits: "sounds good", "let me know", "I will take care of it"
- Punctuation: standard — periods, occasional dashes for asides'
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

**Behavior 1 (High Authority):**
```sql
UPDATE profiles SET behavioral_profile = '- Decision disposition: decides — makes clear decisions, gives definitive answers
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: specific_next_step — commits to concrete actions with detail ("I will send the revised contract by Thursday")
- Scope behavior: expands_scope — proactively raises related issues or next steps the sender has not mentioned
- IF someone asks for approval → THEN grant or deny with reasoning
- IF someone presents options → THEN pick one decisively and explain why
- IF a problem is raised → THEN propose a solution and assign next steps
- IF a deadline is mentioned → THEN confirm or counter-propose with a specific date'
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

**Behavior 2 (Low Authority):**
```sql
UPDATE profiles SET behavioral_profile = '- Decision disposition: defers — avoids committing, routes decisions upward or back to sender
- Response completeness: key_point_only — addresses the most important item, leaves rest
- Commitment pattern: vague_forward — references future action without specifics ("I will look into this")
- Scope behavior: stays_narrow — responds only to what was explicitly asked
- IF someone asks for approval → THEN say you need to check with your team/manager
- IF someone presents options → THEN ask for their recommendation instead of choosing
- IF a problem is raised → THEN acknowledge and say you will escalate or follow up
- IF a deadline is mentioned → THEN say you will do your best but cannot guarantee'
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

**Behavior 3 (Moderate Authority):**
```sql
UPDATE profiles SET behavioral_profile = '- Decision disposition: proposes_solution — identifies the issue and offers a specific fix for consideration
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: conditional_decision — commits contingent on a stated condition ("If the numbers check out, let us move forward")
- Scope behavior: adds_context — provides relevant unrequested info without going off-topic
- IF someone asks for approval → THEN give conditional approval with caveats or questions
- IF someone presents options → THEN analyze trade-offs and recommend one with rationale
- IF a problem is raised → THEN diagnose with targeted questions before proposing a fix
- IF a deadline is mentioned → THEN confirm feasibility and flag any dependencies'
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

**NULL (Control):**
```sql
UPDATE profiles
SET writing_style_guide = NULL, behavioral_profile = NULL
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
```

---

### Troubleshooting

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| Emails not appearing in Supabase | Extension not syncing | Open Outlook web, check extension popup for sync status |
| Emails stuck at `unprocessed` | Worker not running | Check `railway logs` or run `railway run python worker/run_pipeline.py` |
| Backfill says "Draft already exists" | Old draft not deleted | Run the DELETE query from Step 2 |
| Draft not appearing in Outlook | Extension Realtime disconnected | Refresh Outlook tab; extension reconnects and runs `sweepPendingDrafts` |
| `insert_draft` skips with "user has edited it" | `user_edited = true` on the draft row | Delete the draft row entirely before regenerating |
| Backfill errors on missing classification | Email was never classified | Run the full pipeline first, or manually insert a classification row |
| Duplicate drafts in Outlook | Realtime listener + `sweepPendingDrafts` both fire | Check Drafts folder after each batch; delete duplicates manually |
| Extension disconnects mid-session | Chrome extension loses connection intermittently | Call `tabs_context_mcp` to reconnect; if persistent, refresh the tab |
| SQL won't paste into Supabase editor | Multi-line text collapses newlines via `type` action | Use Monaco API: `monaco.editor.getEditors()[n].setValue(sql)` via JS console |

---

## Lessons Learned (Phase 1 Control Run)

Captured during the first control baseline run on 2026-03-30.

### Issues Encountered

1. **Email address typo** — Test plan originally had `nate.mcbrid23@outlook.com` (missing 'e'). Email 1 bounced. Fixed across all files; correct address is `nate.mcbride23@outlook.com`.

2. **Duplicate Ecommerce draft** — The Ecommerce email generated 2 identical drafts at the same timestamp. Root cause: the extension's Realtime listener (`handleNewDraft`) and `sweepPendingDrafts` both processed the same pending draft before either could mark it complete. Manually deleted the duplicate.

3. **Chrome extension disconnections** — The browser extension intermittently lost connection to tabs, especially Supabase. Recoverable by calling `tabs_context_mcp`, but adds friction.

4. **Manual Gmail compose was slow** — Typing 5 emails through the Gmail compose UI took ~20 minutes due to field navigation, body entry, and send-button targeting.

5. **Manual draft capture was slow** — Clicking through each Outlook draft and extracting text via `get_page_text` required per-draft navigation.

### Optimizations for Phase 2

**Do not resend emails.** The 5 test emails are already ingested in the `emails` table. Phase 2 only requires:
1. Update guides in `profiles` table (SQL)
2. Delete old drafts from `drafts` table (SQL)
3. Run `backfill_drafts.py` to regenerate (CLI)
4. Query new draft text from `drafts` table (SQL)
5. Paste into test result files

**Query drafts from Supabase, not Outlook.** Pulling `d.draft_body` from the `drafts` table is faster and more reliable than navigating Outlook's Drafts folder. Use Outlook only as a final visual spot-check.

**Batch by guide combination.** Each of the 9 style/behavior combos produces 5 drafts (one per industry). Run all 5 per combo before switching guides. This means 9 iterations of the update-delete-regenerate-collect loop.

**Pre-check for duplicates.** After each backfill run, verify exactly 5 new drafts exist:
```sql
SELECT COUNT(*) FROM drafts
WHERE user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
  AND email_id IN ('<EMAIL_1_ID>', '<EMAIL_2_ID>', '<EMAIL_3_ID>', '<EMAIL_4_ID>', '<EMAIL_5_ID>')
  AND created_at > NOW() - INTERVAL '10 minutes';
```
If count > 5, deduplicate before recording results.

### Key IDs

| Item | Value |
|------|-------|
| User ID | `f0fe5970-dbe7-4ed2-b263-6431ba590111` |
| Email 1 ID (Real Estate) | `4224f78b-533b-44f5-beff-393ec0fa49e8` |
| Email 2 ID (Insurance) | `84a77745-f5f8-4898-9dc3-255e9a72f874` |
| Email 3 ID (Ecommerce) | `f304edac-d6e4-4972-a137-8b7e8d25e7db` |
| Email 4 ID (Legal) | `68652f74-866a-4f6e-a307-f331df377e8b` |
| Email 5 ID (Prof Services) | `97233be2-3030-4eb5-858d-11c19ca63514` |

### Phase 2 Per-Iteration Checklist

For each of the 9 guide combos:

- [ ] Update `writing_style_guide` in profiles (use SQL from Quick Reference)
- [ ] Update `behavioral_profile` in profiles (use SQL from Quick Reference)
- [ ] Verify both fields updated correctly
- [ ] Delete existing drafts for the 5 test email IDs
- [ ] Run `backfill_drafts.py`
- [ ] Verify 5 new drafts exist (no duplicates)
- [ ] Query `draft_body` for all 5
- [ ] Paste into per-industry result files under the correct test number
- [ ] Spot-check one draft in Outlook Drafts folder (optional)

---

## 7. Results Tracking

**Scoring method:** Fully automated via LLM-as-judge. Each test's per-industry file contains the inbound email, the active guides, and the generated draft — everything the judge needs.

**Per-industry result files:** `testing/test_emails/`
- `real_estate.md` — Tests C1, 1-9
- `insurance.md` — Tests C2, 10-18
- `ecommerce.md` — Tests C3, 19-27
- `legal.md` — Tests C4, 28-36
- `professional_services.md` — Tests C5, 37-45

### Scoring Rubric (1-5 Scale)

**Style Adherence** *(N/A for control tests)*
- **1** — Completely ignores the style guide. Wrong tone, formality, length, greeting/sign-off.
- **2** — Gets one dimension right but misses most others. Tone or length noticeably off.
- **3** — Matches general tone/formality but deviates on specific rules (e.g., exclamation marks when guide says "periods only," 2x target word count).
- **4** — Closely follows guide with minor deviations (sign-off close but not exact, word count slightly outside range).
- **5** — Indistinguishable from someone with this writing style. All dimensions match.

**Behavior Adherence** *(N/A for control tests)*
- **1** — Opposite behavioral pattern (decides when guide says defer, narrow when guide says expand).
- **2** — Partially matches one dimension, contradicts others. Decision disposition or commitment clearly wrong.
- **3** — General posture correct (decisive vs. deferential) but specific IF-THEN rules not followed.
- **4** — Most IF-THEN rules correct. One dimension slightly off.
- **5** — All four dimensions and all applicable IF-THEN rules correctly reflected.

**Content Quality**
- **1** — Ignores action items or responds to something not asked.
- **2** — Acknowledges email but only addresses one action item. Key decisions unaddressed.
- **3** — Addresses primary action item, secondary items superficial or placeholder-only.
- **4** — All action items addressed substantively. Minor gaps but reply moves things forward.
- **5** — Every action item addressed with appropriate substance. Send-ready with minimal editing.

**Naturalness**
- **1** — Reads like AI: bullet lists where humans write sentences, hedging phrases, robotic phrasing.
- **2** — Mostly AI with a few natural touches. Telltale patterns: unnecessary caveats, overly structured.
- **3** — Passable as human but something feels off — too polished, too even-handed.
- **4** — Reads like a real person. Natural flow, appropriate informality, no AI tells.
- **5** — Completely indistinguishable from human-written email.

**Overall**
- **1** — Would not send. Needs complete rewrite.
- **2** — Needs major edits — restructuring, rewriting key sections.
- **3** — Usable starting point but needs meaningful edits.
- **4** — Would send with minor tweaks.
- **5** — Would send as-is.

### Control Results (Baseline)

| Test # | Industry | Style | Behavior | Content Score | Natural Score | Overall | Notes |
|--------|----------|-------|----------|:-------------:|:-------------:|:-------:|-------|
| C1 | Real Estate | NULL | NULL | | | | |
| C2 | Insurance | NULL | NULL | | | | |
| C3 | Ecommerce | NULL | NULL | | | | |
| C4 | Legal | NULL | NULL | | | | |
| C5 | Prof Services | NULL | NULL | | | | |

### Guided Results

| Test # | Industry | Style | Behavior | Style Score | Behavior Score | Content Score | Natural Score | Overall | Delta vs Control | Notes |
|--------|----------|-------|----------|:-----------:|:--------------:|:-------------:|:-------------:|:-------:|:----------------:|-------|
| 1 | Real Estate | A | 1 | | | | | | | |
| 2 | Real Estate | A | 2 | | | | | | | |
| 3 | Real Estate | A | 3 | | | | | | | |
| 4 | Real Estate | B | 1 | | | | | | | |
| 5 | Real Estate | B | 2 | | | | | | | |
| 6 | Real Estate | B | 3 | | | | | | | |
| 7 | Real Estate | C | 1 | | | | | | | |
| 8 | Real Estate | C | 2 | | | | | | | |
| 9 | Real Estate | C | 3 | | | | | | | |
| 10 | Insurance | A | 1 | | | | | | | |
| 11 | Insurance | A | 2 | | | | | | | |
| 12 | Insurance | A | 3 | | | | | | | |
| 13 | Insurance | B | 1 | | | | | | | |
| 14 | Insurance | B | 2 | | | | | | | |
| 15 | Insurance | B | 3 | | | | | | | |
| 16 | Insurance | C | 1 | | | | | | | |
| 17 | Insurance | C | 2 | | | | | | | |
| 18 | Insurance | C | 3 | | | | | | | |
| 19 | Ecommerce | A | 1 | | | | | | | |
| 20 | Ecommerce | A | 2 | | | | | | | |
| 21 | Ecommerce | A | 3 | | | | | | | |
| 22 | Ecommerce | B | 1 | | | | | | | |
| 23 | Ecommerce | B | 2 | | | | | | | |
| 24 | Ecommerce | B | 3 | | | | | | | |
| 25 | Ecommerce | C | 1 | | | | | | | |
| 26 | Ecommerce | C | 2 | | | | | | | |
| 27 | Ecommerce | C | 3 | | | | | | | |
| 28 | Legal | A | 1 | | | | | | | |
| 29 | Legal | A | 2 | | | | | | | |
| 30 | Legal | A | 3 | | | | | | | |
| 31 | Legal | B | 1 | | | | | | | |
| 32 | Legal | B | 2 | | | | | | | |
| 33 | Legal | B | 3 | | | | | | | |
| 34 | Legal | C | 1 | | | | | | | |
| 35 | Legal | C | 2 | | | | | | | |
| 36 | Legal | C | 3 | | | | | | | |
| 37 | Prof Services | A | 1 | | | | | | | |
| 38 | Prof Services | A | 2 | | | | | | | |
| 39 | Prof Services | A | 3 | | | | | | | |
| 40 | Prof Services | B | 1 | | | | | | | |
| 41 | Prof Services | B | 2 | | | | | | | |
| 42 | Prof Services | B | 3 | | | | | | | |
| 43 | Prof Services | C | 1 | | | | | | | |
| 44 | Prof Services | C | 2 | | | | | | | |
| 45 | Prof Services | C | 3 | | | | | | | |

### Summary — Guide Impact by Dimension

| Dimension | Avg Control Score | Avg Guided Score | Avg Delta | Notes |
|-----------|:-----------------:|:----------------:|:---------:|-------|
| Style A (Friendly) | | | | |
| Style B (Professional) | | | | |
| Style C (Balanced) | | | | |
| Behavior 1 (High Auth) | | | | |
| Behavior 2 (Low Auth) | | | | |
| Behavior 3 (Moderate) | | | | |
