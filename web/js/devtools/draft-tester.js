/**
 * Panel 2: Draft Tester
 * Select an email → see classification, existing draft, constructed prompt, and generate a test draft.
 */
import { supabase } from "../supabase-client.js";
import { escapeHtml, showToast } from "../ui.js";
import { createEmailPicker } from "./email-picker.js";
import { buildSystemPrompt, buildUserPrompt } from "./prompt-builder.js";

let styleGuide = null;
let userName = "";
let userTitle = "";

export async function initDraftTester() {
    const panel = document.getElementById("panel-draft-tester");

    // Fetch profile config for style guide + user info
    const { data: { user } } = await supabase.auth.getUser();
    if (!user) { panel.innerHTML = `<div class="em-empty">Not authenticated.</div>`; return; }

    const { data: profile } = await supabase
        .from("profiles")
        .select("writing_style_guide, style_sample_count")
        .eq("id", user.id)
        .single();

    styleGuide = profile?.writing_style_guide || null;
    userName = user.user_metadata?.full_name || user.email?.split("@")[0] || "";
    userTitle = user.user_metadata?.title || "professional";

    panel.innerHTML = `
        <div class="em-detail-grid">
            <div>
                <h3 class="em-section-title">Select Email</h3>
                <div id="dt-draft-picker"></div>
            </div>
            <div>
                <h3 class="em-section-title">Style Guide ${styleGuide ? `<span class="em-badge em-badge-green" style="font-size:10px">Loaded</span>` : `<span class="em-badge em-badge-amber" style="font-size:10px">Not Found</span>`}</h3>
                <div class="em-card">
                    ${styleGuide
                        ? `<div class="em-style-guide-text" style="max-height:260px">${escapeHtml(styleGuide)}</div>`
                        : `<div class="em-empty" style="padding:16px">No style guide available. Onboarding may not have completed.</div>`
                    }
                </div>
            </div>
        </div>
        <div id="dt-draft-detail"></div>
    `;

    createEmailPicker(
        document.getElementById("dt-draft-picker"),
        (email) => renderDraftDetail(email, user.id)
    );
}

async function renderDraftDetail(email, userId) {
    const detail = document.getElementById("dt-draft-detail");
    const cls = email.classifications?.[0] || {};
    const draft = email.drafts?.[0];

    // Fetch contact for sender
    let contact = null;
    if (email.sender_email) {
        const { data } = await supabase
            .from("contacts")
            .select("*")
            .eq("user_id", userId)
            .eq("email", email.sender_email.toLowerCase())
            .single();
        contact = data;
    }

    const systemPrompt = buildSystemPrompt(userName, userTitle);
    const userPrompt = buildUserPrompt(email, cls, contact, styleGuide);

    detail.innerHTML = `
        <div class="em-detail-grid" style="margin-top:24px">
            <div>
                <h3 class="em-section-title">Classification</h3>
                <div class="em-card">
                    ${cls.needs_response != null ? `
                        <div class="em-kv-grid">
                            <div class="em-kv-label">Needs Response</div>
                            <div class="em-kv-value">${cls.needs_response ? '<span class="em-badge em-badge-amber">Yes</span>' : '<span class="em-badge em-badge-slate">No</span>'}</div>
                            <div class="em-kv-label">Confidence</div>
                            <div class="em-kv-value">${cls.confidence != null ? (cls.confidence * 100).toFixed(0) + "%" : "—"}</div>
                            <div class="em-kv-label">Archetype</div>
                            <div class="em-kv-value">${escapeHtml(cls.archetype || "—")}</div>
                            <div class="em-kv-label">Reason</div>
                            <div class="em-kv-value">${escapeHtml(cls.reason || cls.context || "—")}</div>
                            <div class="em-kv-label">Action</div>
                            <div class="em-kv-value">${escapeHtml(cls.action || "—")}</div>
                            <div class="em-kv-label">Project</div>
                            <div class="em-kv-value">${escapeHtml(cls.project || "—")}</div>
                            <div class="em-kv-label">Priority</div>
                            <div class="em-kv-value">${cls.priority === "x" ? '<span class="em-badge em-badge-red">Urgent</span>' : "Normal"}</div>
                        </div>
                    ` : `<div class="em-empty" style="padding:16px">Not classified yet.</div>`}
                </div>
            </div>
            <div>
                <h3 class="em-section-title">Existing Draft</h3>
                <div class="em-card">
                    ${draft ? `<div style="white-space:pre-wrap;font-size:14px;line-height:1.7;color:var(--em-slate-700)">${escapeHtml(draft.draft_body)}</div>`
                           : `<div class="em-empty" style="padding:16px">No draft generated for this email.</div>`}
                </div>
            </div>
        </div>

        ${contact ? `
            <h3 class="em-section-title" style="margin-top:24px">Sender Contact</h3>
            <div class="em-card" style="margin-bottom:24px">
                <div class="em-kv-grid">
                    <div class="em-kv-label">Name</div><div class="em-kv-value">${escapeHtml(contact.name || "—")}</div>
                    <div class="em-kv-label">Organization</div><div class="em-kv-value">${escapeHtml(contact.organization || "—")}</div>
                    <div class="em-kv-label">Role</div><div class="em-kv-value">${escapeHtml(contact.role || "—")}</div>
                    <div class="em-kv-label">Type</div><div class="em-kv-value"><span class="em-badge em-badge-slate">${escapeHtml(contact.contact_type || "unknown")}</span></div>
                    <div class="em-kv-label">Significance</div><div class="em-kv-value">${escapeHtml(contact.relationship_significance || "—")}</div>
                </div>
            </div>
        ` : ""}

        <div style="margin-top:24px;margin-bottom:24px">
            <button class="em-btn em-btn-primary" id="dt-generate-btn">Generate Draft</button>
            <span id="dt-generate-status" style="margin-left:12px;font-size:13px;color:var(--em-slate-500)"></span>
        </div>
        <div id="dt-generated-draft"></div>

        <h3 class="em-section-title" style="margin-top:24px">Constructed Prompt</h3>
        <p style="font-size:13px;color:var(--em-slate-500);margin-bottom:12px">
            The prompts sent to Claude when generating a draft.
        </p>

        <div style="margin-bottom:16px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <span style="font-size:12px;font-weight:600;color:var(--em-slate-500)">SYSTEM PROMPT</span>
                <button class="em-btn em-btn-secondary em-btn-sm em-copy-prompt" data-target="dt-system-prompt">Copy</button>
            </div>
            <div class="em-code-block" id="dt-system-prompt">${escapeHtml(systemPrompt)}</div>
        </div>

        <div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <span style="font-size:12px;font-weight:600;color:var(--em-slate-500)">USER PROMPT</span>
                <button class="em-btn em-btn-secondary em-btn-sm em-copy-prompt" data-target="dt-user-prompt">Copy</button>
            </div>
            <div class="em-code-block" id="dt-user-prompt">${escapeHtml(userPrompt)}</div>
        </div>
    `;

    // Copy buttons
    detail.querySelectorAll(".em-copy-prompt").forEach(btn => {
        btn.addEventListener("click", () => {
            const text = document.getElementById(btn.dataset.target).textContent;
            navigator.clipboard.writeText(text).then(() => {
                showToast("Copied to clipboard", "success");
            });
        });
    });

    // Generate Draft button
    const generateBtn = document.getElementById("dt-generate-btn");
    const statusEl = document.getElementById("dt-generate-status");
    const resultEl = document.getElementById("dt-generated-draft");

    generateBtn.addEventListener("click", async () => {
        generateBtn.disabled = true;
        statusEl.textContent = "Generating...";
        resultEl.innerHTML = `<div class="em-card" style="padding:24px;text-align:center;color:var(--em-slate-400)"><div class="em-skeleton" style="height:80px;border-radius:var(--em-radius-sm)"></div></div>`;

        try {
            const { data: { session } } = await supabase.auth.getSession();
            const resp = await fetch("https://frbvdoszenrrlswegsxq.supabase.co/functions/v1/generate-draft", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${session?.access_token || ""}`,
                    "apikey": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZyYnZkb3N6ZW5ycmxzd2Vnc3hxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI2NjA0OTUsImV4cCI6MjA4ODIzNjQ5NX0.OCYTv_B823u_9o_Q9S-qPpUea9DQt_xpsWuNnolJT7M",
                },
                body: JSON.stringify({ systemPrompt, userPrompt }),
            });

            if (!resp.ok) {
                const errText = await resp.text();
                throw new Error(`HTTP ${resp.status}: ${errText}`);
            }

            const data = await resp.json();
            if (data?.error) throw new Error(data.error);

            const draftText = data?.draft || "";
            if (!draftText) throw new Error("Empty response from API");

            statusEl.textContent = "";
            resultEl.innerHTML = `
                <h3 class="em-section-title">Generated Draft</h3>
                <div class="em-card" style="border-left:3px solid var(--em-blue-600)">
                    <div style="white-space:pre-wrap;font-size:14px;line-height:1.7;color:var(--em-slate-700)">${escapeHtml(draftText)}</div>
                </div>
            `;
            showToast("Draft generated", "success");
        } catch (err) {
            statusEl.textContent = "";
            resultEl.innerHTML = `
                <div class="em-card" style="border-left:3px solid var(--em-red-500);padding:16px">
                    <div style="font-weight:600;color:var(--em-red-600);margin-bottom:4px">Generation failed</div>
                    <div style="font-size:13px;color:var(--em-slate-600)">${escapeHtml(err.message || "Unknown error")}</div>
                </div>
            `;
            showToast("Draft generation failed", "error");
        } finally {
            generateBtn.disabled = false;
        }
    });
}
