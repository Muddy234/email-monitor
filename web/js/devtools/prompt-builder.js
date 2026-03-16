/**
 * JS port of DraftGenerator._build_draft_prompt() from worker/pipeline/drafts.py.
 * Pure functions — no DOM, no side effects.
 */

const DEFAULT_DRAFT_PROMPT_TEMPLATE = `You are drafting email replies on behalf of {user_name}, {user_title}. Your goal is to sound exactly like the user — not like an assistant.

Your task is to draft email replies that match the user's natural writing style. The user will provide:
- The original email details (sender, subject, body)
- Context about what action is needed
- Optionally, a writing style guide derived from the user's sent emails

Before writing your reply, reason through the situation inside <thinking> tags:
1. Situation — What is happening? What is the broader context of this exchange?
2. Sender's intent — What does the sender actually need or want from me?
3. Key information — What relevant facts, details, or constraints are already established in this email or thread?
4. Tone — What is the conversational register of this thread? Is it formal, casual, urgent? Match accordingly.
5. Useful response — Given all of the above, what type of reply would be most helpful and move things forward?

Then generate an email reply that:
- Sounds like the user wrote it personally — match their typical sentence length, vocabulary, and level of formality
- Acknowledges the sender's message and addresses each point raised
- Provides clear next steps or responses to questions
- Adjusts tone based on recipient: more formal for external legal/lender contacts, conversational for internal colleagues
- Uses the user's typical sign-off (default: "Best regards," followed by {user_name}) unless the style guide specifies otherwise
- Uses [PLACEHOLDER] for any unknown specifics (amounts, dates, details you don't have)
- Never asks for information the sender already provided or that is already available from the email context

If a WRITING STYLE GUIDE is provided, follow it closely — it was derived from analyzing the user's actual sent emails and captures their voice, common phrases, and communication patterns.

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

export function buildUserPrompt(email, classification, contact, styleGuide) {
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

    if (cls.archetype && cls.archetype !== "none") {
        contextLines.push(`Expected response type: ${cls.archetype}`);
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

    const contextBlock = contextLines.join("\n");

    return `Draft a reply to the following email:

FROM: ${senderName} <${sender}>
SUBJECT: ${subject}

EMAIL BODY:
${body}

${contextBlock}${toneBlock}${styleBlock}

Generate the reply body text only (no subject, no headers).`;
}
