# StudyPal — Production / Go-Live Prerequisites

Cross-cutting items that must be done **before go-live** but are deliberately
deferred during MVP build. The week-by-week feature work lives in
`studypal-build-plan.md`; this file is the checklist that gates flipping the
switch to a real, non-throwaway deployment. Keep it current as items land.

Status legend: ⬜ not started · 🟡 in progress · ✅ done.

---

## 1. ⬜ Supabase project region → Frankfurt (`eu-central-1`)

**What.** The working MVP project is in `eu-west-1` (Ireland) — chosen for
proximity during bootstrap, not deliberately. For go-live, provision the
production Supabase project in **Central EU (Frankfurt) `eu-central-1`** and
migrate onto it.

**Why.** Marginally better latency routing to South African users than Ireland,
and a cleaner EU-data posture for **POPIA** (SA child data). Region is fixed at
project creation — it cannot be changed in place — so this means a *new project*
+ migrate. Supabase does **not** offer AWS Cape Town (`af-south-1`); true
in-country residency would require self-hosting (the §4 `pg_dump` exit keeps
that door open).

**How (painless while the DB holds no real data).**
1. Create a new project in `eu-central-1`.
2. `make migrate` against it (session pooler DSN — direct host is IPv6-only).
3. Run the RLS isolation tier against it → confirm tenant isolation holds.
4. Re-point `.env` (`DB_DSN`, `STUDYPAL_DB_DSN`) and the frontend `web/.env`
   (`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`) at the new project ref.
5. Decommission the old `eu-west-1` project once the new one is verified.

**Note.** The new project ref changes the JWKS URL / issuer (item 2) and the
Google OAuth callback URL (item 3) — update both to the new ref.

## 2. ⬜ Flip backend into JWKS auth mode

**What.** Set `STUDYPAL_SUPABASE_JWKS_URL`, `STUDYPAL_SUPABASE_JWT_ISS`, and
`STUDYPAL_SUPABASE_JWT_AUD=authenticated` (in the API `.env`) to the production
project. This switches `get_identity` from the `X-User-Id` stub to real Bearer
JWT verification.

**Why / safety.** The stub is disabled outside `dev/test/local/ci`, so a prod
deploy that forgets these vars **fails closed** (401) rather than trusting a
header. Confirm the prod project issues **ES256** asymmetric keys (verify its
`/.well-known/jwks.json` is populated). Leaving `.env` without these keeps local
in stub mode intentionally.

## 3. ⬜ Google OAuth provider

**What.** Create a Google Cloud OAuth **Web** client; set its authorized
redirect URI to `https://<PROD_REF>.supabase.co/auth/v1/callback` and JS origins
to the app origin(s). Enable the Google provider in Supabase → Authentication →
Providers with the client ID/secret. Set Site URL + allow-listed Redirect URLs
(the SPA redirects to its own origin) in Authentication → URL Configuration.

**Why.** The sign-in shell is built and provider-agnostic on the backend, but the
button only works once Google is configured. Add email/magic-link later (needs
item 4).

## 4. ⬜ Own SMTP provider (ARCHITECTURE §4.4)

**What.** Configure a real SMTP provider for Supabase Auth emails.

**Why.** The built-in mailer is rate-limited to 2/hour — unusable for signup
confirmations / magic links / password resets at any real volume.

## 5. ⬜ Backups + keep-alive (ARCHITECTURE §4.5)

**What.** Automated **off-platform `pg_dump`** backup job + a **weekly keep-alive
ping**.

**Why.** The free tier pauses on inactivity; and the exit strategy (§4.6) is
`pg_dump` → self-hosted Postgres. Nothing may be built that breaks this.

## 6. ⬜ POPIA / child-data compliance

**What.** Document the lawful basis for processing children's *special personal
information*, parental consent capture (the app is parent-gated by design), and —
if any data rests outside SA — the §72 cross-border-transfer basis. Confirm the
final region decision (item 1) is deliberate and recorded.

## 7. ⬜ Deploy shape (ARCHITECTURE §2)

**What.** Single VPS, one `docker-compose`: FastAPI container + static frontend
behind a reverse proxy. Wire real secrets via env only.

## 8. ⬜ CI hardening

**What.** Bump GitHub Actions off deprecated Node 20 (`actions/checkout@v4`,
`actions/setup-node@v4`, `astral-sh/setup-uv@v5`, `pnpm/action-setup@v4` are
currently force-run on Node 24). Non-blocking today; do before go-live.

---

_Last updated 2026-07-12. Region switch deferred at the architect's direction —
recorded here as a go-live prerequisite rather than done during MVP build._
