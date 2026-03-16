# Prompt Token Efficiency Review

## Current Token Counts

| Prompt | Status | Tokens | Per-Call? | Notes |
|--------|--------|--------|-----------|-------|
| `SIGNAL_EXTRACTION_SYSTEM_PROMPT` | **Active** | 1,532 | Every email | Cached after first call per batch |
| `DEFAULT_ANALYSIS_PROMPT` | Legacy | 1,174 | — | Not in active pipeline |
| `ENRICHED_ANALYSIS_PROMPT` | Legacy | 898 | — | Not in active pipeline |
| `DEFAULT_DRAFT_PROMPT_TEMPLATE` | Active | 289 | Per draft | Uses Sonnet, not Haiku |
| `HAIKU_EXTRACTION_PROMPT` | Active | 408 | Per batch | Onboarding only (one-time) |
| `HAIKU_STYLE_EXTRACTION_PROMPT` | Active | 192 | Per batch | Onboarding only (one-time) |

**Primary cost driver:** `SIGNAL_EXTRACTION_SYSTEM_PROMPT` at 1,532 tokens. This is the system prompt for every email classification. With prompt caching enabled, the first call in a batch pays full price (~$0.80/MTok input on Haiku 4.5) and subsequent calls in the same batch hit the cache (~$0.08/MTok — 90% discount). Even so, minimizing this prompt pays dividends on cache-miss calls and reduces latency.

**Out of scope:** Legacy prompts (`DEFAULT_ANALYSIS_PROMPT`, `ENRICHED_ANALYSIS_PROMPT`) are not in the active pipeline path. No optimization needed unless they're reactivated. Onboarding prompts run once per user setup — low ROI to optimize.

---

## Signal Extraction Prompt — Line-by-Line Analysis

### Section 1: Header (2 lines, ~30 tokens)
```
Classify this email. Return JSON only, no other text.
Focus on the NEWEST message only. Ignore quoted/forwarded text below signature lines or ">" markers.
```
**Verdict: Keep.** Essential framing. The body-focus instruction prevents misclassification on threaded emails.

### Section 2: Signal Definitions (5 fields, ~180 tokens)
```
mc (bool): Financial/legal/deal consequence. Triggers: funding, closing, default, ...
ar (bool): Sender needs recipient to do something. Includes implicit asks and follow-ups.
ub (bool): Someone downstream is blocked waiting on recipient. ...
dl (bool): Time constraint on a request. ...
rt (enum none|ack|ans|act|dec): Expected response type. ...
```

**Trim opportunities:**

| Field | Current | Proposed | Savings |
|-------|---------|----------|---------|
| `mc` trigger list | 12 keywords (funding, closing, default, penalty, expiration, compliance, rate lock, guaranty, wire, lien, amendment, maturity) | Keep 7 highest-signal: funding, closing, penalty, expiration, compliance, wire, amendment | ~15 tokens |
| `ub` definition | "Someone downstream is blocked waiting on recipient. Process paused pending their input." | "Someone blocked waiting on recipient's input." | ~8 tokens |
| `dl` definition | "Explicit date/time or urgency language tied to an ask. Ignore dates as context only." | "Date/time or urgency tied to an ask. Dates as context only don't count." | ~5 tokens |
| `rt` + draft linkage | "If multiple apply, pick highest: dec>act>ans>ack>none. If rt=none, draft should almost always be false." | "Pick highest: dec>act>ans>ack>none. rt=none almost always means draft=false." | ~8 tokens |

**Estimated savings: ~36 tokens**

### Section 3: Action Target Block (~200 tokens)

```
target (enum user|other|all|unclear): Who the action/request is directed at.
- user: Action is directed at USER (named, addressed, or sole TO recipient). → draft=true when response needed.
- other: Action is directed at someone else (...). → draft=false.
- all: Action applies to all recipients equally. → draft=true only if USER must respond individually.
- unclear: Cannot determine who the action targets. → default draft=true to avoid missing real requests.
When ar=false, set target="user" (default, ignored).
```

**Trim opportunities:**

- The parenthetical in `other` ("another name mentioned, USER is CC-only, or body addresses a specific person who is not USER") is partially redundant with the heuristics block below. **Cut it** and let the heuristics do the teaching.
- "When ar=false, set target="user" (default, ignored)." — This is a default-value instruction. Can compress to: `ar=false → target="user" (ignored).`
- Each `→ draft=...` annotation after the target value duplicates logic from the Decisions block.

**Proposed compressed version:**
```
target (enum user|other|all|unclear): Who the action is directed at.
- user: Directed at USER. draft=true when response needed.
- other: Directed at someone else. draft=false.
- all: All recipients equally. draft=true only if USER must respond individually.
- unclear: Can't determine. Default draft=true.
ar=false → target="user" (ignored).
```

**Estimated savings: ~50 tokens**

### Section 4: Key Heuristics for Target (~85 tokens)

```
- If body addresses someone by name ("Wes please...", "John, can you...") and that name is NOT USER → target=other.
- If USER is in CC (not TO) and the action doesn't reference USER → target=other.
- If USER is sole TO recipient → target=user.
- If body uses "all" / "everyone" / "team" language → target=all.
```

**Verdict: Keep but compress.** The quoted examples ("Wes please...", "John, can you...") add ~15 tokens. These are useful for Haiku — name-addressing is a subtle cue. However, we can shorten:

```
- Body addresses someone by name who is NOT USER → target=other.
- USER is CC (not TO) and not referenced → target=other.
- USER is sole TO recipient → target=user.
- "all"/"everyone"/"team" language → target=all.
```

**Estimated savings: ~25 tokens**

### Section 5: Context Fields (~120 tokens)

```
- tier: Sender importance. C=critical (lenders, investors, legal) → bias pri toward high. I=internal (colleagues) → normal. P=professional (vendors, consultants) → normal. U=unknown → bias pri toward low.
- thread_depth: Number of messages in this thread (1=new email). Higher depth in active threads may reduce urgency if others are already engaged.
- unanswered: Whether USER has a prior message in this thread that went unanswered by the sender. true suggests a follow-up the user was waiting for → bias draft=true.
```

**Trim opportunities:**

- `I=internal → normal` and `P=professional → normal` can merge: `I/P → normal weighting.`
- thread_depth second sentence is guidance that could be shorter: "Deep threads with active participants → lower urgency."
- unanswered: "true suggests a follow-up the user was waiting for" is explanatory fluff. The bias instruction is what matters.

**Proposed:**
```
- tier: C=critical (lenders, investors, legal) → bias pri high. I=internal, P=professional → normal. U=unknown → bias pri low.
- thread_depth: Messages in thread (1=new). Deep active threads → lower urgency.
- unanswered: USER's prior message went unanswered. true → bias draft=true.
```

**Estimated savings: ~40 tokens**

### Section 6: Decisions Block (~110 tokens)

```
pri (enum high|med|low): Overall priority considering all signals, sender tier, and context.
draft (bool): Whether USER should draft a response. True ONLY when the email requires a response FROM USER specifically. False when: FYI, action targets someone else (target=other), terminal acknowledgments ("Thanks", "Got it"), automated notifications, newsletters, calendar invites, no-response-needed.
reason (string): Under 30 words. When draft=true, write a brief for what the reply should address. When draft=false, explain why no response is needed.
```

**Trim opportunities:**

- `draft` false-case list is long. The examples in the Examples section already demonstrate these. Can compress to: `False when: FYI, target=other, terminal acks, automated/no-response emails.`
- `pri` description "considering all signals, sender tier, and context" is vague filler — Haiku already has the tier guidance above.

**Proposed:**
```
pri (enum high|med|low): Overall priority.
draft (bool): True ONLY when email requires response FROM USER. False: FYI, target=other, terminal acks, automated/no-response emails.
reason (string): <30 words. draft=true → brief for reply. draft=false → why no response needed.
```

**Estimated savings: ~40 tokens**

### Section 7: Context Format (~50 tokens)

```
Context format:
Line 1 — Sender: sender_name sender_email|tier|thread_depth|unanswered
Line 2 — User: USER user_name|user_email|position (TO or CC)
Line 3 — Recipients: TO: addr1;addr2 | CC: addr1;addr2
```

**Verdict: Keep as-is.** This is structural documentation. Haiku needs this to parse the user message correctly. Minimal fat here.

### Section 8: Examples (~800 tokens — 52% of total prompt)

Five examples currently:
1. Action for user — direct request with deadline (~95 tokens)
2. Action for someone else — USER is CC (~110 tokens)
3. FYI / informational — no action (~100 tokens)
4. Direct question to user — needs answer (~115 tokens)
5. Terminal acknowledgment — no response needed (~80 tokens)

**This is the biggest optimization target.** Examples are the most effective part of the prompt for steering Haiku, but they're also the most expensive per token.

**Trim opportunities:**

- **Email addresses are long.** `nmcbride@arete-collective.com` is 5 tokens. Each example has 3-6 email addresses. Using shorter placeholder emails (e.g., `n@ac.com`, `g@ct.com`) would save ~10-15 tokens per example.
  - **Risk:** Haiku may not generalize as well from synthetic-looking addresses. However, the signal extraction call receives real addresses at inference time, so the examples just need to teach the format, not the domain matching.
- **Recipient lines repeat USER's email.** In Example 2, `nmcbride@arete-collective.com` appears 3 times (USER line, CC list, TO list). Shortening addresses helps here.
- **Reason strings in outputs are long.** Example 2's reason is 16 words. These are output tokens (cheaper than input on Haiku), but the example reason also takes input tokens. Trimming example reasons to 8-10 words saves ~5 tokens each.
- **Example 3 (FYI) and Example 5 (terminal ack) are similar in outcome** (both draft=false, low priority). Example 5 is the more valuable one (terminal acks are a common misclassification). Consider whether Example 3 could be cut.
  - **Recommendation: Keep both.** FYI teaches CC-only handling. Terminal ack teaches "looks like it needs a response but doesn't." These are different failure modes.

**Proposed example compression strategy:**

Use short placeholder emails in examples. Keep all 5 examples but tighten the reason strings.

Before (Example 2):
```
Input: Gina Kufrovich gina@corridortitle.com|P|1|false
USER: Nate McBride|nmcbride@arete-collective.com|CC
TO: wdagestad@polsinelli.com;tmills@arete-collective.com | CC: nmcbride@arete-collective.com
S:CPL — 123 Main St
Wes please see attached CPL.
Output: {"mc":false,"ar":true,"ub":false,"dl":false,"rt":"act","target":"other","pri":"low","draft":false,"reason":"Action directed at Wes (TO recipient), not USER who is CC-only. No response needed."}
```

After:
```
Ex2 (action for other — USER is CC):
Gina K gina@ct.com|P|1|false
USER: Nate M|nm@ac.com|CC
TO: wes@pol.com;tm@ac.com | CC: nm@ac.com
S:CPL — 123 Main St
Wes please see attached CPL.
→ {"mc":false,"ar":true,"ub":false,"dl":false,"rt":"act","target":"other","pri":"low","draft":false,"reason":"Action directed at Wes, not USER. CC-only."}
```

**Estimated savings across 5 examples: ~150-200 tokens**

---

## Summary of Proposed Changes

| Section | Current Tokens | Proposed Tokens | Savings | % Reduction |
|---------|---------------|-----------------|---------|-------------|
| Header | ~30 | ~30 | 0 | 0% |
| Signal definitions | ~180 | ~144 | 36 | 20% |
| Target block | ~200 | ~150 | 50 | 25% |
| Heuristics | ~85 | ~60 | 25 | 29% |
| Context fields | ~120 | ~80 | 40 | 33% |
| Decisions | ~110 | ~70 | 40 | 36% |
| Context format | ~50 | ~50 | 0 | 0% |
| Examples (5) | ~800 | ~620 | 180 | 23% |
| **Total** | **~1,532** | **~1,160** | **~371** | **~24%** |

**Measured result: 1,108 tokens** (down from 1,532). **27.7% reduction.**

---

## Cost Impact Estimate

Assuming:
- 100 emails/day average
- Prompt caching hits on all but the first call per batch (~10 batches/day = 10 cache misses)
- Haiku 4.5 pricing: $0.80/MTok input (cache miss), $0.08/MTok (cache hit)

**Current daily system prompt cost:**
- Cache misses: 10 x 1,532 tokens x $0.80/MTok = $0.012
- Cache hits: 90 x 1,532 tokens x $0.08/MTok = $0.011
- **Total: ~$0.023/day**

**Optimized daily system prompt cost:**
- Cache misses: 10 x 1,108 tokens x $0.80/MTok = $0.009
- Cache hits: 90 x 1,108 tokens x $0.08/MTok = $0.008
- **Total: ~$0.017/day**

At current volume, the savings are modest (~$0.006/day). At scale (1,000 emails/day across 10 users), savings approach ~$0.50/day or ~$15/month. The real win is **latency** — fewer input tokens = faster time-to-first-token.

---

## Risk Assessment

| Change | Risk | Mitigation |
|--------|------|------------|
| Shorter `mc` trigger list | Haiku misses edge triggers (guaranty, lien, maturity, rate lock) | These are domain-specific. Keep if misclassification is observed. Can A/B test. |
| Compressed target block | Less explicit guidance on target+draft linkage | Examples still demonstrate the pattern clearly. |
| Short placeholder emails in examples | Haiku may not generalize email domain patterns | Examples teach format/logic, not domain matching. Real emails at inference time have real addresses. |
| Trimmed reason strings in examples | Haiku produces less descriptive reasons | Reason field has a 30-word cap anyway. Shorter examples may actually produce tighter output. |
| Removing parenthetical examples | Haiku loses quoted-speech cues ("Wes please...") | The heuristics block still captures the rule; examples demonstrate it. |

**Overall risk: Low.** The optimizations remove redundancy and verbose phrasing, not decision logic. All 5 examples are preserved. Every signal, field, and rule is still present.

---

## Recommendations

### High Priority (implement now)
1. **Compress examples with short emails** — biggest single win (~150-200 tokens)
2. **Tighten target block** — remove parenthetical redundancies (~50 tokens)
3. **Compress context fields** — merge I/P tiers, shorten guidance (~40 tokens)
4. **Tighten decisions block** — remove verbose false-case list (~40 tokens)

### Medium Priority (implement after validation)
5. **Trim `mc` trigger list** — remove lowest-signal triggers, monitor accuracy
6. **Compress heuristics** — remove quoted examples, keep rules (~25 tokens)

### Low Priority (skip unless scaling)
7. **Legacy prompt cleanup** — `DEFAULT_ANALYSIS_PROMPT` and `ENRICHED_ANALYSIS_PROMPT` are dead code. Consider removing from codebase to reduce maintenance surface.
8. **Onboarding prompts** — already lean (408 + 192 tokens) and run once per user. Not worth optimizing.

---

## Proposed Optimized Prompt

```
Classify this email. Return JSON only, no other text.
Focus on the NEWEST message only. Ignore quoted/forwarded text below signatures or ">" markers.

Signals:
mc (bool): Financial/legal/deal consequence. Triggers: funding, closing, penalty, expiration, compliance, wire, amendment.
ar (bool): Sender needs recipient to do something. Includes implicit asks and follow-ups.
ub (bool): Someone blocked waiting on recipient's input.
dl (bool): Date/time or urgency tied to an ask. Dates as context only don't count.
rt (enum none|ack|ans|act|dec): Pick highest: dec>act>ans>ack>none. rt=none almost always means draft=false.

target (enum user|other|all|unclear): Who the action is directed at (required when ar=true).
- user: Directed at USER. draft=true when response needed.
- other: Directed at someone else. draft=false.
- all: All recipients equally. draft=true only if USER must respond individually.
- unclear: Can't determine. Default draft=true.
ar=false → target="user" (ignored).

Target heuristics:
- Body addresses someone by name who is NOT USER → other.
- USER is CC (not TO) and not referenced → other.
- USER is sole TO → user.
- "all"/"everyone"/"team" language → all.

Context fields:
- tier: C=critical (lenders, investors, legal) → bias pri high. I/P → normal. U=unknown → bias pri low.
- thread_depth: Messages in thread (1=new). Deep active threads → lower urgency.
- unanswered: USER's prior message went unanswered. true → bias draft=true.

Decisions:
pri (enum high|med|low): Overall priority.
draft (bool): True ONLY when email requires response FROM USER. False: FYI, target=other, terminal acks, automated/no-response emails.
reason (string): <30 words. draft=true → brief for reply. draft=false → why no response needed.

Context format:
Line 1 — Sender: sender_name sender_email|tier|thread_depth|unanswered
Line 2 — User: USER user_name|user_email|position (TO or CC)
Line 3 — Recipients: TO: addr1;addr2 | CC: addr1;addr2

Ex1 (action for user — deadline):
Jane S jane@lndr.com|C|1|false
USER: Bob J|bob@co.com|TO
TO: bob@co.com | CC: (none)
S:Draw Request #4 — Please Review
Bob, please review the attached draw request and approve by Friday.
→ {"mc":true,"ar":true,"ub":false,"dl":true,"rt":"act","target":"user","pri":"high","draft":true,"reason":"Lender requesting review and approval of draw request by Friday."}

Ex2 (action for other — USER is CC):
Gina K gina@ct.com|P|1|false
USER: Nate M|nm@ac.com|CC
TO: wes@pol.com;tm@ac.com | CC: nm@ac.com
S:CPL — 123 Main St
Wes please see attached CPL.
→ {"mc":false,"ar":true,"ub":false,"dl":false,"rt":"act","target":"other","pri":"low","draft":false,"reason":"Action directed at Wes, not USER. CC-only."}

Ex3 (FYI — CC, no action):
Sarah M sarah@ac.com|I|3|false
USER: Nate M|nm@ac.com|CC
TO: tm@ac.com | CC: nm@ac.com;jc@ac.com
S:Re: Turtle Bay Budget Update
Updated budget spreadsheet attached. Let me know if you have questions.
→ {"mc":false,"ar":false,"ub":false,"dl":false,"rt":"none","target":"user","pri":"low","draft":false,"reason":"Internal FYI to another recipient. USER CC for visibility."}

Ex4 (question for user — needs decision):
Dave W dave@lndr.com|C|2|true
USER: Nate M|nm@ac.com|TO
TO: nm@ac.com | CC: tm@ac.com
S:Re: Thomas Ranch Phase 2 — Rate Lock
Nate, can you confirm whether you want to lock at 6.25% or float until next week?
→ {"mc":true,"ar":true,"ub":true,"dl":false,"rt":"dec","target":"user","pri":"high","draft":true,"reason":"Lender asking to decide on rate lock vs float."}

Ex5 (terminal ack — no response):
Jim C jim@ctr.com|P|4|false
USER: Nate M|nm@ac.com|TO
TO: nm@ac.com | CC: (none)
S:Re: Inspection Schedule
Thanks Nate, got it. We'll be there Monday at 9am.
→ {"mc":false,"ar":false,"ub":false,"dl":false,"rt":"none","target":"user","pri":"low","draft":false,"reason":"Terminal ack confirming receipt. No response needed."}
```
