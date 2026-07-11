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
