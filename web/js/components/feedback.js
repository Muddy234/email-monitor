/**
 * Feedback widget — thumbs up/down with correction dropdown.
 * Renders inline on email cards (Drafts and Notable tabs).
 */
import { supabase } from "../supabase-client.js";
import { showToast, escapeHtml } from "../ui.js";

// Correction options by tab context
const CORRECTIONS = {
    drafts: [
        { value: "no_response_needed", label: "This doesn't need a response" },
        { value: "wrong_priority", label: "Priority is wrong" },
        { value: "draft_quality", label: "Draft tone/content was off" },
        { value: "other", label: "Other" },
    ],
    notable: [
        { value: "response_needed", label: "This actually needs a response" },
        { value: "wrong_priority", label: "Priority is wrong" },
        { value: "other", label: "Other" },
    ],
};

const PRIORITY_OPTIONS = ["high", "med", "low"];

/**
 * Render feedback HTML for an email card.
 * @param {string} emailId
 * @param {string} tab — "drafts" or "notable"
 * @returns {string} HTML string
 */
export function renderFeedbackControls(emailId, tab) {
    if (tab !== "drafts" && tab !== "notable") return "";

    return `
        <div class="em-feedback" data-email-id="${emailId}" data-tab="${tab}">
            <div class="em-feedback-buttons">
                <button class="em-feedback-btn em-feedback-up" data-email-id="${emailId}" title="Good classification">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/>
                        <path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>
                    </svg>
                </button>
                <button class="em-feedback-btn em-feedback-down" data-email-id="${emailId}" title="Misclassified">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3H10z"/>
                        <path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/>
                    </svg>
                </button>
            </div>
            <div class="em-feedback-dropdown" data-email-id="${emailId}" style="display: none;">
                <div class="em-feedback-dropdown-label">What went wrong?</div>
                <div class="em-feedback-options" data-email-id="${emailId}">
                    ${(CORRECTIONS[tab] || []).map(opt => `
                        <button class="em-feedback-option" data-email-id="${emailId}" data-value="${opt.value}">${escapeHtml(opt.label)}</button>
                    `).join("")}
                </div>
                <div class="em-feedback-priority" data-email-id="${emailId}" style="display: none;">
                    <div class="em-feedback-dropdown-label">Correct priority:</div>
                    <div class="em-feedback-priority-options">
                        ${PRIORITY_OPTIONS.map(p => `
                            <button class="em-feedback-priority-btn" data-email-id="${emailId}" data-priority="${p}">${p.charAt(0).toUpperCase() + p.slice(1)}</button>
                        `).join("")}
                    </div>
                </div>
                <div class="em-feedback-other" data-email-id="${emailId}" style="display: none;">
                    <textarea class="em-feedback-text" data-email-id="${emailId}" placeholder="What should have happened?" rows="2"></textarea>
                    <button class="em-btn em-btn-primary em-btn-sm em-feedback-submit-other" data-email-id="${emailId}">Submit</button>
                </div>
            </div>
        </div>
    `;
}

/**
 * Bind event listeners for all feedback controls in a container.
 * Call this after rendering email cards.
 */
export function bindFeedbackEvents() {
    // Thumbs up
    document.querySelectorAll(".em-feedback-up").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const emailId = btn.dataset.emailId;
            await submitFeedback(emailId, "positive", null, null);
            markFeedbackDone(emailId, "up");
        });
    });

    // Thumbs down — toggle dropdown
    document.querySelectorAll(".em-feedback-down").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const emailId = btn.dataset.emailId;
            const dropdown = document.querySelector(`.em-feedback-dropdown[data-email-id="${emailId}"]`);
            if (dropdown) {
                const visible = dropdown.style.display !== "none";
                dropdown.style.display = visible ? "none" : "block";
            }
        });
    });

    // Correction option buttons
    document.querySelectorAll(".em-feedback-option").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const emailId = btn.dataset.emailId;
            const value = btn.dataset.value;

            if (value === "wrong_priority") {
                // Show priority selector
                const priorityEl = document.querySelector(`.em-feedback-priority[data-email-id="${emailId}"]`);
                if (priorityEl) priorityEl.style.display = "block";
                // Highlight selected option
                btn.closest(".em-feedback-options").querySelectorAll(".em-feedback-option").forEach(o => o.classList.remove("selected"));
                btn.classList.add("selected");
                return;
            }

            if (value === "other") {
                // Show text input
                const otherEl = document.querySelector(`.em-feedback-other[data-email-id="${emailId}"]`);
                if (otherEl) otherEl.style.display = "block";
                btn.closest(".em-feedback-options").querySelectorAll(".em-feedback-option").forEach(o => o.classList.remove("selected"));
                btn.classList.add("selected");
                return;
            }

            // Direct submission
            await submitFeedback(emailId, "negative", value, null);
            markFeedbackDone(emailId, "down");
        });
    });

    // Priority selection
    document.querySelectorAll(".em-feedback-priority-btn").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const emailId = btn.dataset.emailId;
            await submitFeedback(emailId, "negative", "wrong_priority", btn.dataset.priority);
            markFeedbackDone(emailId, "down");
        });
    });

    // Other — submit button
    document.querySelectorAll(".em-feedback-submit-other").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const emailId = btn.dataset.emailId;
            const textarea = document.querySelector(`.em-feedback-text[data-email-id="${emailId}"]`);
            const text = textarea?.value?.trim() || "";
            await submitFeedback(emailId, "negative", "other", text);
            markFeedbackDone(emailId, "down");
        });
    });
}

/**
 * Submit feedback to Supabase.
 */
async function submitFeedback(emailId, feedbackType, correctionCategory, correctionValue) {
    try {
        const { data: { session } } = await supabase.auth.getSession();
        if (!session) return;

        const { error } = await supabase.from("feedback").insert({
            user_id: session.user.id,
            email_id: emailId,
            feedback_type: feedbackType,
            correction_category: correctionCategory,
            correction_value: correctionValue,
        });

        if (error) throw error;
        showToast("Thanks for the feedback");
    } catch (err) {
        showToast("Failed to save feedback", "error");
    }
}

/**
 * Replace feedback controls with a "done" state after submission.
 */
function markFeedbackDone(emailId, type) {
    const container = document.querySelector(`.em-feedback[data-email-id="${emailId}"]`);
    if (!container) return;

    const icon = type === "up" ? "&#128077;" : "&#128078;";
    container.innerHTML = `<span class="em-feedback-done">${icon} Feedback recorded</span>`;
}
