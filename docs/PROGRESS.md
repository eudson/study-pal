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

---

## 2026-07-12 — Parent view: scope→draft→approve + Settings/Family

Orchestrator-driven; `api/` by the backend agent, `web/` by the frontend +
uiux agents, verification + wiring by the orchestrator. Runs against
**FakeClaude** (live generation C4 still deferred). Local stack in **stub auth**.

**What shipped**
- **Spine CRUD + cycle state machine (`api/`).** RLS-scoped, Pydantic-modelled
  repos/routers for family → child → subject → cycle (previously only
  `/assessments` existed). `api/services/cycle.py` state machine — transitions
  only via service fns (ARCHITECTURE §5); this slice drives
  `SCOPE_UPLOADED → GENERATING_A → PARENT_REVIEWS_DRAFT → APPROVED_PRINTED`, the
  approve transition records `parent_approval_at` (rule 8). Endpoints: bootstrap
  `POST /families` + list; children/subjects/cycles CRUD;
  `POST /cycles/{id}/generate` (wires FakeClaude + advances state);
  `POST /cycles/{id}/approve`; `PATCH /children/{id}`;
  `POST /children/{id}/archive`.
- **Onboarding bootstrap.** New users hit an RLS bootstrap deadlock
  (`families_tenant_insert` needs prior membership; `family_members` has no
  INSERT policy). Solved with a `SECURITY DEFINER` `app_bootstrap_family(...)`
  (migration 0003) — elevated privilege lives in the DB function, never on the
  request path (§10 R1). Stub header relaxed to accept `<user_id>` alone.
- **Per-child settings.** `children.archived_at` + `visibility_defaults` jsonb
  (migration 0005); `list_children` excludes archived. Visibility defaults
  **persist but are not yet consumed** — the Publish gate that reads them is the
  next slice (architect decision, 2026-07-12).
- **Parent UI (`web/`, `data-mode="parent"`).** Home (empty/active), New cycle
  (child + freeform subject + language, no `if subject==`), text-first scope,
  Generating, Draft preview + **Approve/PublishGate**, No-printer screen view.
  Settings/Family: family list, Account (sign-out moved here), child profile
  (edit name/grade, visibility toggles, archive-with-confirm `Dialog`), add
  child. Shared primitives: `StickerButton`, `Chip`, `Dialog`. Avatar now opens
  `/settings` (fixed an accidental-sign-out bug). Design ported from the locked
  Sticker & Stamp tokens; baseline reset extracted to `base.css` so `tokens.css`
  stays pure tokens (shadow + backdrop-overlay tokens added deliberately).
- **Migrations 0003–0005** applied to the live eu-west-1 project.

**Bugs found & fixed during live testing**
- `SET LOCAL request.jwt.claims` was cleared on COMMIT, breaking any
  multi-transaction flow (generate does two transitions) → switched to
  session-scoped `set_config(..., false)`; DB-tier regression tests added.
- Child mutations relied on refetch-after-navigate (flaky, list lagged) →
  all four now write the result into the `["children"]` cache directly.
- `CycleResponse.assessments` was raw JSONB → typed as `list[Assessment]`;
  frontend cast removed.

**Verification**
- `make lint` — clean (ruff + `ruff format --check` + mypy strict; tsc + eslint).
- `make test` — 146 passed, 33 DB-tier skipped (no local Docker DB), 2
  fixture-gate deselected. Fixtures untouched (E1 pending-artefacts gate still
  intentionally red).
- `make codegen` — regenerated, idempotent (zero drift).
- **Live end-to-end smoke** on the real eu-west-1 DB (stub auth): bootstrap →
  child → subject → cycle → generate → `PARENT_REVIEWS_DRAFT` → approve →
  `APPROVED_PRINTED`; child edit → archive → list-excludes-archived. All green.

**What's next / deferred**
- **Publish gate + child-visibility (next slice)** — parent Mark review /
  Publish gate (design p9) + child results view that reads `visibility_defaults`
  (ARCHITECTURE §5, rule 8). This makes the persisted toggles functional.
- **Supabase Storage** scope upload (photo/PDF) — text-first for now.
- **Live generation (C4)** — still deferred pending architect discussion.
- **Fixtures (E1)** — transcribe artefacts, flip the replay gate on.
- **Design export** committed at `docs/design/StudyPal Journeys.html` — the
  Claude Design journeys reference for the parent/child screens.

---

## 2026-07-13 — Diagnostic loop: capture → grade → review → publish → gap report

Autonomous orchestrator session (architect away; decisions delegated to **Fable 5**,
who APPROVE'd the plan with 5 deltas — see `[[diagnostic-loop-build]]` memory).
FakeClaude/FakeGrader throughout (live Claude still deferred). Two commits:
`7dd855d` (Phase 1+2), `1abc2c0` (Phase 3+4).

**What shipped (all on `main`, all green)**
- **Phase 1 — child capture** (`data-mode="child"` kiosk mode under the parent
  session; access model A). Memo-free `GET /cycles/{id}/capture` (stripped
  projection — walk-the-JSON exclusion test); guarded `POST /cycles/{id}/submissions`
  (state + child-chain + RLS) → `APPROVED_PRINTED → ANSWERS_ENTERED`. Child app:
  capture flow + all 10 question-type inputs + SkipControl + PhotoProofCapture
  (proof-only, upload deferred) + SubmitCelebration; full sticker energy + star
  chrome. Kiosk mode is a UX convention, NOT a security boundary (parent JWT).
- **Phase 2 — grading engine.** `question_marks` (0006; suggested+final marks,
  grading_path snapshot, needs_review, ai_rationale, matched_alternative,
  error_category; 0.5-step CHECK; Decimal). `services/grading.py` by question type
  (never subject): AUTO / AUTO_FUZZY (normalized-exact-or-parent, never auto-zeros)
  / CLAUDE_ASSIST via FakeGrader (one batched call/submission). `POST /cycles/{id}/grade`
  → `AUTO_MARKED`; `GET /cycles/{id}/marks`.
- **Phase 3 — mark review + publish.** Enriched review payload (child answer +
  memo, parent-only). `PATCH /cycles/{id}/marks/{qid}` (final_marks 0.5 steps,
  error_category, reviewed/overridden). Publish (0007: `marks_published_at` +
  `published_visibility`): guard all final_marks set; freeze visibility snapshot;
  approval-gated `PARENT_REVIEW_MARKS → GAP_REPORT`. UI: p8 Mark review
  (ReviewRow/ConfidenceFlag/MarkEditor/"Left as growing" plum) + p9 Publish gate
  (toggles prefilled from child defaults).
- **Phase 4 — gap report (BACKEND ONLY).** Deterministic `derive_gap_report`
  (mastered = full marks / growing = partial-or-zero; error_category + gap_tags
  passthrough; subject-agnostic). `gap_reports` (0008); `POST/GET /cycles/{id}/gap-report`.
  **No parent UI yet.**

**Verification** — `make lint` clean, `make test` **307 passed** / 36 DB-skipped,
`make codegen` idempotent, web tsc/eslint/build clean. Fixtures untouched (E1 gate
still intentionally red). **Live full-loop smoke green** on eu-west-1: capture
(memo-clean) → submit → grade → review → publish (visibility frozen) → gap report;
final state `GAP_REPORT`. Migrations 0006–0008 applied to the live project.

**Stopped here** (session limit). NOT done: **gap-report UI (p10)**, all of
**study pack (Phase 5)** — `GAP_REPORT → GENERATING_STUDY_PACK → STUDY_PACK_DONE`,
`study_packs` table, FakeStudyPack, p11 UI — and the **child results view** (publish
records the snapshot but no child-facing results screen exists). These are the
next items — see HANDOFF.

**Known limitation (record, don't mistake for enforcement):** child kiosk mode
runs under the parent JWT; server-side guards enforce content-safety, but it is
not a hard security boundary. A scoped child session is a future consideration.

---

## 2026-07-13 (session 2) — Gap report UI + Child results view + Study pack (Phase 5)

Built the next three locked items on top of the diagnostic loop (all on FakeClaude/FakeGrader; live Claude C4 still deferred).

- **1. Gap report UI (p10).** Parent screen `$cycleId.gap-report.tsx` (generate-if-missing
  via GET→404→POST). `GapChip` error-taxonomy component (concept_gap | format_misread |
  careless→"Slip" | not_attempted), whole family in plum/`--growing` — never red. Mastered=teal
  / growing=plum lists; half-marks display correct; score card. Entry from `PublishedPage`.
- **2. Child results view (security).** New `GET /cycles/{id}/child-results` — server-side
  projection from the FROZEN `published_visibility` snapshot; **all four toggles gated**
  (accuracy/effort/growing/ai_rationale → fields omitted when off). Memo structurally excluded
  (new `child_results` schema/service; never imports `render_correct_answer`; uses memo-free
  `render_child_answer`); `error_category`/`gap_tags` excluded too. Derive-if-missing gap report
  in-memory (no persist/transition). Guards: 401 / 404 (RLS other-family) / 409 (pre-publish).
  Full authz matrix tested incl. the critical snapshot-drift test (flip visibility after publish →
  response still follows the frozen snapshot). Kiosk UI `/results/$cycleId` (ResultSummary/StampWall,
  DESIGN §5–6), renders only server-present fields, zero parent routes. Design reviewed by advisor
  (Fable 5) before build — REVISE→APPROVE.
- **3. Study pack (Phase 5).** `GAP_REPORT → GENERATING_STUDY_PACK → STUDY_PACK_DONE` via
  `cycle.py` only. `study_packs` (0009, RLS mirrors 0008) + `FakeStudyPack` (derives strictly from
  growing `gap_tags`, subject-agnostic, one-call-per-artefact §8, separate from rendering). BOTH
  artefacts: first WeasyPrint StudyPack PDF template (§9) + structured on-screen items. Recorded
  parent approval `approved_at` (golden rule 8) before child visibility. Endpoints: generate / get /
  approve / pdf. p11 parent UI (`StudyPackCard`, approve gate, PDF download via authed blob fetch).

**Verification** — `make lint` clean (ruff+format+mypy; tsc+eslint). `make test` **357 passed** /
38 DB-skipped / 2 fixture-gate deselected (E1 still intentionally red, untouched). `make codegen`
idempotent. **Zero new design tokens.** Migration **0009 applied to live eu-west-1**. Backend live
smoke green: server boots on migrated DB, all 4 new endpoint groups mounted, return 401 unauth
(guards active).

**UI click-through smoke (done, via a seeded cycle to GAP_REPORT under the architect's family) caught
3 bugs — all fixed:**
1. **Nested routes never rendered.** `/cycles/$cycleId` had no `<Outlet/>`, so the four child routes
   (gap-report, review, publish, study-pack) changed the URL but kept showing the detail page. (This
   also meant review/publish were only ever driven via API before, never in-browser.) Fixed:
   `$cycleId.tsx` is now a layout that renders `<Outlet/>` when a child route is active.
2. **Generate-if-missing broken on both gap-report and study-pack routes.** The 404 check read
   `res.error.status` (the FastAPI `{detail}` body — no `.status`) instead of `res.response.status`,
   so it threw "Failed to load" instead of POSTing generate. (Gap report only appeared to work because
   the smoke pre-seeded its report via API.) Fixed to `res.response?.status === 404`.
3. **Body text rendered as Times serif.** `base.css` set no global `font-family`, so body copy fell
   back to the UA serif; only elements explicitly setting `--font-display` looked right. Fixed:
   `body { font-family: var(--font-body) }`. A uiux sweep then corrected 3 chips to `--font-display`
   and a Fredoka faux-bold weight; confirmed no stray font names outside tokens.css.

Verified live: gap report (mastered/growing + GapChips), child-results kiosk, study pack (card,
PDF 17.5 KB, approve). Re-verified green after fixes (lint clean, 357 passed). Font system unchanged —
architect confirmed keeping the locked two-font DESIGN §3 (Fredoka display + Atkinson body).

**New deps (flagged):** `weasyprint>=62`, `jinja2>=3.1` (first WeasyPrint usage; WeasyPrint is the
locked §2 PDF choice). **Future leak note:** `StudyPackItem.answer`/`worked_example` are parent-
reference — the deferred child on-screen practice view MUST strip them server-side like child-results.
**Token flag:** `StampWall.stampLabel` hardcodes `14px` (no size token) — add `--fs-stamp-label`?

**Next:** Variant B retest + A/B comparison (loop tail); live Claude (C4); Fixtures E1; Supabase
Storage for photos/scope; scoped child session token.

---

## 2026-07-16 — Variant B retest + A/B comparison (Week 6 loop tail — CLOSES THE LOOP)

Autonomous `/goal` session (architect away). Driver-orchestrated; backend by the backend
agent, `web/` by the frontend + uiux agents, planning/verification/decisions by the driver
with a read-only **advisor** review before build (verdict REVISE → all conditions met).
FakeClaude/FakeGrader throughout (live Claude C4 still deferred). Decisions logged in
ARCHITECTURE §10 "Decisions made in absence — pending review".

**Assessment first.** Baseline confirmed green (357 passed). Triaged the remaining work: the
one large *autonomously buildable* item was the Variant B tail (the transition edges
`STUDY_PACK_DONE → GENERATING_B → CYCLE_COMPLETE` existed in `_ALLOWED` but had no service
fns, router, generation, or UI). Live Claude (C4) and Fixtures E1 confirmed **blocked on the
architect** (placeholder API key; empty `fixtures/*/inputs` — no source artefacts). Supabase
Storage + scoped child session token remain deferred.

**What shipped (all on `main`-to-be, all green):**
- **Backend (`api/`).** Two cycle service fns (`advance_to_generating_b`,
  `advance_to_cycle_complete`) — §5 state machine UNCHANGED (no new enum states; B's whole
  capture→mark→review sub-loop runs under the single `GENERATING_B` state, sub-phase inferred
  from data presence). `GenerationService.generate_variant_b(VariantBRequest)` on FakeClaude +
  versioned prompt `generate_variant_b_v1.md` → a `variant="B"` assessment (structure preserved,
  all values changed, `gap_tags` retargeted), stored as a SECOND assessment row (no migration —
  `assessments` already supports 2 variants/cycle). Pure `derive_ab_comparison(gap_a, gap_b)`
  + `ABComparison` schema, matching on `gap_tags` (B has new qids) → closed / persisting / new.
  New `routers/variant_b.py` (8 endpoints: generate / capture / submit / grade / marks / review /
  comparison / complete) — dedicated B endpoints reuse the pure services; the Variant-A endpoints
  are byte-for-byte unchanged, so B cannot corrupt A's *published* marks by construction.
  `question_marks` cycle queries are now **variant-explicit** (`list_for_cycle(cycle_id, variant)`,
  `get_submission_id_for_cycle(cycle_id, variant)`) — never recency-inferred; every A caller passes
  `"A"`. B's gap report is derived **in-memory** (not stored → no `gap_reports` migration).
- **Frontend (`web/`).** `make codegen` (8 new SDK fns + `ABComparison`, idempotent/hash-stable).
  Variant B capture + mark-review **reuse the Weeks 3–5 components UNCHANGED** via a `?variant=a|b`
  search param on the existing `capture/$cycleId.tsx` and `$cycleId.review.tsx` (defaults to `a` —
  every existing A link behaves identically; leaf question/mark components untouched). New
  `$cycleId.comparison.tsx` mirrors the gap-report screen: Closed (teal/`--success`), Still growing
  + New (plum/`--growing`, never red), an A→B score card, and a "Complete cycle" gate →
  `CYCLE_COMPLETE`. Dispatcher branches added for `STUDY_PACK_DONE` ("Start Variant B retest"),
  `GENERATING_B` (B sub-flow menu), `CYCLE_COMPLETE` (read-only comparison).
- **uiux conformance sweep: PASS, zero fixes** — no red in diagnostic data, tokens-only
  (**zero new tokens**), correct Fredoka/Atkinson typography, visually consistent with gap-report.

**Verification** — `make lint` clean (ruff + format + mypy on `api`; tsc + eslint on `web`);
`make test` **378 passed** / 38 DB-skipped / 2 fixture-gate deselected (E1 still intentionally red,
untouched); `make codegen` idempotent (hash-verified); `pnpm build` clean. New tests incl. the
**A+B coexistence gate** (advisor guardrail: A and B marks provably never bleed together),
comparison partitioning (incl. half-marks), generation determinism/gap-tag propagation, and full
variant_b router authz/state/happy-path (generate → capture → submit → grade → review → comparison
→ complete → `CYCLE_COMPLETE`). **No migration** this session. Not yet run: live Supabase smoke /
in-browser click-through (prior sessions did these against eu-west-1; this session verified via the
in-memory + gate tiers only — flagged for the architect's live pass).

**Deferred debt (logged, non-blocking, in ARCHITECTURE §10):** (a) storing B's gap report would
need a `variant` column + `UNIQUE(cycle_id, variant)` migration; (b) explicit B-phase `*_B` states
as a possible future §5 refinement. **Carried token flag (unchanged):** `StampWall.stampLabel`
hardcodes `14px` — candidate `--fs-stamp-label`.

**Milestone:** the full diagnostic loop is now app-orchestrated end-to-end,
`SCOPE_UPLOADED → CYCLE_COMPLETE`, on FakeClaude — build-plan Week 6 milestone met.

**Follow-up refactor (same session, architect-directed) — variant-parameterized endpoints.**
The initial cut used dedicated `variant_b.py` endpoints (isolation-by-separate-endpoints). The
architect flagged the per-variant-file structure as a smell (implies `variant_c.py` tomorrow) and
directed a full refactor: capture/grade/review are now ONE variant-parameterized set — `variant`
is an optional query param defaulting to `"A"`, so A's URLs/operation_ids/behaviour are unchanged
(the existing A tests pass **untouched** — the regression proof), and B calls the same endpoints
with `?variant=B`. Per-variant phase rules (legal states + on-success advance + published
predicate) live in one table `api/services/phase.py::PHASE_CONFIG`. The 5 duplicated B endpoints
were deleted; `variant_b.py → retest.py` keeps only the B-specific 3 (`generateVariantB`,
`getAbComparison`, `completeCycle`). The old isolation-safety property is replaced by an explicit
**published-immutability write guard** (`is_published(cycle)` → 409 on any write to a published
variant), regression-tested. Frontend collapsed its A/B SDK switch to single parameterized calls.
A hypothetical Variant C is now a new `PHASE_CONFIG` row, zero new files. Verified: `make lint`
clean, `make test` **379 passed** (+1 published-immutability test), codegen idempotent, `pnpm build`
clean. Logged in ARCHITECTURE §10.

**Next (all blocked-on-architect or deferred):** live Claude (C4 — needs real API key + go-ahead);
Fixtures E1 (needs the 5 historical source artefacts); Supabase Storage for photos/scope; scoped
child session token; a live eu-west-1 smoke + in-browser click-through of the Variant B tail.
