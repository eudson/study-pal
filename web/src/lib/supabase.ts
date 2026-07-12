import { createClient } from "@supabase/supabase-js";

// The Supabase client is the SPA's only auth surface. It handles the Google
// OAuth redirect (PKCE), persists the session, and silently refreshes the
// access token — the JWT we forward to the API as a Bearer credential.
//
// URL + anon key are public, project-specific values (Project Settings → API);
// they carry no secret. The service-role key MUST NEVER appear in the frontend.

const url = import.meta.env.VITE_SUPABASE_URL;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!url || !anonKey) {
  throw new Error(
    "VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY must be set (copy web/.env.example → web/.env).",
  );
}

export const supabase = createClient(url, anonKey, {
  auth: {
    flowType: "pkce",
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
});
