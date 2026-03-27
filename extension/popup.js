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

// Onboarding status ordering — maps worker pipeline phases to linear progression
const ONBOARDING_ORDER = [
  "starting", "collecting", "statistics", "persisting",
  "extracting", "synthesizing", "style_guide", "training",
  "complete", "complete_partial",
];

function onboardingAtLeast(current, threshold) {
  if (!current) return false;
  const ci = ONBOARDING_ORDER.indexOf(current);
  const ti = ONBOARDING_ORDER.indexOf(threshold);
  if (ci === -1 || ti === -1) return false;
  return ci >= ti;
}

async function fetchOnboardingStatus(session) {
  const uid = session.user?.id;
  if (!uid) return null;
  try {
    const resp = await fetch(
      `${SUPABASE_URL}/rest/v1/profiles?id=eq.${uid}&select=onboarding_status`,
      {
        headers: {
          apikey: SUPABASE_ANON_KEY,
          Authorization: `Bearer ${session.access_token}`,
        },
      }
    );
    const rows = resp.ok ? await resp.json() : [];
    return rows?.[0]?.onboarding_status || null;
  } catch (_) {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Token refresh — popup can't access background's getValidAccessToken()
// ---------------------------------------------------------------------------

async function refreshSupabaseSession(session) {
  if (!session?.refresh_token) return null;
  try {
    const resp = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=refresh_token`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        apikey: SUPABASE_ANON_KEY,
      },
      body: JSON.stringify({ refresh_token: session.refresh_token }),
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    const refreshed = {
      access_token: data.access_token,
      refresh_token: data.refresh_token,
      expires_at: Math.floor(Date.now() / 1000) + (data.expires_in || 3600),
      user: {
        id: data.user?.id,
        email: data.user?.email,
        name: data.user?.user_metadata?.full_name || data.user?.user_metadata?.name || session.user?.name || "",
      },
    };
    await chrome.storage.local.set({ supabaseSession: refreshed });
    return refreshed;
  } catch (_) {
    return null;
  }
}

/**
 * Get a valid session, refreshing the token if within 5 min of expiry.
 * Returns null if no session or refresh fails.
 */
async function getValidSession() {
  const result = await chrome.storage.local.get("supabaseSession");
  const session = result.supabaseSession;
  if (!session?.access_token) return null;

  const now = Math.floor(Date.now() / 1000);
  if (session.expires_at - now > 300) return session;

  // Token expired or expiring soon — refresh
  const refreshed = await refreshSupabaseSession(session);
  return refreshed || session; // fall back to old session if refresh fails
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
    last_login_at: Math.floor(Date.now() / 1000),
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
  if (!uid) return { drafts: 0, notable: 0, processed: 0 };

  try {
    // Fetch drafts, notable signals, and weekly stats in parallel
    const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString();

    const [emails, events, runs] = await Promise.all([
      supabaseQuery(`emails?select=id,status,classifications(needs_response),drafts(id,status)&user_id=eq.${uid}&status=not.in.(completed,dismissed)&drafts.status=neq.deleted&order=received_time.desc`, session),
      supabaseQuery(`response_events?select=email_id,pri,mc,sender_tier,rt&user_id=eq.${uid}`, session),
      supabaseQuery(`pipeline_runs?select=emails_processed,drafts_generated&user_id=eq.${uid}&started_at=gte.${weekAgo}`, session),
    ]);

    // Index response events by email_id
    const evMap = {};
    for (const ev of events) evMap[ev.email_id] = ev;

    // Count drafts and notable using same logic as web dashboard
    let draftCount = 0;
    let notableCount = 0;
    for (const email of emails) {
      const hasDraft = email.drafts && email.drafts.length > 0;
      if (hasDraft) {
        draftCount++;
        continue;
      }
      const cls = email.classifications?.[0];
      if (!cls || cls.needs_response) continue;
      const ev = evMap[email.id];
      if (!ev) continue;
      if (ev.pri === "high" || ev.pri === "med" || ev.mc === true ||
          ev.sender_tier === "C" || ev.sender_tier === "I" || ev.rt !== "none") {
        notableCount++;
      }
    }

    // Aggregate weekly stats
    let processed = 0;
    for (const run of runs) {
      processed += run.emails_processed || 0;
    }

    return { drafts: draftCount, notable: notableCount, processed };
  } catch (_) {
    return { drafts: 0, notable: 0, processed: 0 };
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
  const session = await getValidSession();
  const state = await getState();

  if (!session || !session.access_token) {
    // Check for pending signup (phone verify in progress)
    const hasPending = await restorePendingSignup();
    if (hasPending) {
      showView("loginView");
      showPhoneVerifyFlow();
      // If code was already sent, jump to OTP input
      if (pendingPhone) {
        document.getElementById("phoneVerifyToggle").style.display = "none";
        document.getElementById("phoneInputGroup").style.display = "none";
        document.getElementById("codeInputGroup").style.display = "";
        document.getElementById("verifyHint").textContent = "Enter the code sent to your phone";
      }
      return;
    }
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
    // Check onboarding_status to determine correct view
    const obStatus = await fetchOnboardingStatus(session);
    if (obStatus === "complete" || obStatus === "complete_partial") {
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

async function updateSetupChecklist(session) {
  const setCheck = (id, state) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (state === "done") {
      el.textContent = "\u2705";
      el.className = "check-icon check-done";
    } else if (state === "active") {
      el.textContent = "\u2B58";
      el.className = "check-icon check-active";
    } else {
      el.textContent = "\u2B58";
      el.className = "check-icon check-pending";
    }
  };

  // Auth is always done
  setCheck("checkAuth", "done");

  // Check Outlook token via background
  const status = await new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "getStatus" }, (s) => {
      if (chrome.runtime.lastError || !s) resolve(null);
      else resolve(s);
    });
  });

  const hasToken = status && status.has_token && !status.token_expired;
  setCheck("checkOutlook", hasToken ? "done" : "pending");

  // Fetch onboarding_status from profiles
  const obStatus = await fetchOnboardingStatus(session);

  // Handle failed state
  const errorEl = document.getElementById("setupError");
  if (obStatus === "failed") {
    errorEl.textContent = "Something went wrong during setup. Please try syncing again.";
    errorEl.style.display = "block";
    setCheck("checkSyncing", "pending");
    setCheck("checkBehavior", "pending");
    setCheck("checkStyle", "pending");
    setCheck("checkComplete", "pending");
    document.getElementById("setupSyncBtn").style.display = "";
    return;
  } else if (errorEl) {
    errorEl.style.display = "none";
  }

  // Determine step states based on onboarding_status
  const syncingDone = onboardingAtLeast(obStatus, "statistics");
  const behaviorDone = onboardingAtLeast(obStatus, "extracting");
  const styleDone = onboardingAtLeast(obStatus, "training");
  const allDone = obStatus === "complete" || obStatus === "complete_partial";

  // Syncing emails — time-based progress (4 cycles × 45s = 180s)
  const SYNC_DURATION_MS = 180_000;
  const syncProgress = document.getElementById("syncProgress");
  if (syncingDone) {
    setCheck("checkSyncing", "done");
    if (syncProgress) syncProgress.textContent = "";
    chrome.storage.local.remove("syncStartedAt");
  } else if (hasToken && (obStatus || status?.last_sync)) {
    setCheck("checkSyncing", "active");
    const stored = await new Promise((r) =>
      chrome.storage.local.get("syncStartedAt", (d) => r(d.syncStartedAt))
    );
    if (!stored) {
      chrome.storage.local.set({ syncStartedAt: Date.now() });
    }
    const startedAt = stored || Date.now();
    const elapsed = Date.now() - startedAt;
    const pct = Math.min(99, Math.round((elapsed / SYNC_DURATION_MS) * 100));
    if (syncProgress) syncProgress.textContent = `(${pct}%)`;
  } else {
    setCheck("checkSyncing", "pending");
    if (syncProgress) syncProgress.textContent = "";
  }

  // Learning your email behavior
  if (behaviorDone) {
    setCheck("checkBehavior", "done");
  } else if (syncingDone) {
    setCheck("checkBehavior", "active");
  } else {
    setCheck("checkBehavior", "pending");
  }

  // Learning your writing style
  if (styleDone) {
    setCheck("checkStyle", "done");
  } else if (behaviorDone) {
    setCheck("checkStyle", "active");
  } else {
    setCheck("checkStyle", "pending");
  }

  // Onboarding complete
  if (allDone) {
    setCheck("checkComplete", "done");
  } else if (styleDone) {
    setCheck("checkComplete", "active");
  } else {
    setCheck("checkComplete", "pending");
  }

  // Hide Sync Now button once onboarding has started
  const syncBtn = document.getElementById("setupSyncBtn");
  if (obStatus) {
    syncBtn.style.display = "none";
  } else {
    syncBtn.style.display = "";
  }

  // Transition to dashboard when complete (brief delay so user sees the finished checklist)
  if (allDone) {
    setTimeout(async () => {
      await setState("complete");
      const session = await getValidSession();
      if (session) {
        showStatusView(session);
      }
    }, 2000);
  }
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

  // Check for Outlook account mismatch
  chrome.storage.local.get("outlookMismatch", (result) => {
    const banner = document.getElementById("outlookMismatchBanner");
    if (result.outlookMismatch) {
      const expected = result.outlookMismatch.expected;
      banner.textContent = `This extension is linked to ${expected}. Please sign in with that Outlook account.`;
      banner.style.display = "block";
    } else {
      banner.style.display = "none";
    }
  });

  // Show connected Outlook email + pipeline stage from one query
  try {
    const uid = session.user?.id;
    if (uid) {
      const resp = await fetch(
        `${SUPABASE_URL}/rest/v1/profiles?id=eq.${uid}&select=connected_outlook_email,pipeline_stage`,
        {
          headers: {
            apikey: SUPABASE_ANON_KEY,
            Authorization: `Bearer ${session.access_token}`,
          },
        }
      );
      if (resp.ok) {
        const rows = await resp.json();
        const connectedEl = document.getElementById("connectedOutlookEmail");
        const outlookAddr = rows?.[0]?.connected_outlook_email;
        if (outlookAddr && connectedEl) {
          connectedEl.textContent = `Outlook: ${outlookAddr}`;
          connectedEl.style.display = "block";
        }
        renderPipelineBar(rows?.[0]?.pipeline_stage || "idle");
      }
    }
  } catch (_) {}

  // Fetch counts and render headline
  const counts = await fetchCounts(session);
  renderHeadline(counts);
  renderQuickStats(counts);
  renderDeepLinks(counts);
}

// ---------------------------------------------------------------------------
// Pipeline progress bar
// ---------------------------------------------------------------------------

let _lastPipelineStage = null;

function renderPipelineBar(stage) {
  const bar = document.getElementById("pipelineBar");
  if (!bar) return;

  const isIdle = !stage || stage === "idle";
  const effective = isIdle ? "gathering" : stage;

  // Skip re-render if stage unchanged — prevents animation restarts
  if (effective === _lastPipelineStage) return;
  _lastPipelineStage = effective;

  const phases = ["gathering", "analyzing", "drafting"];
  const fills = {
    gathering: document.getElementById("phaseGathering"),
    analyzing: document.getElementById("phaseAnalyzing"),
    drafting: document.getElementById("phaseDrafting"),
  };
  const labels = {
    gathering: document.getElementById("labelGathering"),
    analyzing: document.getElementById("labelAnalyzing"),
    drafting: document.getElementById("labelDrafting"),
  };

  bar.style.display = "block";
  const activeIdx = phases.indexOf(effective);

  for (let i = 0; i < phases.length; i++) {
    const fill = fills[phases[i]];
    const label = labels[phases[i]];
    if (!fill || !label) continue;

    fill.className = "pipeline-phase-fill";
    fill.style.animationDelay = "";
    label.className = "pipeline-label";

    if (i < activeIdx) {
      fill.classList.add("filled");
      label.classList.add("done");
    } else if (i === activeIdx) {
      if (isIdle && i === 0) {
        // Sync to wall clock so position persists across popup opens
        fill.style.animationDelay = `-${Date.now() % 45000}ms`;
        fill.classList.add("waiting");
      } else {
        // Analyzing/drafting — pulse
        fill.classList.add("active-pulse");
      }
      label.classList.add("active");
    }
  }
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
    ctaEl.textContent = "";
    card.onclick = () => openDashboardTab("/app/dashboard.html");
  }
}

function renderQuickStats(counts) {
  const draftsEl = document.getElementById("statDrafts");
  const notableEl = document.getElementById("statNotable");
  removeSkeleton("statDrafts");
  removeSkeleton("statNotable");
  draftsEl.textContent = counts.drafts;
  notableEl.textContent = counts.notable;
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
let pendingUserId = null;
let pendingPhone = null;
let pendingEmail = null;
let pendingPassword = null;

// Persist pending signup state so phone verify survives popup close/reopen
async function savePendingSignup() {
  await chrome.storage.local.set({
    pendingSignup: {
      userId: pendingUserId,
      phone: pendingPhone,
      email: pendingEmail,
      password: pendingPassword,
    },
  });
}

async function clearPendingSignup() {
  pendingUserId = null;
  pendingPhone = null;
  pendingEmail = null;
  pendingPassword = null;
  await chrome.storage.local.remove("pendingSignup");
}

async function restorePendingSignup() {
  const result = await chrome.storage.local.get("pendingSignup");
  const ps = result.pendingSignup;
  if (!ps?.userId) return false;
  pendingUserId = ps.userId;
  pendingPhone = ps.phone || null;
  pendingEmail = ps.email || null;
  pendingPassword = ps.password || null;
  return true;
}

/**
 * Format a raw phone string to E.164.
 * Strips non-digits, prepends +1 for 10-digit US numbers.
 * NOTE: Hardcodes US country code. Add country selector for international users later.
 */
function formatPhoneE164(raw) {
  const digits = raw.replace(/\D/g, "");
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith("1")) return `+${digits}`;
  if (raw.startsWith("+")) return raw.replace(/[^\d+]/g, "");
  return `+${digits}`;
}

async function callEdgeFunction(name, body) {
  const resp = await fetch(`${SUPABASE_URL}/functions/v1/${name}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      apikey: SUPABASE_ANON_KEY,
    },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

function showPhoneVerifyFlow() {
  document.getElementById("phoneVerifySection").style.display = "";
  document.getElementById("loginBtn").style.display = "none";
  document.getElementById("toggleAuth").style.display = "none";
  document.getElementById("nameGroup").style.display = "none";
  document.getElementById("authEmail").parentElement.style.display = "none";
  document.getElementById("authPassword").parentElement.style.display = "none";
  hideError();
}

function resetPhoneVerifyState() {
  pendingUserId = null;
  pendingPhone = null;
  document.getElementById("phoneVerifySection").style.display = "none";
  document.getElementById("phoneInputGroup").style.display = "none";
  document.getElementById("codeInputGroup").style.display = "none";
  document.getElementById("phoneVerifyToggle").style.display = "";
  document.getElementById("loginBtn").style.display = "";
  document.getElementById("toggleAuth").style.display = "";
  document.getElementById("nameGroup").style.display = isSignUpMode ? "" : "none";
  document.getElementById("authEmail").parentElement.style.display = "";
  document.getElementById("authPassword").parentElement.style.display = "";
}

// Welcome → Get Started
document.getElementById("getStartedBtn").addEventListener("click", async () => {
  await setState("login");
  showView("loginView");
});

// Phone verify → Back to login
document.getElementById("backToLogin").addEventListener("click", async () => {
  await clearPendingSignup();
  resetPhoneVerifyState();
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
      const displayName = document.getElementById("authName").value.trim();
      if (!displayName) {
        showError("Please enter the name you'd like used in email sign-offs");
        btn.disabled = false;
        btn.textContent = "Sign Up";
        return;
      }
      const result = await authRequest("/signup", { email, password });
      const signupUserId = result.user?.id || result.id || null;
      // Write display_name to the profile row created by the DB trigger
      if (signupUserId && displayName) {
        try {
          const token = result.access_token || SUPABASE_ANON_KEY;
          await fetch(`${SUPABASE_URL}/rest/v1/profiles?id=eq.${signupUserId}`, {
            method: "PATCH",
            headers: {
              "Content-Type": "application/json",
              apikey: SUPABASE_ANON_KEY,
              Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({ display_name: displayName }),
          });
        } catch (_) {}
      }
      if (result.access_token) {
        session = sessionFromResponse(result);
        await clearPendingSignup();
      } else {
        // No session = email confirmation pending.
        // Persist state so phone verify survives popup close/reopen.
        pendingUserId = signupUserId;
        pendingEmail = email;
        pendingPassword = password;
        await savePendingSignup();
        btn.disabled = false;
        btn.textContent = "Sign Up";
        showPhoneVerifyFlow();
        return;
      }
    } else {
      const result = await authRequest("/token?grant_type=password", { email, password });
      session = sessionFromResponse(result);
    }

    await clearPendingSignup();
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
  document.getElementById("nameGroup").style.display = isSignUpMode ? "" : "none";
  resetPhoneVerifyState();
  hideError();
});

// --- Phone OTP verify handlers ---

// "Verify by phone" toggle
document.getElementById("phoneVerifyToggle").addEventListener("click", () => {
  document.getElementById("phoneVerifyToggle").style.display = "none";
  document.getElementById("phoneInputGroup").style.display = "";
  document.getElementById("verifyHint").textContent = "Enter your phone number to verify your account";
});

// Send code
document.getElementById("sendCodeBtn").addEventListener("click", async () => {
  hideError();
  const raw = document.getElementById("phoneInput").value.trim();
  if (!raw) {
    showError("Please enter your phone number");
    return;
  }

  if (!pendingUserId) {
    showError("No pending signup found. Please sign up again.");
    return;
  }

  const phone = formatPhoneE164(raw);
  const btn = document.getElementById("sendCodeBtn");
  btn.disabled = true;
  btn.textContent = "Sending...";

  try {
    await callEdgeFunction("phone-verify-start", { userId: pendingUserId, phone });
    pendingPhone = phone;
    await savePendingSignup();
    document.getElementById("phoneInputGroup").style.display = "none";
    document.getElementById("codeInputGroup").style.display = "";
    document.getElementById("verifyHint").textContent = "Enter the code sent to your phone";
  } catch (err) {
    showError(err.message || "Failed to send verification code");
  } finally {
    btn.disabled = false;
    btn.textContent = "Send Code";
  }
});

// Verify code
document.getElementById("verifyCodeBtn").addEventListener("click", async () => {
  hideError();
  const code = document.getElementById("otpInput").value.trim();
  if (!code) {
    showError("Please enter the verification code");
    return;
  }

  if (!pendingUserId || !pendingPhone) {
    showError("No pending verification. Please start over.");
    return;
  }

  const btn = document.getElementById("verifyCodeBtn");
  btn.disabled = true;
  btn.textContent = "Verifying...";

  try {
    const data = await callEdgeFunction("phone-verify-confirm", {
      userId: pendingUserId,
      phone: pendingPhone,
      code,
    });

    if (data.access_token) {
      const session = sessionFromResponse(data);
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

      await clearPendingSignup();
      chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });
      await setState("setup");
      showSetupView(session);
    } else if (data.confirmed) {
      // Account confirmed but no auto-session — sign in with stored credentials
      const email = pendingEmail || document.getElementById("authEmail").value.trim();
      const password = pendingPassword || document.getElementById("authPassword").value;
      if (email && password) {
        const loginResult = await authRequest("/token?grant_type=password", { email, password });
        const session = sessionFromResponse(loginResult);
        await chrome.storage.local.set({ supabaseSession: session });
        await setWorkerActive(session.access_token, session.user.id, true);
        await clearPendingSignup();
        chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });
        await setState("setup");
        showSetupView(session);
      } else {
        showError("Account confirmed. Please log in with your email and password.");
        await clearPendingSignup();
        resetPhoneVerifyState();
      }
    } else {
      showError("Verification succeeded but no session returned. Please try logging in.");
    }
  } catch (err) {
    showError(err.message || "Verification failed");
    btn.disabled = false;
    btn.textContent = "Verify";
  }
});

// Enter key handlers for phone verify inputs
document.getElementById("phoneInput")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("sendCodeBtn").click();
});
document.getElementById("otpInput")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("verifyCodeBtn").click();
});

// Logout — shared handler + two-step confirmation for each container
async function performLogout() {
  const result = await chrome.storage.local.get("supabaseSession");
  const session = result.supabaseSession;
  if (session?.access_token && session?.user?.id) {
    setWorkerActive(session.access_token, session.user.id, false).catch(() => {});
  }
  await chrome.storage.local.remove("supabaseSession");
  await chrome.storage.local.remove("lastSyncTime");
  await chrome.storage.session.remove("exchangeToken");
  await clearPendingSignup();
  await setState("login");
  chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });
  showView("loginView");
}

function initLogoutContainer(containerId, btnId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = `<button class="btn btn-sm btn-outline" id="${btnId}">Logout</button>`;
  document.getElementById(btnId).addEventListener("click", () => showLogoutConfirm(containerId, btnId));
}

function showLogoutConfirm(containerId, btnId) {
  const container = document.getElementById(containerId);
  container.innerHTML = `<span class="logout-confirm">
    <span>Sure?</span>
    <button class="btn btn-sm btn-danger" id="${btnId}Yes">Yes</button>
    <button class="btn btn-sm btn-outline" id="${btnId}Cancel">No</button>
  </span>`;
  document.getElementById(`${btnId}Yes`).addEventListener("click", performLogout);
  document.getElementById(`${btnId}Cancel`).addEventListener("click", () => initLogoutContainer(containerId, btnId));
}

// Status view logout
function initLogoutBtn() { initLogoutContainer("logoutContainer", "logoutBtn"); }
document.getElementById("logoutBtn").addEventListener("click", () => showLogoutConfirm("logoutContainer", "logoutBtn"));

// Setup view logout
initLogoutContainer("setupLogoutContainer", "setupLogoutBtn");

// Sync (setup view only)
function friendlySyncError(raw) {
  if (!raw) return null;
  if (raw === "OUTLOOK_MISMATCH") return "Wrong Outlook account — see details above";
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
      getValidSession().then((session) => {
        if (session) updateSetupChecklist(session);
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

// Dashboard refresh (15s)
setInterval(async () => {
  if (document.getElementById("statusView").style.display !== "none") {
    const session = await getValidSession();
    if (session) refreshStatus(session);
  }
}, 15000);

// Setup view refresh (5s — faster polling during onboarding)
setInterval(async () => {
  if (document.getElementById("setupView").style.display !== "none") {
    const session = await getValidSession();
    if (session) updateSetupChecklist(session);
  }
}, 5000);
