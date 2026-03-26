/**
 * Content script — runs on Outlook pages.
 *
 * Primary token capture: reads MSAL Exchange token from localStorage,
 * polls every 60 s for refreshes, sends token to service worker.
 */

const TOKEN_POLL_INTERVAL_MS = 60_000;

/** Scan localStorage for the MSAL Exchange access token with Mail scopes. */
function findExchangeToken() {
  const keys = Object.keys(localStorage);
  // Collect candidates and pick the best match by priority:
  // 1. mail.read (work/org accounts)
  // 2. mbi_ssl  (personal accounts — OWA Exchange token)
  // 3. .default (fallback)
  let best = null;
  let bestPriority = Infinity;
  for (const key of keys) {
    const lk = key.toLowerCase();
    if (!lk.includes("accesstoken") || !lk.includes("outlook.office.com")) {
      continue;
    }
    try {
      const entry = JSON.parse(localStorage.getItem(key));
      const t = (entry.target || "").toLowerCase();
      let priority = -1;
      if (t.includes("mail.read")) priority = 0;
      else if (t.includes("mbi_ssl")) priority = 1;
      else if (t.includes(".default")) priority = 2;
      if (priority >= 0 && priority < bestPriority) {
        bestPriority = priority;
        best = {
          token: entry.secret,
          expiresOn: parseInt(entry.expiresOn, 10),
          cachedAt: parseInt(entry.cachedAt, 10),
          clientId: entry.clientId,
          origin: location.origin,
        };
        if (priority === 0) return best; // can't do better
      }
    } catch (_) {
      continue;
    }
  }
  return best;
}

/** Send the token (or null) to the service worker. */
function sendToken(tokenData) {
  try {
    chrome.runtime.sendMessage(
      { type: "token_update", data: tokenData },
      () => {
        if (chrome.runtime.lastError) { /* noop */ }
      }
    );
  } catch (_) {
    // Extension context invalidated — stop polling
  }
}

// --- Initial capture on page load ---
sendToken(findExchangeToken());

// --- Poll every 60 s for refreshed tokens ---
setInterval(() => sendToken(findExchangeToken()), TOKEN_POLL_INTERVAL_MS);
