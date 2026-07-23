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

---

## 2026-07-16 (session 2) — Variant-agnostic cycle rebuild: `(round, phase)` model

Architect-directed. The Variant B work (above) shipped, then the architect flagged the
remaining variant-based code as a scalability smell ("a `variant_c.py` tomorrow"; the
frontend still forked via `VariantBPage`). Full FE+BE audit → root cause = an **asymmetric
state machine** (round 1's capture→grade→review were real states; round 2's were crammed
into one `GENERATING_B` state). Decision (architect-approved, advisor-signed REVISE→met,
`docs/design/round-phase-architecture.md`): rebuild the cycle on orthogonal **`round` +
generic `phase`** so every round runs one identical flow.

**Shipped in gated phases (each: `make lint` + no-DB + real-Postgres DB-tier green):**
- **P1** (`9332157`) — schema foundation: `CyclePhase` enum + total `state↔(round,phase)`
  mappings; `cycles.round/phase`; new `cycle_round_approvals (cycle_id, round)` + RLS (closes
  the per-round approval-clobber gap); `gap_reports`/`study_packs`/`assessments` gain `round`
  with `UNIQUE(cycle_id, round)`. Additive; `state` stays the driver. Validated 0001→0013 from
  scratch on real Postgres, idempotent.
- **P2** (`4958fa9`) — generic `advance_phase`/`start_next_round`; all `advance_to_*` become
  delegating wrappers; per-round approval dual-write. Behaviour-preserving.
- **P3** (`0ea7abf`) — collapsed `generate`/`generate_variant_b` into one `_generate(strategy)`
  (Scope vs Retarget round-input strategies).
- **P4-1** (`ab244e4`) — the core collapse: round 2 gets REAL phases (symmetric draft-approval);
  `round_phase_to_state` round-agnostic; `PHASE_CONFIG` re-keyed by phase (A/B rows merge);
  routers guard by phase; retest generate→DRAFT_REVIEW. Round-1 A tests unchanged (regression net);
  `test_variant_b` rewritten to the new flow.
- **P4-2** (`7c718dc`) — gap-report/study-pack/child-results parameterized by round; child-results
  reads the FROZEN per-round `cycle_round_approvals[round].published_visibility` (round 2's publish
  can no longer clobber round 1's contract); round-2 artifacts persist per round.
- **P5** (`4ccf791`) — frontend dispatches on `(round, phase)`; **`VariantBPage` deleted**; one set
  of phase pages for every round; `roundConfig(round)` drives labels/comparison/child-visibility.
  Also fixed a latent round-1 gap (frontend never triggered grade — masked by seeded-cycle testing);
  uiux pass PASS (+ a copy-correctness fix: retest results are parent-only in v1).
- **P6** — home card migrated off `cycle.state` to `(round, phase)` (last variant-baked FE code);
  ARCHITECTURE §5 rewritten to the generic model + §10 ratification (supersedes the in-absence
  variant_b entries).

**Result:** backend is variant-agnostic (`variant` is a derived label, never control flow — one
`PHASE_CONFIG`, one generation service, per-round persistence); a hypothetical round 3 is a
`_PHASE_ALLOWED`/config addition, zero new files/endpoints/pages. Round-1 A behaviour identical
throughout (its tests, unchanged, are the regression proof). Final: no-DB **447 passed**, DB-tier
**487 passed** (only the intentional `fixture_gate` red), web typecheck/lint/build clean, codegen current.

**Process notes / disclosures:**
- **CI had been red since ≥2026-07-13** — prior "green" was local `make test` with the DB tier
  *skipped* (no local Postgres); CI runs the DB tier where stale test harnesses failed
  (`submissions.cycle_id`, bad jsonb literals, a `SET LOCAL jwt.claims` cleared-on-commit). Fixed
  (`83e1852`, test-harness only) → **CI now green** (api+web+codegen). A local Postgres is now
  wired into verification so the DB tier is a real gate.
- **Unintended live migration:** `make migrate` sources `.env` internally (overriding a shell DSN),
  so P1's 0010–0013 applied to the live eu-west-1 project. Additive/non-destructive; it's the schema
  the running server needs. Further live migrations held until the refactor is signed off (use
  `MIGRATE_DSN` to target a DB explicitly).

**Deferred hygiene (logged, non-blocking):** drop the shadowed `cycles.state`/`CycleState` enum +
single-valued approval columns (a forward-only migration; nothing branches on them); tidy the one
round-1-vs-2 branch in `retest.py`'s comparison helper.

**Next:** live browser re-test of the uniform loop (round 1 + round 2 through the same pages) on the
running stack (eu-west-1 already has the P1 schema); then the standing deferrals — live Claude (C4),
Fixtures E1, Supabase Storage, scoped child session token.

---

## 2026-07-23 — Live browser re-test of the uniform loop (findings)

First real in-browser click-through of the `(round, phase)` uniform loop against the running stack
(uvicorn `--reload` on :8000 + Vite on :5173, **live eu-west-1 DB**, stub auth). Driven headless via
the `browse` tool; parent identity seeded by injecting a Supabase session into localStorage for a
freshly bootstrapped stub user (`X-User-Id`, stub-auth mode). Pushed the 6 local P2–P6 commits to
`origin/main` first (architect-approved).

**Round 1 — GREEN end-to-end.** `SCOPE_UPLOADED → GENERATING → DRAFT_REVIEW → PRINTED →
ANSWERS_ENTERED → AUTO_MARKED → PARENT_REVIEW_MARKS → GAP_REPORT (PUBLISHED) → STUDY_PACK`, plus the
child kiosk results view. Verified live: new-cycle + add-child + scope generate (FakeClaude draft),
approve, memo-free kiosk capture (all question types: OptionGrid/blanks/NumberPad+working/TableGrid),
submit + auto-grade (exact-match auto-award, non-match → `needs_review`, never auto-zeros), parent
mark review (half-steps, "left as growing", AI rationale notes), publish (froze the 4-toggle
`published_visibility` snapshot, AI-rationale OFF), gap report (2 mastered teal / 2 growing plum,
**no red**, concept-gap chip, half-marks 4.5/9), study pack (structured items + **valid WeasyPrint
PDF** 17KB + recorded parent approval), and the child results view. **Child-results projection is
clean**: server-filtered from the frozen snapshot, `ai_rationale: null` (toggle was off), **no memo**,
no AI notes — only the child's own answers. `make lint`/`make test` blind spots not exercised here;
this was the live tier the prior sessions flagged as not-yet-run.

**Round 2 (retest) — BLOCKED by two live Postgres-repo bugs.** Clicking "Start Variant B retest"
(`POST /cycles/{id}/variant-b`) fails and strands the cycle at `round 2 / GENERATING` with no round-2
assessment. Both are masked by the in-memory test repo, which is why the 447/487 suites are green.
`ARCHITECT DECISION NEEDED`: fix now (small, scoped) vs schedule.

- **Bug A (404 "No Variant-A assessment found").** `postgres_family.py::update_cycle_state`'s
  `RETURNING` clause omits the `assessments` aggregate that `get_cycle` builds via the `json_agg`
  LEFT JOIN. So `start_next_round` (`services/cycle.py`) returns a `CycleResponse` with
  `assessments=[]`, and `routers/retest.py::generate_variant_b` → `resolve_assessment(cycle,"A")`
  (`services/phase.py:118`, filters `cycle.assessments` by `.variant`) returns `None` → 404. The
  memory repo's `update_cycle_state` does `cycle.model_copy(update=…)`, which **preserves**
  `assessments` → tests never see it. Fix: re-fetch via `get_cycle` after `start_next_round` in the
  router (or populate `assessments` in `update_cycle_state`'s return).
- **Bug B (500 duplicate key).** `postgres.py::AssessmentRepository.save`'s `INSERT INTO assessments`
  never sets `round`. Migration `0013` added `round int NOT NULL DEFAULT 1` + `UNIQUE(cycle_id,
  round)`. Round-2's assessment therefore defaults to `round=1` and collides with round 1 →
  duplicate-key 500 (surfaces once Bug A is fixed). Fix: thread `round` (= `cycle.round`) into the
  assessment save. **Likely same-class risk** for `gap_reports`/`study_packs` saves (also gained
  `round` + `UNIQUE(cycle_id, round)` in `0011`/`0012`) — audit their repo saves before round 2 runs.

**Root theme:** the P1–P6 round refactor added `round` to persistence, but the Postgres repo layer
wasn't fully wired to carry it (assessments-in-RETURNING; round-in-INSERT), and the memory-repo test
tier hides both. **This is exactly the gap the "live smoke not yet run" flag warned about.**

**Other observations (non-blocking):** (a) every API call is 4–8s round-trip to eu-west-1 (remote DB
latency, not a code bug) — Frankfurt region move is already deferred to go-live; (b) a hard deep-link
navigation to a cycle URL can flash `{"detail":"Missing identity header"}` when a request fires before
the auth interceptor attaches `X-User-Id` (transient; recovers on the normal in-app path).

**State left behind:** test family `BrowseTest Family` (stub user `1111…7777`) + one cycle
`a985dcbb-422c-4e95-98c4-0148afbe333c` stranded at `round 2 / GENERATING` in eu-west-1 — useful as a
live repro for the two fixes. No code changed, no migration, nothing committed beyond the P2–P6 push.

**Next:** architect call on fixing Bug A + Bug B (and the gap/study-pack round-save audit) so round 2
runs live, then re-run the round-2 tail (retest generate → draft approve → capture → grade → review →
publish → comparison → complete) in the browser; then the standing deferrals (C4, E1, Storage).

### Same-session addendum — Bugs A + B fixed (architect-approved "fix both now"), round 2 verified live

Backend agent fixed both at the repo layer (no migration; columns already existed). **Not committed**
(awaiting approval).
- **Bug A** — `postgres_family.py::update_cycle_state` **and** `publish_marks` now wrap the UPDATE in a
  CTE + the same `json_agg` LEFT JOIN `get_cycle` uses, so their returned `CycleResponse` carries
  `assessments` (matches the memory repo's `model_copy` semantics). Fixed at the repo root, not with a
  router re-fetch, because several routers return that object directly and would otherwise leak
  `assessments: []` to clients.
- **Bug B** — `postgres.py::AssessmentRepository.save` now `SELECT`s `round` from the owning cycle
  (alongside the `family_id` it already fetched) and threads it into the INSERT + `ON CONFLICT`.
  `variant` stays a display label; `round` is the unique-constraint source of truth.
- **Audit:** `gap_reports`/`study_packs` saves were already correct (explicit `round` param, callers
  pass `round=round_`) — done right in P4-2.
- **New DB-tier tests** `api/tests/test_retest_postgres_repo_bugs.py`, proven regressions (reverting
  each fix reproduces the exact 404 / `UniqueViolation`). Verification: ruff + mypy clean, `pytest`
  **488 passed** (DB tier actually running), fixture_gate still the intentional pre-existing red.

**Round 2 re-run — GREEN end to end.** On the same live eu-west-1 cycle: `POST /variant-b` → 201
(`round 2 / DRAFT_REVIEW`, assessments `['A','B']`, round-2 row persisted at `round=2`), then the
**same** phase pages round 1 uses (`?variant=b`): draft approve → capture (all types) → auto-grade →
mark review → publish (froze round-2 snapshot in `cycle_round_approvals[2]`) → A-vs-B comparison →
**Complete cycle → `CYCLE_COMPLETE / round 2 / COMPLETE`**. The uniform `(round, phase)` loop is now
verified in a real browser across BOTH rounds. Answered round 2 at 9/9 (vs round 1's 4.5/9); the
comparison score card correctly shows `A 4.5/9 → B 9/9`.

**New caveat (non-blocking, FakeClaude limitation).** The A-vs-B comparison's closed/persisting/new
partitioning came back **all-empty** ("0 closed · No comparable areas") despite both round-1 growing
gaps being resolved. Root cause: the comparison matches on `gap_tags`, but **FakeClaude generates
assessment questions with `gap_tags: []`** (`derive_gap_report` passes `question.gap_tags` through —
gap report items show `error_category: "concept_gap"` but `gap_tags: []`, and
`summary.growing_gap_tags: []`). So the partitioning is a no-op on fakes and will stay empty
regardless of performance; only the marks-based score card is meaningful. The comparison unit tests
pass because they construct gap reports with tags directly. **This resolves once C4 (live Claude)
emits real question `gap_tags`** — or add tags to FakeClaude if the comparison needs exercising before
C4. Logged for the architect; not a blocker.

**Updated next:** architect approval to commit the two repo fixes + new test; consider seeding
FakeClaude `gap_tags` so the comparison partitioning is demoable pre-C4; then the standing deferrals
(C4, E1, Storage, scoped child token).

### Same-session addendum 2 — repo fixes committed + FakeClaude gap_tags seeded

- Bug A + Bug B fixes + `test_retest_postgres_repo_bugs.py` **committed and pushed** (`33cfd33`).
- **Comparison caveat resolved:** `FakeClaude` now seeds deterministic, subject-agnostic `gap_tags`
  on round-1 questions (`_seed_deterministic_gap_tags`, scheme `"{type}-{section}{index}"`, e.g.
  `calculation-b1` — derived from structural fields only, never subject/text; golden rule 4). The
  gap-report passthrough and variant-B retarget already propagated tags — round 1 having them was the
  only missing hop, so it now cascades: A-growing tag mastered in B → **closed**, still growing →
  **persisting**, new → **new**. New `test_fakeclaude_gap_tags.py` (4 tests) drives the real
  GenerationService+FakeClaude pipeline (never hand-builds a GapReport), incl. reproducing the exact
  live A 4.5/9 → B 9/9 scenario and asserting `closed` non-empty. Verified: ruff+mypy clean, `pytest`
  **492 passed** (DB tier ran); and confirmed live on the running server — a fresh generate now
  returns questions with non-empty `gap_tags` (was `[]`). Note (architect FYI): the tag scheme keys a
  gap to a fixed structural slot, adequate for FakeClaude's fixed 4-question samples; real Claude
  prompts already do concept-based tagging.
