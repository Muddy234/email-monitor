# Enhanced Extension Popup UI

## Overview
Redesign the Status view (view 4) of the Chrome extension popup. The Welcome, Login, and Setup views stay unchanged. Only `popup.html` and `popup.js` are modified.

## Files to Modify
- `extension/popup.html` — HTML structure + CSS
- `extension/popup.js` — Data fetching, rendering, event handlers

---

## Layout (top to bottom)

### 1. User Bar (compact)
```
[user@email.com]           [● Connected]
```
- Email on left, connection status on right (replaces the full connection bar)
- Green dot + "Connected" or red dot + short hint ("Session expired", "Open Outlook to connect")
- Logout button removed from here → moves to Advanced

### 2. Stat Cards (3 columns)
```
┌──────────┐ ┌──────────┐ ┌──────────┐
│     5    │ │     2    │ │    18    │
│  Needs   │ │  Drafts  │ │Processed │
│ Attention│ │  Ready   │ │          │
└──────────┘ └──────────┘ └──────────┘
```
- **Needs Attention** — `needs_response=true` classifications that have NO corresponding draft (client-side cross-reference of Q2 and Q3 results). Amber border when > 0.
- **Drafts Ready** — drafts created today
- **Processed** — emails with `status=completed` today (replaces "Received")
- Loading state: animated skeleton pulse until data arrives, with 300ms minimum display time to avoid jarring flash on fast connections

### 3. Needs Attention Mini-List (conditional)
When Needs Attention > 0:
```
NEEDS ATTENTION
┌─────────────────────────────────────┐
│ Jim Crigler  Re: Cottages      2h  │
│ Dave Wittwer  Closing docs     4h  │
│ Sarah Chen   Budget review     6h  │
├─────────────────────────────────────┤
│ View all 7 →                        │
└─────────────────────────────────────┘
```
- Each row: **sender_name** (bold) | subject (truncated) | relative time
- Click → opens email in Outlook via deep link (see Deep Link Format below)
- Max 3 items; "View all N →" links to `https://clarion-ai.app/app/emails.html`
- When Needs Attention = 0: replaced by "✅ All caught up"

### 4. Activity Feed
```
RECENT ACTIVITY
┌─────────────────────────────────────┐
│ Draft created for Jim Crigler  2m  │
│ 12 emails classified          18m  │
│ Draft created for Dave Wittwer 1h  │
└─────────────────────────────────────┘
```
- Merges recent drafts + pipeline_runs (last 24h), sorted by time, max 3
- Draft events: "Draft created for {sender_name}"
- Pipeline events: "N emails classified" — **filter out runs where `emails_processed === 0`** to avoid "0 emails classified" clutter
- Empty state: "No recent activity"

### 5. Primary Action
```
[        Open Dashboard        ]
```
- Full-width primary button (only primary CTA)
- Links to `https://clarion-ai.app/app/dashboard.html`
- "Sync Now" demoted to Advanced section

### 6. Advanced (collapsible, unchanged content + additions)
```
▶ Advanced
  [Sync Now]  [Logout]
  Auth:       ● Authenticated
  Outlook:    ● Connected
  Expires:    ~4h remaining
  Origin:     outlook.office.com
  Realtime:   ● Connected
  Last sync:  2m ago
```
- Sync Now + Logout buttons move here
- Logout shows inline confirmation: "Are you sure? [Yes] [Cancel]"
- All existing debug fields preserved

---

## Outlook Deep Link Format

The `email_ref` field stores the OWA/EWS `ItemId.Id` (the Exchange immutable item ID, set at `background.js:188` via `item.ItemId?.Id`). This is **not** the Internet Message-ID header.

The correct Outlook deep link format for this ID type is:
```
https://outlook.office.com/mail/id/{encodeURIComponent(email_ref)}
```

The `/mail/id/` path expects the EWS ItemId, which is exactly what we store. The ID contains `+`, `/`, `=` characters that require encoding.

---

## Dashboard URLs (canonical references)

| Link | URL |
|------|-----|
| Dashboard home | `https://clarion-ai.app/app/dashboard.html` |
| Emails list | `https://clarion-ai.app/app/emails.html` |
| "View all N →" target | `https://clarion-ai.app/app/emails.html` |
| "Open Dashboard" button | `https://clarion-ai.app/app/dashboard.html` |

These match the existing `visitWebBtn` handler at `popup.js:498`.

---

## Queries (4 parallel requests)

| # | Endpoint | Purpose |
|---|----------|---------|
| Q1 | `emails?select=id&user_id=eq.{uid}&folder=eq.Inbox&received_time=gte.{today}&status=eq.completed` | Processed count |
| Q2 | `classifications?select=id,email_id,action,created_at,emails(id,email_ref,subject,sender_name,received_time)&user_id=eq.{uid}&needs_response=eq.true&created_at=gte.{today}&order=created_at.desc` | Attention candidates + mini-list email data (embedded join) |
| Q3 | `drafts?select=id,email_id,created_at,emails(sender_name)&user_id=eq.{uid}&created_at=gte.{today}&order=created_at.desc` | Drafts count + activity feed + draft exclusion set |
| Q4 | `pipeline_runs?select=id,status,emails_processed,drafts_generated,finished_at&user_id=eq.{uid}&order=finished_at.desc&limit=5` | Activity feed (classification events) |

### Needs Attention calculation (client-side post-filter)

Supabase PostgREST doesn't support left-join-with-null-check in a single query. Instead, the client computes "Needs Attention" by cross-referencing Q2 and Q3:

```javascript
const draftedEmailIds = new Set(drafts.map(d => d.email_id));
const attentionItems = classifications.filter(c => !draftedEmailIds.has(c.email_id));
```

This ensures the count and mini-list exclude emails that already have drafts.

### Refresh strategy

**Replace the 5-second polling interval with:**
1. **Fetch on popup open** — `checkSessionAndRender()` triggers `refreshStatus()` once
2. **30-second background interval** — reduced from 5s to 30s (~120 requests/hour vs ~720)
3. **Event-driven refresh** — after "Sync Now" completes, immediately refresh
4. **Future improvement** — leverage the existing Supabase Realtime WebSocket connection (already tracked in `realtimeStatus`) to push updates to the popup instead of polling. This would eliminate the interval entirely. Out of scope for this iteration but noted as a follow-up.

---

## JS Changes Summary

| Function | Action |
|----------|--------|
| `fetchStats()` | **Delete** — replaced by `fetchEnhancedStats()` |
| `fetchEnhancedStats(session)` | **New** — 4 parallel queries, client-side cross-reference for attention count |
| `renderStatusData(data)` | **New** — updates stat cards, attention list, activity feed, empty states |
| `renderAttentionList(emails, total)` | **New** — builds clickable email rows with Outlook deep links |
| `renderActivityFeed(activities)` | **New** — builds activity feed from merged drafts + pipeline runs (filters out 0-processed runs) |
| `setStatValue(id, value)` | **New** — removes skeleton class, sets number |
| `escapeHtml(str)` | **New** — XSS prevention; **must be applied to all `sender_name` and `subject` values** since these originate from external email headers and are primary injection vectors |
| `refreshStatus(session)` | **Rewrite** — inline connection indicator, calls `fetchEnhancedStats` |
| `showStatusView(session)` | **Minor update** — reference new element IDs |
| Logout handler | **Rewrite** — two-step confirmation (Logout → "Are you sure?" → Yes/Cancel) |
| Periodic interval | **Change** — 5000ms → 30000ms |

### Skeleton minimum display time

To avoid a jarring flash of skeleton → content on fast connections, enforce a 300ms minimum:

```javascript
async function refreshStatusData(session) {
  const fetchStart = Date.now();
  const data = await fetchEnhancedStats(session);
  const elapsed = Date.now() - fetchStart;
  if (elapsed < 300) {
    await new Promise(r => setTimeout(r, 300 - elapsed));
  }
  renderStatusData(data);
}
```

This only applies on the initial load when skeletons are visible. Subsequent refreshes (where numbers are already showing) skip the delay.

---

## CSS Additions

### Skeleton loading pulse
```css
.skeleton-text {
  background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: pulse 1.5s ease-in-out infinite;
  border-radius: 3px;
  min-height: 22px;
  min-width: 24px;
}
@keyframes pulse {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
```

### Attention card accent (when count > 0)
```css
.stat-card.attention-active {
  border: 1.5px solid #f59e0b;
  background: #fffbeb;
}
```

### Email mini-list items
```css
.attention-item {
  display: flex;
  align-items: baseline;
  padding: 6px 0;
  border-bottom: 1px solid #f0f0f0;
  cursor: pointer;
  gap: 6px;
}
.attention-item:hover { background: #f9f9f9; }
.attention-sender { font-weight: 600; font-size: 12px; max-width: 100px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.attention-subject { font-size: 12px; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.attention-time { font-size: 10px; color: #999; white-space: nowrap; }
.attention-more { font-size: 12px; color: #3b82f6; cursor: pointer; }
```

### Activity feed
```css
.activity-item {
  display: flex;
  justify-content: space-between;
  padding: 5px 0;
  border-bottom: 1px solid #f0f0f0;
  font-size: 12px;
}
.activity-text { color: #444; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.activity-time { color: #999; font-size: 10px; margin-left: 8px; }
.activity-empty { font-size: 12px; color: #999; padding: 8px 0; }
```

### Inline connection indicator
```css
.connection-indicator {
  font-size: 11px;
  color: #666;
  display: flex;
  align-items: center;
  gap: 4px;
}
```

### Caught-up state
```css
.caught-up {
  text-align: center;
  padding: 12px 0;
  font-size: 13px;
  color: #22c55e;
}
```

---

## Verification
1. Load the unpacked extension in Chrome (`chrome://extensions` → Load unpacked)
2. Open the popup while logged in — confirm skeleton loading appears for ~300ms, then stats populate
3. Verify Needs Attention count excludes emails that already have drafts
4. Verify attention mini-list shows when there are unresolved needs_response emails
5. Click an email in the mini-list — confirm it opens the correct email in Outlook (verify the URL uses `encodeURIComponent` on the EWS ItemId)
6. Verify "View all N →" links to `https://clarion-ai.app/app/emails.html`
7. Verify "All caught up" shows when attention count = 0
8. Verify activity feed shows recent drafts and classification events, and does NOT show "0 emails classified" entries
9. Verify `escapeHtml()` is applied to all `sender_name` and `subject` values in both attention list and activity feed
10. Open Advanced → click Logout → confirm two-step confirmation works
11. Open Advanced → click Sync Now → confirm sync still works and triggers an immediate data refresh
12. Test disconnected state (close Outlook tab) → confirm connection indicator turns red with hint
13. Confirm popup doesn't poll excessively — interval should be 30s, not 5s
