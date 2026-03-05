/**
 * Supabase client singleton for the dashboard.
 * Anon key only — no service role key allowed here.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = "https://frbvdoszenrrlswegsxq.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_QopttEruBVdosoVJGy4j2A__5CFfx8W";

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
