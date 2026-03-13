/** Popup script — auth flow + status dashboard.
 *  Depends on: supabase-config.js (loaded via <script> in popup.html)
 *
 *  Onboarding state machine (stored in chrome.storage.local as onboardingState):
 *    "welcome" → first install, show value prop + Get Started
 *    "login"   → show login/signup form
 *    "setup"   → post-login checklist (auth ✓, token ?, sync ?)
 *    "complete" → full status dashboard
 */

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function setStatusDot(el, color, text) {
  el.textContent = "";
  const span = document.createElement("span");
  span.className = `dot ${color}`;
  el.appendChild(span);
  el.appendChild(document.createTextNode(text));
}

function relativeTime(ts) {
  if (!ts) return "—";
  const diff = Date.now() - new Date(ts).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
  return `${Math.floor(diff / 3_600_000)}h`;
}

function escapeHtml(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function setStatValue(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("skeleton-text");
  el.textContent = value !== null && value !== undefined ? value : "—";
}

function showError(msg) {
  const el = document.getElementById("authError");
  el.textContent = msg;
  el.style.display = "block";
}

function hideError() {
  const el = document.getElementById("authError");
  el.style.display = "none";
}

function showStatusError(msg) {
  const el = document.getElementById("statusError");
  if (!el) return;
  el.textContent = msg;
  el.style.display = "block";
}

function hideStatusError() {
  const el = document.getElementById("statusError");
  if (!el) return;
  el.style.display = "none";
}

const ALL_VIEWS = ["welcomeView", "loginView", "setupView", "statusView"];

function showView(viewId) {
  for (const id of ALL_VIEWS) {
    document.getElementById(id).style.display = id === viewId ? "block" : "none";
  }
}

// ---------------------------------------------------------------------------
// Auth helpers (direct REST — popup can't access background's functions)
// ---------------------------------------------------------------------------

async function authRequest(endpoint, body) {
  const resp = await fetch(`${SUPABASE_URL}/auth/v1${endpoint}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      apikey: SUPABASE_ANON_KEY,
    },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error_description || data.msg || data.error || "Auth failed");
  }
  return data;
}

async function setWorkerActive(accessToken, userId, active) {
  try {
    const resp = await fetch(`${SUPABASE_URL}/rest/v1/profiles?id=eq.${userId}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        apikey: SUPABASE_ANON_KEY,
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify({ worker_active: active }),
    });
    if (!resp.ok && DEBUG) console.warn("Failed to set worker_active:", resp.status);
  } catch (e) {
    if (DEBUG) console.warn("Failed to set worker_active:", e);
  }
}

function sessionFromResponse(data) {
  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + (data.expires_in || 3600),
    user: {
      id: data.user?.id,
      email: data.user?.email,
      name: data.user?.user_metadata?.full_name || data.user?.user_metadata?.name || "",
    },
  };
}

// ---------------------------------------------------------------------------
// Stats helpers
// ---------------------------------------------------------------------------

async function supabaseQuery(path, session) {
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    headers: {
      apikey: SUPABASE_ANON_KEY,
      Authorization: `Bearer ${session.access_token}`,
    },
  });
  if (!resp.ok) throw new Error(`Query failed: ${resp.status}`);
  return resp.json();
}

function todayISO() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.toISOString();
}

async function fetchEnhancedStats(session) {
  const today = todayISO();
  const uid = session.user?.id;
  if (!uid) return null;

  try {
    const [processed, classifications, drafts, pipelineRuns] = await Promise.all([
      // Q1: Processed count
      supabaseQuery(
        `emails?select=id&user_id=eq.${uid}&folder=eq.Inbox&received_time=gte.${today}&status=eq.completed`,
        session
      ),
      // Q2: Attention candidates with embedded email data
      supabaseQuery(
        `classifications?select=id,email_id,action,created_at,emails(id,email_ref,subject,sender_name,received_time)&user_id=eq.${uid}&needs_response=eq.true&created_at=gte.${today}&order=created_at.desc`,
        session
      ),
      // Q3: Drafts with sender name
      supabaseQuery(
        `drafts?select=id,email_id,created_at,emails(sender_name)&user_id=eq.${uid}&created_at=gte.${today}&order=created_at.desc`,
        session
      ),
      // Q4: Pipeline runs for activity feed
      supabaseQuery(
        `pipeline_runs?select=id,status,emails_processed,drafts_generated,finished_at&user_id=eq.${uid}&order=finished_at.desc&limit=5`,
        session
      ),
    ]);

    // Client-side cross-reference: exclude classifications that already have drafts
    const draftedEmailIds = new Set(drafts.map(d => d.email_id));
    const attentionItems = classifications.filter(c => !draftedEmailIds.has(c.email_id));

    // Build activity feed: merge drafts + pipeline runs, sorted by time, max 3
    const activities = [];
    for (const d of drafts) {
      const senderName = d.emails?.sender_name || "Unknown";
      activities.push({
        text: `Draft created for ${senderName}`,
        time: d.created_at,
      });
    }
    for (const r of pipelineRuns) {
      if (r.emails_processed > 0 && r.finished_at) {
        activities.push({
          text: `${r.emails_processed} emails classified`,
          time: r.finished_at,
        });
      }
    }
    activities.sort((a, b) => new Date(b.time) - new Date(a.time));

    return {
      processedCount: processed.length,
      draftsCount: drafts.length,
      attentionItems,
      attentionCount: attentionItems.length,
      activities: activities.slice(0, 3),
    };
  } catch (_) {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Render helpers for enhanced status view
// ---------------------------------------------------------------------------

function renderStatusData(data) {
  if (!data) return;

  setStatValue("statAttention", data.attentionCount);
  setStatValue("statDrafts", data.draftsCount);
  setStatValue("statProcessed", data.processedCount);

  // Amber accent on attention card when count > 0
  const card = document.getElementById("attentionCard");
  if (card) {
    card.classList.toggle("attention-active", data.attentionCount > 0);
  }

  renderAttentionList(data.attentionItems, data.attentionCount);
  renderActivityFeed(data.activities);
}

function renderAttentionList(items, total) {
  const section = document.getElementById("attentionSection");
  if (!section) return;

  if (total === 0) {
    section.innerHTML = '<div class="caught-up">All caught up</div>';
    return;
  }

  let html = '<div class="section-title">Needs Attention</div>';
  const display = items.slice(0, 3);
  for (const c of display) {
    const email = c.emails;
    if (!email) continue;
    const ref = email.email_ref ? encodeURIComponent(email.email_ref) : "";
    const link = ref ? `https://outlook.office.com/mail/id/${ref}` : "#";
    const sender = escapeHtml(email.sender_name || "Unknown");
    const subject = escapeHtml(email.subject || "(no subject)");
    const time = relativeTime(email.received_time);
    html += `<div class="attention-item" data-link="${escapeHtml(link)}">
      <span class="attention-sender">${sender}</span>
      <span class="attention-subject">${subject}</span>
      <span class="attention-time">${time}</span>
    </div>`;
  }
  if (total > 3) {
    html += `<div class="attention-more" id="viewAllAttention">View all ${total} →</div>`;
  }
  section.innerHTML = html;

  // Bind click handlers
  section.querySelectorAll(".attention-item").forEach(el => {
    el.addEventListener("click", () => {
      const url = el.getAttribute("data-link");
      if (url && url !== "#") chrome.tabs.create({ url });
    });
  });
  const viewAll = document.getElementById("viewAllAttention");
  if (viewAll) {
    viewAll.addEventListener("click", () => {
      chrome.tabs.create({ url: "https://clarion-ai.app/app/emails.html" });
    });
  }
}

function renderActivityFeed(activities) {
  const section = document.getElementById("activitySection");
  if (!section) return;

  if (!activities || activities.length === 0) {
    section.innerHTML = '<div class="section-title">Recent Activity</div><div class="activity-empty">No recent activity</div>';
    return;
  }

  let html = '<div class="section-title">Recent Activity</div>';
  for (const a of activities) {
    html += `<div class="activity-item">
      <span class="activity-text">${escapeHtml(a.text)}</span>
      <span class="activity-time">${relativeTime(a.time)}</span>
    </div>`;
  }
  section.innerHTML = html;
}

let initialLoadDone = false;

async function refreshStatusData(session) {
  if (!session || !session.access_token) return;

  const useDelay = !initialLoadDone;
  const fetchStart = Date.now();
  const data = await fetchEnhancedStats(session);
  if (useDelay) {
    const elapsed = Date.now() - fetchStart;
    if (elapsed < 300) {
      await new Promise(r => setTimeout(r, 300 - elapsed));
    }
    initialLoadDone = true;
  }
  renderStatusData(data);
}

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

async function getState() {
  const result = await chrome.storage.local.get("onboardingState");
  return result.onboardingState || "welcome";
}

async function setState(state) {
  await chrome.storage.local.set({ onboardingState: state });
}

async function checkSessionAndRender() {
  const result = await chrome.storage.local.get("supabaseSession");
  const session = result.supabaseSession;
  const state = await getState();

  if (!session || !session.access_token) {
    // No session — show welcome or login depending on state
    if (state === "welcome") {
      showView("welcomeView");
    } else {
      showView("loginView");
    }
    return;
  }

  // Has session — determine if setup or complete
  if (state === "complete") {
    showStatusView(session);
  } else {
    // Any non-complete state with a valid session → setup
    await setState("setup");
    showSetupView(session);
  }
}

// ---------------------------------------------------------------------------
// View renderers
// ---------------------------------------------------------------------------

function showSetupView(session) {
  showView("setupView");

  // Dynamic greeting
  const name = session.user?.name;
  const greeting = document.getElementById("setupGreeting");
  greeting.textContent = name ? `Welcome, ${name}! Almost there.` : "Almost there!";

  // Update checklist
  updateSetupChecklist(session);
}

function updateSetupChecklist(session) {
  const setCheck = (id, done) => {
    const el = document.getElementById(id);
    if (done) {
      el.textContent = "\u2705";
      el.className = "check-icon check-done";
    } else {
      el.textContent = "\u2B58";
      el.className = "check-icon check-pending";
    }
  };

  // Auth — always done if we're on this view
  setCheck("checkAuth", true);

  // Token + sync — ask background
  chrome.runtime.sendMessage({ type: "getStatus" }, async (status) => {
    if (chrome.runtime.lastError || !status) return;

    const hasToken = status.has_token && !status.token_expired;
    setCheck("checkOutlook", hasToken);
    setCheck("checkSync", !!status.last_sync);

    // All three done → transition to complete
    if (hasToken && status.last_sync) {
      await setState("complete");
      const result = await chrome.storage.local.get("supabaseSession");
      if (result.supabaseSession) {
        showStatusView(result.supabaseSession);
      }
    }
  });
}

function showStatusView(session) {
  showView("statusView");
  initialLoadDone = false;
  document.getElementById("userEmail").textContent = session.user?.email || "—";
  refreshStatus(session);
}

function refreshStatus(session) {
  // Connection indicator (inline in user bar)
  chrome.runtime.sendMessage({ type: "getStatus" }, (status) => {
    if (chrome.runtime.lastError || !status) return;

    const now = Math.floor(Date.now() / 1000);
    const supabaseOk = session && session.expires_at > now;
    const outlookOk = status.has_token && !status.token_expired;
    const connected = supabaseOk && outlookOk;

    // Inline connection indicator
    const indicator = document.getElementById("connectionIndicator");
    if (indicator) {
      indicator.innerHTML = "";
      const dot = document.createElement("span");
      dot.className = `dot ${connected ? "green" : "red"}`;
      indicator.appendChild(dot);
      const txt = document.createElement("span");
      txt.id = "connectionText";
      txt.textContent = connected ? "Connected" : "Disconnected";
      indicator.appendChild(txt);
    }

    const hint = document.getElementById("connectionHint");
    if (!connected) {
      let hintMsg = "";
      if (!supabaseOk) hintMsg = "Session expired — please log in again";
      else if (!status.has_token) hintMsg = "Open Outlook in this browser to connect";
      else if (status.token_expired) hintMsg = "Reopen Outlook to refresh your session";
      hint.textContent = hintMsg;
      hint.style.display = hintMsg ? "block" : "none";
    } else {
      hint.style.display = "none";
    }

    // Advanced section
    const authEl = document.getElementById("supabaseAuth");
    setStatusDot(authEl, supabaseOk ? "green" : "red",
      supabaseOk ? "Authenticated" : "Expired");

    const tokenEl = document.getElementById("tokenStatus");
    const expiresEl = document.getElementById("tokenExpires");
    const originEl = document.getElementById("tokenOrigin");
    const realtimeEl = document.getElementById("realtimeStatus");
    const syncEl = document.getElementById("lastSync");

    if (!status.has_token) {
      setStatusDot(tokenEl, "red", "Not connected");
      expiresEl.textContent = "—";
    } else if (status.token_expired) {
      setStatusDot(tokenEl, "red", "Expired");
      expiresEl.textContent = "—";
    } else {
      setStatusDot(tokenEl, "green", "Connected");
      if (status.token_expires) {
        const exp = new Date(status.token_expires);
        const hours = Math.max(0, Math.round((exp - Date.now()) / 3_600_000));
        expiresEl.textContent = `~${hours}h remaining`;
      }
    }

    originEl.textContent = status.token_origin
      ? new URL(status.token_origin).hostname
      : "—";

    if (realtimeEl) {
      setStatusDot(realtimeEl, status.realtime_connected ? "green" : "gray",
        status.realtime_connected ? "Connected" : "Disconnected");
    }

    const syncAgo = relativeTime(status.last_sync);
    syncEl.textContent = status.last_sync
      ? (syncAgo === "just now" ? syncAgo : syncAgo + " ago")
      : "never";
  });

  // Enhanced stats + render
  refreshStatusData(session);
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

let isSignUpMode = false;

// Welcome → Get Started
document.getElementById("getStartedBtn").addEventListener("click", async () => {
  await setState("login");
  showView("loginView");
});

// Login / Sign Up
document.getElementById("loginBtn").addEventListener("click", async () => {
  hideError();
  const email = document.getElementById("authEmail").value.trim();
  const password = document.getElementById("authPassword").value;

  if (!email || !password) {
    showError("Email and password are required");
    return;
  }

  const btn = document.getElementById("loginBtn");
  btn.disabled = true;
  btn.textContent = isSignUpMode ? "Signing up..." : "Logging in...";

  try {
    let session;
    if (isSignUpMode) {
      const result = await authRequest("/signup", { email, password });
      if (result.access_token) {
        session = sessionFromResponse(result);
      } else {
        btn.disabled = false;
        btn.textContent = "Sign Up";
        showError("Check your email to confirm your account");
        return;
      }
    } else {
      const result = await authRequest("/token?grant_type=password", { email, password });
      session = sessionFromResponse(result);
    }

    await chrome.storage.local.set({ supabaseSession: session });

    // Activate worker processing
    await setWorkerActive(session.access_token, session.user.id, true);

    // Set timezone on profile so the worker knows business hours
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Chicago";
      await fetch(`${SUPABASE_URL}/rest/v1/profiles?id=eq.${session.user.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          apikey: SUPABASE_ANON_KEY,
          Authorization: `Bearer ${session.access_token}`,
        },
        body: JSON.stringify({ timezone: tz }),
      });
    } catch (_) {}

    // Notify background to initialize Supabase features
    chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });

    // Transition to setup checklist
    await setState("setup");
    showSetupView(session);
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = isSignUpMode ? "Sign Up" : "Log In";
  }
});

document.getElementById("toggleAuth").addEventListener("click", () => {
  isSignUpMode = !isSignUpMode;
  const btn = document.getElementById("loginBtn");
  const link = document.getElementById("toggleAuth");
  if (isSignUpMode) {
    btn.textContent = "Sign Up";
    link.textContent = "Already have an account? Log in";
  } else {
    btn.textContent = "Log In";
    link.textContent = "Don't have an account? Sign up";
  }
  hideError();
});

// Logout — two-step confirmation
function initLogoutBtn() {
  const container = document.getElementById("logoutContainer");
  container.innerHTML = '<button class="btn btn-sm btn-outline" id="logoutBtn">Logout</button>';
  document.getElementById("logoutBtn").addEventListener("click", showLogoutConfirm);
}

function showLogoutConfirm() {
  const container = document.getElementById("logoutContainer");
  container.innerHTML = `<span class="logout-confirm">
    <span>Are you sure?</span>
    <button class="btn btn-sm btn-danger" id="logoutYes">Yes</button>
    <button class="btn btn-sm btn-outline" id="logoutCancel">Cancel</button>
  </span>`;

  document.getElementById("logoutYes").addEventListener("click", async () => {
    const result = await chrome.storage.local.get("supabaseSession");
    const session = result.supabaseSession;
    if (session?.access_token && session?.user?.id) {
      setWorkerActive(session.access_token, session.user.id, false).catch(() => {});
    }
    await chrome.storage.local.remove("supabaseSession");
    await chrome.storage.local.remove("lastSyncTime");
    await chrome.storage.session.remove("exchangeToken");
    await setState("login");
    chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });
    showView("loginView");
  });

  document.getElementById("logoutCancel").addEventListener("click", () => initLogoutBtn());
}

document.getElementById("logoutBtn").addEventListener("click", showLogoutConfirm);

// Sync buttons (status view + setup view)
function friendlySyncError(raw) {
  if (!raw) return null;
  if (raw === "No valid Outlook token") return "Outlook not connected — open Outlook in this browser";
  if (raw === "Not logged in to Supabase") return "Please log in first";
  if (raw === "TOKEN_EXPIRED") return "Outlook session expired — reopen Outlook to refresh";
  if (/Failed to fetch|NetworkError|TypeError/i.test(raw)) return "Unable to reach server — check your internet connection";
  if (/50[234]/i.test(raw)) return "Server temporarily unavailable — try again in a moment";
  return "Sync failed — try again later";
}

function handleSyncClick(btn, errorFn) {
  btn.disabled = true;
  btn.textContent = "Syncing...";
  chrome.runtime.sendMessage({ type: "syncNow" }, (resp) => {
    btn.disabled = false;
    btn.textContent = "Sync Now";
    if (resp && resp.error) {
      errorFn(friendlySyncError(resp.error));
    } else {
      // Refresh stats after successful sync
      chrome.storage.local.get("supabaseSession", (result) => {
        if (result.supabaseSession) refreshStatus(result.supabaseSession);
      });
    }
  });
}

document.getElementById("syncNowBtn").addEventListener("click", () => {
  hideStatusError();
  handleSyncClick(document.getElementById("syncNowBtn"), showStatusError);
});

document.getElementById("setupSyncBtn").addEventListener("click", () => {
  hideError();
  handleSyncClick(document.getElementById("setupSyncBtn"), showError);
});

document.getElementById("visitWebBtn").addEventListener("click", () => {
  chrome.tabs.create({ url: "https://clarion-ai.app/app/dashboard.html" });
});

// Allow Enter key to submit
document.getElementById("authPassword").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("loginBtn").click();
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

checkSessionAndRender();

// Periodic refresh — updates whichever view is visible
setInterval(() => {
  const statusVisible = document.getElementById("statusView").style.display !== "none";
  const setupVisible = document.getElementById("setupView").style.display !== "none";
  if (statusVisible) {
    chrome.storage.local.get("supabaseSession", (result) => {
      if (result.supabaseSession) refreshStatus(result.supabaseSession);
    });
  } else if (setupVisible) {
    chrome.storage.local.get("supabaseSession", (result) => {
      if (result.supabaseSession) updateSetupChecklist(result.supabaseSession);
    });
  }
}, 30000);
