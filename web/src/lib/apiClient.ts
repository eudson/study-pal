import { client } from "../api/client.gen";
import { supabase } from "./supabase";
import { getActiveKioskSession, matchKioskEndpoint } from "./kioskSession";

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

// Request interceptor: attach either the scoped child kiosk credential or
// the normal parent credential (X-User-Id, and — for the three kiosk-shaped
// endpoints below — Authorization), never both.
//
// Kiosk-vs-parent decision:
// `matchKioskEndpoint` inspects the outgoing request's URL path + method and
// recognises exactly the three kiosk-eligible endpoints:
//   GET  /cycles/{cycleId}/capture        (scope "capture")
//   POST /cycles/{cycleId}/submissions    (scope "capture")
//   GET  /cycles/{cycleId}/child-results  (scope "results")
// Every other request (all parent-only calls, including `mintChildSession`
// itself) is untouched by the block below and keeps exactly the prior
// behavior (X-User-Id only; Authorization is stamped separately by the
// `auth` callback in `client.setConfig`, above, via the SDK's own security
// handling for those endpoints' `bearer` scheme).
//
// A subtlety specific to the three kiosk endpoints: their OpenAPI security
// list is `[x-child-session, bearer, x-user-id]`, and @hey-api/client-fetch's
// built-in security handling applies only the FIRST scheme for which the
// shared `auth()` callback (configured above) returns a value — regardless
// of the scheme's own type. Because `auth()` returns the *parent's* Supabase
// access token whenever a parent is signed in, that built-in step will have
// already stamped `X-Child-Session` with the parent's raw JWT (not a valid
// kiosk token) and skipped `Authorization` entirely, for BOTH kiosk and
// parent-mode requests, before this interceptor ever runs. We treat that
// value as untrustworthy and always decide the header set ourselves for
// these three endpoints, in both branches below.
client.interceptors.request.use(async (request) => {
  const url = new URL(request.url);
  const kioskMatch = matchKioskEndpoint(url.pathname, request.method);

  if (kioskMatch) {
    // Never trust whatever the SDK's own security handling may have set.
    request.headers.delete("X-Child-Session");

    const activeSession = getActiveKioskSession(kioskMatch.cycleId, kioskMatch.scope);
    if (activeSession) {
      // Child-mode request: scoped token only. No parent credential rides
      // along on this request under any circumstance.
      request.headers.set("X-Child-Session", activeSession.token);
      request.headers.delete("Authorization");
      request.headers.delete("X-User-Id");
      return request;
    }
  }

  // Parent-credentialed path: every non-kiosk request, and the fallback for
  // a kiosk-shaped request with no active kiosk session (e.g. direct
  // navigation to /capture/$cycleId without going through the mint-then-
  // navigate handoff — the backend still accepts the parent session there).
  const { data } = await supabase.auth.getSession();
  const userId = data.session?.user?.id;
  if (userId) {
    request.headers.set("X-User-Id", userId);
  }
  if (kioskMatch) {
    // Only the three kiosk-shaped endpoints need Authorization stamped
    // explicitly here (see subtlety above) — every other endpoint already
    // gets it from the SDK's own security handling.
    const accessToken = data.session?.access_token;
    if (accessToken) {
      request.headers.set("Authorization", `Bearer ${accessToken}`);
    }
  }
  return request;
});
