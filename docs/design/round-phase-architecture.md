# Round/Phase architecture — variant-agnostic cycle model

**Status:** ✅ IMPLEMENTED (P1–P6, 2026-07-16). Architect-approved direction; advisor-reviewed
(REVISE → all required changes folded in below). Backend variant-agnostic, frontend phase-driven,
`VariantBPage` deleted; ARCHITECTURE §5/§10 updated. Deferred hygiene: drop the shadowed
`state`/enum + single-valued approval columns; tidy the one round-1-vs-2 branch in `retest.py`.
Supersedes the
"Variant B = same-cycle under `GENERATING_B`" decisions in ARCHITECTURE §10 (2026-07-16).
This is a §5 state-machine change; §5/§10 are rewritten in P6 (the ratification point).

**Three irreducible round-specifics** (everything else collapses): (1) generation *input*
(scope vs prior-round + gaps); (2) the cross-round *comparison*; (3) round 1's
`SCOPE_UPLOADED` preamble phase, which round 2+ skips (they start at `GENERATING`). Named
here so nobody later "fixes" (3) as an accidental asymmetry.

**Hard rule for the whole refactor:** after this lands, **no service/control-flow logic may
branch on `variant`** — all logic keys on `round`. `variant` survives only as a derived UI
display label (round 1→"A", 2→"B"). If any post-refactor code reads `variant` to decide
behaviour, the fork was not actually collapsed.

## 1. Problem

The cycle state machine is **asymmetric by variant**. Variant A's `capture → grade →
review` is four real cycle states (`APPROVED_PRINTED → ANSWERS_ENTERED → AUTO_MARKED →
PARENT_REVIEW_MARKS`); Variant B's entire `capture → grade → review` is crammed into the
single `GENERATING_B` state, with sub-progress inferred from data presence.

Every variant-forked piece of code descends from that one asymmetry (full catalogue in the
2026-07-16 audits): `VariantBPage` vs A's per-state pages, ~5 of 6 `PHASE_CONFIG` rows,
`completeCycle` as a bespoke endpoint, B's gap report never persisted, the near-duplicate
`generate_variant_b`, and the frontend continue-forks. Only **two** things are genuinely
variant-specific: (1) generation *input* (scope vs prior-round + gaps), (2) the cross-round
*comparison*. Everything else is an artifact.

## 2. Target model — `(round, phase)`

Replace the flat, variant-baked 12-value `CycleState` enum with two orthogonal axes on the
cycle:

- **`round: int`** — 1 = diagnostic (was "A"), 2 = retest (was "B"), … N. The generic axis.
- **`phase: CyclePhase`** — a generic enum **every round traverses identically**.

```
CyclePhase (per round, linear):
  SCOPE_UPLOADED     # round 1 only — the cycle's initial phase (scope drives round 1)
  GENERATING         # generating this round's paper
  DRAFT_REVIEW       # parent reviews + approves the draft  (EVERY round — rule 8)
  PRINTED            # approved; ready for the child (capture)
  ANSWERS_ENTERED    # child submitted
  MARKED             # auto/assist graded
  REVIEW_MARKS       # parent reviewing marks
  PUBLISHED          # marks published (child-visible per config) + gap report available
  STUDY_PACK         # study pack generated + approved (optional per round)

Cycle-level terminal:
  COMPLETE           # the cycle is done (comparison available when max round ≥ 2)
```

Transition within a round advances `phase`. Moving to the next round is a single
`start_next_round` transition: `round += 1`, `phase = GENERATING`. `COMPLETE` is terminal.

### Round shape is uniform; only the *ends* differ, and those are config

Every round shares the identical middle: `GENERATING → DRAFT_REVIEW → PRINTED →
ANSWERS_ENTERED → MARKED → REVIEW_MARKS → PUBLISHED → STUDY_PACK`. The differences are
per-round **config**, not code:

| Per-round datum | Round 1 (diagnostic) | Round 2+ (retest) |
|---|---|---|
| `generation_input` | `scope_text` | `prior_assessment + gaps + note` |
| `label` | "Diagnostic" / "Variant A" | "Retest" / "Variant B" |
| `has_comparison` | false | true (compare to prior round) |
| `results_child_visible` | true | **false in v1** (parent-only; flip per round) |
| initial phase | `SCOPE_UPLOADED` | `GENERATING` |

**`results_child_visible` ≠ paper visibility.** The child *always* works the printed paper
(paper is child-visible every round — that's why `DRAFT_REVIEW` approval is mandatory every
round). `results_child_visible` gates only whether the *marked results / gap report* are
shown back to the child; it is false for round 2 in v1. No implementer may skip round 2's
`DRAFT_REVIEW` on the basis of this flag.

**Decision (2026-07-16): every round gets `DRAFT_REVIEW`** — the parent approves each
round's generated paper before the child sees it. This fixes a latent golden-rule-8 gap
(today B's paper is child-visible with no recorded parent approval) and keeps rounds uniform.

## 3. What collapses (the payoff)

- **`PHASE_CONFIG`** → one table indexed by `phase` (not by variant). Same rules for every
  round. The A/B rows disappear.
- **`advance_*` fns** → generic `advance_phase(cycle, to_phase)` + `start_next_round(cycle)`.
- **`generate` / `generate_variant_b`** → one `generate(cycle, round)` with a per-round
  input **strategy** (scope vs prior+gaps). The shared machinery (one call + one repair +
  validation + id/round stamping) stops being copy-pasted.
- **`completeCycle`** → the ordinary terminal transition, not a bespoke endpoint.
- **Frontend dispatcher** → maps `phase → one generic phase component`; `VariantBPage`
  **is deleted**; `?variant=a|b` → an opaque `round` key into the per-round config.
- **B's gap report / study pack** → persisted per round (see §4), no more derive-in-memory
  special-case. Retires the §10 deferred debt.

**Irreducibly variant/round-specific and staying so:** the per-round generation *input
strategy*, and the cross-round *comparison* (`derive_ab_comparison`, generalized to
"compare round i vs round j").

## 4. Schema / migrations (forward-only, idempotent — §4 R1, §10 R4)

1. **`cycles`** — add `round int NOT NULL DEFAULT 1` and a generic `phase text` column.
   Backfill `phase` + `round` from the existing `state` (mapping in §5), then the app reads
   `(round, phase)`. Keep the old `state` column write-shadowed for one migration if it
   de-risks rollback, else drop after backfill. (Advisor to confirm: single generic `phase`
   column + `round`, vs a composite. Recommendation: two columns, `round` + `phase`.)
2. **`assessments`** — add `round int`; backfill `variant='A'→1`, `'B'→2`. Keep `variant`
   as a **derived display label** (round 1→"A", 2→"B") or drop it in favour of `round`
   (advisor call — leaning: add `round`, keep `variant` derived to avoid churning the
   `Assessment` Pydantic model's `Literal["A","B"]` in one step). Add `UNIQUE(cycle_id, round)`.
3. **`gap_reports`** — add `round int NOT NULL DEFAULT 1`; swap the unique constraint. The
   existing constraint is Postgres-auto-named (`gap_reports_cycle_id_key`) and inline, so the
   swap is **not** trivially idempotent — it must reference that name:
   `ALTER TABLE gap_reports DROP CONSTRAINT IF EXISTS gap_reports_cycle_id_key;` then
   `CREATE UNIQUE INDEX IF NOT EXISTS gap_reports_cycle_round_key ON gap_reports (cycle_id, round);`
   **Bundled in P1 with the repo change**: `postgres_gap_report.py:57` `ON CONFLICT (cycle_id)`
   → `ON CONFLICT (cycle_id, round)` — constraint + upsert target move together or inserts break.
4. **`study_packs`** — same pattern: add `round int NOT NULL DEFAULT 1`; drop
   `study_packs_cycle_id_key`, `CREATE UNIQUE INDEX IF NOT EXISTS ... (cycle_id, round)`; and
   `postgres_study_pack.py:56` `ON CONFLICT (cycle_id)` → `ON CONFLICT (cycle_id, round)` in P1.
5. **`question_marks`** — already round-safe (keyed on `submission_id`, which is per-round
   via its assessment). No change.
6. **`cycle_round_approvals` (NEW — required, advisor).** The approval records are currently
   single-valued on `cycles` (`parent_approval_at`, `parent_approval_note`,
   `marks_published_at`, `published_visibility`), and `postgres_family.py` writes them with
   `COALESCE(%s, existing)` — i.e. **overwrite**. With per-round approval, round 2 would
   clobber round 1's recorded approval — the exact rule-8 audit gap we are closing, merely
   relocated. Fix: a table keyed `(cycle_id, round)`:
   ```sql
   CREATE TABLE IF NOT EXISTS cycle_round_approvals (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     cycle_id uuid NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
     family_id uuid NOT NULL,              -- for RLS, mirroring 0008
     round int NOT NULL,
     draft_approved_at timestamptz,
     draft_approval_note text,
     marks_published_at timestamptz,
     published_visibility jsonb,
     UNIQUE (cycle_id, round)
   );
   -- + ENABLE/FORCE RLS + the standard 4 family_members-join policies (mirror 0008).
   ```
   The published-immutability predicate in `phase.py` becomes `is_published(cycle, round)`
   reading this round's row; `child_results.py:118`'s frozen-snapshot read becomes per round.
   Do **NOT** hang these on `assessments` — 0001 documents those columns as *derived from the
   JSONB document*; workflow/approval state is not derived and would break that invariant.
   The old single-valued `cycles` approval columns are backfilled into round 1's row, then
   read through the new table (drop the old columns in P6 with `state`).

RLS is unaffected — tenancy still resolves via the `family_members` join (§10 2026-07-12);
`round`/`phase` are ordinary non-tenant columns. Request path stays on the non-privileged
role.

### Backfill state → (round, phase)

```
SCOPE_UPLOADED         -> (1, SCOPE_UPLOADED)
GENERATING_A           -> (1, GENERATING)
PARENT_REVIEWS_DRAFT   -> (1, DRAFT_REVIEW)
APPROVED_PRINTED       -> (1, PRINTED)
ANSWERS_ENTERED        -> (1, ANSWERS_ENTERED)
AUTO_MARKED            -> (1, MARKED)
PARENT_REVIEW_MARKS    -> (1, REVIEW_MARKS)
GAP_REPORT             -> (1, PUBLISHED)
GENERATING_STUDY_PACK  -> (1, STUDY_PACK)   # + a generating/done sub-status (see §6)
STUDY_PACK_DONE        -> (1, STUDY_PACK)
GENERATING_B           -> (2, GENERATING)   # LOSSY: old GENERATING_B crammed B's whole
                                             # capture->grade->review sub-loop; a mid-B cycle
                                             # loses sub-progress. Acceptable ONLY because
                                             # there is no production data (fixtures empty,
                                             # disposable Postgres §4). Backfill note required.
CYCLE_COMPLETE         -> (2, COMPLETE)      # COMPLETE is only reachable via GENERATING_B, so
                                             # round is always 2 here — hardcode round=2 (do
                                             # not leave `max_round` undefined in the SQL).
```

The backfill migration includes a test asserting every one of the 12 old states maps to
exactly one `(round, phase)` (total, unambiguous — advisor-verified).

## 5. Backend collapse

- `schemas/family.py`: `CyclePhase` enum; `CycleResponse` gains `round` + `phase` (drop
  `state`, or keep as a computed compat field during transition).
- `services/cycle.py`: `_ALLOWED` becomes a per-phase linear map (round-independent) +
  `start_next_round`. `advance_phase(repo, cycle_id, to_phase)` replaces the seven
  `advance_to_*`, and **dual-writes the shadowed `state` column** (compat, until P6).
  Parent-approval recorded per round in `cycle_round_approvals` on `DRAFT_REVIEW → PRINTED`
  and `REVIEW_MARKS → PUBLISHED` (child-visible gates, rule 8) for **every** round.
  **Pin in P2:** `start_next_round` is legal from `STUDY_PACK` and from `PUBLISHED`
  (pack skipped); `COMPLETE` is reached from `PUBLISHED`/`STUDY_PACK` of the final round.
- `services/phase.py`: `PHASE_CONFIG` re-keyed by `phase` (one row per phase). The
  published-immutability predicate becomes `is_published(cycle, round)` reading
  `cycle_round_approvals`; `results_child_visible` is per-round config, not a hardcoded
  B=`False`. `CycleResponse.state` stays a **computed compat field** (from `round`+`phase`)
  until P5 so the stale generated TS client keeps type-checking through P1–P4.
- `services/generation_service.py`: `generate(cycle, round)` + a `RoundInputStrategy`
  (`ScopeStrategy` for round 1, `RetargetStrategy` for round ≥2). Prompts stay versioned.
- `routers/`: capture/grade/review already generic — re-key their guards to `phase`. Fold
  `retest.py`'s `generateVariantB`/`completeCycle` into generic `POST /cycles/{id}/rounds`
  (start next round) + the normal terminal advance; keep `getComparison` (cross-round).
- `routers/gap_report.py`, `study_pack.py`, `child_results.py`: parameterize by `round`
  (default 1), persist per round.

## 6. Sub-questions — RESOLVED (advisor 2026-07-16)

1. **STUDY_PACK: one phase, status from the row.** Generation is synchronous (FakeClaude),
   so there is no durable "generating" state to represent; row-absent / `approved_at NULL` /
   `approved_at` set covers it, and the child gate is the explicit `approved_at`, never phase
   inference. (No re-introduction of data-presence *state* inference for a child-visible gate.)
2. **`variant`: add `round`, keep `variant` as a derived display label.** Hard rule (§ header):
   no service logic branches on `variant` post-refactor — all control flow keys on `round`.
   `variant` is a UI string only, to avoid churning `Assessment`'s `Literal["A","B"]` in one
   step; drop it in a later pass.
3. **Comparison: generalize the function, constrain the endpoint.** Keep `derive_ab_comparison`
   pair-shaped ("compare gap report i vs j"); expose only `(1, current)` in the public API for
   v1. No arbitrary-pair query params or UI now (YAGNI).
4. **Old `state` column: keep it shadowed until P6, do NOT drop in P1.** `cycle.py` (P2) and
   the routers (P4) still read `state`; dropping it in P1 breaks the P1–P4 phase gates. Keep
   `state` as a plain shadowed column, **dual-written by `cycle.py`** through the transition,
   drop in P6 once the last reader is gone. (A Postgres `GENERATED` column can't back it —
   `(round,phase)→state` is not 1:1, since `STUDY_PACK` collapses two old states.) Same for
   the old single-valued `cycles` approval columns (§4.6): shadow + backfill, drop in P6.

## 7. Phased rollout (each phase: `make lint` + `make test` green before the next)

- **P1 — schema + migrations + Pydantic models** (`CyclePhase`, `round`;
  `cycle_round_approvals` table + RLS; the idempotent `UNIQUE(cycle_id,round)` swaps WITH the
  `ON CONFLICT` repo changes bundled; migrations 0010+; backfill + a totality test). Repos
  updated; `state` and old approval columns kept shadowed. Existing tests migrated to
  `(round, phase)`.
- **P2 — state machine** (`cycle.py` generic advances, `phase.py` re-keyed). A's behaviour
  provably unchanged (existing A tests, re-expressed in phases, stay green).
- **P3 — generation** (`generate(round, strategy)`; collapse the two methods).
- **P4 — routers** (re-key guards to phase; fold retest endpoints into generic round/compare;
  parameterize gap-report/study-pack/child-results by round). `make codegen`.
- **P5 — frontend** (dispatcher maps `phase → PhaseHub`; delete `VariantBPage`; `round`
  config drives labels/comparison/next-round). Reuse Weeks 3–5 leaf components unchanged.
- **P6 — fixtures + docs** (§7 fixtures assert new phase strings; ARCHITECTURE §5/§10; this
  doc → "implemented").

Fixtures are still empty (E1 deferred) and data is disposable — the migration/backfill cost
is at its minimum now. That is the reason to do this before E1, not after.

**Reduced safety margin (register this):** because §7 fixtures are empty, this spine
migration lands **without the golden-rule-1 fixture gate**. The sole regression net is the
existing unit/integration suite (`test_variant_b.py`, `test_review.py`, `test_capture.py`,
`test_grading.py`, `test_child_results.py`, …) re-expressed in phases. "Existing A tests,
re-expressed in phases, stay green" is therefore a **hard gate**, not a soft one — it carries
all the load-bearing regression protection for the refactor.

## 8. Risk register

- **Touches the published-marks trust path.** Mitigation: the child-visible gates
  (`DRAFT_REVIEW → PRINTED`, `REVIEW_MARKS → PUBLISHED`) must record parent approval for
  every round; the published-immutability guard (2026-07-16) carries over, re-keyed to
  `(round, phase)`. Existing A tests are the regression proof.
- **State backfill correctness.** Mitigation: deterministic mapping (§4), a migration test
  asserting every old state maps to exactly one `(round, phase)`.
- **Big blast radius.** Mitigation: strict phase gates (§7); A behaviour frozen by its
  existing tests re-expressed in phases; advisor sign-off before P1.
- **§5 is law.** This doc + ARCHITECTURE §5/§10 are updated together in P6 (and §5 direction
  is already architect-approved).
