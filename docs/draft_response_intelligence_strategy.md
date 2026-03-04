# Email Response Intelligence - Strategy Proposal

## The Problem

The email monitoring tool currently generates draft responses for emails that don't actually require a reply from Nate. The root cause is twofold:

1. **No recipient awareness** - The tool doesn't know whether Nate is in the To or CC field, and doesn't extract recipient lists at all.
2. **Overly broad response criteria** - The AI is instructed to flag `needs_response: true` for any actionable request in the email, regardless of whether it's directed at Nate or someone else on the thread.

This results in unnecessary draft responses being created for group emails where the action belongs to another recipient, forwarded FYI messages, threads Nate is passively observing, and emails where someone else has already handled the request.

---

## Proposed Solution: Multi-Signal Response Intelligence

Rather than relying solely on AI judgment with minimal context, we enrich the analysis with structured signals extracted from the email and from historical correspondence patterns. These signals are passed to the AI alongside the email content, giving it the context to make significantly better decisions about whether a draft is warranted.

---

## Signal 1: To/CC Recipient Positioning

**What it does:** Extracts the full To and CC recipient lists from each email and determines Nate's position (To, CC, or BCC).

**Why it matters:** Being in the To field signals direct involvement. Being in CC typically means "for your awareness." This is the most fundamental signal currently missing.

**Data passed to AI:**
- `user_position: "TO"` or `"CC"` or `"BCC"`
- `to_recipients: ["Tyler Mills", "Joe Rentfro", "Gina Kufrovich"]`
- `cc_recipients: ["Nate McBride"]`

**Implementation:** Extract `message.To` and `message.CC` from the Outlook COM message object during Phase 1 (email extraction).

---

## Signal 2: Direct Name Mention

**What it does:** Scans the email body for explicit mentions of Nate's name or email address.

**Why it matters:** Even when Nate is in CC, if the sender writes "Nate, can you review this?" that overrides the CC positioning. Conversely, a To-line email with no mention of Nate's name in a multi-recipient message suggests the request may be aimed at another recipient.

**Detection targets:**
- "Nate" (standalone word, not part of another word)
- "@Nate McBride" or "@nmcbride"
- "Nate McBride"

**Data passed to AI:**
- `user_mentioned_by_name: true/false`
- `name_mention_context: "Nate, can you send the updated pro forma?"` (the sentence containing the mention)

**Implementation:** Regex scan on email body before sending to AI. No API cost.

---

## Signal 3: FYI & No-Response Language Detection

**What it does:** Detects explicit language in the email indicating no response is expected.

**Why it matters:** Senders frequently signal intent directly in their message. The tool currently ignores these signals entirely.

**Detection patterns:**

*FYI / Informational:*
- "FYI", "For your information", "For your reference"
- "For your records", "Just a heads up"
- "Looping you in", "Keeping you in the loop"
- "For visibility", "For awareness"

*Terminal Acknowledgments (short messages that close a thread):*
- "Thanks!", "Thank you!", "Got it", "Will do"
- "Sounds good", "Perfect", "Noted"
- "Acknowledged", "Received"

*Explicit No-Response:*
- "No action needed", "No response necessary"
- "Nothing needed from you", "No reply needed"

**Data passed to AI:**
- `fyi_language_detected: true/false`
- `terminal_acknowledgment: true/false` (message body under 50 characters and matches pattern)

**Implementation:** Keyword/phrase matching on email body. No API cost.

---

## Signal 4: Subject Line Classification

**What it does:** Analyzes the email subject prefix to infer the message's purpose.

**Why it matters:** Forwarded emails (FW:) are disproportionately informational. Deep reply chains (RE: RE: RE:) where Nate hasn't participated suggest passive observation. New-subject emails carry higher intent.

**Classification:**

| Prefix | Classification | Response Likelihood |
|--------|---------------|-------------------|
| *(none)* | New conversation | Higher |
| `RE:` | Active reply | Context-dependent |
| `FW:` | Forwarded content | Lower (usually FYI) |
| `FW: FW:` | Chain forward | Very low |

**Data passed to AI:**
- `subject_type: "new"` or `"reply"` or `"forward"` or `"chain_forward"`

**Implementation:** String prefix parsing. No API cost.

---

## Signal 5: Recipient Count

**What it does:** Counts the total number of recipients (To + CC) on the email.

**Why it matters:** An email sent to one person is almost certainly directed at them. An email sent to six people is more likely a broadcast or group discussion. The probability that any specific recipient needs to respond decreases as recipient count increases.

**Data passed to AI:**
- `total_recipients: 4`
- `to_count: 3`
- `cc_count: 1`

**Implementation:** Derived from Signal 1 (recipient extraction). No additional cost.

---

## Signal 6: Thread Participation History

**What it does:** For emails in an existing conversation thread, checks whether Nate has actively participated (sent replies) in that thread.

**Why it matters:** If a conversation has 10 messages and Nate has never replied, he's a passive observer. If he's been actively replying, he's an engaged participant who may need to respond again. This is one of the strongest behavioral signals available.

**Data passed to AI:**
- `thread_message_count: 8`
- `user_replies_in_thread: 0`
- `user_active_in_thread: false`

**Implementation:** Uses the existing `ConversationID` grouping to pull the full thread history. Checks the Sent Items folder for messages matching the same `ConversationID`. Moderate implementation effort.

---

## Signal 7: Historical Response Rate Per Sender

**What it does:** For each frequent sender, calculates what percentage of their emails Nate typically responds to.

**Why it matters:** Tyler Mills sends ~236 emails/month. If Nate replies to ~30 of them, his response rate to Tyler is approximately 13%. That's a strong baseline prior: most Tyler emails don't need a response. Compare that with a lender contact like Amy Tieu, where the response rate might be 70-80%.

**Data passed to AI:**
- `sender_historical_response_rate: 0.13`
- `sender_emails_last_30_days: 236`
- `sender_nate_replies_last_30_days: 30`

**Implementation:** Requires scanning the Sent Items folder and matching replies to incoming sender ConversationIDs. Calculated during the contact directory build (see Contact Intelligence Matrix below). Moderate effort.

---

## Signal 8: Already-Handled Detection

**What it does:** Checks whether someone else has already replied to an email thread after the message being analyzed, potentially resolving the question or request.

**Why it matters:** If Tyler asks a question to four people and Gina already replied with the answer, Nate likely doesn't need to also respond. This prevents the tool from drafting responses to questions that have already been addressed.

**Data passed to AI:**
- `subsequent_replies_exist: true`
- `subsequent_reply_from: "Gina Kufrovich"`
- `question_potentially_resolved: true`

**Implementation:** After extracting the target email, check the conversation thread for newer messages from other recipients. Moderate effort, integrates with conversation grouping.

---

## Signal 9: Outlook Importance Flag

**What it does:** Reads the sender-set importance level from the Outlook message object.

**Why it matters:** While most emails are set to Normal, a Low importance flag is a mild signal toward "no response needed." A High importance flag adds urgency weight. This is free data already available on the message object.

**Data passed to AI:**
- `outlook_importance: "normal"` or `"high"` or `"low"`

**Implementation:** Read `message.Importance` property during Phase 1 extraction. Trivial.

---

## The Contact Intelligence Matrix

### Concept

Rather than treating every sender as unknown, the tool builds and maintains a directory of Nate's most frequent contacts with structured profile data. When an email arrives, the tool looks up every person on the email (sender + recipients) and provides their profiles to the AI, enabling it to reason about who is best positioned to respond.

### What the Matrix Contains

For each contact in the directory:

| Field | Example | Source |
|-------|---------|--------|
| Name | Gina Kufrovich | Email header |
| Email | gina.kufrovich@corridortitle.com | Email header |
| Organization | Corridor Title | Inferred from domain + email content |
| Role | SVP, Lead Escrow Officer | Inferred from email signatures |
| Expertise areas | Title closings, escrow, property transfers, Thomas Ranch filings | Inferred from subject lines and email content |
| Relationship to Nate | External vendor - title company | Inferred from communication patterns |
| Emails/month | 49 | Counted from scan |
| Nate's response rate | ~25% | Calculated from Sent Items |
| Common co-recipients | Joe Rentfro, Tyler Mills, Thomas Ikard | Extracted from To/CC fields |

### How It's Built

1. **Scan** - A scheduled job scans the last 30 days of email across configured Outlook folders (same folders the tool already monitors).
2. **Rank** - Senders are ranked by frequency. The top 20-25 contacts are selected for profiling (this covers the vast majority of email volume).
3. **Profile** - The contact data (subject lines, email snippets, co-recipient patterns) is sent to Claude in a single batch for profile generation. Claude infers organization, role, and expertise from the content.
4. **Store** - Profiles are stored in the existing SQLite database in a `contacts` table.
5. **Review** - Nate can review and edit profiles through the existing web dashboard (optional but recommended for accuracy).
6. **Refresh** - The matrix is rebuilt on a configurable schedule (e.g., monthly, or on-demand). New contacts are added; existing profiles are updated with revised statistics.

### How It's Used at Analysis Time

When an email arrives for analysis in Phase 3, the tool:

1. Looks up the sender in the contact directory
2. Looks up all To/CC recipients in the contact directory
3. Appends the matched profiles to the analysis prompt
4. The AI uses the profiles to reason about who the request is directed at

**Example reasoning the AI could perform:**

> *"This email is from Tyler Mills (CDO, internal). It's addressed To: Nate McBride, Joe Rentfro, Gina Kufrovich, and Wes Dagestad. The email asks about the status of closing doc execution for Thomas Ranch Section 3. Gina Kufrovich is the Lead Escrow Officer at Corridor Title who handles all Thomas Ranch closings. Wes Dagestad is the real estate attorney at Polsinelli handling the Thomas Ranch/Twain deal. This question falls squarely in their domain. Nate is likely included for visibility. `needs_response: false`"*

### Cost

Based on a live test against Nate's actual email data (1,102 emails over 30 days):

| Component | Cost |
|-----------|------|
| Outlook scan (local Python/COM) | $0.00 |
| Claude profiling (20 contacts, ~7,000 input tokens) | $0.04 (Sonnet) or $0.004 (Haiku) |
| **Monthly maintenance cost** | **Under $0.05** |

The per-run cost at analysis time is negligible: the contact profiles add approximately 1,500-2,000 tokens to the analysis prompt (a few cents per pipeline run at most).

---

## How It All Works Together

### Before (Current State)

```
Email arrives
  --> Filter: Is sender whitelisted/blacklisted? Does subject match project keywords?
  --> Claude: "Does this email contain any actionable request?"
  --> If yes: Generate draft response
```

The AI sees only the email body with no context about who Nate is relative to the recipients, what the other recipients do, or whether Nate typically responds to this sender.

### After (Proposed State)

```
Email arrives
  --> Filter: Same blacklist/whitelist/keyword checks
  --> Enrich with signals:
      - To/CC position
      - Name mention scan
      - FYI language detection
      - Subject classification
      - Recipient count
      - Thread participation history
      - Sender response rate
      - Contact profiles for all parties
  --> Claude receives email + all signals + contact profiles
  --> AI reasons holistically about whether Nate specifically needs to respond
  --> If yes: Generate draft response
```

### Example: The Tyler Mills Email at 3:07 PM

Under the current system, this email was classified as important (sender domain: arete-collective.com) and Claude flagged it as `needs_response: true` because it contained an actionable request — even though the request was directed at someone else.

Under the proposed system, the AI would receive:

```
user_position: CC
user_mentioned_by_name: false
subject_type: reply
total_recipients: 4
user_active_in_thread: false (0 replies in thread)
sender_response_rate: 0.13
fyi_language_detected: false
contact_profiles:
  - Tyler Mills: CDO, Arete (internal), 236 emails/mo
  - Joe Rentfro: Managing Director, construction/contracts expert
  - Gina Kufrovich: SVP Escrow, Corridor Title, Thomas Ranch closings
  - [other recipient profile]
```

Result: `needs_response: false`. No draft generated.

---

## Implementation Approach

### Phase 1: Signal Extraction (Foundational)

Add To/CC extraction, name mention detection, FYI language scanning, subject classification, recipient count, and importance flag to the email extraction pipeline. These are all low-effort, zero-API-cost enhancements that provide immediate value.

### Phase 2: Contact Intelligence Matrix

Build the contact scanning, profiling, and storage system. Create the `contacts` table in SQLite. Add a dashboard page for reviewing/editing profiles. Wire the contact lookup into the Phase 3 analysis prompt.

### Phase 3: Behavioral Signals

Add thread participation checking (Sent Items scan by ConversationID), historical response rate calculation (integrated into contact directory), and already-handled detection. These require more data access but significantly improve accuracy.

### Phase 4: Prompt Refinement & Tuning

Update the analysis system prompt to incorporate all new signals. Run against historical emails to validate accuracy. Tune the prompt language and signal weighting based on observed results. Potentially add a feedback mechanism where Nate can flag incorrect decisions to improve future accuracy.

---

## Expected Outcome

The combination of structured signals and contact intelligence should substantially reduce false-positive draft generation while maintaining sensitivity to emails that genuinely require Nate's attention. The system shifts from asking "does this email contain any actionable request?" to asking "does this email contain an actionable request that Nate specifically is the right person to handle?"
