# Phone OTP Fallback Verification

## Problem
Corporate email gateways (Mimecast, Proofpoint, etc.) block verification emails from new/unknown domains. Users behind these gateways can't complete signup. Email from `notifications.clarion-ai.app` via Resend is correctly authenticated (DKIM/SPF/DMARC), but reputation-based blocking persists.

## Solution
Add phone OTP as a fallback verification method. After signup, if the user doesn't receive the email, they can enter their phone number and verify via SMS instead. Uses Supabase Edge Functions with the service role key to verify unverified users who don't yet have a session.

## Architecture

**Why Edge Functions?** After `signUp()`, the user has no session (email not yet confirmed), so client-side `updateUser()` won't work. Edge Functions use `SUPABASE_SERVICE_ROLE_KEY` to operate on unverified users.

**Simplified approach:** Use `supabase.auth.signInWithOtp({ phone })` and `supabase.auth.verifyOtp({ phone, token, type: 'sms' })` on a service-role Supabase client inside the Edge Functions. This leverages Supabase's built-in OTP send, expiry, and rate limiting rather than manually calling REST endpoints. The `phone_verify_attempts` table is no longer needed for rate limiting — Supabase enforces `auth.rate_limits.otp.period` natively (default: 60s between sends).

We still need the `phone-verify-start` edge function for userId validation (unconfirmed check, phone not claimed) and the `phone-verify-confirm` function for the email_confirm side-door + phone cleanup. But the OTP lifecycle itself is handled by Supabase.

**Flow:**
1. User signs up → gets `user.id` back (no session)
2. UI shows "Check your email" + "Didn't receive it? Verify by phone instead"
3. User clicks fallback → enters phone number
4. `phone-verify-start` edge function: validates userId is unconfirmed, stores phone on user, calls `signInWithOtp({ phone })` to trigger SMS
5. User enters 6-digit code
6. `phone-verify-confirm` edge function: calls `verifyOtp({ phone, token, type: 'sms' })`, confirms email, clears phone, returns session
7. User is logged in

**Mid-flow drop-off recovery:** If the user closes the tab/popup after signup but before phone verification, their account exists in an unconfirmed state. Re-signing up with the same email would fail. To handle this:
- On signup, if Supabase returns a user with `identities: []` (existing unconfirmed account), detect this and re-surface the phone verify flow with that user's ID
- The UI shows the same "Didn't receive the email? Verify by phone" prompt
- Edge function validates the userId is still unconfirmed before proceeding
- This avoids a "email already taken" dead end

## Manual Setup (Before Code)
- **Supabase Dashboard → Authentication → Providers → Phone**: Enable, select Twilio, add Account SID + Auth Token + Messaging Service SID
- **Twilio Console**: Get a phone number or set up Messaging Service

## Security

### Rate Limiting
Supabase's built-in phone auth rate limiting handles the OTP send cadence (`auth.rate_limits.otp.period`, default 60s between sends per phone). This prevents rapid-fire SMS abuse without a custom table.

For additional defense (optional, can add later if abuse is observed):
- The `phone-verify-start` edge function can add a simple in-function check: query `auth.users` to count recent phone updates for this userId as a secondary throttle
- Twilio also has its own rate limiting and fraud detection that can be configured in the Twilio console

### userId Validation
The userId is passed client-side through the entire flow, meaning anyone who knows/guesses a userId could attach their phone to someone else's account.

**Checks in `phone-verify-start`:**
1. Target userId must exist
2. Target userId must be in an **unconfirmed** state (`email_confirmed_at IS NULL`)
3. The phone number must not already belong to a different confirmed user

### email_confirm Side-Door (Document Clearly)
The confirm function uses phone OTP success to also set `email_confirm: true` on the user. This is intentional — phone verification serves as an alternative identity proof, allowing users whose email verification was blocked by a corporate gateway to complete signup. **This must be clearly commented in code** so it doesn't look like a bug later.

### Phone Number Cleanup After Verification
The phone number is written to the auth user record via `admin.updateUserById()` to enable OTP send/verify. Since phone is purely a verification mechanism (not used for notifications or account recovery), `phone-verify-confirm` clears it after successful verification:
```
admin.updateUserById(userId, { email_confirm: true, phone: "" })
```
This minimizes stored PII. If phone-based features (notifications, recovery) are added later, this behavior would change.

## Files to Create

### 1. `supabase/functions/phone-verify-start/index.ts`
- Accepts `{ userId, phone }` (phone in E.164 format)
- Creates Supabase client with `SUPABASE_SERVICE_ROLE_KEY`
- **Validation**:
  - `admin.getUserById(userId)` — must exist and have `email_confirmed_at === null`
  - Phone not already on a different confirmed user
- Stores phone on user: `admin.updateUserById(userId, { phone })`
- Sends OTP: `supabase.auth.signInWithOtp({ phone })` — Supabase handles OTP generation, SMS delivery via Twilio, rate limiting, and expiry
- Returns `{ success: true }` or error with descriptive message
- Auth: requires `apikey` header (anon key) — no JWT needed since user has no session

### 2. `supabase/functions/phone-verify-confirm/index.ts`
- Accepts `{ userId, phone, code }`
- Creates Supabase client with `SUPABASE_SERVICE_ROLE_KEY`
- Verifies OTP: `supabase.auth.verifyOtp({ phone, token: code, type: 'sms' })`
- On success:
  - Confirms email + clears phone: `admin.updateUserById(userId, { email_confirm: true, phone: "" })`
  - **Comment clearly**: Phone OTP success side-doors email confirmation. This is intentional — it serves as alternative identity verification for users whose email confirmation was blocked by corporate gateways. Phone is cleared after verification to minimize PII storage.
- Returns the session from `verifyOtp` response: `{ access_token, refresh_token, user }`
- Validation: code is 6 digits, phone matches E.164

## Files to Modify

### 3. `web/app/login.html`
Add phone OTP section (hidden by default) after the `loginBtn`, before `toggleAuth`:
```html
<div id="phoneVerifySection" style="display: none;">
    <p class="em-form-info">Check your email to confirm your account</p>
    <button class="em-login-toggle" id="phoneVerifyToggle">
        Didn't receive the email? Verify by phone instead
    </button>
    <div id="phoneInputGroup" style="display: none;">
        <div class="em-form-group">
            <label class="em-form-label" for="phoneInput">Phone number</label>
            <input class="em-form-input" type="tel" id="phoneInput" placeholder="+1 (555) 123-4567">
        </div>
        <button class="em-btn em-btn-primary" id="sendCodeBtn" style="width: 100%; justify-content: center;">Send Code</button>
    </div>
    <div id="codeInputGroup" style="display: none;">
        <div class="em-form-group">
            <label class="em-form-label" for="otpInput">Verification code</label>
            <input class="em-form-input" type="text" id="otpInput" placeholder="123456" maxlength="6" inputmode="numeric">
        </div>
        <button class="em-btn em-btn-primary" id="verifyCodeBtn" style="width: 100%; justify-content: center;">Verify</button>
    </div>
</div>
```

### 4. `web/js/pages/login.js`
- After `signUp()` returns without a session: store `pendingUserId = result.user.id`, show `phoneVerifySection`, hide the form fields
- **Drop-off recovery**: If `signUp()` returns `user.identities === []` (existing unconfirmed account), treat the same as a fresh signup — extract the user ID from the response and show the phone verify flow. Supabase re-sends the confirmation email in this case, so the "check your email" message is still valid.
- `sendCodeBtn` click: format phone to E.164, call `phone-verify-start` edge function, show `codeInputGroup`
- `verifyCodeBtn` click: call `phone-verify-confirm` edge function, use returned session to redirect to dashboard
- Helper: `formatPhoneE164(raw)` — strip non-digits, prepend `+1` if 10 digits
  - **Note**: Hardcodes US country code. Fine for US-only launch. Add country code selector if international users are needed later.

### 5. `extension/popup.html`
Add phone OTP elements inside `#loginView`, after `#authError`:
```html
<div id="phoneVerifySection" style="display: none;">
    <div style="font-size: 12px; color: #78716C; margin-bottom: 8px;">Check your email to confirm your account</div>
    <div class="auth-link" id="phoneVerifyToggle">Didn't receive the email? Verify by phone</div>
    <div id="phoneInputGroup" style="display: none; margin-top: 8px;">
        <div class="form-group">
            <label for="phoneInput">Phone number</label>
            <input type="tel" id="phoneInput" placeholder="+1 (555) 123-4567">
        </div>
        <button class="btn btn-primary" id="sendCodeBtn">Send Code</button>
    </div>
    <div id="codeInputGroup" style="display: none; margin-top: 8px;">
        <div class="form-group">
            <label for="otpInput">Verification code</label>
            <input type="text" id="otpInput" placeholder="123456" maxlength="6" inputmode="numeric">
        </div>
        <button class="btn btn-primary" id="verifyCodeBtn">Verify</button>
    </div>
</div>
```
**Testing note**: At 320px wide, verify that error states (rate limit exceeded, invalid code) don't push the verify button below the fold. The phone input + send + OTP input + verify + error message stack can get tight.

### 6. `extension/popup.js`
- After signup returns without `access_token` (~line 449): store `pendingUserId = result.id`, show `phoneVerifySection`, hide login form fields
- **Drop-off recovery**: If signup response has `identities: []` (or no `id` at top level), the user existed but is unconfirmed. Call a lookup or re-attempt signup to get the userId, then show phone verify flow.
- Wire up `sendCodeBtn` and `verifyCodeBtn` with fetch calls to edge functions using `SUPABASE_URL + "/functions/v1/phone-verify-start"` etc.
- On successful verify: create session from response, save to `chrome.storage.local`, proceed to setup view
- Uses existing `SUPABASE_URL` and `SUPABASE_ANON_KEY` from supabase-config.js

## Tech Debt: Shared Phone Verify Logic
The edge function call logic and UI state machine (send → wait for code → verify → session) is identical between `login.js` and `popup.js`. Currently implemented twice because the extension uses raw REST calls (no Supabase JS SDK) while the web app uses the SDK.

**Ideal**: Extract the edge function calls + state transitions into a shared module (e.g., `phone-verify-client.js`) that both can import. The extension's build setup may not support this cleanly today — if so, flag as tech debt and keep the two implementations in sync manually.

## What's NOT Changing
- **`auth.js`**: No changes — phone verify bypasses normal auth flow
- **Supabase email confirmation**: Still the primary path. Phone is only a fallback
- **Worker pipeline**: No impact
- **CSP headers**: Both `login.html` and `popup.html` already allow `connect-src` to Supabase domain (covers edge functions)
- **`phone_verify_attempts` table**: No longer needed — Supabase handles OTP rate limiting natively

## Verification
1. **Happy path**: Sign up → skip email → enter phone → receive SMS → enter code → land on dashboard
2. **Email path still works**: Sign up → click email link → confirmed as before
3. **Invalid code**: Enter wrong OTP → see error, can retry
4. **Rate limiting**: Supabase rejects rapid OTP sends (default 60s between sends)
5. **userId abuse**: Try calling `phone-verify-start` with an already-confirmed userId → rejected
6. **Drop-off recovery**: Close tab after signup → reopen → try signup again with same email → phone verify flow re-surfaces
7. **Phone cleanup**: After successful verification, confirm user record has phone cleared
8. **Extension flow**: Same flow works in the 320px popup, error states don't overflow
9. **Edge function errors**: Missing phone / bad format → clear error message
