# Plan: Seamless Email Verification → Extension Auto-Login

## Problem
After signup, user clicks the email verification link and lands on clarion-ai.app with no guidance. They must manually open the extension, navigate back to login, and re-enter credentials. This is confusing and lossy.

## Goal
Click verification link → extension automatically picks up the session → popup shows onboarding setup. No manual re-login.

---

## How It Works Today

1. User signs up via extension popup or web login page
2. Supabase sends confirmation email with a link like:
   `https://frbvdoszenrrlswegsxq.supabase.co/auth/v1/verify?type=signup&token=...&redirect_to=<default>`
3. User clicks link → Supabase confirms email → redirects to site root (no custom redirect configured)
4. User lands on clarion-ai.app homepage — dead end
5. `web-auth-sync.js` content script runs on `clarion-ai.app/*` pages but there's **no session in localStorage** because the redirect doesn't carry auth tokens
6. User must manually: open extension → click "back to login" → login with credentials → onboarding starts

## How It Will Work

1. User signs up (same as today)
2. Supabase sends confirmation email — but redirect URL now points to `https://clarion-ai.app/app/verified.html`
3. User clicks link → Supabase confirms email → redirects to `/app/verified.html#access_token=...&refresh_token=...`
4. `verified.html` page:
   - Extracts tokens from URL hash fragment (Supabase appends them automatically with implicit flow)
   - Calls `supabase.auth.setSession()` to establish a valid session in localStorage
   - Shows brief "Email verified!" confirmation message
   - Displays instruction: "Your Clarion extension is updating now — check the extension icon"
5. `web-auth-sync.js` (already running as content script on `clarion-ai.app/*`):
   - Polls localStorage every 5s — detects the new session
   - Syncs it to `chrome.storage.local.supabaseSession`
   - Sends `supabaseSessionChanged` message to background
6. Extension popup, next time opened, sees valid session → renders setup/onboarding view automatically

---

## Implementation Steps

### Step 1: Configure Supabase Redirect URL

**Where:** Supabase Dashboard → Auth → URL Configuration
**Action:** Add `https://clarion-ai.app/app/verified.html` to the list of allowed redirect URLs.

Also update the signup calls to pass this redirect URL:

**`extension/popup.js` (~line 802)** — update the signup REST call:
```js
const result = await authRequest("/signup", {
  email,
  password,
  options: {
    emailRedirectTo: "https://clarion-ai.app/app/verified.html",
  },
});
```

**`web/js/auth.js` (~line 59)** — update the `signUp` function:
```js
const { data, error } = await supabase.auth.signUp({
  email,
  password,
  options: {
    emailRedirectTo: "https://clarion-ai.app/app/verified.html",
  },
});
```

### Step 2: Create `web/app/verified.html`

New page that handles the email verification redirect. Minimal — just extracts the session and shows a confirmation message.

```
web/app/verified.html
```

- Uses the existing Supabase JS SDK (`supabase-client.js`) to call `supabase.auth.getSession()` after page load (the SDK auto-detects tokens in the URL hash)
- If session exists: show "Email verified! Your Clarion extension is updating — you can close this tab"
- If no session (e.g., token expired): show "Verification link may have expired. Please log in." with a link to `/app/login.html`
- Uses existing `app.css` for consistent styling

### Step 3: Create `web/js/pages/verified.js`

Logic for the verified page:

```js
import { supabase } from "../supabase-client.js";

// Supabase SDK automatically picks up tokens from URL hash on init
const { data: { session }, error } = await supabase.auth.getSession();

const msgEl = document.getElementById("verifiedMessage");
const errorEl = document.getElementById("verifiedError");

if (session) {
  // Session is now in localStorage — web-auth-sync.js will pick it up
  msgEl.style.display = "block";
} else {
  errorEl.style.display = "block";
}
```

### Step 4: Extension popup — auto-advance on session detection

**`extension/popup.js`** — The popup already calls `checkSessionAndRender()` on open, and the 5s interval re-checks during setup view. But if the user is still on the **login view** when the session syncs, nothing happens until they manually interact.

Add a `chrome.storage.onChanged` listener so the popup reacts immediately when `web-auth-sync.js` pushes a new session:

```js
// Near the bottom of popup.js, after checkSessionAndRender()
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.supabaseSession?.newValue) {
    // Session just appeared (likely from web-auth-sync after email verification)
    checkSessionAndRender();
  }
});
```

This makes the popup **instantly** react when the session arrives, rather than requiring the user to close/reopen it.

### Step 5: Supabase Dashboard Configuration

In the Supabase dashboard (manual step, not code):
- Go to **Auth → URL Configuration**
- Add `https://clarion-ai.app/app/verified.html` to **Redirect URLs**
- This allows Supabase to redirect to this URL after email confirmation

---

## Files Changed

| File | Change |
|------|--------|
| `web/app/verified.html` | **NEW** — verification landing page |
| `web/js/pages/verified.js` | **NEW** — session extraction logic |
| `web/js/auth.js` | Add `emailRedirectTo` option to `signUp()` |
| `extension/popup.js` | Add `chrome.storage.onChanged` listener for instant session detection; update signup REST call with redirect URL |

## Manual Steps (Not Code)

- Add `https://clarion-ai.app/app/verified.html` to Supabase Auth → Redirect URLs

---

## Edge Cases

- **Token expired before user clicks link:** `verified.js` detects no session, shows "link expired" message with login link
- **User already verified (clicks link twice):** Supabase returns error, page shows fallback message
- **Extension not installed:** Page still works, session lands in localStorage for web dashboard use; the "extension is updating" message just won't apply (could add extension-detection logic later, but not necessary for v1)
- **Phone OTP users:** No change — phone verify flow bypasses email entirely, already works correctly
- **Corporate email gateways mangling links:** Phone OTP fallback remains available as today

## What This Doesn't Change

- Phone OTP flow (untouched)
- Web dashboard login flow (untouched — still redirects to dashboard)
- `web-auth-sync.js` polling logic (unchanged — we're leveraging it as-is)
- Background service worker (unchanged)
- Onboarding pipeline (unchanged)
