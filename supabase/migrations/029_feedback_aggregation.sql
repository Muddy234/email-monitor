-- Feedback aggregation RPC for prompt injection.
-- Joins feedback → emails to resolve sender_email, then aggregates
-- per (user_id, sender_email) for the worker to inject as soft bias
-- into the Haiku signal extraction prompt.

create or replace function public.get_feedback_summary(
    p_user_id uuid,
    p_sender_emails text[]
)
returns table (
    sender_email text,
    positive_count int,
    negative_count int,
    top_correction_category text,
    top_correction_value text
)
language sql stable
as $$
    with raw as (
        select
            e.sender_email,
            f.feedback_type,
            f.correction_category,
            f.correction_value
        from public.feedback f
        join public.emails e on e.id = f.email_id
        where f.user_id = p_user_id
          and e.sender_email = any(p_sender_emails)
    ),
    agg as (
        select
            r.sender_email,
            count(*) filter (where r.feedback_type = 'positive')::int as positive_count,
            count(*) filter (where r.feedback_type = 'negative')::int as negative_count
        from raw r
        group by r.sender_email
    ),
    top_correction as (
        select distinct on (r.sender_email)
            r.sender_email,
            r.correction_category,
            r.correction_value
        from raw r
        where r.feedback_type = 'negative'
          and r.correction_category in ('wrong_priority', 'no_response_needed', 'response_needed')
        group by r.sender_email, r.correction_category, r.correction_value
        order by r.sender_email, count(*) desc
    )
    select
        a.sender_email,
        a.positive_count,
        a.negative_count,
        tc.correction_category as top_correction_category,
        tc.correction_value as top_correction_value
    from agg a
    left join top_correction tc using (sender_email)
    where (a.positive_count + a.negative_count) >= 2;
$$;
