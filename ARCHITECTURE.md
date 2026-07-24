# StudyPal — ARCHITECTURE.md
**This document is law for all AI agents working in this repository. Read it fully before writing code. If a task conflicts with this document, stop and ask the architect (the human) — do not improvise around a constraint.**

## 1. What StudyPal is

A paper-first diagnostic learning app for parents. Loop: parent uploads school scope → app generates printable diagnostic test (Variant A) + memorandum → child completes on paper → child enters answers on iPad (photo of paper = proof) → grading (auto + Claude-assist) → parent reviews and approves marks → gap report → targeted study pack → Variant B retest → A-vs-B comparison. The AI does the pedagogy; the app does orchestration, state, and trust. Parent-gated at every step that publishes to the child.

## 2. Locked stack — do not substitute

| Layer | Choice | Notes |
|---|---|---|
| Frontend | Vite + React + TypeScript SPA, PWA | TanStack Router + TanStack Query; vite-plugin-pwa. **No Next.js, no SSR.** |
| Backend | FastAPI monolith (Python 3.12+, Pydantic v2) | ALL business logic lives here: Claude calls, state machine, grading, PDF |
| Database/Auth/Storage | Supabase (managed Postgres) | Treated as disposable managed Postgres — see §4 |
| PDF | WeasyPrint (HTML/CSS → PDF) | ReportLab permitted only for ported legacy templates |
| AI | Anthropic Claude API | Called only from `api/`; never from the frontend |
| Deploy | Single VPS, one docker-compose | FastAPI container + static frontend behind reverse proxy |

Repo layout:
```
/api          FastAPI app (routers/, services/, schemas/, templates/ for WeasyPrint, migrations/)
/web          Vite React SPA (src/routes/, src/components/, src/api/ ← GENERATED client only)
/fixtures     The 5 historical cycles (scope inputs, expected schema outputs, child answers, historical marks)
/ARCHITECTURE.md
docker-compose.yml
```

## 3. The schema is the single source of truth

- Pydantic models in `api/schemas/` define every data shape. The canonical file is `assessment_schema.py`.
- The TypeScript client in `web/src/api/` is **generated from FastAPI's OpenAPI spec** (regenerate on every API change). Agents must never hand-write API types in `web/`.
- Assessment documents are stored as JSONB inside a relational spine: `families → children → subjects → cycles → assessments → submissions → question_marks → gap_reports → study_packs`. Query-relevant fields (variant, state, totals, subject, language) are promoted to columns; the document stays whole in JSONB.
- The app is **subject- and language-agnostic**. `subject` is a freeform string the app never interprets. All subject intelligence lives in question types and prompts. Any PR that adds `if subject == "maths"`-style logic is wrong by definition.

## 4. Supabase exit-strategy constraints (non-negotiable)

1. All schema changes as plain SQL migration files in `api/migrations/` — never dashboard-only changes.
2. Only Auth, Storage, and RLS may be used from Supabase. No Supabase Edge Functions, no Supabase-only extensions in core logic.
3. Multi-tenancy via RLS on `family_id` from day one, even while a single family uses it.
4. Own SMTP provider for auth emails (built-in mailer is rate-limited to 2/hour).
5. Automated off-platform `pg_dump` backup job + weekly keep-alive ping (free tier pauses on inactivity).
6. Exit path = `pg_dump` to self-hosted Postgres + auth swap. Nothing may be built that breaks this.

## 5. Cycle state machine — `(round, phase)`

The cycle is modelled on two **orthogonal, variant-agnostic** axes (rebuilt
2026-07-16; design + rationale in `docs/design/round-phase-architecture.md`):

- **`round: int`** — 1 = diagnostic, 2 = retest, … N. The generic axis.
- **`phase: CyclePhase`** — a generic phase **every round traverses identically**:

```
(round r)  GENERATING → DRAFT_REVIEW → PRINTED → ANSWERS_ENTERED
           → MARKED → REVIEW_MARKS → PUBLISHED → STUDY_PACK
round 1 only: SCOPE_UPLOADED is the initial phase (scope drives round-1 generation)
cross-round:  start_next_round: (r, STUDY_PACK|PUBLISHED) → (r+1, GENERATING)
terminal:     COMPLETE (from the final round's PUBLISHED or STUDY_PACK)
```

- **There is no variant-specific flow.** Round 2 (Variant B) runs through the
  SAME phases and the SAME endpoints as round 1 — the only round-specific things
  are (a) generation *input* (round 1 = scope; round ≥2 = prior round's assessment
  + flagged gaps), (b) the cross-round *comparison*, and (c) round 1's
  `SCOPE_UPLOADED` preamble. Everything else is one generic path parameterized by
  round. **No service/router logic may branch on `variant`** — `variant` is a
  derived display label only; all control flow keys on `(round, phase)`.
- `round` + `phase` live on the `cycles` table; **transitions only via service
  functions in `api/services/cycle.py`** (`advance_phase`, `start_next_round`) —
  never by direct column update elsewhere. The old flat `state` column/enum is a
  deprecated, round-agnostic compat shim (scheduled for removal; nothing branches
  on it).
- Every transition that makes anything visible to the child requires explicit
  parent approval recorded with a timestamp — **per round**, in
  `cycle_round_approvals (cycle_id, round)` (draft-paper approval on
  `DRAFT_REVIEW → PRINTED`; marks publish on `REVIEW_MARKS → PUBLISHED`). Every
  round's generated paper is parent-approved before the child sees it (golden
  rule 8, symmetric across rounds).
- Variant B / round ≥2 is a **regeneration** on the prior round's spec with
  values/contexts changed and flagged gaps deliberately retargeted — **not new
  architecture** (one generation service, per-round input strategy).

## 6. Question types and grading paths

Grading attaches to **question type, never subject**:

| Types | Path |
|---|---|
| mcq, true_false, matching, ordering, fill_blank(number) | AUTO |
| fill_blank(word), short_answer | AUTO_FUZZY (accepted alternatives; low confidence → parent) |
| calculation | Final answer AUTO; method marks CLAUDE_ASSIST from proof photo → parent confirms |
| table_completion, labelling, extended_response | CLAUDE_ASSIST suggestion → parent confirms/edits |

Error taxonomy (gap reports): `concept_gap | format_misread | careless | not_attempted`. Half-marks (0.5) are legal everywhere marks appear.

## 7. Testing rules — fixtures are the merge gate

- `/fixtures` holds the 5 real historical cycles. They are the ground truth.
- CI must stay green: (a) every fixture's scope input generates schema-valid output; (b) every fixture renders to PDF without error; (c) grading replay agrees with historical mark decisions within the tolerance defined per fixture.
- **Agents may not merge a schema change that breaks a fixture** without a parent-approved, dated migration note appended to §10 of this file.

## 8. Conventions

- Python: ruff (lint + format), full type hints, Pydantic v2 idioms, `pytest`. No bare `dict` crossing service boundaries — use models.
- TypeScript: strict mode; components colocated by route; TanStack Query for all server state (no ad-hoc fetch).
- Secrets only via environment variables; `ANTHROPIC_API_KEY` never leaves `api/`. Log token usage on every Claude call.
- Claude calls: one call per generation artefact; grading is one **batched** call per submission. Prompts live in `api/services/prompts/` as versioned files, not inline strings.

## 9. PDF standards (ported from the manual workflow)

A4; header (school/subject/grade/variant/date/duration/total marks); name lines; instructions box; sections with headings and mark totals; per-question marks in square brackets `[3]`; generous working space for calculations; footer page numbers. Memorandum: answers bold, method notes in grey italic, tick allocation, total confirmed at end. Content language of the assessment applies to the document (e.g. Afrikaans papers render in Afrikaans); diagrams as SVG or embedded image-generation prompt boxes.

## 10. Decision log

- **2026-07-11** Supabase replaces Firebase/Firestore (relational Phase-2 analytics; clean pg_dump exit). Exit constraints in §4.
- **2026-07-11** FastAPI monolith over NestJS+Python split (one backend language; Pydantic validates Claude's JSON on the critical path).
- **2026-07-11** Vite+React SPA over Next.js (no SSR/SEO need; backend already FastAPI; faster dev; removes server/client component agent-error class; no Node at runtime).
- **2026-07-11** WeasyPrint over headless Chromium (HTML/CSS templating without a browser dependency; ReportLab retained for proven legacy templates).
- **2026-07-11** No vision-grading of handwriting in MVP (discovery: unreliable). Child inputs answers; photos are proof/audit only.
- **2026-07-12** RLS tenancy via a `family_members` join table, not a JWT custom claim. RLS policies resolve `family_id` by joining the authenticated `user_id` (`family_id IN (SELECT family_id FROM family_members WHERE user_id = <current>)`). Keeps authorization entirely in our Postgres (survives the §4 Supabase-exit swap), avoids a Supabase-hosted token hook and stale-JWT-until-refresh. Request queries run as a non-privileged role carrying the verified JWT; the service-role key is confined to migrations and ops jobs — never the request path.
- **2026-07-12** Forward-only, idempotent SQL migrations (plain SQL, `IF NOT EXISTS`, applied-migrations ledger). No paired down-migrations pre-production; reintroduce reversible migrations only once there is production data that can't be recreated. Consistent with §4 (disposable managed Postgres, pg_dump exit).
- **2026-07-12** Design locked: "Sticker & Stamp" identity (paper canvas, ink-outline sticker components, coral/teal/gold/plum semantics, Fredoka + Atkinson Hyperlegible, teacher-stamp reward language). Deliberate trade-dress distance from Duolingo maintained; no punishment mechanics — wrong answers are diagnostic data ("growing", plum, never red). Tokens: `web/src/styles/tokens.css`. Spec: `docs/DESIGN.md`. uiux agent unblocked.

---

- **2026-07-16 — Cycle model rebuilt on generic `(round, phase)` (architect-approved).** Replaces the flat variant-baked `CycleState` enum with orthogonal `round` + generic `phase` (§5). The backend is now variant-agnostic — one set of capture/grade/review/publish/gap-report/study-pack/child-results endpoints and ONE `PHASE_CONFIG` keyed by phase drive every round; `variant` is a derived display label, never control flow. Round 2 flows through the SAME phases as round 1 (including symmetric parent draft-approval — golden rule 8), with per-round approvals in `cycle_round_approvals (cycle_id, round)` and per-round gap-report/study-pack persistence (`UNIQUE(cycle_id, round)`). The frontend dispatches on `(round, phase)`; `VariantBPage` deleted. Only irreducibly round-specific: generation input strategy (scope vs prior+gaps), the cross-round comparison, and round-1's `SCOPE_UPLOADED` preamble. Design + advisor sign-off (REVISE→met): `docs/design/round-phase-architecture.md`. Shipped in phases P1–P6, each gated on real-Postgres DB-tier + no-DB suites (fixture gate empty pending E1 — existing tests are the regression net). **This SUPERSEDES the five "decisions made in absence" below** (same-cycle-under-`GENERATING_B`, variant-param endpoints, in-memory B gap report, deferred child-B), and retires the deferred debt they logged (B gap report now persists per round; explicit per-round phases now exist). **Deferred hygiene (logged, non-blocking):** drop the shadowed `cycles.state`/enum + single-valued approval columns (nothing branches on them; a forward-only migration); the `retest.py` comparison helper's one round-1-vs-2 branch.

- **2026-07-24 — Child kiosk hardened with a scoped session token (supersedes the "kiosk = accepted risk" note in the diagnostic-loop decision A).** A stateless, short-lived (~4h) HS256 token (`X-Child-Session` header, dedicated secret `STUDYPAL_CHILD_SESSION_SECRET`, pinned `algorithms=["HS256"]`, verified in isolation from the Supabase asymmetric JWKS path) scopes a child device to ONE cycle + child and only the kiosk endpoints (`GET .../capture`, `POST .../submissions`, `GET .../child-results`); every other endpoint rejects it. Its `sub` is the **owning parent's `user_id`**, fed into the UNCHANGED `family_members` RLS join — `cycle_id`/`child_id`/`family_id` in the token are API-layer assertions checked against the server-resolved cycle/subject (403 on mismatch), **never a DB tenancy claim and no claim-keyed RLS policy** (§10 2026-07-12 preserved; §4 exit clean; no migration). The parent mints it (`POST /cycles/{id}/child-session`), scope inferred from phase (PRINTED → `capture`; published + child-visible round → `results` — a separate post-publish grant, never a stretched capture token). Advisor security review REVISE→all must-fixes met. **v1 scope:** same-device handoff; cross-device (QR/link) and DB-backed revocation deferred.

---

### Decisions made in absence — SUPERSEDED 2026-07-16 by the `(round, phase)` rebuild above (kept as history)

*(Autonomous `/goal` session, architect away; decided by the driver with a read-only advisor review — verdict REVISE→conditions met. These described the intermediate variant-parameterized approach that the architect then chose to replace with the generic `(round, phase)` model above.)*

- **2026-07-16 — Variant B is same-cycle; §5 state machine kept as-is (NO new enum states).** Variant B reuses the one `GENERATING_B` state for its whole capture→mark→review sub-loop (sub-phase inferred from data presence), per §5 line "Variant B is a regeneration call … not new architecture." Rejected alternative: adding explicit `*_B` phase states — that would be a §5 enumeration change requiring architect sign-off, which an in-absence session must not make. Two service fns added (`advance_to_generating_b`, `advance_to_cycle_complete`); the `_ALLOWED` edges already existed.
- **2026-07-16 — capture/grade/review are ONE variant-parameterized set of endpoints; `variant` is an optional query param defaulting to `"A"`.** *(Superseded the initial "dedicated `variant_b.py` endpoints" cut on the architect's call — a per-variant-file structure implied a `variant_c.py`-per-variant convention and duplicated 5 endpoints.)* The shared capture/grade/review endpoints keep their A URLs + operation_ids unchanged (so A behaviour is provably identical — the existing A tests pass untouched); Variant B calls them with `?variant=B`. Per-variant phase rules (legal states + on-success state advance) live in a single table `api/services/phase.py::PHASE_CONFIG` (variant is cycle-*phase*, not subject — this is NOT the golden-rule-4 `if subject ==` ban; it is data-driven, never scattered `if variant ==`). The B-specific 3 endpoints (`generateVariantB`, `getAbComparison`, `completeCycle`) live in `api/routers/retest.py`. Adding a hypothetical Variant C is a new `PHASE_CONFIG` row, zero new files.
- **2026-07-16 — A's published marks are protected by an explicit universal write guard, not by endpoint isolation.** The trade the above refactor makes: instead of "A's paths are physically separate so B can't touch them," every write (submit/grade/review-PATCH) checks `PHASE_CONFIG[variant].is_published(cycle)` and returns 409 if that variant's marks are already published (A: `marks_published_at is not None`; B: never, in v1). This is belt-and-suspenders on top of the state guards and is regression-tested (`test_variant_b.py::TestPublishedImmutabilityGuard`).
- **2026-07-16 — `question_marks` cycle queries are now variant-explicit, never recency-inferred.** `list_for_cycle(cycle_id, variant)` and `get_submission_id_for_cycle(cycle_id, variant)` take an explicit `variant`; every existing A caller passes `"A"`. An A+B coexistence test proves A and B marks never bleed together. No RLS/tenancy change (family_id still resolved via the `family_members` join).
- **2026-07-16 — B's gap report is derived in-memory, NOT persisted → no migration.** Marks are the source of truth and `derive_gap_report` is pure, so B's gap report is deterministically reconstructable on every `GET …/comparison`. This avoids a `UNIQUE(cycle_id)`-touching migration on the live `gap_reports` table. The A/B comparison (`derive_ab_comparison`) matches gaps on `gap_tags` (B has new question_ids), partitioning into closed / persisting / new.
- **2026-07-16 — Child-facing Variant B results deferred.** Nothing in the B sub-loop is child-visible in v1, so golden rule 8 is not triggered by any B transition (`CYCLE_COMPLETE` publishes nothing new). The A/B comparison is parent-facing; B mark-review does NOT reuse the publish gate (avoids a `marks_published_at` collision).

**Deferred debt logged for the architect (not blocking):** (a) storing B's gap report for symmetry/auditability would need a `variant` column + `UNIQUE(cycle_id, variant)` migration — deliberately not done in-absence; (b) explicit B-phase `*_B` states remain a possible future §5 refinement if implicit-sub-phase auditability becomes a concern.
