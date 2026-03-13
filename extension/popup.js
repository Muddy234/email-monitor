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

function setDraftCount(value) {
  const el = document.getElementById("draftCount");
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

async function fetchDraftCount(session) {
  const uid = session.user?.id;
  if (!uid) return null;

  try {
    const drafts = await supabaseQuery(
      `drafts?select=id&user_id=eq.${uid}&status=eq.pending`,
      session
    );
    return drafts.length;
  } catch (_) {
    return null;
  }
}

async function refreshDraftCount(session) {
  if (!session || !session.access_token) return;
  const count = await fetchDraftCount(session);
  setDraftCount(count);
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
  document.getElementById("userEmail").textContent = session.user?.email || "—";
  refreshStatus(session);
}

function refreshStatus(session) {
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
    } else {
      hint.style.display = "none";
    }
  });

  // Draft count
  refreshDraftCount(session);
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

document.getElementById("setupSyncBtn").addEventListener("click", () => {
  hideError();
  handleSyncClick(document.getElementById("setupSyncBtn"), showError);
});

document.getElementById("visitWebBtn").addEventListener("click", () => {
  chrome.tabs.create({ url: "https://clarion-ai.app/app/dashboard.html" });
});

// Draft card → navigate existing Outlook tab to drafts, or open new tab
document.getElementById("draftCard").addEventListener("click", () => {
  const outlookPatterns = [
    "https://outlook.office.com/*",
    "https://outlook.office365.com/*",
    "https://outlook.live.com/*",
    "https://outlook.cloud.microsoft/*",
  ];
  const draftsUrl = "https://outlook.office.com/mail/drafts";

  // Query all Outlook domains in parallel
  Promise.all(outlookPatterns.map(url =>
    chrome.tabs.query({ url })
  )).then(results => {
    const tab = results.flat()[0];
    if (tab) {
      // Build drafts URL on the same domain the user is already on
      const origin = new URL(tab.url).origin;
      chrome.tabs.update(tab.id, { url: `${origin}/mail/drafts`, active: true });
      chrome.windows.update(tab.windowId, { focused: true });
    } else {
      chrome.tabs.create({ url: draftsUrl });
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

// Periodic refresh — draft count + connection status
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
