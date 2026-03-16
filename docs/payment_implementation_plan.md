# Payment Implementation Plan — $10/month Subscription

## Overview

Add a $10/month subscription gate using **Stripe Checkout + Webhooks** and a Supabase `subscriptions` table. Users sign up free, then must subscribe before the worker processes their emails or the dashboard shows results.

---

## Architecture

```
User (Dashboard)  →  Stripe Checkout  →  Stripe (hosted payment page)
                                              │
                                      webhook callback
                                              │
                                              ▼
                              Supabase Edge Function (webhook handler)
                                              │
                                         INSERT/UPDATE
                                              │
                                              ▼
                                    subscriptions table
                                              │
                              ┌───────────────┴───────────────┐
                              ▼                               ▼
                     Dashboard (gate UI)              Worker (gate pipeline)
```

**Why Stripe Checkout (not Elements)?**
- No PCI burden — Stripe hosts the entire payment form
- Handles SCA/3DS automatically
- Customer portal for self-service cancel/update card
- Minimal frontend code (redirect, not embed)

---

## Stripe Setup (one-time, manual)

| Step | Action |
|------|--------|
| 1 | Create Stripe account (or use existing) |
| 2 | Create a Product: "Clarion AI Pro" |
| 3 | Create a Price: $10/month, recurring, USD |
| 4 | Note the `price_id` (e.g. `price_1Xyz...`) → store as env var `STRIPE_PRICE_ID` |
| 5 | Create a webhook endpoint pointing to the Edge Function URL |
| 6 | Note the webhook signing secret → store as env var `STRIPE_WEBHOOK_SECRET` |
| 7 | Store `STRIPE_SECRET_KEY` as env var (Edge Function + Worker) |
| 8 | Store `STRIPE_PUBLISHABLE_KEY` as env var (Dashboard frontend) |

---

## Database Changes

### New table: `subscriptions`

```sql
CREATE TABLE subscriptions (
  id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id         UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  stripe_customer_id    TEXT,
  stripe_subscription_id TEXT,
  status          TEXT NOT NULL DEFAULT 'inactive',
    -- active, past_due, canceled, inactive, trialing
  plan            TEXT NOT NULL DEFAULT 'pro',
  current_period_start  TIMESTAMPTZ,
  current_period_end    TIMESTAMPTZ,
  cancel_at_period_end  BOOLEAN DEFAULT false,
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now(),

  UNIQUE (user_id),
  UNIQUE (stripe_subscription_id)
);

-- RLS: users read their own subscription
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users read own subscription"
  ON subscriptions FOR SELECT
  USING (auth.uid() = user_id);

-- Trigger: auto-create inactive subscription row on signup
CREATE OR REPLACE FUNCTION create_default_subscription()
RETURNS trigger AS $$
BEGIN
  INSERT INTO subscriptions (user_id, status)
  VALUES (NEW.id, 'inactive');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created_subscription
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION create_default_subscription();
```

### Backfill existing users

```sql
INSERT INTO subscriptions (user_id, status, plan, current_period_start, current_period_end)
SELECT
  id,
  'active',
  'pro',
  now(),
  null   -- no period end; grandfathered users have no renewal date
FROM auth.users
WHERE id NOT IN (SELECT user_id FROM subscriptions);
```

- Existing users are grandfathered as `active` so they aren't locked out.
- `current_period_end` is `null` for grandfathered users. The billing card should handle this as a special display case: show "Grandfathered — no renewal" instead of a date. This avoids the silent time bomb where a 30-day expiration date passes, the worker keeps processing (it checks `status`, not `current_period_end`), but the billing card shows a stale past date with no actual renewal happening.
- **Open decision:** whether grandfathered users stay free indefinitely or get migrated to paid after a grace period. Does not need to be solved now. When the decision is made, backfill `current_period_end` and `stripe_subscription_id` for those users.

---

## Supabase Edge Function: `stripe-webhook`

Handles Stripe webhook events. Deployed via `supabase functions deploy stripe-webhook`.

**Events to handle:**

| Event | Action |
|-------|--------|
| `checkout.session.completed` | Set subscription to `active`, store `stripe_customer_id` + `stripe_subscription_id` |
| `customer.subscription.updated` | Update `status`, `current_period_start`, `current_period_end`, `cancel_at_period_end` |
| `customer.subscription.deleted` | Set status to `canceled` |
| `invoice.payment_failed` | Set status to `past_due` |

**Key logic:**
- Verify webhook signature using `STRIPE_WEBHOOK_SECRET`
- Map Stripe customer to Supabase user via `client_reference_id` (set during Checkout creation) or metadata
- Use service role key for DB writes (bypasses RLS)

**File:** `supabase/functions/stripe-webhook/index.ts`

---

## Supabase Edge Function: `create-checkout-session`

Called from the dashboard to start the payment flow.

**Input:** Supabase JWT (from Authorization header)
**Output:** `{ url: "https://...", type: "checkout" | "portal" }`

**Logic:**
1. Verify JWT, extract `user_id` and `email`
2. Check `subscriptions` table for current status:
   - If `status` is already `active` → return `{ url: portalUrl, type: "portal" }` instead of creating a new checkout. This prevents double subscriptions from double-clicks, back-button, or bookmarked checkout URLs.
   - **Note for future-you:** This means a user who is active but has a failing card will land in the portal (billing management) instead of a checkout page if they somehow hit "Subscribe." That's the correct behavior — they need to update their card, not create a second subscription — but it can be confusing. The `type` field lets the dashboard show appropriate context (e.g. "Redirecting to billing management..." vs. "Redirecting to checkout...").
3. Check if user already has a `stripe_customer_id` → reuse or create new
4. Create Stripe Checkout Session:
   - `mode: 'subscription'`
   - `line_items: [{ price: STRIPE_PRICE_ID, quantity: 1 }]`
   - `client_reference_id: user_id`
   - `customer_email: email` (or `customer: existing_stripe_customer_id`)
   - `success_url: https://clarion.ai/app/dashboard?checkout=success`
   - `cancel_url: https://clarion.ai/app/dashboard?checkout=canceled`
5. Return `{ url: checkoutUrl, type: "checkout" }`

**Dashboard handling:** The frontend should check the `type` field and redirect accordingly. Both are valid outcomes — the distinction exists so the UI can show the right loading message during the redirect.

**File:** `supabase/functions/create-checkout-session/index.ts`

---

## Supabase Edge Function: `create-portal-session`

Lets users manage their subscription (cancel, update card) via Stripe Customer Portal.

**Input:** Supabase JWT
**Output:** `{ url: "https://billing.stripe.com/..." }`

**Logic:**
1. Verify JWT, look up `stripe_customer_id` from `subscriptions`
2. Create Stripe Billing Portal Session
3. Return the portal URL

**File:** `supabase/functions/create-portal-session/index.ts`

---

## Dashboard UI Changes

### New file: `web/js/subscription.js`

Shared module that fetches and caches the user's subscription status.

```javascript
export async function getSubscription() {
  // Query: subscriptions?user_id=eq.{uid}&select=status,plan,...
  // Returns { status, plan, current_period_end, cancel_at_period_end }
}

export function isSubscriptionActive(sub) {
  return sub?.status === 'active' || sub?.status === 'trialing';
}
```

### Gate logic in `web/js/auth.js`

After `requireAuth()` succeeds, check subscription status:
- If `active`, `trialing`, or `past_due` → proceed normally (worker still processes for `past_due`)
- If `past_due` → show a non-blocking warning banner: "Your payment failed. Please update your payment method to avoid interruption." with a link to the Stripe portal
- If `inactive` or `canceled` → show paywall overlay (blocks dashboard)

### Paywall component

A full-page overlay shown on all `/app/*` pages when subscription is not active:

```
┌─────────────────────────────────────────┐
│                                         │
│          Subscribe to Clarion AI        │
│                                         │
│   Your inbox, intelligently managed.    │
│   AI-powered email classification       │
│   and draft generation.                 │
│                                         │
│           $10 / month                   │
│                                         │
│      [ Subscribe Now ]  (primary btn)   │
│                                         │
│   past_due? "Update payment method"     │
│   canceled? "Resubscribe"               │
│                                         │
└─────────────────────────────────────────┘
```

- "Subscribe Now" calls `create-checkout-session` Edge Function → redirect to Stripe
- Past-due state shows "Update payment method" → calls `create-portal-session`

### Checkout-to-active race condition

Stripe's webhook is asynchronous — when the user lands back on `?checkout=success`, the webhook may not have fired yet. Naively re-fetching subscription status would show the paywall again, causing panic.

**Solution: poll on success return.**

When the dashboard detects `?checkout=success` in the URL:
1. Show a "Confirming your payment..." spinner overlay (replaces paywall)
2. Poll `subscriptions?user_id=eq.{uid}&select=status` every 2 seconds
3. On `status=active` → dismiss spinner, load dashboard normally
4. After 10 seconds (5 attempts) with no activation → show fallback message:
   "Payment received. Your account is being activated — please refresh in a moment."
5. Strip `?checkout=success` from the URL via `history.replaceState` to prevent re-triggering on refresh

This confirms the webhook actually landed before dismissing the gate.

### Billing section in dashboard or settings

Add a billing card (on dashboard or a new `/app/settings.html` page):

```
SUBSCRIPTION
┌─────────────────────────────────────┐
│  Plan:     Clarion AI Pro           │
│  Status:   Active                   │
│  Renews:   April 13, 2026          │
│                                     │
│  [ Manage Subscription ]            │
│    (opens Stripe Customer Portal)   │
└─────────────────────────────────────┘
```

- "Manage Subscription" calls `create-portal-session` → redirect
- If `cancel_at_period_end = true`, show "Cancels on {date}" instead of "Renews"
- If `current_period_end` is `null` (grandfathered user), show "Grandfathered — no renewal" and hide the "Manage Subscription" button (they have no Stripe customer to manage)

---

## Worker Gate

In `worker/run_pipeline.py`, before processing a user's emails:

```python
# Check subscription status — fail open on errors
ALLOWED_STATUSES = ("active", "trialing", "past_due")

try:
    sub = supabase.table("subscriptions") \
        .select("status") \
        .eq("user_id", user_id) \
        .single() \
        .execute()
    sub_status = sub.data.get("status")
except Exception as e:
    logger.warning(f"Subscription check failed for {user_id}, processing anyway: {e}")
    sub_status = None  # fail open

if sub_status is not None and sub_status not in ALLOWED_STATUSES:
    logger.info(f"Skipping user {user_id}: subscription {sub_status}")
    continue
```

**Key decisions:**

- **Fail open:** If the subscription query fails (Supabase down, network blip), process anyway. It's worse to not deliver the service someone paid for than to occasionally process a lapsed user. The cost of a false-positive (one extra Haiku call) is negligible vs. a false-negative (paying user gets nothing).
- **`past_due` treated as active:** Stripe retries failed payments over ~3 weeks before canceling. During that window the user still considers themselves subscribed — their card just failed once. Stopping the worker immediately creates a confusing experience. The worker gate should match whatever grace period is configured in Stripe's retry settings.
- **Scale note:** At current scale, one Supabase query per user per batch is fine. If the user count grows significantly, consider caching subscription status in memory with a 5-minute TTL to reduce query volume.

---

## Extension Behavior

No changes to the extension itself. The extension syncs emails regardless (so data is ready when the user subscribes). The worker gate prevents classification/draft generation for non-subscribers.

Optionally, the popup could show a "Subscribe" link if the subscription is inactive, but this is low priority.

---

## Env Vars Summary

| Variable | Where | Purpose |
|----------|-------|---------|
| `STRIPE_SECRET_KEY` | Edge Functions, Worker | Stripe API calls |
| `STRIPE_PUBLISHABLE_KEY` | Dashboard (frontend) | Checkout redirect |
| `STRIPE_PRICE_ID` | Edge Function (create-checkout) | $10/month price |
| `STRIPE_WEBHOOK_SECRET` | Edge Function (webhook) | Verify webhook signatures |

---

## Implementation Order

1. **Create Stripe account + product/price** (manual, no code)
2. **Add `subscriptions` table + RLS + trigger** (SQL migration)
3. **Backfill existing users** (one-time SQL)
4. **Deploy `stripe-webhook` Edge Function** (handles payment events)
5. **Register webhook URL in Stripe dashboard** (point to Edge Function) — do this immediately after deploying the webhook function
6. **Validate webhook path** using `stripe trigger checkout.session.completed` and confirm the subscription row updates correctly. Webhook bugs are the hardest to debug once the full flow is wired up — validating this path early saves pain.
7. **Deploy `create-checkout-session` Edge Function** (starts payment flow)
8. **Deploy `create-portal-session` Edge Function** (manage subscription)
9. **Add `subscription.js` module** to dashboard
10. **Add paywall overlay + checkout polling** to protected pages
11. **Add billing card + past_due warning banner** to dashboard
12. **Add worker gate** in `run_pipeline.py`
13. **Test end-to-end:** signup → paywall → checkout → polling spinner → active → dashboard loads → cancel → paywall returns

---

## Security Considerations

- **Webhook verification:** Always verify Stripe webhook signatures; reject unsigned events
- **No Stripe keys in frontend:** Only the publishable key is exposed client-side
- **Server-side subscription check:** Worker checks DB, not a client-provided claim
- **RLS on subscriptions:** Users can only read their own row; writes happen via service role in Edge Functions
- **Idempotent webhook handling:** Stripe may retry events; use `stripe_subscription_id` as unique key to avoid duplicate processing
- **Session creation rate limiting:** `create-checkout-session` and `create-portal-session` should check subscription status before hitting the Stripe API (the checkout function already does via the duplicate guard). Additionally, apply basic rate limiting (e.g. 5 calls per user per minute) to prevent a bad actor from spamming session creation. Not dangerous, but creates noise in the Stripe dashboard and could hit Stripe's API rate limits.

---

## Future Extensions (out of scope for now)

- **Free trial period** — set `status: 'trialing'` with a 7-day window via Stripe trial config
- **Annual plan** — add a second Price ($100/year) and let users pick during Checkout
- **Usage-based billing** — meter based on `token_usage` table (already tracks cost per user)
- **Team/org plans** — multiple users under one subscription
- **Promo codes** — Stripe Checkout supports `allow_promotion_codes: true`
