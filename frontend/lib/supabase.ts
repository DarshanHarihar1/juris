import { createClient } from "@supabase/supabase-js";

// Anon/publishable key — safe in the browser. Used only for read-only Realtime on
// events_log (RLS allows anon select). Placeholder fallbacks keep the build from
// throwing when env is absent (e.g. page-data collection); real values inline at build.
const url = process.env.NEXT_PUBLIC_SUPABASE_URL || "https://placeholder.supabase.co";
const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "placeholder-anon-key";

export const supabase = createClient(url, anon, {
  realtime: { params: { eventsPerSecond: 10 } },
});
