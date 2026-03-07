# Clarion AI — Prediction Model Implementation Plan

**Version:** 1.0
**Date:** March 6, 2026
**Input:** `email_extraction.json` (v2.0 — 5,570 messages, 120-day window)
**Goal:** Given an incoming email, predict whether the user will respond and, if so, generate a draft in the user's voice.

---

## Prerequisites — Data Cleanup

Before building the model, the following issues identified during data review must be resolved. These directly affect model accuracy.

### P1: Remove automated messages from response_events

**Problem:** 420 automated messages (Bloomberg, postmaster, KnowBe4, Paylocity, SharePoint) are included in response_events. 39 are incorrectly tagged as responded=true due to thread-matching artifacts (e.g., forwarding a Bloomberg article gets attributed as a "response" to Bloomberg).

**Fix:** Filter response_events to exclude any record where the inbound_message_id maps to a message with is_automated=true. This reduces the response_events table from 4,378 to ~3,958 records and removes 39 false positive response labels.

**Validation:** After filtering, recompute overall_reply_rate in user_profile. It should increase slightly (the automated emails were dragging it down with false negatives).

### P2: Tighten has_action_language detection

**Problem:** Fires on 94.5% of emails, providing zero discriminating power. Response rate with action language (37.5%) is actually lower than without (38.5%).

**Fix:** Replace single-word matching with phrase-level detection. Only flag as true when the first two sentences of the body (excluding signatures and quoted content) contain phrases like: "can you", "could you", "please review", "please send", "need by", "deadline is", "your approval", "waiting on", "action required", "please confirm". Single words like "please" appearing in signatures or footers should not trigger the flag.

**Validation:** After tightening, has_action_language should fire on 20-40% of emails. Re-measure the response rate split — it should show meaningful separation.

### P3: Exclude self-forward addresses from contacts

**Problem:** nate.mcbrideac@gmail.com is the #3 contact by sent_total (181 sends, all forwards). This is a self-archiving pattern, not a communication relationship. It inflates forward counts and pollutes the contact graph.

**Fix:** Add an `is_self` boolean to the contacts table. Flag any contact matching the user's known personal addresses. Exclude is_self contacts from all downstream feature calculations (co-recipient clusters, forward targets, domain aggregations).

**Validation:** After exclusion, forward_pct in user_profile should drop. forward_to_count across other contacts should remain unchanged.

### P4: Cap response latency and flag thread revivals

**Problem:** Latency outliers up to 2,350 hours (98 days) skew mean calculations. These are thread revivals, not genuine responses.

**Fix:** Add a `is_thread_revival` boolean to response_events. Set to true when response_latency_hrs exceeds 168 hours (7 days). When computing avg_reply_latency_hrs and median_reply_latency_hrs in both contacts and user_profile, exclude thread revivals. Keep the raw latency value intact for reference.

**Validation:** After exclusion, avg_reply_latency_hrs in user_profile should drop significantly from 71.88 hours. Median should remain relatively stable since it was already robust to outliers.

### P5: Verify body_length stripping

**Problem:** Thread_0070 shows a user_avg_body_length of 10,743 characters for a single message. This suggests quoted reply content and/or signatures may not be stripped consistently.

**Fix:** Spot-check the 20 longest body_length values in the messages table. For each, confirm that the body_snippet doesn't begin with quoted content ("On [date]..." or ">") and that signature blocks aren't included. Adjust the stripping logic as needed.

**Validation:** After fix, the longest body_length values should rarely exceed 3,000-4,000 characters for typical business email.

### P6: Document the validation warning

**Problem:** meta.validation_warnings is 1, but no detail is provided on what triggered it.

**Fix:** Add a `validation_details` array to meta that describes each warning (e.g., "Contact nate.mcbrideac@gmail.com has sent_total > 10 and reply_rate is null").

### P7: Verify other_replies_before_user accuracy

**Problem:** This field hasn't been validated against known threads.

**Fix:** Select 5-10 response_events from threads you know well (e.g., the Thomas Ranch Twain thread). Manually count how many other people replied between the inbound message and your response. Compare against the computed value. If systematically off, debug the computation logic.

---

## Phase 1: Feature Importance Analysis

**Objective:** Determine which features in the dataset actually predict response behavior, and by how much.

**Approach:** For each candidate feature, compute the response rate when the feature is present versus absent. The difference (or ratio) tells you how much predictive signal that feature carries.

### 1.1 — Join the data

The response_events table has the email-level features (has_question, mentions_user_name, user_in_to, etc.) but lacks the sender-level and thread-level context. Build a joined analysis table:

```
analysis_table = response_events
    LEFT JOIN contacts ON response_events.sender = contacts.email
    LEFT JOIN threads ON response_events.conversation_id = threads.conversation_id
```

Each row in the analysis table represents one inbound email with:
- Its own features (from response_events)
- The sender's historical profile (from contacts)
- The thread's context (from threads)
- The target variable: responded (true/false)

### 1.2 — Measure univariate signal strength

For each candidate feature, compute:

| Metric | Formula |
|--------|---------|
| Response rate when present | count(responded=true AND feature=true) / count(feature=true) |
| Response rate when absent | count(responded=true AND feature=false) / count(feature=false) |
| Lift | rate_when_present / baseline_response_rate |
| Separation | rate_when_present - rate_when_absent |

**Candidate features (boolean):**

| Feature | Source Table | What It Measures |
|---------|-------------|------------------|
| user_in_to | response_events | Was user directly addressed? |
| user_sole_to | response_events | Was user the only recipient? |
| mentions_user_name | response_events | Does the email call out the user? |
| has_question | response_events | Does the email contain a question? |
| has_action_language | response_events | Does the email request action? (after P2 fix) |
| has_attachments | response_events | Does the email include files? |
| sender.is_internal | contacts | Is the sender internal? |

**Candidate features (continuous — bin into ranges for analysis):**

| Feature | Source Table | Suggested Bins |
|---------|-------------|----------------|
| sender.reply_rate | contacts | 0-0.1, 0.1-0.3, 0.3-0.5, 0.5-0.7, 0.7-1.0 |
| sender.reply_rate_30d | contacts | Same bins |
| thread.user_participation_rate | threads | 0, 0.01-0.1, 0.1-0.3, 0.3+ |
| recipient_count | response_events | 1, 2-3, 4-6, 7+ |
| thread_depth_at_receipt | response_events | 1, 2-3, 4-6, 7+ |
| body_length | response_events | 0-100, 100-500, 500-2000, 2000+ |

### 1.3 — Measure feature interactions

After univariate analysis, test key feature combinations. The goal is to find pairs where the combined signal is stronger than either feature alone.

**Priority interactions to test:**

- `sender.reply_rate` (binned) × `user_in_to` — Does being in TO matter more for low-reply-rate senders?
- `sender.reply_rate` × `mentions_user_name` — Does name mention compensate for low reply rate?
- `user_in_to` × `recipient_count` — Does being in TO matter less when there are many recipients?
- `sender.is_internal` × `user_in_to` — Do internal emails behave differently than external?
- `thread.user_participation_rate` × `thread_depth_at_receipt` — Deep threads where user rarely participates = monitoring behavior?

For each combination, compute the response rate in each cell. If a combination shows a response rate dramatically different from what you'd expect given the individual features, that interaction should become an explicit feature in the scoring model.

### 1.4 — Rank features by importance

After computing all univariate and interaction signals, rank features by their separation value (response rate when present minus response rate when absent). Features with less than 3 percentage points of separation should be dropped from the model — they add complexity without improving predictions.

**Expected outcome (based on preliminary analysis):**

| Feature | Expected Rank | Preliminary Separation |
|---------|---------------|----------------------|
| sender.reply_rate | 1 | Very high (dominant feature) |
| mentions_user_name | 2 | ~13 points |
| user_in_to | 3 | ~12.5 points |
| thread.user_participation_rate | 4 | Unknown — likely high |
| has_question | 5 | ~5.4 points |
| recipient_count | 6 | Unknown |
| user_sole_to | 7 | ~3.5 points |
| has_action_language | TBD | Depends on P2 fix |
| has_attachments | TBD | Unknown |

### 1.5 — Output

Produce a feature report showing each feature's response rate split, lift, separation, and interaction effects. This report directly informs the weights in Phase 2.

---

## Phase 2: Build the Calibrated Scorer

**Objective:** Create a scoring function that takes an incoming email's metadata and returns a response probability.

### 2.1 — Scoring architecture

The scorer uses a **base-rate-plus-adjustments** approach:

```
Step 1: Start with the sender's historical reply_rate as the base probability.
        - If sender is known: use contacts.reply_rate (prefer reply_rate_30d if available)
        - If sender is unknown but domain is known: use domains.avg_reply_rate
        - If both are unknown: use user_profile.overall_reply_rate

Step 2: Apply lift multipliers from Phase 1 for each feature present.
        - Multiply base probability by each applicable lift factor
        - Cap the result at 0.95 (never 100% confident)
        - Floor the result at 0.01 (never 0% confident)

Step 3: Output the adjusted probability.
```

### 2.2 — Example calculation

An email arrives from Gina at Corridor Title. She's in the contacts table with a reply_rate of 0.728.

```
Base probability:                     0.728
User is in TO (lift 1.13):            0.728 × 1.13 = 0.823
Email mentions "Nate" (lift 1.46):    0.823 × 1.46 = would exceed cap
Capped at:                            0.95

Result: 0.95 → HIGH CONFIDENCE → Generate draft
```

Another email arrives from a Bloomberg newsletter:

```
Base probability:                     Sender is automated → 0.00
Result: excluded from scoring entirely (is_automated = true)
```

Another email arrives from someone you don't know at a new domain, CC-only, no question, no name mention:

```
Base probability:                     Unknown sender/domain → use overall_reply_rate = 0.169
User in CC only (lift <1, say 0.71):  0.169 × 0.71 = 0.120
No other positive signals.

Result: 0.12 → LOW CONFIDENCE → Do nothing
```

### 2.3 — Derive the actual lift factors

Using the response_events analysis from Phase 1, compute lift factors:

```
lift(feature) = response_rate_when_feature_is_true / baseline_response_rate
```

For features that reduce response probability (like CC-only placement), the lift will be less than 1.0, which correctly pulls the score down.

For continuous features like sender.reply_rate, you don't compute a lift — it IS the base probability. The lift factors only apply to the boolean and categorical features.

### 2.4 — Handle feature interactions

If Phase 1 reveals strong interactions, implement them as conditional lifts. For example, if "user_in_to × recipient_count > 6" has a much lower response rate than "user_in_to × recipient_count <= 3", implement as:

```
if user_in_to AND recipient_count > 6:
    apply lift_to_group (e.g., 1.05)
elif user_in_to AND recipient_count <= 3:
    apply lift_to_direct (e.g., 1.25)
```

### 2.5 — Build the scorer as a function

The scorer should be a pure function with this signature:

```
Input:
    - inbound email metadata (sender, to_recipients, cc_recipients, subject,
      has_question, has_action_language, mentions_user_name, body_length,
      recipient_count, has_attachments)
    - contacts lookup table
    - domains lookup table
    - threads lookup table (for thread context if conversation_id is known)
    - user_profile

Output:
    - response_probability (float, 0.01 to 0.95)
    - confidence_tier ("high", "medium", "low")
    - contributing_factors (list of features that influenced the score, for explainability)
```

The `contributing_factors` output is important for user trust. When Clarion AI surfaces a draft, it can show: "Drafted because you respond to 73% of emails from this sender, and this email mentions you by name."

### 2.6 — Set the decision thresholds

| Probability Range | Confidence Tier | Action |
|-------------------|----------------|--------|
| 0.60 and above | High | Auto-generate draft, surface to user |
| 0.35 to 0.59 | Medium | Flag as "likely needs response" — draft on click |
| Below 0.35 | Low | Do nothing |

These thresholds are starting points. Calibrate them after backtesting (Phase 3) by looking at what threshold values maximize precision without sacrificing too much recall.

**Important:** Given the 16.87% base response rate, the model needs to be precision-oriented. A false positive (drafting a response for an email the user ignores) is much more likely than a false negative and erodes user trust faster. Err on the side of higher thresholds early on and lower them as the model proves accurate.

---

## Phase 3: Backtest Against Historical Data

**Objective:** Measure how well the scorer performs against the known response_events before deploying it.

### 3.1 — Score every response event

Run the scorer against every row in the response_events table (after the P1-P7 cleanup). Each event gets a predicted probability and confidence tier.

### 3.2 — Compute accuracy metrics

Using the high-confidence threshold (0.60) as the draft trigger:

| Metric | Formula | What It Tells You |
|--------|---------|-------------------|
| Precision | true_positives / (true_positives + false_positives) | When the model says "draft," how often is it right? |
| Recall | true_positives / (true_positives + false_negatives) | Of all the emails you did respond to, how many did the model catch? |
| False positive rate | false_positives / (false_positives + true_negatives) | How often does the model draft unnecessarily? |

Where:
- True positive = model predicted high confidence AND user responded
- False positive = model predicted high confidence AND user did NOT respond
- False negative = model predicted low/medium AND user DID respond
- True negative = model predicted low/medium AND user did NOT respond

### 3.3 — Target metrics

For a v1 launch, aim for:

- **Precision ≥ 70%** — At least 7 out of 10 auto-drafted emails should be ones the user actually would have responded to.
- **Recall ≥ 40%** — The model catches at least 4 out of 10 emails that need a response. The rest the user handles manually (no worse than having no tool at all).
- **False positive rate ≤ 5%** — No more than 1 in 20 non-response emails gets an unnecessary draft.

Precision matters more than recall at launch. Users will tolerate missing some drafts (they already handle all emails manually). They won't tolerate being bombarded with bad drafts.

### 3.4 — Adjust thresholds

If precision is too low, raise the high-confidence threshold. If recall is too low, check whether the medium tier is catching the missed emails — if it is, the "flag but don't draft" tier is working as intended.

### 3.5 — Examine the misclassifications

Pull the false positives and false negatives. Look for patterns:

- Are false positives clustered around certain senders or domains?
- Are false negatives emails where the user responded unusually late (>48 hours)?
- Are there feature combinations that consistently mislead the model?

Use these patterns to refine the lift factors or add new conditional rules.

---

## Phase 4: Cold-Start Handling via Contact Graph

**Objective:** Provide reasonable predictions for senders not in the contacts table.

### 4.1 — Build the contact graph

Using the contacts table (co_recipients_top5 field) and the threads table (participants field), construct a graph where:

- Nodes = contacts
- Edges = co-appearance in emails, weighted by frequency
- Clusters = groups of contacts that frequently appear together

### 4.2 — Domain-based fallback

When an email arrives from an unknown sender at a known domain:

```
1. Look up domains.avg_reply_rate for the sender's domain
2. Use that as the base probability
3. Apply the same lift factors as usual for TO/CC, mentions, questions, etc.
```

### 4.3 — Co-participant-based fallback

When an email arrives from an unknown sender at an unknown domain, but the email includes known contacts:

```
1. Identify which known contacts are on the TO/CC line
2. Look up those contacts' reply_rates
3. Average them, weighted by the user's interaction volume with each
4. Use that weighted average as the base probability
```

This handles the "new attorney at a firm you've never emailed, but Wes is also on the thread" scenario.

### 4.4 — Full cold start

When the sender is unknown, the domain is unknown, and no known contacts are on the email:

```
1. Use user_profile.overall_reply_rate as the base probability
2. Lean heavily on email-level features (user_in_to, mentions_user_name,
   has_question, user_sole_to) to adjust
3. Be conservative — bias toward the "flag" tier rather than auto-drafting
```

---

## Phase 5: Voice Profile for Draft Generation

**Objective:** When the model decides to generate a draft, ensure it sounds like the user wrote it.

### 5.1 — Extract the voice profile from sent emails

Analyze the sent messages in the messages table to extract:

- **Greeting patterns** — Frequency distribution of how the user opens emails ("Hi [name],", "Hey [name],", "[Name] —", no greeting, etc.)
- **Closing patterns** — How the user signs off ("Thanks,", "Thank you,", "Best,", no closing, etc.)
- **Sentence length** — Average and standard deviation of words per sentence
- **Paragraph structure** — Average paragraphs per email, average sentences per paragraph
- **Email length by type** — Average word count for replies vs. forwards vs. new threads
- **Contraction rate** — Frequency of contractions vs. full forms
- **Formality markers** — Presence of hedging ("I think," "probably"), directness ("Please send," "We need"), and conversational tone ("Just checking in," "Quick question")
- **Punctuation habits** — Use of em dashes, semicolons, exclamation points, ellipses

### 5.2 — Segment the profile

Build style profiles at three levels:

| Level | Scope | When to Use |
|-------|-------|-------------|
| Global | All sent emails | Fallback for unknown recipients |
| Domain | Sent emails to each domain | New contacts at known organizations |
| Recipient | Sent emails to specific contacts | Known contacts with 10+ sent emails |

The segmentation captures how the user adjusts tone across audiences. Internal emails likely differ from emails to outside counsel or lender contacts.

### 5.3 — Build the generation prompt

At draft time, construct the LLM prompt with:

```
1. System prompt: The extracted style feature profile
   ("Write in short, direct paragraphs. Open with 'Hi [name],' or no greeting.
    Use contractions. Average 8-12 words per sentence. Close with 'Thanks,'")

2. Few-shot examples: 2-3 of the user's recent sent emails to the same
   recipient or domain, selected by recency and relevance

3. Thread context: The full thread history so the model understands
   the conversation state

4. Instruction: Generate a reply to the most recent message in the user's voice
```

### 5.4 — Example selection logic

When selecting few-shot examples for the prompt:

```
Priority 1: Same recipient, same message_type (reply), last 30 days
Priority 2: Same domain, same message_type, last 30 days
Priority 3: Same topic cluster, last 30 days
Priority 4: Any sent email of the same message_type, last 30 days
```

Filter out emails with unusually short or long body_length (outliers make bad examples). Prefer emails where the thread context is similar (e.g., both are mid-thread replies rather than thread initiators).

---

## Phase 6: Feedback Loop

**Objective:** Improve the model over time using real user behavior.

### 6.1 — Capture feedback signals

Every time the model generates or suggests a draft, record:

| Signal | Meaning |
|--------|---------|
| Draft accepted (sent as-is or with minor edits) | Strong positive — model was right to draft, voice was correct |
| Draft heavily edited then sent | Weak positive — model was right to draft, but voice or content needs work |
| Draft dismissed, user wrote their own reply | Moderate negative — model was right that a response was needed, but draft wasn't useful |
| Draft dismissed, user never responded | Strong negative — model was wrong, this email didn't need a response |
| Email flagged (medium tier), user clicked and responded | Positive signal for lowering the draft threshold for this type of email |
| Email not flagged, user responded manually | False negative — model missed an email that needed a response |

### 6.2 — Retrain cadence

After collecting feedback, periodically:

1. Recompute contact reply rates incorporating the new data
2. Re-measure feature lift factors to see if signal strengths have shifted
3. Adjust decision thresholds based on observed precision and recall
4. Update the voice profile as new sent emails accumulate

A monthly retraining cycle is reasonable initially. As the dataset grows, consider moving to weekly.

### 6.3 — Transition to ML classifier

Once you have 500+ feedback events (roughly 2-3 months of active use), you have enough data to train a lightweight classifier. A logistic regression or gradient-boosted decision tree (XGBoost/LightGBM) on the feature set would be appropriate.

The classifier replaces the manual lift factors with learned weights, and can capture complex feature interactions that the heuristic scorer misses. But the heuristic scorer remains valuable as a baseline and fallback — if the classifier produces a surprising result, the heuristic score provides a sanity check.

---

## Implementation Sequence

| Order | Phase | Estimated Effort | Dependency |
|-------|-------|-----------------|------------|
| 0 | Data cleanup (P1-P7) | 1-2 days | None — do this first |
| 1 | Feature importance analysis | 1-2 days | Clean data |
| 2 | Calibrated scorer | 2-3 days | Feature analysis complete |
| 3 | Backtest and threshold calibration | 1 day | Scorer built |
| 4 | Cold-start handling | 1-2 days | Scorer built, contact graph derived |
| 5 | Voice profile extraction | 2-3 days | Sent email corpus |
| 6 | Draft generation integration | 3-5 days | Scorer + voice profile |
| 7 | Feedback capture | 1-2 days | Draft generation live |
| 8 | ML classifier | 2-3 days | 500+ feedback events collected |

**Total to MVP (Phases 0-6):** ~2-3 weeks
**Total to ML upgrade (Phase 8):** ~3-4 months after launch

---

## Appendix: Re-Scoring the Tyler Email With This Model

Using the model from this plan to score Tyler's "Any comments though?" email from the Thomas Ranch thread:

```
Sender: tmills@arete-collective.com
    → contacts.reply_rate = 0.327
    → contacts.reply_rate_30d = 0.268
    → Use 30d rate as it's more current: base = 0.268

Features present:
    → user_in_to = true (lift TBD from Phase 1, preliminary ~1.13)
    → mentions_user_name = false (no lift)
    → has_question = true (lift TBD, preliminary ~1.03)
    → recipient_count = 5 (likely a slight negative lift for group emails)
    → thread.user_participation_rate = low (1 out of 20+ messages)
        → This is the key feature: very low participation = monitoring role
        → Expected lift well below 1.0

Estimated score: 0.268 × 1.13 × 1.03 × 0.90 (group) × 0.60 (low participation)
              ≈ 0.168

Result: 0.168 → LOW CONFIDENCE → Do nothing ✓
```

The model correctly identifies this as an email the user would not respond to, primarily because the sender's 30-day reply rate is moderate and the user's participation in this thread is very low. No draft generated.
