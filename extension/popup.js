/** Popup script — auth flow + headline stat dashboard.
 *  Depends on: supabase-config.js (loaded via <script> in popup.html)
 *
 *  Onboarding state machine (stored in chrome.storage.local as onboardingState):
 *    "welcome"  → first install, show value prop + Get Started
 *    "login"    → show login/signup form
 *    "setup"    → post-login checklist (auth ✓, token ?, sync ?)
 *    "complete" → headline stat dashboard
 */

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

function removeSkeleton(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove("skeleton-text");
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

async function fetchCounts(session) {
  const uid = session.user?.id;
  if (!uid) return { drafts: 0, notable: 0, processed: 0, draftsGenerated: 0 };

  try {
    // Fetch drafts, notable signals, and weekly stats in parallel
    const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString();

    const [drafts, events, runs] = await Promise.all([
      supabaseQuery(`drafts?select=id&user_id=eq.${uid}&status=eq.pending`, session),
      supabaseQuery(`response_events?select=email_id,pri,mc,sender_tier,rt&user_id=eq.${uid}`, session),
      supabaseQuery(`pipeline_runs?select=emails_processed,drafts_generated&user_id=eq.${uid}&started_at=gte.${weekAgo}`, session),
    ]);

    const draftCount = drafts.length;

    // Count notable: high/med priority, financial, critical/internal sender, or has response type
    let notableCount = 0;
    for (const ev of events) {
      if (ev.pri === "high" || ev.pri === "med" || ev.mc === true ||
          ev.sender_tier === "C" || ev.sender_tier === "I" || ev.rt !== "none") {
        notableCount++;
      }
    }

    // Aggregate weekly stats
    let processed = 0, draftsGenerated = 0;
    for (const run of runs) {
      processed += run.emails_processed || 0;
      draftsGenerated += run.drafts_generated || 0;
    }

    return { drafts: draftCount, notable: notableCount, processed, draftsGenerated };
  } catch (_) {
    return { drafts: 0, notable: 0, processed: 0, draftsGenerated: 0 };
  }
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
    if (state === "welcome") {
      showView("welcomeView");
    } else {
      showView("loginView");
    }
    return;
  }

  if (state === "complete") {
    showStatusView(session);
  } else {
    // Check if user already has sync history — if so, skip setup
    const statusReady = await new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "getStatus" }, (status) => {
        if (chrome.runtime.lastError || !status) return resolve(false);
        resolve(status.has_token && !status.token_expired && !!status.last_sync);
      });
    });

    if (statusReady) {
      await setState("complete");
      showStatusView(session);
    } else {
      await setState("setup");
      showSetupView(session);
    }
  }
}

// ---------------------------------------------------------------------------
// View renderers
// ---------------------------------------------------------------------------

function showSetupView(session) {
  showView("setupView");

  const name = session.user?.name;
  const greeting = document.getElementById("setupGreeting");
  greeting.textContent = name ? `Welcome, ${name}! Almost there.` : "Almost there!";

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

  setCheck("checkAuth", true);

  chrome.runtime.sendMessage({ type: "getStatus" }, async (status) => {
    if (chrome.runtime.lastError || !status) return;

    const hasToken = status.has_token && !status.token_expired;
    setCheck("checkOutlook", hasToken);
    setCheck("checkSync", !!status.last_sync);

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
  document.getElementById("userEmail").textContent = session.user?.email || "—";
  refreshStatus(session);
}

async function refreshStatus(session) {
  // Connection indicator
  chrome.runtime.sendMessage({ type: "getStatus" }, (status) => {
    if (chrome.runtime.lastError || !status) return;

    const now = Math.floor(Date.now() / 1000);
    const supabaseOk = session && session.expires_at > now;
    const outlookOk = status.has_token && !status.token_expired;
    const connected = supabaseOk && outlookOk;

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

      // Error state headline
      if (hintMsg) {
        const el = document.getElementById("statusError");
        el.textContent = hintMsg;
        el.style.display = "block";
      }
    } else {
      hint.style.display = "none";
      const el = document.getElementById("statusError");
      el.style.display = "none";
    }
  });

  // Fetch counts and render headline
  const counts = await fetchCounts(session);
  renderHeadline(counts);
  renderQuickStats(counts);
  renderDeepLinks(counts);
}

// ---------------------------------------------------------------------------
// Headline stat rendering
// ---------------------------------------------------------------------------

function renderHeadline(counts) {
  const numEl = document.getElementById("headlineNumber");
  const textEl = document.getElementById("headlineText");
  const ctaEl = document.getElementById("headlineCta");
  const card = document.getElementById("headlineCard");

  removeSkeleton("headlineNumber");

  if (counts.drafts > 0) {
    numEl.textContent = counts.drafts;
    textEl.textContent = `draft${counts.drafts === 1 ? "" : "s"} ready to review`;
    ctaEl.textContent = "Click to view in Outlook";
    card.onclick = () => navigateToOutlookDrafts();
  } else if (counts.notable > 0) {
    numEl.textContent = counts.notable;
    textEl.textContent = `email${counts.notable === 1 ? "" : "s"} need your attention`;
    ctaEl.textContent = "View on dashboard";
    card.onclick = () => openDashboardTab("/app/emails.html?tab=notable");
  } else {
    numEl.textContent = counts.processed || "0";
    textEl.textContent = "emails handled this week";
    ctaEl.textContent = "All caught up";
    card.onclick = () => openDashboardTab("/app/dashboard.html");
  }
}

function renderQuickStats(counts) {
  const processedEl = document.getElementById("statProcessed");
  const draftsEl = document.getElementById("statDrafts");
  removeSkeleton("statProcessed");
  removeSkeleton("statDrafts");
  processedEl.textContent = counts.processed;
  draftsEl.textContent = counts.draftsGenerated;
}

function renderDeepLinks(counts) {
  const container = document.getElementById("deepLinks");
  let html = "";

  if (counts.drafts > 0) {
    html += `<button class="deep-link" id="linkDrafts">View Drafts (${counts.drafts}) <span class="deep-link-arrow">→</span></button>`;
  }
  if (counts.notable > 0) {
    html += `<button class="deep-link" id="linkNotable">View Notable (${counts.notable}) <span class="deep-link-arrow">→</span></button>`;
  }
  html += `<button class="deep-link" id="linkFeedback">Give Feedback <span class="deep-link-arrow">→</span></button>`;
  html += `<button class="deep-link deep-link-primary" id="linkDashboard">Open Dashboard <span class="deep-link-arrow">→</span></button>`;

  container.innerHTML = html;

  // Bind events
  document.getElementById("linkDrafts")?.addEventListener("click", () => navigateToOutlookDrafts());
  document.getElementById("linkNotable")?.addEventListener("click", () => openDashboardTab("/app/emails.html?tab=notable"));
  document.getElementById("linkFeedback")?.addEventListener("click", () => openDashboardTab("/app/emails.html"));
  document.getElementById("linkDashboard")?.addEventListener("click", () => openDashboardTab("/app/dashboard.html"));
}

// ---------------------------------------------------------------------------
// Navigation helpers
// ---------------------------------------------------------------------------

function navigateToOutlookDrafts() {
  chrome.tabs.query({}, (tabs) => {
    const outlookTab = tabs.find(t =>
      t.url && /^https:\/\/outlook\.(office\.com|office365\.com|live\.com|cloud\.microsoft)(\/|$)/.test(t.url)
    );
    if (outlookTab) {
      const origin = new URL(outlookTab.url).origin;
      chrome.tabs.update(outlookTab.id, { url: `${origin}/mail/drafts`, active: true });
      chrome.windows.update(outlookTab.windowId, { focused: true });
    } else {
      chrome.tabs.create({ url: "https://outlook.office.com/mail/drafts" });
    }
  });
}

function openDashboardTab(path) {
  chrome.tabs.create({ url: `https://clarion-ai.app${path}` });
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
    await setWorkerActive(session.access_token, session.user.id, true);

    // Set timezone
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

    chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });
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
    <span>Sure?</span>
    <button class="btn btn-sm btn-danger" id="logoutYes">Yes</button>
    <button class="btn btn-sm btn-outline" id="logoutCancel">No</button>
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

// Sync (setup view only)
function friendlySyncError(raw) {
  if (!raw) return null;
  if (raw === "No valid Outlook token") return "Outlook not connected — open Outlook in this browser";
  if (raw === "Not logged in to Supabase") return "Please log in first";
  if (raw === "TOKEN_EXPIRED") return "Outlook session expired — reopen Outlook to refresh";
  if (/Failed to fetch|NetworkError|TypeError/i.test(raw)) return "Unable to reach server — check your internet connection";
  if (/50[234]/i.test(raw)) return "Server temporarily unavailable — try again in a moment";
  return "Sync failed — try again later";
}

document.getElementById("setupSyncBtn").addEventListener("click", () => {
  hideError();
  const btn = document.getElementById("setupSyncBtn");
  btn.disabled = true;
  btn.textContent = "Syncing...";
  chrome.runtime.sendMessage({ type: "syncNow" }, (resp) => {
    btn.disabled = false;
    btn.textContent = "Sync Now";
    if (resp && resp.error) {
      showError(friendlySyncError(resp.error));
    } else {
      chrome.storage.local.get("supabaseSession", (result) => {
        if (result.supabaseSession) updateSetupChecklist(result.supabaseSession);
      });
    }
  });
});

// Allow Enter key to submit
document.getElementById("authPassword").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("loginBtn").click();
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

checkSessionAndRender();

// Periodic refresh (every 15s)
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
}, 15000);
