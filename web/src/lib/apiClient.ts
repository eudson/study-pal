import { client } from "../api/client.gen";
import { supabase } from "./supabase";

// Configured once at startup (imported from main.tsx). Default to same-origin
// so browser requests go through the Vite dev proxy / reverse proxy and never
// need CORS.
//
// `auth` is invoked only for endpoints that declare a security scheme (e.g.
// POST /assessments/generate). It returns the current access token, which the
// client sends as `Authorization: Bearer <jwt>` — the credential the API's
// JWKS verifier checks. getSession() returns a valid (auto-refreshed) token,
// or undefined when signed out, in which case the API replies 401.
client.setConfig({
  baseUrl: import.meta.env.VITE_API_BASE_URL || "/",
  auth: async () => {
    const { data } = await supabase.auth.getSession();
    return data.session?.access_token;
  },
});

// Request interceptor: inject X-User-Id on every request.
// In stub-auth mode the backend resolves identity from this header.
// In JWKS mode (prod) the header value equals the JWT `sub`, so it is
// redundant but harmless — the server validates the Bearer token and ignores
// the header.
client.interceptors.request.use(async (request) => {
  const { data } = await supabase.auth.getSession();
  const userId = data.session?.user?.id;
  if (userId) {
    request.headers.set("X-User-Id", userId);
  }
  return request;
});
