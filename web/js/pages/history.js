/**
 * History page — pipeline run log table with expandable detail.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, showEmpty, formatDate, formatDuration } from "../ui.js";

import { ensureAccess } from "../subscription.js";

await requireAuth();
await ensureAccess();
listenAuthChanges();
await renderNav();

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const historyBody = document.getElementById("historyBody");
const refreshBtn = document.getElementById("refreshBtn");

refreshBtn.addEventListener("click", loadRuns);

// -------------------------------------------------------------------------
// Load pipeline runs
// -------------------------------------------------------------------------

async function loadRuns() {
    try {
        const { data, error } = await supabase
            .from("pipeline_runs")
            .select("*")
            .order("started_at", { ascending: false })
            .limit(50);

        if (error) throw error;

        if (!data || data.length === 0) {
            historyBody.innerHTML = `<tr><td colspan="7"><div class="em-empty" style="padding: 24px;">No pipeline runs recorded.</div></td></tr>`;
            return;
        }

        historyBody.innerHTML = data.map(run => {
            const duration = run.finished_at && run.started_at
                ? new Date(run.finished_at) - new Date(run.started_at)
                : null;

            const statusBadge = run.status === "completed"
                ? `<span class="em-badge em-badge-green">completed</span>`
                : run.status === "failed"
                ? `<span class="em-badge em-badge-red">failed</span>`
                : `<span class="em-badge em-badge-amber">${escapeHtml(run.status)}</span>`;

            const hasDetail = run.error_message || run.log_output;
            const rowClass = run.status === "failed" ? " em-row-failed" : "";

            return `
                <tr class="em-table-clickable${rowClass}" data-run-id="${run.id}">
                    <td>${formatDate(run.started_at)}</td>
                    <td>${escapeHtml(run.trigger_type || "scheduled")}</td>
                    <td>${statusBadge}</td>
                    <td>${run.emails_scanned || 0}</td>
                    <td>${run.emails_processed || 0}</td>
                    <td>${run.drafts_generated || 0}</td>
                    <td>${formatDuration(duration)}</td>
                </tr>
                ${hasDetail ? `
                    <tr class="em-detail-row" data-detail-for="${run.id}">
                        <td colspan="7" class="em-detail-cell">
                            ${run.error_message ? `<div style="color: var(--em-red-600); margin-bottom: 8px;"><strong>Error:</strong> ${escapeHtml(run.error_message)}</div>` : ""}
                            ${run.log_output ? `<div><strong>Log:</strong>\n${escapeHtml(run.log_output)}</div>` : ""}
                        </td>
                    </tr>
                ` : ""}
            `;
        }).join("");

        bindRowEvents();
    } catch (err) {
        showError(`Failed to load history: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Row click to expand detail
// -------------------------------------------------------------------------

function bindRowEvents() {
    document.querySelectorAll(".em-table-clickable").forEach(row => {
        row.addEventListener("click", () => {
            const runId = row.dataset.runId;
            const detailRow = document.querySelector(`.em-detail-row[data-detail-for="${runId}"]`);
            if (detailRow) {
                detailRow.classList.toggle("visible");
            }
        });
    });
}

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

loadRuns();
