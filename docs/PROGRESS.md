# StudyPal — Progress Log

Dated entries appended after each working session: what was done, verification
results (lint/tests/fixtures), what's next, and any `ARCHITECT DECISION NEEDED` blockers.

Task source of truth: docs/studypal-build-plan.md

---

## 2026-07-11 — BOOTSTRAP milestone (credential-free Week 1 slice)

Orchestrator-driven; `api/` work by the backend agent, `web/` work by the
frontend agent, root tooling + verification by the orchestrator.

**What shipped**
- **Monorepo scaffold.** `api/` (FastAPI, Python 3.12+, Pydantic v2, uv-managed)
  and `web/` (Vite + React + TS strict, TanStack Router + Query, vite-plugin-pwa,
  pnpm). Root `Makefile` (`dev`, `test`, `lint`, `codegen`; `migrate` = stub),
  `docker-compose.yml` (api + web dev servers, hot reload, **no `.env` required**),
  and `.github/workflows/ci.yml` (api lint+test, web lint+build, codegen-drift guard).
- **api/**: `GET /health` → `HealthStatus`; `POST /assessments/validate` →
  `ValidationResult` (200 with structured issues, valid or not), wiring the
  existing `assessment_schema.py` (left untouched). Persistence behind an
  `AssessmentRepository` **Protocol** with an **in-memory** impl only — no DB
  driver, no Supabase, no auth; designed so a Postgres impl drops in by changing
  only `dependencies.py`. pytest: 5 tests (health, validate valid+invalid, repo).
- **web/**: PWA landing page (app name + tagline) with a live API health
  indicator fetched through the **generated** `@hey-api/openapi-ts` client
  (`getHealth`) — no hand-written API types. PWA off in dev; dev proxy makes
  health calls same-origin.
- **make codegen** end to end: FastAPI OpenAPI → `web/src/api/` — verified
  idempotent (identical hashes across runs; zero manual edits).

**Verification**
- `make lint` — clean (ruff + `ruff format --check` + mypy strict on api; tsc
  strict + eslint on web).
- `make test` — 5 passed.
- `make codegen` — regenerates with zero drift.
- Runtime smoke (docker daemon unavailable in this sandbox, so servers run
  directly instead of via `make dev` / docker-compose, which is configured but
  unexercised here): `uvicorn main:app` → `/docs` 200, `/health` 200,
  `/assessments/validate` returns `valid:true` for a good assessment and
  `valid:false` + total-mismatch issue for a bad one. `pnpm dev` → landing page
  200; `/health` through the Vite proxy returns the healthy payload; headless
  Chromium render shows "StudyPal" + green **"API healthy"** indicator
  (`rgb(39,174,96)`), no console errors.
- **Fixtures**: untouched and unexercised — no fixture gates run in this slice
  (no generation/grading/PDF yet).

**What's next / deferred Week 1 remainder**
- `ARCHITECT DECISION NEEDED`: none blocking. Remaining Week 1 work is
  credential-gated and awaits secrets/accounts:
  - **Supabase wiring** — project, plain-SQL migrations from commit one, RLS
    multi-tenant model on `family_id`, Auth (Google Sign-In) into the SPA shell,
    own SMTP, keep-alive + off-platform `pg_dump` backup. Then swap the in-memory
    repository for a Postgres impl behind the same Protocol.
  - **Claude generation** — generation endpoint calling Claude, versioned prompt
    v1 under `api/services/prompts/`, token logging per call.
  - **Fixture hardening** — Maths (calculation/method marks) + Afrikaans
    (non-English content language) as the first schema stressors.
- Single-container reverse proxy deferred to deploy.

---

## 2026-07-12 — PR-2: RLS on real Supabase + CI gate + JWKS auth

Context: PR-1 (persistence + RLS + generation, credential-free) landed earlier
(commit `f152b7f`); the Supabase project was created and `.env` wired. This
session took PR-2 from "db is wired" to real tenant isolation + real auth.

**What shipped**
- **RLS adapted to real Supabase.** `0002_rls.sql` made portable: the local
  auth-emulation preamble (`CREATE ROLE authenticated`, `auth` schema,
  `auth.uid()`, auth grants) is now guarded behind `IF NOT EXISTS (auth.uid())`.
  Real Supabase supplies those (`auth.uid()` owned by `supabase_auth_admin`,
  which our `postgres` role cannot `CREATE OR REPLACE`), so on Supabase the
  preamble is skipped and only the RLS grants + `family_members`-join policies
  apply; a bare local Postgres still gets the emulation. Idempotent, forward-only
  (§10 R4). No `0003` needed — 0002 was unapplied on the real target and would
  otherwise error mid-`make migrate`.
- **Migrations applied to the live project.** Ledger `0001_spine,0002_rls`;
  `auth.uid()` untouched; RLS `ENABLE`+`FORCE` on all 7 tenant tables, 25
  policies. Reconnaissance confirmed `postgres` has `bypassrls=true` (owner path)
  and `authenticated` has `bypassrls=false` — the §10 R1 model exactly.
- **`make migrate` DSN precedence** (`MIGRATE_DSN > STUDYPAL_DB_DSN > DB_DSN >
  local default`) so it uses the session pooler by default. The Supabase DIRECT
  host (`db.<ref>.supabase.co`) is **IPv6-only** and unreachable from IPv4-only
  networks; the session pooler (`:5432`, session mode) is the DDL-capable IPv4
  path. Documented in Makefile + `.env(.example)`.
- **CI RLS gate.** The `api` job gained a `postgres:17` service + job-level
  `STUDYPAL_DB_DSN` + a `make migrate` step, so the 15-test RLS isolation tier
  runs on every push instead of skipping (bare Postgres → 0002's emulation path,
  mirroring the docker `db` service).
- **Real JWKS JWT verification (backend).** Dual-mode `get_identity`: when
  `STUDYPAL_SUPABASE_JWKS_URL` is set it requires a `Bearer` JWT and verifies
  sig/iss/aud/exp against Supabase's JWKS (project uses **ES256**), taking
  `user_id` from the verified `sub`; otherwise the `X-User-Id` stub applies
  (local/test/credential-free). Asymmetric-only (HS* refused → no alg-confusion),
  fail-closed on every error, prod-misconfig guard (stub disabled outside
  dev/test/local/ci). `Identity.family_id` made optional (token carries no
  family; RLS resolves tenancy from `user_id`). Added `pyjwt[crypto]`.

**Verification**
- `make lint` — clean (ruff + `ruff format --check` + mypy strict on api; tsc
  strict + eslint on web).
- `make test` — 94 passed, 15 RLS skipped locally (no DB in that run),
  2 fixture-gate deselected. New `test_auth_jwks.py`: valid / expired /
  wrong-iss / wrong-aud / bad-sig / alg-confusion / missing-exp / non-uuid-sub /
  unresolvable-key / dual-mode wiring / prod-guard / end-to-end through
  `/assessments/generate`.
- **RLS tier against real Supabase: 15/15 green** (cross-tenant isolation both
  ways, deny-by-default, non-privileged role can't DROP/CREATE, promoted-column
  round-trip, `family_members` self-view). Test data cleaned up; DB left empty.
- **CI path reproduced locally** against an ephemeral `postgres:17`: emulation
  applied, 25 policies, 15/15 green.
- **Real-JWKS smoke**: `PyJWKClient` fetches the live ES256 signing key.
- `make codegen` — regenerated `web/src/api/sdk.gen.ts` (generate endpoint now
  advertises Bearer + x-user-id); zero further drift.
- **Fixtures**: untouched/unexercised (no generation/grading/PDF changes here).

**What's next / deferred**
- `ARCHITECT DECISION NEEDED`: none blocking. Confirm the Supabase project
  **region** was chosen deliberately for SA child data (POPIA).
- **SPA Google Sign-In shell** (frontend half of auth B) — needed to mint a real
  Supabase token and prove full end-to-end token verification.
- **Live generation (C4)** — set `STUDYPAL_ANTHROPIC_API_KEY`, real `ClaudeClient`
  alongside `FakeClaude`, one live smoke generation, token logging.
- **Fixtures (E1)** — transcribe Maths + Afrikaans artefacts → expected
  `Assessment` JSON; flip the real fixture-replay gate on.
- **Ops (D, deferrable)** — SMTP + keep-alive + off-platform `pg_dump`.
- Enabling JWKS locally is a deploy step: `.env` is intentionally left in stub
  mode (setting the JWKS vars flips get_settings into JWKS mode and would break
  the stub tests).
