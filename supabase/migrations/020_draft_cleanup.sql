-- Stale draft cleanup: RPC to find drafts whose conversation has a sent reply.
-- Called by the extension on every alarm cycle to delete stale drafts from Outlook.

CREATE OR REPLACE FUNCTION public.find_stale_drafts()
RETURNS TABLE (
  draft_id uuid,
  outlook_draft_id text,
  status text
)
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  RETURN QUERY
  SELECT d.id        AS draft_id,
         d.outlook_draft_id,
         d.status
  FROM public.drafts d
  JOIN public.emails parent ON parent.id = d.email_id
  WHERE d.user_id = auth.uid()
    AND d.status IN ('written', 'pending')
    AND d.draft_deleted = false
    AND EXISTS (
      SELECT 1
      FROM public.emails sent
      WHERE sent.user_id  = auth.uid()
        AND sent.conversation_id = parent.conversation_id
        AND sent.folder    = 'Sent Items'
        AND sent.received_time > d.created_at
    )
  LIMIT 10;
END;
$$;
