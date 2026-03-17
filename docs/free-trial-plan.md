# 7-Day Free Trial Implementation Plan

## Overview

Every new user gets 7 days of full access (onboarding, email sync, drafts, dashboard — everything). No credit card required. A visible countdown shows remaining time. After 7 days, the service locks out until they subscribe.

**Key design decision:** The trial is managed entirely in the DB, not Stripe. Stripe only enters the picture when users convert to paid. This avoids Stripe trial complexity and keeps the flow card-free.

---

## Current State (what already works in our favor)

- `subscriptions` table already has `trialing` as a valid status (line 10 of `013_subscriptions.sql`)
- `isSubscriptionActive()` in JS already returns `true` for `trialing` (`subscription.js:37`)
- `is_subscription_active()` in Python worker already accepts `trialing` (`supabase_client.py:44`)
- Stripe webhook already maps Stripe's `trialing` status correctly (`stripe-webhook/index.ts:133`)
- Account page already renders a "Trial" badge (`account.js:80`)
- Signup trigger creates a subscription row with `status='inactive'` — we just change this to `trialing`

**What's missing:**
- No `trial_ends_at` column — we need to know when the trial expires
- No trial expiry check — `isSubscriptionActive()` returns true for `trialing` regardless of date
- No countdown UI anywhere
- No lockout screen when trial expires
- Worker doesn't check trial expiry either

---

## Step 1 — Migration: Add `trial_ends_at` column

**File: `supabase/migrations/014_trial_ends_at.sql`** (new file)

```sql
ALTER TABLE public.subscriptions
  ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ;

-- Update signup trigger: new users start with 7-day trial
CREATE OR REPLACE FUNCTION create_default_subscription()
RETURNS trigger AS $$
BEGIN
  INSERT INTO subscriptions (user_id, status, trial_ends_at)
  VALUES (NEW.id, 'trialing', now() + interval '7 days');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
```

- Adds `trial_ends_at` column
- Replaces the signup trigger: new users get `status='trialing'` + `trial_ends_at = now() + 7 days`
- Existing users unaffected (their `trial_ends_at` stays NULL)

**Run manually on Supabase:** `supabase db push` or execute via SQL editor.

---

## Step 2 — Update `getSubscription()` to fetch `trial_ends_at`

**File: `web/js/subscription.js`**

- Line 22: Add `trial_ends_at` to the `.select()` string
- Add new exported helper:

```js
export function getTrialDaysRemaining(sub) {
    if (sub?.status !== "trialing" || !sub?.trial_ends_at) return null;
    const ms = new Date(sub.trial_ends_at) - Date.now();
    if (ms <= 0) return 0;
    return Math.ceil(ms / (1000 * 60 * 60 * 24));
}

export function isTrialExpired(sub) {
    if (sub?.status !== "trialing") return false;
    if (!sub?.trial_ends_at) return false;
    return new Date(sub.trial_ends_at) <= new Date();
}
```

- Update `isSubscriptionActive()` to account for trial expiry:

```js
export function isSubscriptionActive(sub) {
    if (sub?.status === "active" || sub?.status === "past_due") return true;
    if (sub?.status === "trialing") return !isTrialExpired(sub);
    return false;
}
```

---

## Step 3 — Trial banner in nav sidebar

**File: `web/js/nav.js`**

- Import `getTrialDaysRemaining` from `subscription.js`
- Change `renderNav()` signature to `renderNav(sub = null)` — accepts optional subscription object to avoid redundant fetches. Falls back to calling `getSubscription()` internally only if `sub` is null (e.g. account.js).
- After rendering the nav links, if user is trialing, inject a trial countdown element above the footer:

```html
<div class="em-trial-banner">
  <div class="em-trial-days">3</div>
  <div class="em-trial-text">days left in trial</div>
  <a href="/app/account.html" class="em-trial-cta">Subscribe</a>
</div>
```

- Shows on every page via the shared nav
- Links to Account page for conversion
- Color shifts: green (5-7 days), amber (2-4 days), red (0-1 days)

**File: `web/css/app.css`**
- Add `.em-trial-banner`, `.em-trial-days`, `.em-trial-text`, `.em-trial-cta` styles
- Color variants: `.em-trial-green`, `.em-trial-amber`, `.em-trial-red`

---

## Step 4 — Lockout gate on all protected pages

**File: `web/js/subscription.js`**

Add a new exported function `ensureAccess()`:

```js
export async function ensureAccess() {
    const sub = await getSubscription();
    if (isSubscriptionActive(sub) || isGrandfathered(sub)) return sub;
    // Trial expired or inactive — redirect to account page
    window.location.replace("/app/account.html");
    return new Promise(() => {}); // hang like requireAuth
}
```

**Files: All 6 user-facing pages** (not account.js, not login.js):
- `dashboard.js` — add `const sub = await ensureAccess();` after `await requireAuth();`, pass `sub` to `renderNav(sub)`, remove standalone `getSubscription()` call
- `emails.js` — add `await ensureAccess();` after `await requireAuth();`
- `contacts.js` — same
- `analytics.js` — same
- `history.js` — same (dev-only, but still gated)
- `devtools.js` — same (dev-only, but still gated)

**Not gated:**
- `account.js` — must remain accessible so expired users can subscribe
- `login.js` — pre-auth

Pattern (pages with existing `getSubscription()` call like dashboard.js):
```js
await requireAuth();
const subscription = await ensureAccess();  // replaces standalone getSubscription()
listenAuthChanges();
await renderNav(subscription);
```

Pattern (pages without existing `getSubscription()` call):
```js
await requireAuth();
await ensureAccess();
listenAuthChanges();
await renderNav();
```

---

## Step 5 — Account page: trial-aware rendering

**File: `web/js/pages/account.js`**

Update `renderAccount()` to handle the trial-expired state:

- If `status === 'trialing'` and not expired: show "Trial" badge + countdown text ("X days remaining") + "Subscribe" CTA
- If `status === 'trialing'` and expired: show "Trial Expired" badge (red) + prominent subscribe CTA + message like "Your 7-day trial has ended. Subscribe to continue using Clarion AI."
- If `status === 'inactive'`: same subscribe CTA (handles edge case of users who somehow have inactive status)

Import `getTrialDaysRemaining`, `isTrialExpired` from `subscription.js`.

---

## Step 6 — Worker: check trial expiry

**File: `worker/supabase_client.py`**

Update `is_subscription_active()` to also fetch and check `trial_ends_at`:

```python
def is_subscription_active(self, user_id):
    try:
        result = (
            self.client.table("subscriptions")
            .select("status, trial_ends_at")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if not result.data:
            return False
        status = result.data["status"]
        if status in ("active", "past_due"):
            return True
        if status == "trialing":
            ends = result.data.get("trial_ends_at")
            if not ends:
                return False
            return datetime.fromisoformat(ends.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        return False
    except Exception as e:
        logger.warning(f"Subscription check failed for {user_id[:8]}..., fail-open: {e}")
        return True
```

Also add `timezone` to the existing top-level import on line 9: `from datetime import datetime, timedelta, timezone`

This ensures the worker stops processing emails for users with expired trials.

---

## Step 7 — Extension: no changes needed

The extension has zero subscription checks (confirmed via grep). It syncs emails to Supabase regardless. The worker already gates processing, so expired-trial users' emails get synced but not scored or drafted. This is intentional:
- If they subscribe later, their email history is available
- No user data is lost

---

## Step 8 — Stripe webhook: no changes needed

The webhook already handles `trialing` status from Stripe (`stripe-webhook/index.ts:133`). When a user subscribes after trial, Stripe sends `checkout.session.completed` which sets `status='active'`. This naturally overrides the trial state.

---

## Files Modified (summary)

| File | Change |
|------|--------|
| `supabase/migrations/014_trial_ends_at.sql` | New — add column + update trigger |
| `web/js/subscription.js` | Add `trial_ends_at` to select, add `getTrialDaysRemaining()`, `isTrialExpired()`, `ensureAccess()`, update `isSubscriptionActive()` |
| `web/js/nav.js` | Trial countdown banner in sidebar |
| `web/css/app.css` | Trial banner styles |
| `web/js/pages/account.js` | Trial-aware rendering (expired state, countdown) |
| `web/js/pages/dashboard.js` | Add `ensureAccess()` call |
| `web/js/pages/emails.js` | Add `ensureAccess()` call |
| `web/js/pages/contacts.js` | Add `ensureAccess()` call |
| `web/js/pages/analytics.js` | Add `ensureAccess()` call |
| `web/js/pages/history.js` | Add `ensureAccess()` call |
| `web/js/pages/devtools.js` | Add `ensureAccess()` call |
| `worker/supabase_client.py` | Trial expiry check in `is_subscription_active()` |

---

## Verification

1. **New user signup** — create account, verify `subscriptions` row has `status='trialing'` and `trial_ends_at` = 7 days out
2. **Dashboard loads** — trial user sees full dashboard + trial countdown in sidebar
3. **All pages accessible** — emails, contacts, analytics all load during active trial
4. **Trial countdown** — sidebar shows correct days remaining, color matches urgency
5. **Trial expiry** — manually set `trial_ends_at` to past via SQL, refresh page → redirected to account.html
6. **Account page accessible when expired** — shows "Trial Expired" + subscribe CTA
7. **Worker gating** — expired trial user's emails are not processed (check logs)
8. **Subscription conversion** — click Subscribe, complete Stripe checkout, verify `status` changes to `active` and pages unlock
9. **Existing users** — grandfathered/active users see no trial banner, no behavior change
10. **Extension** — continues syncing regardless of trial status (expected)
