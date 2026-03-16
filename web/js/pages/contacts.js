/**
 * Contacts page — view and manage per-contact preferences.
 * VIP flag, draft preference, priority override.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, showEmpty, showToast, getParam, setParam, escapeHtml } from "../ui.js";
import { requireSubscription } from "../subscription.js";

await requireAuth();
listenAuthChanges();
await renderNav();
if (!(await requireSubscription())) throw new Error("subscription_required");

// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------

let allContacts = [];
let searchQuery = getParam("q", "");

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const container = document.getElementById("contactsContainer");
const searchInput = document.getElementById("searchInput");
const pageSubtitle = document.getElementById("pageSubtitle");
const modal = document.getElementById("contactModal");
const modalTitle = document.getElementById("modalTitle");
const modalBody = document.getElementById("modalBody");
const modalClose = document.getElementById("modalClose");
const modalCancel = document.getElementById("modalCancel");
const modalSave = document.getElementById("modalSave");

searchInput.value = searchQuery;

// -------------------------------------------------------------------------
// Search
// -------------------------------------------------------------------------

let searchTimer = null;
searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        searchQuery = searchInput.value.trim().toLowerCase();
        setParam("q", searchQuery);
        renderContacts();
    }, 300);
});

// -------------------------------------------------------------------------
// Load contacts
// -------------------------------------------------------------------------

async function loadContacts() {
    try {
        const { data, error } = await supabase
            .from("contacts")
            .select("id, email, name, organization, contact_type, emails_per_month, is_vip, draft_preference, priority_override, total_received")
            .order("total_received", { ascending: false, nullsFirst: false });

        if (error) throw error;
        allContacts = data || [];
        renderContacts();
    } catch (err) {
        showError(`Failed to load contacts: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Render
// -------------------------------------------------------------------------

function renderContacts() {
    let filtered = allContacts;
    if (searchQuery) {
        filtered = allContacts.filter(c =>
            (c.name || "").toLowerCase().includes(searchQuery) ||
            (c.email || "").toLowerCase().includes(searchQuery) ||
            (c.organization || "").toLowerCase().includes(searchQuery)
        );
    }

    const vipCount = allContacts.filter(c => c.is_vip).length;
    pageSubtitle.textContent = `${allContacts.length} contacts${vipCount > 0 ? ` · ${vipCount} VIP` : ""}`;

    if (filtered.length === 0) {
        showEmpty(container, allContacts.length === 0 ? "No contacts synced yet." : "No contacts match your search.");
        return;
    }

    const typeLabels = {
        internal: "Internal",
        external_legal: "Legal",
        external_lender: "Lender",
        external_vendor: "Vendor",
        investor: "Investor",
        unknown: "",
    };

    let html = `<div class="em-contacts-list">`;
    for (const contact of filtered) {
        const typeLabel = typeLabels[contact.contact_type] || contact.contact_type || "";
        const draftLabel = contact.draft_preference === "always" ? "Always draft"
            : contact.draft_preference === "never" ? "Never draft"
            : "";
        const prioLabel = contact.priority_override ? `Force ${contact.priority_override}` : "";

        const tags = [];
        if (contact.is_vip) tags.push(`<span class="em-badge em-badge-amber">VIP</span>`);
        if (draftLabel) tags.push(`<span class="em-badge em-badge-blue">${draftLabel}</span>`);
        if (prioLabel) tags.push(`<span class="em-badge em-badge-slate">${prioLabel}</span>`);

        html += `
            <div class="em-contact-row" data-id="${contact.id}">
                <div class="em-contact-info">
                    <div class="em-contact-name">
                        ${escapeHtml(contact.name || contact.email)}
                        ${typeLabel ? `<span class="em-contact-type">${escapeHtml(typeLabel)}</span>` : ""}
                    </div>
                    <div class="em-contact-email">${escapeHtml(contact.email)}</div>
                </div>
                <div class="em-contact-meta">
                    ${tags.join("")}
                    ${contact.emails_per_month ? `<span class="em-contact-freq">~${contact.emails_per_month}/mo</span>` : ""}
                    <button class="em-btn em-btn-secondary em-btn-sm em-contact-edit" data-id="${contact.id}">Edit</button>
                </div>
            </div>
        `;
    }
    html += `</div>`;

    container.innerHTML = html;
    bindContactEvents();
}

// -------------------------------------------------------------------------
// Event binding
// -------------------------------------------------------------------------

function bindContactEvents() {
    document.querySelectorAll(".em-contact-edit").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            openEditModal(btn.dataset.id);
        });
    });
}

// -------------------------------------------------------------------------
// Edit modal
// -------------------------------------------------------------------------

let editingContactId = null;

function openEditModal(contactId) {
    const contact = allContacts.find(c => c.id === contactId);
    if (!contact) return;

    editingContactId = contactId;
    modalTitle.textContent = contact.name || contact.email;

    modalBody.innerHTML = `
        <div class="em-form-group">
            <label class="em-form-label">
                <input type="checkbox" id="editVip" ${contact.is_vip ? "checked" : ""}>
                Mark as VIP
            </label>
            <div class="em-form-hint">VIP contacts are always treated as high priority</div>
        </div>
        <div class="em-form-group">
            <label class="em-form-label">Draft preference</label>
            <div class="em-form-hint">Control whether Clarion auto-drafts replies for this sender</div>
            <select id="editDraftPref" class="em-form-select">
                <option value="auto" ${contact.draft_preference === "auto" ? "selected" : ""}>Use AI judgment</option>
                <option value="always" ${contact.draft_preference === "always" ? "selected" : ""}>Always draft</option>
                <option value="never" ${contact.draft_preference === "never" ? "selected" : ""}>Never draft</option>
            </select>
        </div>
        <div class="em-form-group">
            <label class="em-form-label">Priority override</label>
            <div class="em-form-hint">Force a priority level for all emails from this sender</div>
            <select id="editPriorityOverride" class="em-form-select">
                <option value="" ${!contact.priority_override ? "selected" : ""}>None (use AI)</option>
                <option value="high" ${contact.priority_override === "high" ? "selected" : ""}>High</option>
                <option value="med" ${contact.priority_override === "med" ? "selected" : ""}>Medium</option>
                <option value="low" ${contact.priority_override === "low" ? "selected" : ""}>Low</option>
            </select>
        </div>
    `;

    modal.style.display = "flex";
}

function closeModal() {
    modal.style.display = "none";
    editingContactId = null;
}

modalClose.addEventListener("click", closeModal);
modalCancel.addEventListener("click", closeModal);
modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
});

modalSave.addEventListener("click", async () => {
    if (!editingContactId) return;

    const isVip = document.getElementById("editVip").checked;
    const draftPref = document.getElementById("editDraftPref").value;
    const prioOverride = document.getElementById("editPriorityOverride").value || null;

    modalSave.disabled = true;
    modalSave.textContent = "Saving...";

    try {
        const { error } = await supabase
            .from("contacts")
            .update({
                is_vip: isVip,
                draft_preference: draftPref,
                priority_override: prioOverride,
            })
            .eq("id", editingContactId);

        if (error) throw error;

        // Update local state
        const contact = allContacts.find(c => c.id === editingContactId);
        if (contact) {
            contact.is_vip = isVip;
            contact.draft_preference = draftPref;
            contact.priority_override = prioOverride;
        }

        closeModal();
        renderContacts();
        showToast("Contact updated");
    } catch (err) {
        showError(`Failed to save: ${err.message}`);
    } finally {
        modalSave.disabled = false;
        modalSave.textContent = "Save";
    }
});

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

loadContacts();
