# StudyPal — MVP Scope v1
*Synthesised from 5 manual discovery cycles (Maths, Natural Sciences, Social Sciences, English, Music) run May–July 2026. Decisions confirmed: full diagnostic loop in MVP; subject- and language-agnostic from day one.*

---

## 1. Product definition

StudyPal orchestrates a paper-first diagnostic learning cycle: a parent uploads the school's scope, the app generates a printable diagnostic test, the child completes it on paper, answers are captured and graded with the parent in the loop, gaps are analysed, a targeted study pack is generated, and a structurally identical Variant B retest measures improvement. The AI does the pedagogy; the app does the orchestration, state, and trust.

## 2. Discovery findings → design principles

| # | Finding (from the 5 runs) | Design principle |
|---|---|---|
| 1 | Scope-photos → test + memo generation worked reliably every cycle | Generation is the centrepiece; invest prompt/schema effort here |
| 2 | Grading handwriting photos was unreliable (Maths ≈ estimate; crossed-out work, blanks) | Child **inputs** answers; photo is proof/audit only. No vision-grading in MVP |
| 3 | Method marks, half-marks per cell, "right word wrong slot" errors | Assessments must be generated as **structured data with mark rules**, PDF rendered from it |
| 4 | Parent independently cross-checked AI marking | Parent review screen shows per-question rationale, editable marks, explicit approve step |
| 5 | Measurable gains came from the A → study pack → B cycle | The **cycle**, not the single test, is the unit of value |
| 6 | Format misreading (tables, multi-step) recurred across all subjects | Error taxonomy is subject-agnostic: concept gap / format misread / careless / not attempted |
| 7 | Every phase needed explicit "ready to build" confirmations | Cycle state machine with parent-gated transitions replaces chat turn-taking |

## 3. Core architecture decision: subject-agnostic assessment schema

No subject-specific logic anywhere. One generic schema serves every subject and language:

**Assessment** → sections → questions, where each question carries:
- `questionType` (from taxonomy below)
- `answerSchema` (expected answer(s), tolerances, accepted alternatives)
- `markRules` (total marks, method-mark split, half-mark cells, tick allocation)
- `contentLanguage` (en, af, fr, zu … — generation and grading happen in that language; UI language is separate)
- `renderHints` (working space, table layout, diagram reference)

Grading strategy attaches to **question type, not subject**:

| Question type | Grading path |
|---|---|
| MCQ, true/false, matching, ordering, fill-in-number | Auto-mark from child input |
| Fill-in-blank (word), one-word/short answer | Auto-mark with fuzzy/alternatives; low-confidence → parent |
| Calculation with working | Final answer auto; method marks → Claude-assist from proof photo + parent confirm |
| Extended response, comprehension, labelling, creative writing | Claude-assist suggestion + parent confirms/edits |

This taxonomy covered everything across all 5 subjects tested, including Afrikaans language work and Music stave questions — evidence the generic layer holds.

## 4. Cycle state machine (MVP backbone)

`SCOPE_UPLOADED → GENERATING_A → PARENT_REVIEWS_DRAFT → APPROVED_PRINTED → ANSWERS_ENTERED → AUTO_MARKED → PARENT_REVIEW_MARKS → GAP_REPORT → GENERATING_STUDY_PACK → STUDY_PACK_DONE → GENERATING_B → (repeat capture/mark/review) → CYCLE_COMPLETE (A vs B comparison)`

Every transition that publishes anything to the child is parent-gated. Variant B is generated from the *same structured spec* as A with values/contexts changed and flagged gaps deliberately retargeted — this is a regeneration call, not new architecture.

## 5. MVP modules (IN)

1. **Auth & family** — Supabase Auth + Google Sign-In; single family, multi-tenant via row-level security from day one.
2. **Scope intake** — photo/PDF upload of school scope + workbook pages; stored in Supabase Storage; passed to generation.
3. **Generation engine** — backend service → Claude: structured assessment JSON (any subject, any content language) + memorandum with mark rules + gap-retarget logic for Variant B + study pack content.
4. **PDF rendering** — test paper, memorandum, study pack rendered from structured data (established formatting standards: sections, mark brackets, working space, footer pages).
5. **Answer capture (tablet, child)** — per-question input matched to question type (MCQ picker, number pad, text field, matching UI); photo upload of paper as proof; "not attempted" is a first-class state. Includes a **screen-view fallback** for no-printer situations: test displays on the tablet, child works on blank paper, then enters answers as normal.
6. **Grading** — auto path + Claude-assist path per table above; confidence flagging.
7. **Parent review (phone/web)** — per-question mark + rationale, editable, approve/publish gate; child-visibility configuration.
8. **Gap report** — per-cycle error patterns using the generic taxonomy; feeds study pack and Variant B generation.
9. **Study pack** — rule boxes, wrong-vs-correct examples, drills with answers at back, cheat sheet; visual aids as embedded image-generation prompts (no image generation in MVP).
10. **Cycle summary** — A vs B per-gap comparison ("closed / persisting / new").

## 6. Explicitly OUT (later modules)

**Phase 2:** progress tracking across cycles and terms; sibling sharing of variants; printable game cards / drill games module; actual image generation for visual aids; on-device notifications and scheduling (session planner); second-AI cross-check integration.

**Phase 3:** admin portal (Supabase dashboard until then); multi-family onboarding and roles UI; subscriptions + BYOK; teacher/school features; localised UI languages; offline mode; full on-screen *answering* mode (the screen-view + paper-work fallback is in MVP; direct on-screen answering is deferred and will be cheap later since per-question input components already exist).

## 7. Risks & feasibility notes (6-week plan impact)

- **Full loop in 6 weeks is heavy, but tractable:** study pack and Variant B are additional *generation calls* on existing structured data — the expensive builds are answer capture UX and parent review UX. Sequence those first.
- **Structured generation is the critical path.** If Claude's assessment JSON is inconsistent, everything downstream breaks. Week 1–2 should harden the schema + prompts against all 5 historical subjects as test fixtures (they exist — reuse them).
- **Answer capture for young children:** Grade 1 number pad is in the vision; MVP tested with Grade 5. Keep input components per question type composable.
- **Claude-assist grading cost/latency:** batch open-ended questions per submission into one call.

## 8. Stack decisions (revised 11 July 2026)

**Backend: Supabase replaces Firebase/Firestore.** Rationale: Phase 2 analytics (gap trends, progress-over-time, per-question error queries) are relational; Postgres avoids the Firestore→SQL migration later. Assessment documents live as JSONB inside a relational spine (families → children → cycles → assessments → questions → responses → marks).

**Exit-strategy constraints (non-negotiable):** Supabase is treated as disposable managed Postgres. Concretely:
- All schema as plain SQL migration files — no dashboard-only schema changes
- No Supabase-exclusive features beyond Auth, Storage, and RLS; business logic lives in our own service layer, not in platform-specific edge features
- Own SMTP provider for auth emails from day one (built-in mailer is rate-limited)
- Exit path is `pg_dump` to self-hosted Postgres + swapping the auth module — the original fallback plan becomes the migration plan, not the starting point
- Free-tier phase: weekly keep-alive ping (projects pause on inactivity); automated off-platform backups

**Backend logic: FastAPI monolith (Python + Pydantic).** All business logic in one service: Claude calls, cycle state machine, grading engine, PDF generation. Pydantic models are the single source of truth for the assessment schema; FastAPI's OpenAPI spec auto-generates the TypeScript client so frontend and backend types never drift (agent-maintained).

**PDF rendering: WeasyPrint (HTML/CSS → PDF) as primary,** reusing established formatting standards as templates; existing ReportLab code retained where already proven. No headless browser, no second service.

**Frontend: Vite + React + TypeScript SPA (PWA).** TanStack Router + Query; vite-plugin-pwa for iPad home-screen install (child mode) ; two modes in one responsive app: parent (phone/desktop) and child (tablet). No SSR — fully authenticated app, no SEO need; any future marketing site is a separate static page. Builds to static files, so no Node.js at runtime.

**Deployment: single VPS, one docker-compose** (FastAPI container + static frontend behind a reverse proxy) + managed Supabase. Fits the own-infra, low-cost-forever principle; agent-friendly (one repo: `api/` + `web/`, ARCHITECTURE.md at root as the agents' standing constraint document, the 5 historical cycles as CI fixtures).

## 9. Open decisions (non-blocking)

- Whether the parent can edit the generated test before printing (draft-review step) or only regenerate.
- Where memo PDFs live: parent-only by default (assumed yes).
- Variant B timing: immediately after study pack vs parent-scheduled (assumed parent-triggered).
