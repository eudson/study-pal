/**
 * Kiosk session store — the narrow, scoped credential used once a parent
 * hands the device to the child (docs: same-device kiosk handoff).
 *
 * Holds the short-lived token minted by `POST /cycles/{id}/child-session`
 * (SDK: `mintChildSession`) in `sessionStorage`, scoped to a single browser
 * tab/session so it never leaks across tabs and is gone on browser close.
 * Deliberately plain TS types — no Pydantic/generated-schema coupling here,
 * this is UI-local state, not a server contract.
 */

const STORAGE_KEY = "studypal.kioskSession";

export type KioskScope = "capture" | "results";

export interface KioskSession {
  token: string;
  scope: KioskScope;
  cycleId: string;
  /** ISO 8601 timestamp (mirrors `ChildSessionResponse.expires_at`). */
  expiresAt: string;
}

export function setKioskSession(session: KioskSession): void {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
}

export function clearKioskSession(): void {
  sessionStorage.removeItem(STORAGE_KEY);
}

/**
 * Reads the stored kiosk session, if any. Does NOT clear it on expiry as a
 * side effect (reads should stay pure) — callers that observe an expired
 * session should call `clearKioskSession()` explicitly.
 */
export function getKioskSession(): KioskSession | null {
  const raw = sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<KioskSession>;
    if (
      typeof parsed.token !== "string" ||
      (parsed.scope !== "capture" && parsed.scope !== "results") ||
      typeof parsed.cycleId !== "string" ||
      typeof parsed.expiresAt !== "string"
    ) {
      return null;
    }
    return parsed as KioskSession;
  } catch {
    return null;
  }
}

function isExpired(session: KioskSession): boolean {
  const expiry = Date.parse(session.expiresAt);
  if (Number.isNaN(expiry)) return true;
  return Date.now() >= expiry;
}

/**
 * True when there is a live (non-expired) kiosk session for the given cycle
 * + scope. An expired token is treated as inactive. Pure — never mutates
 * storage; callers that need to clear an expired session should call
 * `clearKioskSession()` (or use `getActiveKioskSession`, below, which does
 * this for you).
 */
export function isKioskActiveFor(cycleId: string, scope: KioskScope): boolean {
  const session = getKioskSession();
  if (!session) return false;
  if (session.cycleId !== cycleId || session.scope !== scope) return false;
  return !isExpired(session);
}

/**
 * Same match as `isKioskActiveFor`, but returns the session itself, and —
 * unlike `isKioskActiveFor` — clears storage as a side effect when the
 * stored session for this cycle+scope has expired, so an expired token
 * never lingers past the moment it's next checked.
 */
export function getActiveKioskSession(cycleId: string, scope: KioskScope): KioskSession | null {
  const session = getKioskSession();
  if (!session) return null;
  if (session.cycleId !== cycleId || session.scope !== scope) return null;
  if (isExpired(session)) {
    clearKioskSession();
    return null;
  }
  return session;
}

// ─────────────────────────────────────────────────────────
// Kiosk endpoint matching — used by the request interceptor
// (`web/src/lib/apiClient.ts`) to decide which credential plane a given
// outgoing request belongs to. Kept here (co-located with the session
// store) so the URL/method contract for "what counts as a kiosk endpoint"
// has one home.
// ─────────────────────────────────────────────────────────

export interface KioskEndpointMatch {
  cycleId: string;
  scope: KioskScope;
}

const KIOSK_PATH_RE = /\/cycles\/([^/]+)\/(capture|submissions|child-results)\/?$/;

/**
 * Matches the three kiosk-eligible endpoints:
 *   GET  /cycles/{cycleId}/capture        -> scope "capture"
 *   POST /cycles/{cycleId}/submissions    -> scope "capture"
 *   GET  /cycles/{cycleId}/child-results  -> scope "results"
 *
 * `pathname` should be the request URL's path only (no origin/query).
 * `method` is matched case-insensitively (native `Request.method` is
 * already uppercase, but this is defensive).
 */
export function matchKioskEndpoint(pathname: string, method: string): KioskEndpointMatch | null {
  const match = KIOSK_PATH_RE.exec(pathname);
  if (!match) return null;
  const [, cycleId, resource] = match;
  if (!cycleId) return null;
  const upperMethod = method.toUpperCase();
  if (resource === "capture" && upperMethod === "GET") return { cycleId, scope: "capture" };
  if (resource === "submissions" && upperMethod === "POST") return { cycleId, scope: "capture" };
  if (resource === "child-results" && upperMethod === "GET") return { cycleId, scope: "results" };
  return null;
}
