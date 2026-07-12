# StudyPal — 6-Week Build Plan v3
*Reworked against MVP Scope v1 with the locked stack: Vite/React SPA (PWA) + FastAPI/Pydantic monolith + Supabase (Postgres/Auth/Storage) + Claude API + WeasyPrint. 100% agent-written code; parent drives architecture.*

**Standing assets:**
- The 5 historical cycles (Maths, NS, SS, English, Music) = CI fixtures. Real scopes in, real child answers, real mark decisions. Used every week.
- `ARCHITECTURE.md` at repo root = the agents' standing constraint document (stack rules, exit-strategy constraints, schema conventions). Written in Week 1, updated on every architectural decision.

---

## Week 1 — Foundations + Generation Schema v1
**Goal: scope in → valid structured assessment out, via API.**
- ✅ (2026-07-11, BOOTSTRAP) Monorepo scaffold: `api/` (FastAPI) + `web/` (Vite React TS), docker-compose. *(Reverse proxy deferred — dev uses Vite's proxy; single-container reverse proxy lands with deploy.)*
- Supabase project: schema as plain SQL migration files from commit one; RLS multi-tenant model; Auth (Google Sign-In) wired into the SPA shell; own SMTP configured
- Pydantic assessment schema v1: sections, question types, answerSchema, markRules, contentLanguage, renderHints
- Generation endpoint calling Claude; prompt v1; ✅ (2026-07-11, BOOTSTRAP) OpenAPI → generated TS client pipeline. *(Generation/Claude call + prompt v1 deferred; the FastAPI OpenAPI → `@hey-api/openapi-ts` → `web/src/api/` pipeline is live via `make codegen`, with a `POST /assessments/validate` endpoint wiring up `assessment_schema.py`.)*
- Harden against 2 fixtures: **Maths** (calculation/working/method marks) + **Afrikaans** (non-English content language) — the two hardest schema stressors
- ✅ Milestone: POST scope photos → schema-valid Variant A JSON + memo JSON, both fixtures passing in CI

## Week 2 — PDF Rendering + Scope Intake
**Goal: upload a scope on your phone, get printable PDFs back.**
- WeasyPrint HTML/CSS templates: test paper (mark brackets, working space, footer pages) + memorandum (answers bold, method notes, tick allocation) — port the established formatting standards as a template spec
- ✅ (2026-07-12, partial) Scope intake UI (parent mode): **text-first** scope + subject/child tagging + cycle creation — plus the spine CRUD (family→child→subject→cycle), the cycle state machine (`api/services/cycle.py`), draft preview + approve/publish gate, and Settings/Family (child edit/archive, per-child visibility defaults persisted). *Supabase Storage photo/PDF upload deferred; runs on FakeClaude.*
- Harden schema against remaining 3 fixtures (NS tables/labelling, SS matching, Music stave render-hints as SVG)
- Variant B regeneration logic (same spec, changed values, gap-retarget parameter — stub gaps for now)
- ✅ Milestone: end-to-end scope → printed Variant A + memo, all 5 fixtures rendering cleanly

## Week 3 — Child Answer Capture (iPad PWA)
**Goal: child can submit a full test from the iPad browser.**
- PWA install flow (vite-plugin-pwa), child mode entry
- Per-question-type input components: MCQ picker, true/false, number pad, short text, matching, ordering, fill-in-blank
- Question-flow screen driven by assessment JSON via the generated TS client; "not attempted" as explicit state
- Proof-photo capture via file input with camera (paper pages photographed at submission)
- **Screen-view fallback:** render the test in-browser for no-printer cases (child still works on paper)
- ✅ Milestone: child enters a full historical test's answers on the iPad; submission lands in Postgres

## Week 4 — Grading Engine
**Goal: submission → draft marks with rationale.**
- Auto-marker for closed types (exact + fuzzy matching, accepted alternatives, confidence score)
- Claude-assist path: one batched call per submission for open-ended questions + method-mark suggestions from proof photos; returns mark + rationale + confidence per question
- Low-confidence flagging routes questions to parent review
- Replay output against the 5 fixtures' *actual historical mark decisions* to measure agreement
- ✅ Milestone: submit a fixture's answers → draft marked result matching historical marking within tolerance

## Week 5 — Parent Review + Gap Report
**Goal: full single-test cycle end-to-end with your real child.**
- Parent review UI (responsive, phone + desktop): per-question mark, rationale, edit, approve; publish gate; child-visibility config
- Child results view honouring parent config
- Gap report generation using the generic error taxonomy (concept / format misread / careless / not attempted), stored per cycle
- Live dry run: one real subject, real print, real child, real review
- ✅ Milestone: SCOPE_UPLOADED → GAP_REPORT completed live, no chat fallback needed

## Week 6 — Close the Loop + Alpha
**Goal: CYCLE_COMPLETE on the family iPad + parent phones.**
- Study pack generation from gap report (rule boxes, wrong-vs-correct, drills, cheat sheet, embedded image prompts) + WeasyPrint render
- Variant B generation wired to real gap data; second capture/mark/review pass reuses Weeks 3–5 components unchanged
- A vs B comparison summary (closed / persisting / new gaps)
- Alpha: one complete live cycle with one child, one subject
- ✅ Milestone: full diagnostic loop, app-orchestrated end to end

---

## Buffer & risk notes
- **Likely overruns:** WeasyPrint fidelity vs the established PDF standards (Week 2) and Claude-assist grading agreement (Week 4). Week 6 is deliberately light — it's the buffer.
- **Cut-line if behind:** screen-view fallback (W3) and A-vs-B comparison UI (W6) can slip without breaking the alpha; the schema, capture, and review cannot.
- **Agent guardrails:** every week ends with fixtures green in CI; agents may not merge schema changes that break a fixture without a parent-approved migration note in ARCHITECTURE.md.
- **Cost control:** one Claude call per generation artefact, one batched call per grading pass; token usage logged from Week 1. Weekly Supabase keep-alive ping + automated off-platform backup job from Week 1.
- **Definition of alpha done:** a cycle your child completes where you never open this chat project to fill a gap in the app.
