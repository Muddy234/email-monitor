/** Popup script — auth flow + status dashboard. */

// ---------------------------------------------------------------------------
// Supabase config (duplicated here since popup can't use importScripts)
// ---------------------------------------------------------------------------

const SUPABASE_URL = "https://frbvdoszenrrlswegsxq.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_QopttEruBVdosoVJGy4j2A__5CFfx8W";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function dot(color) {
  return `<span class="dot ${color}"></span>`;
}

function relativeTime(ts) {
  if (!ts) return "—";
  const diff = Date.now() - new Date(ts).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  return `${Math.floor(diff / 3_600_000)}h ago`;
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

function sessionFromResponse(data) {
  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + (data.expires_in || 3600),
    user: { id: data.user?.id, email: data.user?.email },
  };
}

// ---------------------------------------------------------------------------
// View toggling
// ---------------------------------------------------------------------------

async function checkSessionAndRender() {
  const result = await chrome.storage.local.get("supabaseSession");
  const session = result.supabaseSession;

  if (session && session.access_token) {
    showStatusView(session);
  } else {
    showLoginView();
  }
}

function showLoginView() {
  document.getElementById("loginView").style.display = "block";
  document.getElementById("statusView").style.display = "none";
}

function showStatusView(session) {
  document.getElementById("loginView").style.display = "none";
  document.getElementById("statusView").style.display = "block";

  // User email
  document.getElementById("userEmail").textContent = session.user?.email || "—";

  // Supabase auth status
  const now = Math.floor(Date.now() / 1000);
  const authEl = document.getElementById("supabaseAuth");
  if (session.expires_at > now) {
    authEl.innerHTML = `${dot("green")}Authenticated`;
  } else {
    authEl.innerHTML = `${dot("yellow")}Token expired`;
  }

  // Refresh the rest of the dashboard
  refreshStatus();
}

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "getStatus" }, (status) => {
    if (chrome.runtime.lastError || !status) return;

    // Outlook token
    const tokenEl = document.getElementById("tokenStatus");
    const expiresEl = document.getElementById("tokenExpires");
    const originEl = document.getElementById("tokenOrigin");

    if (!status.has_token) {
      tokenEl.innerHTML = `${dot("red")}Missing`;
      expiresEl.textContent = "—";
    } else if (status.token_expired) {
      tokenEl.innerHTML = `${dot("yellow")}Expired`;
      expiresEl.textContent = status.token_expires || "—";
    } else {
      tokenEl.innerHTML = `${dot("green")}Valid`;
      if (status.token_expires) {
        const exp = new Date(status.token_expires);
        const hours = Math.max(0, Math.round((exp - Date.now()) / 3_600_000));
        expiresEl.textContent = `~${hours}h remaining`;
      }
    }

    originEl.textContent = status.token_origin
      ? new URL(status.token_origin).hostname
      : "—";

    // Last sync
    const syncEl = document.getElementById("lastSync");
    syncEl.textContent = status.last_sync
      ? relativeTime(status.last_sync)
      : "never";

    // Last command
    const cmdEl = document.getElementById("lastCmd");
    if (status.last_command) {
      cmdEl.textContent = `${status.last_command.action} — ${relativeTime(status.last_command.timestamp)}`;
    } else {
      cmdEl.textContent = "none";
    }
  });
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

let isSignUpMode = false;

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

    // Notify background to initialize Supabase features
    chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });

    showStatusView(session);
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

document.getElementById("logoutBtn").addEventListener("click", async () => {
  await chrome.storage.local.remove("supabaseSession");
  chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });
  showLoginView();
});

document.getElementById("syncNowBtn").addEventListener("click", () => {
  const btn = document.getElementById("syncNowBtn");
  btn.disabled = true;
  btn.textContent = "Syncing...";
  chrome.runtime.sendMessage({ type: "syncNow" }, (resp) => {
    btn.disabled = false;
    btn.textContent = "Sync Now";
    if (resp && resp.error) {
      showError(resp.error);
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
setInterval(refreshStatus, 5000);
