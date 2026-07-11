# CLAUDE.md — StudyPal

You are working in the StudyPal repository. **Before any task, read `ARCHITECTURE.md` — it is law.** If a task conflicts with it, stop and ask the architect (the human). Do not improvise around a constraint, and do not relitigate anything in the ARCHITECTURE.md §10 decision log.

## What this is
Paper-first diagnostic learning app. Loop: scope upload → generated test PDF (Variant A) + memo → child works on paper → child enters answers on iPad PWA (photo = proof only) → auto + Claude-assist grading → parent reviews/approves → gap report → study pack → Variant B retest → A-vs-B comparison. Subject- and language-agnostic by design.

## Repo map
```
api/        FastAPI monolith (Python 3.12+, Pydantic v2) — ALL business logic
  schemas/  assessment_schema.py = single source of truth
  services/ cycle state machine, generation, grading, pdf (WeasyPrint)
  services/prompts/  versioned Claude prompt files (never inline strings)
  migrations/        plain SQL only (Supabase = disposable managed Postgres)
  templates/         WeasyPrint HTML/CSS
web/        Vite + React + TS SPA (PWA) — TanStack Router/Query
  src/api/  GENERATED OpenAPI client ONLY — never hand-write API types
fixtures/   5 real historical cycles = ground truth = merge gate
docs/       studypal-mvp-scope.md (what & why) + studypal-build-plan.md (week-by-week task list agents execute)
.claude/agents/  the agent team (see below)
```

## Commands (the contract — orchestrator creates these Makefile targets in Week 1)
- `make dev` — docker-compose up (api + web dev servers)
- `make test` — pytest incl. fixture gates; `make test-fixtures` — fixture gates only
- `make lint` — ruff + mypy (api), tsc + eslint (web)
- `make codegen` — regenerate web/src/api/ from OpenAPI (run after ANY api change)
- `make migrate` — apply SQL migrations

## Golden rules (violations = rejected work)
1. Fixtures gate every merge. Red fixture = stop, report, do not "fix" the fixture.
2. Pydantic models at every service boundary; no bare dicts.
3. Schema changes → SQL migration file + `make codegen` + fixtures green, in that order.
4. No `if subject == ...` logic anywhere. Subject intelligence lives in question types and prompts only.
5. Only Supabase Auth, Storage, RLS. No Edge Functions, no dashboard-only schema changes.
6. `ANTHROPIC_API_KEY` and all secrets via env only; Claude called only from `api/`; log tokens per call.
7. Half-marks (0.5) are legal everywhere marks appear.
8. Anything visible to the child requires a recorded parent approval.

## Agent team — when to delegate
| Agent | Use when |
|---|---|
| `orchestrator` | Any multi-step feature spanning schema/api/web; release checks |
| `backend` | Work scoped to `api/` (schema, services, grading, PDF, migrations) |
| `frontend` | Work scoped to `web/` (routes, components, capture UX, PWA) |
| `advisor` | Before big decisions; reviews of security/architecture/pedagogy; research. READ-ONLY |
| `uiux` | ⛔ BLOCKED until design tokens are locked — see its activation checklist |

Sequencing rule for cross-stack features: schema → migration → backend service → codegen → frontend → tests → fixtures green.

## Git & commits (hard rules)
- **Commit only when the architect approves.** Do not commit automatically at the end of a task — ask first, and commit only after explicit approval.
- **Never push.** Pushing to any remote requires the architect's explicit approval, every time. No exceptions.
- **No co-authorship.** Do not add `Co-Authored-By` trailers, "Generated with Claude Code" lines, or any similar attribution to commit messages or PR bodies.

## Definition of done (every task)
`make lint` clean, `make test` green (fixtures included), codegen current, no TODOs left silently, one-paragraph summary of what changed and why.

## Work tracking
- `docs/studypal-build-plan.md` is the single source of truth for tasks. Mark completed items in place with ✅ and the date.
- After every working session, the orchestrator appends a dated entry to `docs/PROGRESS.md`: what was done, verification results (lint/tests/fixtures), what's next, and any blockers flagged `ARCHITECT DECISION NEEDED`.

## When in doubt
Ask the architect. For a second opinion first, spawn `advisor` — it will judge the proposal against ARCHITECTURE.md and say APPROVE / REVISE / ESCALATE.
