/** Popup script — queries background for status and renders it. */

function dot(color) {
  return `<span class="dot ${color}"></span>`;
}

function relativeTime(ts) {
  if (!ts) return "—";
  const diff = Date.now() - ts;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  return `${Math.floor(diff / 3_600_000)}h ago`;
}

function refresh() {
  chrome.runtime.sendMessage({ type: "getStatus" }, (status) => {
    if (chrome.runtime.lastError || !status) {
      document.getElementById("tokenStatus").innerHTML = `${dot("gray")}Unavailable`;
      return;
    }

    // Token
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

    // WebSocket
    const wsEl = document.getElementById("wsStatus");
    const backoffEl = document.getElementById("wsBackoff");

    if (status.ws_connected) {
      wsEl.innerHTML = `${dot("green")}Connected`;
    } else {
      wsEl.innerHTML = `${dot("red")}Disconnected`;
    }

    backoffEl.textContent = status.backoff_ms > 0
      ? `${Math.round(status.backoff_ms / 1000)}s`
      : "none";

    // Last command
    const cmdEl = document.getElementById("lastCmd");
    if (status.last_command) {
      cmdEl.textContent = `${status.last_command.action} — ${relativeTime(status.last_command.timestamp)}`;
    } else {
      cmdEl.textContent = "none";
    }
  });
}

// Refresh on open and every 5 s while popup is visible
refresh();
setInterval(refresh, 5000);
