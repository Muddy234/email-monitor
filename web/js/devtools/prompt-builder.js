/**
 * JS port of DraftGenerator._build_draft_prompt() from worker/pipeline/drafts.py.
 * Pure functions — no DOM, no side effects.
 */

const DEFAULT_DRAFT_PROMPT_TEMPLATE = `You are drafting email replies on behalf of {user_name}, {user_title}. Your goal is to sound exactly like the user — not like an assistant.

Your task is to draft email replies that match the user's natural writing style. The user will provide:
- The original email details (sender, subject, body)
- Context about what action is needed
- Optionally, a writing style guide derived from the user's sent emails
- Optionally, prior messages from the email thread (your last reply and the thread opener)

Before writing your reply, reason through the situation inside <thinking> tags:
1. Situation — What is happening? What is the broader context of this exchange?
2. Sender's intent — What does the sender actually need or want from me?
3. Key information — What relevant facts, details, or constraints are already established in this email or thread? Pay special attention to THREAD CONTEXT if provided — it shows what has already been said and done.
4. What I don't know — Identify what information or context you lack to respond substantively. Classify each gap:
   - Peripheral gaps (dates, meeting times, minor details, attachment references) → these can be filled with inline [PLACEHOLDER] tags.
   - Central gaps (disputed or unverified figures, decisions requiring domain expertise the model lacks, statuses or outcomes that depend on information not present in the thread, or any question where the answer determines the substantive direction of the reply) → these require [USER TO COMPLETE] scaffold blocks.
   If central gaps outweigh what you can answer substantively, this is a scaffold draft. It is better to produce a well-organized scaffold than a draft that fabricates or hedges around answers you don't have.
5. Premise check — Before deciding how to respond, actively scan the email for:
   - Numerical discrepancies (figures that don't match, totals that changed, amounts that conflict)
   - Contradictions between referenced documents, previous communications, or stated facts
   - Assumptions embedded in the request that may not hold
   - Signs that the sender's question reveals a comprehension gap rather than a straightforward information need
   - Situational anomalies — does the described situation contain internal contradictions, unexplained changes from prior thread context, or elements that the sender themselves seems uncertain about? Look for: a process that keeps failing, something that should be resolved but isn't, statuses or figures that changed without explanation, or actions that seem inconsistent with the current context. These suggest something may have gone wrong and warrant probing the cause before deciding.
   List any discrepancies or anomalies found. If discrepancies exist, the reply must acknowledge or address them — do not ignore them. Evaluate whether the stated question is the right question, or whether the discrepancies point to a different underlying issue. If no discrepancies exist and the premise is sound, proceed normally.
6. Tone — What is the conversational register of this thread? Is it formal, casual, urgent? Match accordingly.
7. Useful response — Given all of the above, what type of reply would be most helpful and move things forward? If Step 4 identified central gaps, consider whether a targeted diagnostic question could resolve the ambiguity — allowing a conditional decision rather than a full scaffold. A reply that probes the key unknown and provides a contingent answer ("if X, then do Y") is often more useful than deferring entirely. Only offer conditional decisions on matters consistent with the user's apparent authority level as reflected in their behavioral profile and role.
   Response architecture: Based on the sender's underlying need, determine the structure the reply should take — a direct answer, a structured breakdown of multiple items, a logical walkthrough or explanation, a decision with rationale, or a diagnostic question. Build the draft or scaffold to match this structure.
8. Behavioral alignment — If a BEHAVIORAL PROFILE is provided, apply it now. The profile contains IF-THEN rules. For each dimension, find the ONE rule that matches this situation and apply it:
   - Decision disposition: Which rule matches? Lock in: decide, propose solution, defer, delegate, ask for info, or diagnose.
   - Response completeness: Which rule matches? Lock in: address all points or key point only.
   - Commitment pattern: Which rule matches? Lock in: specific next step, conditional decision, vague forward, or redirected ask.
   - Scope: Which rule matches? Lock in: stay narrow, add context, or expand.
   Do not blend or average across rules. Pick one per dimension and commit.

Then generate an email reply that:
- Sounds like the user wrote it personally — match their typical sentence length, vocabulary, and level of formality
- Acknowledges the sender's message and addresses each point raised
- Provides clear next steps or responses to questions
- Adjusts tone based on recipient: more formal for external legal/lender contacts, conversational for internal colleagues
- Always ends with a sign-off greeting followed by {user_name} on the next line. Use the style guide's sign-off greeting if available (e.g. "Best,"), otherwise default to "Best regards,"
- Uses [PLACEHOLDER] for peripheral unknowns: dates, meeting times, minor details, attachment references
- When the sender asks a direct question and the answer is not available from the email context, use [USER TO CONFIRM: brief description] so the user can fill in the correct answer before sending. NEVER fabricate or assume an answer.
- If Step 4 flagged this as a scaffold draft, do not attempt a complete substantive reply. Instead: identify what the sender is ultimately trying to accomplish — not just what they literally asked — and frame the response around that objective. Organize the response into [USER TO COMPLETE: description] sections as needed — this may be multiple labeled items when the email raises distinct questions, or a single block describing the task when the email requires one cohesive explanation you cannot provide. Each [USER TO COMPLETE] block should lead with a brief summary of the issue or discrepancy being addressed, then describe the task the user must perform — not restate the sender's question. When using multiple items, include an open-ended final item for related matters the sender didn't raise but the user may want to address. A well-organized scaffold is more valuable than a draft that hedges around answers you don't have.
- In scaffold drafts, match the closing to the draft's structure. If the scaffold blocks can be filled in from the user's existing knowledge and sent immediately, close naturally without promising follow-up — the user will complete the blanks and send directly. If the scaffold requires the user to research, consult someone, or take action before responding, a follow-up commitment is appropriate.
- Never asks for information the sender already provided or that is already available from the email context

If a WRITING STYLE GUIDE is provided, follow it closely — it was derived from analyzing the user's actual sent emails and captures their voice, common phrases, and communication patterns.

If a BEHAVIORAL PROFILE is provided, follow it for decision-making and action patterns. Priority hierarchy:
- BEHAVIORAL PROFILE governs WHAT the reply does (decide vs defer, probe vs accept, act vs acknowledge)
- WRITING STYLE GUIDE governs HOW the reply is written (tone, vocabulary, sentence structure)
- If the two conflict, the behavioral profile wins
- If the behavioral profile says "no consistent pattern observed" for a dimension, fall back to neutral behavior for that dimension

Output your <thinking> analysis first, then the reply body text (no JSON, no subject line, no headers).
The reply text should be ready to paste into an email above the quoted original message.`;

export function stripQuotedContent(body) {
    if (!body) return "";
    for (const marker of ["From:", "-----Original Message", "________________________________"]) {
        const idx = body.indexOf(marker);
        if (idx > 0) return body.substring(0, idx).trimEnd();
    }
    return body;
}

export function buildSystemPrompt(userName, userTitle) {
    return DEFAULT_DRAFT_PROMPT_TEMPLATE
        .replace(/\{user_name\}/g, userName || "the user")
        .replace(/\{user_title\}/g, userTitle || "professional");
}

/**
 * Build a sender briefing summary from contact data.
 * Port of worker/pipeline/enrichment.py _build_sender_briefing().
 */
export function buildSenderBriefing(contact, email) {
    if (!contact) return "";

    const senderName = contact.name || (email?.sender_email || "").split("@")[0] || "Unknown";

    // Use pre-computed summary if available
    if (contact.relationship_summary) return contact.relationship_summary;

    const rate = contact.response_rate;
    if (rate == null) return `${senderName} — New sender, no prior history`;

    const epm = contact.emails_per_month || 0;
    const isInternal = contact.contact_type === "internal";
    const typeStr = isInternal ? "internal" : "external";
    const freqLabel = epm >= 20 ? "very frequent" : epm >= 5 ? "regular" : epm >= 1 ? "occasional" : "rare";

    let summary = `${senderName} is a ${freqLabel} ${typeStr} contact. User responds to ${(rate * 100).toFixed(0)}% of their emails`;

    const latency = contact.avg_response_time_hours;
    if (latency != null) {
        const latencyLabel = latency < 1 ? "under an hour" : latency < 24 ? `${Math.round(latency)} hours` : `${Math.round(latency / 24)} days`;
        summary += `, typically within ${latencyLabel}`;
    }

    const avgBody = contact.avg_response_body_length || contact.user_avg_body_length;
    if (avgBody != null && latency != null) {
        const lengthLabel = avgBody > 500 ? "detailed" : avgBody > 150 ? "moderate-length" : "brief";
        // Insert length label before latency (match Python format)
        summary = summary.replace(`, typically within`, `, typically ${lengthLabel} replies within`);
    }

    summary += ".";
    return summary;
}

/**
 * Build a thread briefing summary from conversation messages + user aliases.
 * Port of worker/pipeline/enrichment.py _build_thread_briefing().
 */
export function buildThreadBriefing(conversationMessages, userAliases, emailReceivedTime, threadStats) {
    // Fallback to pre-computed thread stats when conversation messages aren't available
    if ((!conversationMessages || !conversationMessages.length) && threadStats) {
        const total = threadStats.total_messages || 0;
        const userCount = threadStats.user_messages || 0;
        const participation = threadStats.participation_rate ?? (total > 0 ? userCount / total : 0);
        const durationDays = Math.round(threadStats.duration_days || 0);

        const ageLabel = durationDays === 0 ? "new (started today)" :
                         durationDays <= 1 ? "recent (1 day old)" :
                         durationDays <= 7 ? `active (${durationDays} days old)` :
                         `long-running (${durationDays} days old)`;

        const others = threadStats.other_responders || [];
        const otherDesc = others.length > 0
            ? `Other participants: ${others.slice(0, 3).join(", ")}`
            : "No other participants";

        return `Thread is ${ageLabel} with ${total} messages. User has contributed ${userCount} (${(participation * 100).toFixed(0)}% participation). ${otherDesc}.`;
    }

    if (!conversationMessages || !conversationMessages.length) return "New conversation";

    const aliases = (userAliases || []).map(a => a.toLowerCase());
    const sorted = [...conversationMessages].sort(
        (a, b) => (a.received_time || "").localeCompare(b.received_time || "")
    );

    const total = sorted.length;
    const userMsgs = sorted.filter(m => aliases.includes((m.sender_email || "").toLowerCase()));
    const userCount = userMsgs.length;
    const participation = total > 0 ? userCount / total : 0;

    // Thread duration
    let durationDays = 0;
    if (sorted.length >= 2) {
        const first = new Date(sorted[0].received_time);
        const last = new Date(sorted[sorted.length - 1].received_time);
        if (!isNaN(first) && !isNaN(last)) {
            durationDays = Math.max(0, Math.round((last - first) / (1000 * 60 * 60 * 24)));
        }
    }

    const ageLabel = durationDays === 0 ? "new (started today)" :
                     durationDays <= 1 ? "recent (1 day old)" :
                     durationDays <= 7 ? `active (${durationDays} days old)` :
                     `long-running (${durationDays} days old)`;

    // Other replies since user's last message
    let otherSince = 0;
    const otherSenders = new Set();
    if (userMsgs.length) {
        const lastUserTime = userMsgs.reduce((max, m) => {
            const t = new Date(m.received_time);
            return t > max ? t : max;
        }, new Date(0));

        for (const m of conversationMessages) {
            const sender = (m.sender_email || "").toLowerCase();
            if (aliases.includes(sender)) continue;
            const mt = new Date(m.received_time);
            if (!isNaN(mt) && mt > lastUserTime) {
                otherSince++;
                otherSenders.add(sender);
            }
        }
    }

    const otherDesc = otherSince > 0
        ? `${otherSince} replies from ${[...otherSenders].slice(0, 3).join(", ")} since user's last message`
        : "No new replies since user's last message";

    return `Thread is ${ageLabel} with ${total} messages. User has contributed ${userCount} (${(participation * 100).toFixed(0)}% participation). ${otherDesc}.`;
}

/**
 * Build THREAD CONTEXT block from conversation messages.
 * Port of worker/pipeline/drafts.py _build_thread_block().
 */
export function buildThreadBlock(conversationMessages, userAliases, emailReceivedTime) {
    if (!conversationMessages || !conversationMessages.length) return "";

    const aliases = (userAliases || []).map(a => a.toLowerCase());
    const sorted = [...conversationMessages].sort(
        (a, b) => (a.received_time || "").localeCompare(b.received_time || "")
    );

    // User's last reply
    let userLast = null;
    const userMsgs = sorted.filter(m => aliases.includes((m.sender_email || "").toLowerCase()));
    if (userMsgs.length) {
        const last = userMsgs[userMsgs.length - 1];
        const body = (last.body || "").substring(0, 1000);
        if (body) {
            userLast = {
                sender: "User",
                received_time: last.received_time || "",
                body,
            };
        }
    }

    // Thread opener (first message if different from inbound email)
    let threadOpener = null;
    if (sorted.length) {
        const opener = sorted[0];
        if (opener.received_time !== emailReceivedTime) {
            const body = (opener.body || "").substring(0, 500);
            if (body) {
                threadOpener = {
                    sender: opener.sender_name || opener.sender_email || "Unknown",
                    received_time: opener.received_time || "",
                    body,
                };
            }
        }
    }

    if (!userLast && !threadOpener) return "";

    const parts = ["\n\nTHREAD CONTEXT (prior messages in this conversation):"];
    if (threadOpener) {
        parts.push(`--- Thread opener (${threadOpener.sender}, ${threadOpener.received_time}) ---`);
        parts.push(threadOpener.body);
    }
    if (userLast) {
        parts.push(`\n--- Your last reply (${userLast.received_time}) ---`);
        parts.push(userLast.body);
    }

    return parts.join("\n");
}

export function buildUserPrompt(email, classification, contact, styleGuide, {
    conversationMessages = [],
    userAliases = [],
    threadStats = null,
    behavioralProfile = null,
} = {}) {
    const subject = email.subject || "(no subject)";
    const senderName = email.sender_name || "Unknown";
    const sender = email.sender_email || "";
    let body = stripQuotedContent(email.body || "");
    if (body.length > 4000) {
        body = body.substring(0, 4000) + "\n[... truncated]";
    }

    // Build context block from classification
    const contextLines = [];
    const cls = classification || {};

    if (cls.reason) {
        contextLines.push(`Why a response is needed: ${cls.reason}`);
    } else {
        if (cls.action) contextLines.push(`ACTION NEEDED: ${cls.action}`);
        if (cls.context) contextLines.push(`CONTEXT: ${cls.context}`);
    }

    // Include archetype only when no behavioral profile exists —
    // the profile's decision disposition rules handle routing when present.
    if (cls.archetype && cls.archetype !== "none" && !behavioralProfile) {
        contextLines.push(`Expected response type: ${cls.archetype}`);
    }

    // Sender briefing from contact data
    const senderSummary = buildSenderBriefing(contact, email);
    if (senderSummary) {
        contextLines.push(`Sender context: ${senderSummary}`);
    }

    // Thread briefing from conversation messages (falls back to threadStats)
    const threadSummary = buildThreadBriefing(conversationMessages, userAliases, email.received_time, threadStats);
    if (threadSummary) {
        contextLines.push(`Thread context: ${threadSummary}`);
    }

    // Tone guidance from contact
    const toneLines = [];
    if (contact) {
        if (contact.contact_type) toneLines.push(`Sender Type: ${contact.contact_type}`);
        if (contact.organization) toneLines.push(`Sender Org: ${contact.organization}`);
    }

    let toneBlock = "";
    if (toneLines.length) {
        toneBlock = "\n\nTONE GUIDANCE:\n" + toneLines.join("\n");
        toneBlock += "\nNote: Use a more formal tone for external_legal and external_lender contacts. Use a conversational but professional tone for internal colleagues.";
    }

    // Style guide block
    let styleBlock = "";
    if (styleGuide) {
        styleBlock = `\n\nWRITING STYLE GUIDE:\n${styleGuide}\n`;
    }

    // Behavioral profile block
    let behavioralBlock = "";
    if (behavioralProfile) {
        behavioralBlock = `\n\nBEHAVIORAL PROFILE:\n${behavioralProfile}\n`;
    }

    // Thread context block (prior messages)
    const threadBlock = buildThreadBlock(conversationMessages, userAliases, email.received_time);

    const contextBlock = contextLines.join("\n");

    return `Draft a reply to the following email:

FROM: ${senderName} <${sender}>
SUBJECT: ${subject}

EMAIL BODY:
${body}

${contextBlock}${toneBlock}${threadBlock}${styleBlock}${behavioralBlock}

Generate the reply body text only (no subject, no headers).`;
}
