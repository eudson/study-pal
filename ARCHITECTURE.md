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

## 5. Cycle state machine

```
SCOPE_UPLOADED → GENERATING_A → PARENT_REVIEWS_DRAFT → APPROVED_PRINTED
→ ANSWERS_ENTERED → AUTO_MARKED → PARENT_REVIEW_MARKS → GAP_REPORT
→ GENERATING_STUDY_PACK → STUDY_PACK_DONE → GENERATING_B
→ (B: capture → mark → review) → CYCLE_COMPLETE
```
- States live in the `cycles` table; transitions only via service functions in `api/services/cycle.py` — never by direct column update elsewhere.
- Every transition that makes anything visible to the child requires explicit parent approval recorded with timestamp.
- Variant B is a **regeneration call** on Variant A's spec with values/contexts changed and flagged gaps deliberately retargeted — not new architecture.

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
