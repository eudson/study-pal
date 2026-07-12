# StudyPal — Google Sign-In Setup (Supabase Auth)

Runbook for wiring **Google OAuth** through **Supabase Auth** into the StudyPal
SPA + FastAPI backend. Written after a verified end-to-end sign-in on the
`eu-west-1` dev project (2026-07-12). Use it to reproduce the setup on the
**production** project (see §7). Companion to `docs/production-readiness.md`
(items 2 & 3) and ARCHITECTURE.md §10 (auth decisions).

---

## 1. How the flow works (so the config makes sense)

```
Browser ── "Sign in with Google" ─▶ Google consent
   ▲                                     │
   │            code (PKCE)              ▼
   └──◀── SPA origin ◀── Supabase /auth/v1/callback ◀── Google
                 │
          Supabase issues a JWT (ES256) → stored session
                 │
   SPA sends it as  Authorization: Bearer <jwt>  on secured API calls
                 │
          FastAPI verifies it against Supabase's JWKS  → user_id (sub)
```

Key consequences:
- **Google never talks to our app directly.** Google redirects to *Supabase's*
  callback; Supabase then redirects back to the SPA. So the redirect URI
  registered in Google is Supabase's callback, not `localhost`.
- The backend is **provider-agnostic**: it only verifies the Supabase JWT. Adding
  email/Apple/etc. later needs no backend change.
- The backend has **two modes** (see §5): a local stub, and real JWKS
  verification. Google sign-in produces a real JWT either way; whether the
  backend *checks* it depends on its config.

---

## 2. Prerequisites

- A Supabase project. Note its **project ref** (the `<ref>` in
  `https://<ref>.supabase.co`). Dev project ref: `swmzbwdnmsuvuhtbiety`.
- A Google account with access to **Google Cloud Console**.
- The SPA origin(s). Dev: `http://localhost:5173`. Prod: your real domain.

---

## 3. Google Cloud Console — create the OAuth client

Google reorganised this area into **"Google Auth Platform"** (APIs & Services →
OAuth consent screen). Sections: Overview / **Branding** / **Audience** /
**Clients** / **Data Access**.

1. **Copy the Supabase callback URL first.** In Supabase → Authentication →
   Providers → Google, copy the **Callback URL (for OAuth)**:
   ```
   https://<PROJECT_REF>.supabase.co/auth/v1/callback
   ```
   Dev value: `https://swmzbwdnmsuvuhtbiety.supabase.co/auth/v1/callback`

2. **OAuth consent screen** (first time only):
   - **Audience:** User type **External**. While unpublished it's in **Testing** —
     add every tester's Google account under **Audience → Test users**, or Google
     blocks them with *"access blocked / app not verified."*
   - **Branding:** app name, support email, developer contact. **Authorized
     domains** also live here — **optional in Testing** (Google usually derives
     `supabase.co` from the redirect URI). Add `supabase.co` (and your prod
     domain) only when you publish. Not required to test.
   - **Data Access (scopes):** `openid`, `.../auth/userinfo.email`,
     `.../auth/userinfo.profile` (defaults are fine).

3. **Create the client** (Clients → **Create client**):
   - Application type: **Web application**.
   - Name: `StudyPal Web`.
   - **Authorized redirect URIs:** paste the Supabase callback URL from step 1,
     **byte-for-byte, no trailing slash**.
   - **Authorized JavaScript origins:** `http://localhost:5173` (add prod origin
     later).
   - Create → copy the **Client ID** and **Client Secret**.

---

## 4. Supabase — enable the Google provider

Authentication → Providers → **Google**. Field-by-field:

| Field | Value |
|---|---|
| **Enable Sign in with Google** | ON (toggle last, then Save) |
| **Client IDs** | your Google **Web Client ID** (comma-separated list; one is enough) |
| **Client Secret (for OAuth)** | your Google **Client Secret** |
| **Skip nonce checks** | **OFF** — less secure; our web PKCE flow supplies the nonce |
| **Allow users without an email** | **OFF** — Google returns email; the parent account needs it |
| **Callback URL (for OAuth)** | read-only; this is what you registered in Google (§3.3) |

Save.

### 4b. URL Configuration (a SEPARATE page — easy to miss)

Authentication → **URL Configuration**. Direct link:
```
https://supabase.com/dashboard/project/<PROJECT_REF>/auth/url-configuration
```
- **Site URL:** `http://localhost:5173` (dev)
- **Redirect URLs** (allow-list, "Add URL"): `http://localhost:5173` and
  `http://localhost:5173/**`

Why: the SPA calls `signInWithOAuth({ redirectTo: window.location.origin })`.
Supabase only redirects back to allow-listed origins. Missing this = you sign in
at Google but **bounce back to the sign-in screen** instead of landing signed-in.

---

## 5. Frontend + backend env

### Frontend — `web/.env` (gitignored)
From Supabase → Settings → API:
```
VITE_SUPABASE_URL=https://<PROJECT_REF>.supabase.co
VITE_SUPABASE_ANON_KEY=<anon public key>
```
The anon key is a **public** client key (safe in the browser bundle, RLS-gated).
`web/src/lib/supabase.ts` throws on startup if either is missing.

### Backend — stub vs JWKS mode (important)
`get_identity` picks its mode from the API env (loaded from the repo-root `.env`,
`STUDYPAL_` prefix):

- **Stub mode (default local):** no `STUDYPAL_SUPABASE_JWKS_URL` set. The backend
  uses the `X-User-Id` header and **ignores Bearer tokens**. `/health` is public,
  so it works; but a *secured* call (e.g. `POST /assessments/generate`) from the
  signed-in SPA will **401**, because the SPA sends `Bearer <jwt>` and the stub
  isn't looking for it. This is expected locally.
- **JWKS mode (production, and to test real authenticated calls locally):** set
  ```
  STUDYPAL_SUPABASE_JWKS_URL=https://<PROJECT_REF>.supabase.co/auth/v1/.well-known/jwks.json
  STUDYPAL_SUPABASE_JWT_ISS=https://<PROJECT_REF>.supabase.co/auth/v1
  STUDYPAL_SUPABASE_JWT_AUD=authenticated
  ```
  Now the backend verifies the real Supabase JWT (ES256) and derives `user_id`
  from `sub`. Stub mode is disabled outside `dev/test/local/ci`, so a prod deploy
  that forgets these vars fails closed (401) rather than trusting a header.

  Note: setting these in the local `.env` flips the whole local stack into JWKS
  mode, which changes how the stub-based tests behave — that's why it's left off
  by default locally.

---

## 6. Verify

1. Start the stack: web `pnpm dev` (`http://localhost:5173`) and, for a green
   health dot, the API (`uvicorn main:app --port 8000`).
2. Open the app → **Sign in with Google** → Google consent → back to the app.
3. Success = the index page shows your **email + Sign out**, and (API running) a
   green **"API healthy"** dot.

---

## 7. What changes for production (Frankfurt project)

Standing up the `eu-central-1` production project (production-readiness.md item 1)
cascades through everything keyed on the project ref:

- **Google client:** add the prod callback `https://<PROD_REF>.supabase.co/auth/v1/callback`
  as a second Authorized redirect URI (same client, or a separate prod client),
  and add the prod origin to Authorized JavaScript origins.
- **Supabase URL Configuration:** Site URL + Redirect URLs = the **production
  domain** (not localhost).
- **Consent screen:** **publish** it (out of Testing) and set **Authorized
  domains** (`supabase.co` + prod domain). Publishing may trigger Google
  verification if you request sensitive scopes (email/profile usually don't).
- **Frontend `web/.env`:** prod `VITE_SUPABASE_URL` + anon key.
- **Backend `.env`:** JWKS vars pointing at `<PROD_REF>` (§5) → flips the backend
  into real verification.
- Confirm the prod project issues **ES256** asymmetric keys (its
  `/.well-known/jwks.json` is populated).

---

## 8. Troubleshooting (symptoms we actually hit or expect)

| Symptom | Cause | Fix |
|---|---|---|
| Google: **"access blocked / app not verified"** | You're not a Test user | Add your account under Google Auth Platform → Audience → Test users |
| **`redirect_uri_mismatch`** | Google's Authorized redirect URI ≠ Supabase callback | Match `https://<ref>.supabase.co/auth/v1/callback` exactly, no trailing slash |
| Sign in at Google, then **bounce back to sign-in screen** | SPA origin not allow-listed | Add `http://localhost:5173` to Supabase URL Configuration → Redirect URLs |
| **"API unreachable"** (red dot) while signed in | FastAPI backend not running | Start the API; this is unrelated to auth |
| Secured API call returns **401** while signed in | Backend in stub mode | Enable JWKS mode (§5) — expected locally otherwise |
| App **white-screens on load** | `web/.env` missing Supabase URL/anon key | Create `web/.env` (§5) |

---

## 9. Security notes

- **Public (safe in browser / dashboards):** Supabase URL, anon key, project ref,
  the callback URL.
- **Secrets (never in the repo or chat):** Google **Client Secret**, Supabase
  **service-role key**, DB passwords. Enter these only in the Google/Supabase
  dashboards or gitignored env files.
- Children's data is in scope for **POPIA** — auth is parent-only by design;
  keep the consent/region posture in `docs/production-readiness.md` current.
