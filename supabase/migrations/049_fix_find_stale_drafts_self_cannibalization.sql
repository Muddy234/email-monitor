-- 049: Fix find_stale_drafts self-cannibalization.
--
-- Bug:
--   After OWA UI Send, the AI draft is transformed into a sent message that
--   keeps the same EWS ItemId. The extension's email sync then writes the
--   sent copy into public.emails with email_ref equal to the draft's
--   outlook_draft_id (folder='Sent Items', received_time > drafts.created_at).
--
--   On the next sweepStaleDrafts cycle (every 5 min via chrome.alarms), the
--   find_stale_drafts RPC's EXISTS clause matched that very row — i.e., the
--   draft's own sent copy — as evidence that "a sent reply already exists in
--   the conversation". The extension then HardDeleted outlook_draft_id, which
--   resolves to the sent copy itself, sending the message to Recoverable Items
--   \ Purges (invisible to OWA's "Recover Deleted Items" UI, which only shows
--   Deletions).
--
--   Symptom: the AI-drafted reply disappears from Sent Items shortly after
--   send and is not in the conversation thread. Inbound replies arriving in
--   the same window made the timing appear correlated with replies, but the
--   trigger is the unconditional 5-minute sweep, not the inbound reply.
--
-- Fix:
--   Exclude the draft's own outlook_draft_id from the EXISTS subquery.
--   Genuine staleness (manual reply via desktop Outlook, mobile, etc.) still
--   triggers cleanup because that sent reply has a different email_ref.
--
-- See: 045_lock_unified_vocabulary.sql for prior version of this RPC.

begin;

set local statement_timeout = '5min';
set local lock_timeout = '30s';

create or replace function public.find_stale_drafts()
returns table (
  draft_id uuid,
  outlook_draft_id text,
  status text
)
language plpgsql
security invoker
as $$
begin
  return query
  select d.id        as draft_id,
         d.outlook_draft_id,
         d.status
  from public.drafts d
  join public.emails parent on parent.id = d.email_id
  where d.user_id = auth.uid()
    and d.status in ('pending', 'done')
    and d.draft_deleted = false
    and exists (
      select 1
      from public.emails sent
      where sent.user_id        = auth.uid()
        and sent.conversation_id = parent.conversation_id
        and sent.folder          = 'Sent Items'
        and sent.received_time   > d.created_at
        and sent.email_ref      <> coalesce(d.outlook_draft_id, '')
    )
  limit 10;
end;
$$;

commit;
