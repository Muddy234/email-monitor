-- Rate-limit table for phone verification attempts.
-- Tracks per-IP request counts to cap SMS cost exposure.
-- No RLS needed — only accessed by edge functions via service role.

CREATE TABLE IF NOT EXISTS public.phone_verify_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ip_address text NOT NULL,
  attempted_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_phone_verify_ip_time
  ON public.phone_verify_attempts (ip_address, attempted_at DESC);

-- Check + record: returns true if under limit, false if blocked.
-- Caller passes the requester's IP; function counts attempts in the
-- last p_window_minutes and compares against p_max_attempts.
CREATE OR REPLACE FUNCTION public.check_phone_verify_rate_limit(
  p_ip text,
  p_max_attempts int DEFAULT 10,
  p_window_minutes int DEFAULT 60
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  recent_count int;
BEGIN
  -- Count recent attempts from this IP
  SELECT count(*)
    INTO recent_count
    FROM public.phone_verify_attempts
   WHERE ip_address = p_ip
     AND attempted_at > now() - (p_window_minutes || ' minutes')::interval;

  IF recent_count >= p_max_attempts THEN
    RETURN false;
  END IF;

  -- Under limit — record this attempt
  INSERT INTO public.phone_verify_attempts (ip_address)
  VALUES (p_ip);

  RETURN true;
END;
$$;

-- Cleanup: purge rows older than 24 hours.
-- Call via pg_cron or a scheduled edge function.
CREATE OR REPLACE FUNCTION public.cleanup_phone_verify_attempts()
RETURNS void
LANGUAGE sql
SECURITY DEFINER
AS $$
  DELETE FROM public.phone_verify_attempts
   WHERE attempted_at < now() - interval '24 hours';
$$;
