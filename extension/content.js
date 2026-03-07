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
  for (const key of keys) {
    if (!key.includes("accesstoken") || !key.includes("outlook.office.com")) {
      continue;
    }
    try {
      const entry = JSON.parse(localStorage.getItem(key));
      if (entry.target && entry.target.toLowerCase().includes("mail.read")) {
        return {
          token: entry.secret,
          expiresOn: parseInt(entry.expiresOn, 10),
          cachedAt: parseInt(entry.cachedAt, 10),
          clientId: entry.clientId,
          origin: location.origin,
        };
      }
    } catch (_) {
      continue;
    }
  }
  return null;
}

/** Send the token (or null) to the service worker. */
function sendToken(tokenData) {
  try {
    chrome.runtime.sendMessage(
      { type: "token_update", data: tokenData },
      () => {
        // Ignore response / errors (extension context may have been invalidated)
        if (chrome.runtime.lastError) { /* noop */ }
      }
    );
  } catch (_) {
    // Extension context invalidated — stop polling
  }
}

/** One capture cycle: find token → send to background. */
function captureAndSend() {
  const tokenData = findExchangeToken();
  sendToken(tokenData);
}

// --- Initial capture on page load ---
captureAndSend();

// --- Poll every 60 s for refreshed tokens ---
setInterval(captureAndSend, TOKEN_POLL_INTERVAL_MS);
