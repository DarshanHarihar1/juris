// Backend API base — used server-side (permalink SSR) and client-side (intake POST).
// No Supabase import here, so importing it never constructs the Realtime client.
export const API_URL =
  process.env.NEXT_PUBLIC_API_URL || "https://juris-web.onrender.com";
