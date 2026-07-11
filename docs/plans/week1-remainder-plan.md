# Plan — Week 1 Remainder (Persistence, Auth, Generation)

**Status:** LOCKED (autoplan-reviewed 2026-07-12, dual independent review → REVISE → revised → approved)
**Split:** PR-1 (credential-free critical path) · PR-2 (credential/artefact-gated)
**Depends on ARCHITECTURE.md:** §3 (schema SoT), §4 (Supabase exit constraints), §5 (state machine), §8 (conventions), §10 (new decisions D-R1, D-R4)

## Goal
A persisted, RLS-scoped, generation-capable API. A request carrying a `family_id` can
persist assessments isolated to that family; `POST /assessments/generate` turns scope text
into a schema-valid `Assessment` via one Claude call. Real Google Sign-In and live Claude are
a separate, gated PR.

## Decisions ratified (this review)
- **D-R1 → `family_members` join table.** RLS resolves tenancy by joining the authenticated
  `user_id` to `family_id`: `family_id IN (SELECT family_id FROM family_members WHERE user_id = <current>)`.
  Authz lives entirely in our Postgres — survives the §4 Supabase-exit swap, no Supabase-specific
  token hook, no stale-JWT-until-refresh. (→ ARCHITECTURE §10)
- **D-R2 → one error-directed repair retry.** Validate Claude output; on failure feed the
  `ValidationResult.issues` back into a single repair call (hard token cap), then structured
  error. Never ship unvalidated. Repair logged as a distinct second call (§8).
- **D-R3 → fail-loud empty-fixture gate.** `make test-fixtures` must FAIL (not green) when
  `/fixtures` has no artefacts. Generation is gated now by fake-Claude + in-tree samples +
  schema-invariant property tests; real fixture replay gates when E1 lands.
- **D-R4 → forward-only idempotent migrations.** Plain SQL, `IF NOT EXISTS`, a small
  applied-migrations ledger. No paired down-migrations pre-production. (→ ARCHITECTURE §10)

## Non-negotiable invariants (both reviewers; enforce in code + tests)
1. **Request queries run as a non-privileged role carrying the verified JWT** (per-transaction
   `SET LOCAL request.jwt.claims` / role `authenticated`). RLS is defence-in-depth, not the only
   line. **Service-role key is used ONLY for migrations + ops jobs — never on the request path.**
   A single service-role request-path query makes RLS decorative → cross-tenant child-data breach.
2. **JWT is verified before any claim is trusted** — signature (JWKS), `iss`, `aud`, `exp`;
   deny-by-default (401, no DB touch) on any failure.
3. **`family_id` provenance** — server-assigned at signup, stored in `family_members`; if ever
   surfaced in a JWT it lives in `app_metadata` (user-immutable), never `user_metadata`.
4. **Every tenant table:** `family_id uuid NOT NULL`, `ENABLE ROW LEVEL SECURITY`, deny-by-default.
5. **Promoted columns are DERIVED from the validated `Assessment` on write, never independently
   writable.** Round-trip test asserts `column == jsonb->>field`. The model/JSONB is SoT (§3).
6. **Claude output crosses exactly one boundary:** the existing `validation_service.validate_assessment`.
   No second validation path. Service assigns `assessment_id`/`cycle_id` (Claude never picks keys).
7. **Scope text is untrusted data** — fenced/delimited in the prompt, length-capped; output
   size-capped (schema has no upper bound on question count → cost-DoS otherwise).

## PR-1 — Persistence + RLS + Generation (credential-free, ships now)
Sequenced per ARCHITECTURE: schema → migration → service → codegen → tests.

- **A1** `0001_spine.sql` — `families → children → subjects → cycles → assessments → submissions`;
  JSONB `assessment` + promoted columns (variant, subject, language, totals). `family_id NOT NULL`
  everywhere. Lifecycle `state` stays on `cycles` only (§5) — no assessment-level state column.
- **A2** `0002_rls.sql` — `ENABLE ROW LEVEL SECURITY` + deny-by-default + `family_members`-join
  policy on every tenant table. Split from A1 so the RLS enable/policy is auditable per table.
- **A3** `PostgresAssessmentRepository` — satisfies the repository Protocol, but the Protocol/
  dependency is made **request-scoped and tenant-aware**: `get_assessment_repository` receives the
  verified identity and opens a connection with `SET LOCAL` claims. Promoted columns derived on save.
  *(This resolves the "tenant-blind Protocol" gap — the Protocol's tenancy contract is written down;
  identity travels via the injected connection.)*
- **B2 (stub seam)** JWT auth dependency with a **fake/injected `family_id`** for now (real verifier
  in PR-2). Persistence + RLS are fully built and tested behind this seam.
- **C1** `api/services/prompts/generate_assessment.v1.md` (versioned file, no inline strings).
- **C2** `GenerationService` — builds prompt, calls Claude behind a `ClaudeClient` interface with a
  **deterministic `FakeClaude`** (returns the in-tree Maths/Afrikaans samples), routes output through
  `validate_assessment`, one error-directed repair retry, per-call token logging.
- **C3** `POST /assessments/generate` + `make codegen`.
- **migrate** target — apply `api/migrations/*.sql` in order, idempotent, via the migration role.
- **Tests (the point of PR-1):**
  - **Local-Postgres RLS tier (docker):** apply migrations, set claims, assert **family B cannot
    read family A**, assert deny-by-default, assert service-role is not on the request path.
  - Generation with `FakeClaude`: valid → round-trips; invalid → structured error after one retry;
    injected-instruction JSON → schema rejects.
  - Promoted-column/JSONB consistency round-trip.
  - `make test-fixtures` fails loud on empty `/fixtures`.

**PR-1 DoD:** `make lint`/`test` green incl. the RLS isolation tier; `make codegen` zero drift;
`make migrate` applies to local Postgres; **negative tenant-isolation test passes**; dated
`docs/PROGRESS.md` entry.

## PR-2 — Real Auth + Live Generation + Ops (gated on your inputs)
- **B1** Supabase project + Google provider; **B2-real** JWKS JWT verification replacing the stub;
  **B3** SPA Sign-In screen + session + authed client.
- **A4** run migrations against real Supabase; integration-test the repo live.
- **C4** wire `ANTHROPIC_API_KEY`; one live smoke generation.
- **D1** own SMTP; keep-alive ping + off-platform `pg_dump` (scripts written in PR-1, provisioned here).
- **E1 (architect)** transcribe Maths + Afrikaans fixture artefacts → expected `Assessment` JSON
  (raw photos stay local/gitignored). **E2** real fixture-replay gate switches on.

## Preconditions for PR-2 (you provide)
Supabase project (URL + keys) · Google OAuth client · `ANTHROPIC_API_KEY` · SMTP creds ·
Maths + Afrikaans fixture artefacts.

## Deferred / explicitly out
Live SMTP + real backups until a second user exists (scripts kept, §4-conformant). Only
`assessments` gets a typed repository this week; other spine tables are DDL + RLS only (no
bare-dict access). PDF (W2), capture (W3), grading (W4) unchanged.
