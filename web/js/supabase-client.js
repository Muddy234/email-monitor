/**
 * Supabase client singleton for the dashboard.
 * Anon key only — no service role key allowed here.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = "https://frbvdoszenrrlswegsxq.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZyYnZkb3N6ZW5ycmxzd2Vnc3hxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI2NjA0OTUsImV4cCI6MjA4ODIzNjQ5NX0.OCYTv_B823u_9o_Q9S-qPpUea9DQt_xpsWuNnolJT7M";

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
