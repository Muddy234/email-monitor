# Clarion AI — Feature & Functionality Questionnaire

Instructions: Pick your answer(s) for multiple choice. Add notes where you want to elaborate. Open-ended questions are marked with ✏️.

---

## 1. Target User & Usage Pattern

**1.1 Who is the target user?**
- [ ] Just me (solo tool)
- [ ] Me + a small team at my company
- [X] Any professional dealing with high email volume
- [ ] Notes: ___

**1.2 How tech-savvy is the target user?**
- [ ] Power user — minimal hand-holding needed
- [X] Average — familiar with email but not dev tools
- [X] Non-technical — needs guided workflows

**1.3 How should Clarion feel to use?**
- [X] Background autopilot — set it and forget it, check in occasionally
- [ ] Active command center — I live in it throughout the day
- [ ] Morning briefing tool — review once, then work from Outlook
- [X] Notes: Should function entierly in the background, but users have the ability to utilize the website and popup ui for additional context

**1.4 Which is the primary touchpoint?**
- [ ] Extension popup — quick glances, dashboard is secondary
- [X] Web dashboard — the popup just tells me things are working
- [ ] Both equally
- [ ] Notes: ___

---

## 2. Extension Popup

> *Currently shows: connection status, draft count (clickable to Outlook), "Open Dashboard" button, logout.*

**2.1 The popup currently does:**
- [ ] Too little — I want more info/actions here
- [ ] Too much — simplify it further
- [ ] The wrong things — it should focus on something else
- [X] Notes: This is where I'm struggling. I really don't know what SHOULD go in the popup. I like the simplicity of the popup right now, but I wonder if the User will feel like they are missing something, or that the extension isn't doing anything. I don't want this to be used heavily, but it needs to tell the user 'hey, i'm working hard, I know you can't see it, but I am."

**2.2 What should you see when you click the extension icon? (pick up to 3)**
- [X] Connection/sync health status
- [X] Count of drafts ready in Outlook
- [ ] Count of emails needing response
- [ ] List of high-priority emails (sender + subject)
- [ ] Preview of latest AI-generated drafts
- [X] Quick stats (emails processed today, drafts generated)
- [ ] Nothing beyond a green "all good" indicator
- [X] Other: Still struggling on this a bit, I need help brainstorming.

**2.3 Should you be able to take actions from the popup?**
- [ ] No — it should just show status and link elsewhere
- [X] Light actions only (mark done, snooze, dismiss)
- [X] Yes — approve/reject/edit drafts right in the popup
- [X] Notes: I'm not certain on this. The extension should function completely in the background, so I'm not sure what items should be accomplished in the popup ui. Perhaps a whitelist/blacklist, or feedback on any particular email they've recieved that the LLM miscategorized?

**2.4 Should the popup show alerts for high-priority items?**
- [X] Yes — show a count/list of urgent items
- [ ] No — just show totals, I'll triage in the dashboard
- [ ] Only if something is truly urgent (P1-P2)

**2.5 Should the popup link to specific dashboard sections?**
- [ ] Yes — jump to "Needs Response", "Drafts Ready", etc.
- [ ] No — just link to the dashboard homepage
- [X] Both — homepage link + contextual deep links

---

## 3. Dashboard Home Page

> *Currently shows: 3 metric cards (Emails Synced, Needs Response, Drafts Ready), pipeline funnel, latest run status, time-range filter.*

**3.1 What question should the dashboard answer first?**
- [X] "What do I need to deal with right now?"
- [X] "What happened since I last checked?"
- [X] "How is Clarion performing overall?"
- [X] "Give me a summary of my email landscape"
- [X] Other: I'm open to ideas, what makes sense to have here? What would a user want to see here?

**3.2 Rate the current dashboard elements:**

| Element | Keep | Remove | Rethink |
|---|---|---|---|
| Emails Synced metric | [ ] | [ ] | [X] |
| Needs Response metric | [ ] | [ ] | [X] |
| Drafts Ready metric | [X] | [ ] | [ ] |
| Pipeline funnel visualization | [ ] | [ ] | [X] |
| Latest pipeline run status | [ ] | [X] | [ ] |
| Time-range filter (1d/7d/30d) | [X] | [ ] | [ ] |

**3.3 What would you ADD to the dashboard home? (pick any)**
- [X] Action-oriented email list ("your to-do list")
- [X] Daily/weekly activity summary
- [X] Trend charts (email volume, response times)
- [X] Top senders needing attention
- [ ] Recent draft activity feed
- [ ] Nothing — keep it minimal
- [ ] Other: ___

---

## 4. Email Management

> *Currently: 4 groups (Drafts Ready, Needs Response, Other, Completed). Expandable cards with full email body, classification, draft preview/edit. Actions: view/edit/delete draft, mark done, generate draft.*

**4.1 Is the 4-group structure right?**
- [ ] Yes, keep it
- [ ] Mostly — but drop "Other" (it's noise)
- [ ] Mostly — but hide "Completed" by default
- [ ] Drop both "Other" and "Completed"
- [X] Rethink it entirely
- [X] Notes: Right now, this 4-group structure doesn't mean much and I'm always confused as to what each items means. Having context into what's happening in the app and why is helpful, but only if it's intuitive. 

**4.2 When viewing an email, what matters most? (pick your top 5)**
- [X] Sender name
- [X] Subject line
- [X] Email body (full)
- [ ] Email body (snippet/summary only)
- [ ] Priority score
- [ ] Classification reasoning (why Clarion flagged it)
- [X] Suggested action
- [X] Draft preview
- [ ] Time received
- [ ] Conversation thread context

**4.3 Should you be able to edit drafts in the dashboard?**
- [ ] Yes — it's essential, I edit here before going to Outlook
- [ ] Nice to have but not critical
- [X] No — I just review here, edit in Outlook

**4.4 Should you be able to send/reply from the dashboard?**
- [ ] Yes — full send capability
- [ ] No — Outlook is for sending, dashboard is for review
- [X] Maybe later, not a priority now

**4.5 Do you want bulk actions?**
- [ ] Yes — "Mark all done", "Generate all drafts", etc.
- [X] Just "Mark all done" would be enough
- [ ] No — I handle emails individually

**4.6 Do you want more control when generating a draft?**
- [ ] No — just generate it, the AI knows what to do
- [X] Yes — let me add instructions (e.g., "decline politely", "mention Tuesday meeting")
- [X] Yes — let me pick tone/length/style options
- [X] Both instructions and style options

---

## 5. Priority & Classification

> *Currently: P1-P10 scale, needs_response flag, classification with context and suggested action.*

**5.1 Is the P1-P10 scale useful?**
- [X] Yes, I like the granularity
- [ ] Too granular — simplify to High / Medium / Low
- [ ] I don't really look at priority scores
- [X] Notes: This is helpful for the user to understand why things were classified the way they were. Perhaps there is a feedback system that allows the user to inform the future model how to do things?

**5.2 Should you be able to override priorities?**
- [X] Yes — manually adjust per email
- [ ] Yes — set rules per sender or topic
- [ ] No — trust the AI
- [ ] Both manual and rules

**5.3 Do you want to see classification reasoning?**
- [X] Yes — show me WHY it was scored/flagged
- [ ] Only when I disagree with the score
- [ ] No — just show the result

**5.4 Should there be action categories beyond "needs response"?**
- [ ] No — "needs response" vs "doesn't" is enough
- [ ] Yes — add: FYI Only, Follow Up Later, Delegate
- [X] Yes — add: I need help understanding what the user might WANT to see here. I'm still struggling with how best to demonstrate the features and functions of the tool.

---

## 6. Draft Quality & Control

**6.1 How often do you use drafts as-is (no edits)?**
- [X] Almost always — they're good enough
- [ ] About half the time
- [ ] Rarely — I usually edit significantly
- [ ] Notes on common edits: ___

**6.2 Do you want draft variations (e.g., concise vs. detailed vs. formal)?**
- [ ] Yes — show me 2-3 options
- [X] No — one good draft is enough
- [ ] Let me pick a style before generating

**6.3 Should Clarion auto-write drafts to Outlook, or wait for approval?**
- [X] Auto-write (current behavior) — I'll edit in Outlook
- [ ] Wait for my approval in the dashboard first
- [ ] Auto-write for low priority, approval for high priority
- [ ] Notes: ___

**6.4 Should Clarion learn from your draft edits over time?**
- [X] Yes — track my edits and adjust style automatically
- [ ] Maybe — show me what it learned and let me confirm
- [ ] No — I'll manage the writing style guide manually

---

## 7. Notifications

**7.1 How should Clarion notify you? (pick all that apply)**
- [X] Badge count on extension icon (current)
- [ ] Browser push notifications
- [ ] Dashboard indicators only
- [X] Email digest (daily or weekly)
- [ ] Don't notify — I'll check when I check
- [ ] Other: ___

**7.2 Should notifications distinguish urgency levels?**
- [ ] Yes — different treatment for urgent vs. routine
- [ ] No — one notification style is fine
- [X] Only alert me for P1-P2 / truly urgent items

---

## 8. Contacts & Relationships

> *Currently: contact frequency data exists but is only visible in Dev Tools.*

**8.1 Do you want a contacts/people view in the main dashboard?**
- [ ] Yes — see all emails from a person, their priority, response history
- [X] Not a full view, but show contact context on individual emails
- [ ] No — I don't need this

**8.2 Do you want per-contact settings?**
- [X] Yes — VIP flag, auto-draft preferences, custom priority
- [ ] Just a VIP / ignore flag would be enough
- [ ] No

**8.3 Do you want relationship health indicators?**
- [ ] Yes — "You haven't responded to X in 5 days" type alerts
- [X] No — that's overstepping

---

## 9. Pipeline & Dev Tools

> *Currently: History page (pipeline runs table), Dev Tools page (onboarding, draft tester, scorer inspector, pipeline trace).*

**9.1 Pipeline run history is:**
- [ ] Something I check regularly
- [ ] Only useful when something breaks
- [X] Not useful to me at all

**9.2 How should errors/failures be surfaced?**
- [ ] Banner or alert on the dashboard
- [X] Badge/indicator in the extension popup
- [ ] Both dashboard and popup
- [ ] Just in the history page — I'll check if things seem off

**9.3 What should happen with Dev Tools long-term?**
- [ ] Keep it — I use it for tuning and debugging
- [ ] Move useful parts (draft tester, style guide) to main UI, drop the rest
- [X] Hide it behind a developer mode toggle
- [ ] Remove it entirely
- [ ] Notes: ___

---

## 10. Potential New Features

Rate each: **Must Have (M) / Nice to Have (N) / Don't Want (X) / Not Sure (?)**

| Feature | M | N | X | ? |
|---|---|---|---|---|
| Full-text email search (body, not just sender/subject) | [ ] | [X] | [ ] | [ ] |
| Filters & saved views (e.g., "All P1-P3 this week") | [ ] | [X] | [ ] | [ ] |
| Snooze / follow-up reminders | [ ] | [X] | [ ] | [ ] |
| Email threading (full conversation view) | [X] | [ ] | [ ] | [ ] |
| Delegation ("Forward to X with context") | [ ] | [X] | [ ] | [ ] |
| Response templates | [ ] | [ ] | [X] | [ ] |
| Analytics & insights (trends, patterns) | [X] | [ ] | [ ] | [ ] |
| Keyboard shortcuts | [ ] | [X] | [ ] | [ ] |
| Mobile-responsive dashboard | [X] | [ ] | [ ] | [ ] |
| Calendar integration | [ ] | [X] | [ ] | [ ] |
| Attachment previews in dashboard | [ ] | [ ] | [X] | [ ] |
| Multi-email-account support | [ ] | [ ] | [X] | [ ] |
| Team/shared inbox features | [ ] | [ ] | [X] | [ ] |
| Integrations (Slack, Teams, CRM) | [ ] | [ ] | [X] | [ ] |
| Undo actions / draft edit history | [ ] | [ ] | [X] | [ ] |

---

## 11. Pain Points & Vision

**11.1 ✏️ What's the most frustrating thing about the current dashboard?**
I'm confused by what it's trying to tell me. I don't understand the categories, it's not intuitive, there isn't value added. I want the dashboard to be simple and provide the user with insights and allow them to get more granular detail into the app. But everything needs to be simple and intuitive.
> ___

**11.2 ✏️ What's the most frustrating thing about the extension popup?**
It feels like it's lacking 'something'. The 'X factor'. It's basic, which is good, but I feel like something is missing.
> ___

**11.3 ✏️ Is there anything you built that you never actually use?**
N/A
> ___

**11.4 ✏️ Is there anything you do manually that Clarion should handle?**
N/A
> ___

**11.5 ✏️ In one sentence, what should Clarion feel like to use?**
A relief. Emails come in non-stop, Clarion should give the user comfort that it's sorting through all those emails and finding the ones the user needs to focus on, drafts the response in their draft inbox, and the user can pop into drafts, review the email, click send, and feel like they aren't dropping the ball.
> ___

**11.6 What existing product is closest to your vision for Clarion?**
- [ ] Superhuman (speed + keyboard-driven email)
- [ ] Microsoft Copilot (AI assistant embedded in workflow)
- [ ] Linear (clean, opinionated task management)
- [ ] Notion (flexible workspace)
- [X] None — Clarion is its own thing
- [ ] Other: ___

**11.7 ✏️ If Clarion could only do ONE thing really well, what should it be?**
Draft relevant, tone-specific responses to important emails that feel like the user typed them theirselves. 
> ___

**11.8 ✏️ What should Clarion explicitly NOT try to do?**
Overcomplicate the users life.
> ___

---

## 12. Follow-Up: Popup, Grouping, Dashboard & Feedback

> *These follow-ups target the specific areas you flagged as uncertain in sections 2–5.*

### Extension Popup

**F1. Would a "recent activity" micro-feed solve the missing X factor?** Something like:
- "12 min ago — Drafted reply to Sarah Chen re: Q3 forecast"
- "45 min ago — Scanned 8 new emails, 2 need your response"
- "2 hrs ago — Synced 23 emails"

A compact log that says "here's what I've been doing while you weren't looking."

- [ ] Yes — that's exactly what's missing
- [ ] Partially — I like the idea but it might be too busy
- [X] No — that's not it
- [X] Notes: This feels like clutter. No one likes looking at a log of activity. Boring and not relevant. 

**F2. Should the popup surface a single "headline" stat?** Instead of several metrics, one primary number like:
- "You have 3 drafts ready to review" (actionable)
- "Clarion handled 47 emails this week" (proof of value)
- "2 high-priority emails need your attention" (urgency)

- [X] Yes — one clear headline + supporting detail underneath
- [ ] No — I prefer a few small stats side by side
- [X] Notes: Simplicity is CRITICAL. Nothing should be confusing, everything should be intuitive. 

---

### Email Grouping

**F3. Does this grouping model make more sense?**

1. **Review & Send** — Drafts are ready, go to Outlook and send them
2. **Needs Your Input** — Clarion flagged these but needs guidance before drafting (too complex, missing context, or user chose manual draft control)
3. **Handled** — Clarion processed these, no action needed (collapsed/hidden by default)

Only show things the user needs to **do something about**, grouped by what that action is.

- [X] Yes — this is much closer
- [ ] Partially — I like the direction, but would adjust (explain below)
- [ ] No — still not right
- [ ] Notes: ___

**F4. Should "no action needed" emails be visible at all?**
If Clarion scanned an email and determined it doesn't need a response, should the user even see it in the dashboard?

- [X] No — only show emails that need user action
- [ ] Show a collapsed count ("42 emails handled, no action needed") with expand option
- [ ] Yes — show everything, but clearly de-emphasize low-priority items
- [X] Notes: All emails should be available in the website for the last 30 days or some period of time, but don't show emails on the dashboard that the user doesn't need.

---

### Action Categories

**F5. Do these action labels feel intuitive?**

| Label | Meaning |
|---|---|
| **Send Draft** | Draft is in Outlook, review and send it |
| **Write Reply** | Needs a response but Clarion couldn't/didn't auto-draft — user should reply manually or provide instructions |
| **Review** | FYI — no response needed, but worth reading |
| **Waiting** | You responded, waiting on the other person |
| **Done** | Completed, archived |

- [ ] Yes — these make sense as categories
- [ ] Mostly — but I'd change some (explain below)
- [X] No — too many categories
- [X] Notes: Simplicity. Perhaps it's drafted emails, review but no response needed (need a better way to say this, a single word or something more simple to tell the user they should look at this email to be sure they're up to speed, but a response is unlikely), other emails
- [X] **Final answer: Three categories — Drafts, Notable, and everything else (accessible but not shown by default)**

---

### Dashboard Home Layout

**F6. Does this layout hierarchy match your instinct?**

1. **Top of page**: One-liner status ("You have 4 drafts to review, 2 emails need attention")
2. **Middle**: Actionable email list — the things you need to deal with right now (your "to-do list")
3. **Bottom**: Insights panel — trends, volume, top senders, weekly summary

Urgent first, context second, analytics third.

- [X] Yes — this hierarchy feels right
- [ ] I'd reorder it (explain below)
- [ ] Too much for one page — split across tabs or pages
- [ ] Notes: ___

---

### Feedback & Learning System

**F7. What does "feedback" look like to you?**

- [X] Thumbs up / thumbs down on each draft or classification
- [ ] Quick correction: "This should be [High/Low priority]" dropdown
- [ ] Free-text: "This was wrong because..."
- [X] Implicit only — just learn from my edits, don't ask me for feedback
- [X] Notes: Perhpas it's a thumbs up or down to start, then if they click down, there is a dropdown of reasons why the action the LLM took was wrong.

**F8. Where should the feedback mechanism live?**

- [X] On each email card in the dashboard
- [X] In the extension popup (quick feedback on recent items)
- [ ] Both
- [X] Notes: Perhaps there is a feedback button on the popup that redirects to the website.
