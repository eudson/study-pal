"""Retest (round 2 / "Variant B") generation + cross-round comparison endpoints.

P4 of the generic (round, phase) redesign (docs/design/round-phase-
architecture.md §2, §3, §5, §7): round 2 now traverses the exact same real
phase sequence as round 1 — GENERATING -> DRAFT_REVIEW -> PRINTED ->
ANSWERS_ENTERED -> MARKED -> REVIEW_MARKS -> PUBLISHED -> (STUDY_PACK) ->
COMPLETE — including its own parent DRAFT_REVIEW approval (golden rule 8,
recorded per round in ``cycle_round_approvals``). The old "Variant B's whole
capture->grade->review sub-loop crammed into a single GENERATING_B state" is
retired.

The round-2 capture -> submit -> grade -> review -> publish sub-flow is
served by the SAME shared endpoints round 1 uses (``routers/capture.py``,
``routers/grading.py``, ``routers/review.py``, including the parent draft
approval at ``POST /cycles/{id}/approve`` and the publish gate at
``POST /cycles/{id}/publish``) via ``?variant=B`` — see ``services/phase.py``
for the phase-guard/advance table, now round-agnostic. This router keeps
only the genuinely round-specific endpoints: starting round 2 + generating
its assessment, the cross-round comparison, and the terminal completion
transition.

    POST /cycles/{cycle_id}/variant-b
        Start round 2 (``start_next_round``) and generate its assessment from
        the stored round-1 assessment + round-1 gap report's growing gaps.
        Advances the new round to DRAFT_REVIEW (paper generated, awaiting
        parent approval — NOT skipped past, unlike the old collapsed flow).
        Guard: round 1 must be at a settled STUDY_PACK phase, or PUBLISHED
        (pack skipped). Idempotent: if round 2 already has an assessment,
        returns it without regenerating or re-advancing the phase.
        operation_id: generateVariantB

    GET /cycles/{cycle_id}/comparison
        Derive the round-1-vs-round-2 comparison (pure, in-memory — never
        persisted). Guard: round 2 must have been started. Requires round 2
        to be fully marked (409 otherwise).
        operation_id: getAbComparison

    POST /cycles/{cycle_id}/complete
        Terminal transition to COMPLETE from round 2's PUBLISHED (or settled
        STUDY_PACK) phase. Requires the comparison to be derivable (round 2
        fully marked) — 409 otherwise.
        operation_id: completeCycle

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- All state transitions go only through api/services/cycle.py (ARCHITECTURE.md §5).
- Every marks-repo call here hard-targets an explicit variant ("A" or "B") —
  never inferred by recency (Week 6 guardrail carried over).
- No control flow branches on ``variant`` here — everything keys on
  ``cycle.round`` / ``cycle.phase`` (design header hard rule).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_assessment_repository,
    get_family_repository,
    get_gap_report_repository,
    get_question_mark_repository,
)
from routers.families import _resolve_family_id
from schemas.assessment_schema import Assessment, VariantBRequest
from schemas.comparison import ABComparison
from schemas.family import CyclePhase, CycleResponse
from schemas.gap_report import GapReport
from schemas.identity import Identity
from services.auth import get_identity
from services.claude_client import FakeClaude
from services.comparison import derive_ab_comparison
from services.cycle import (
    IllegalTransitionError,
    advance_phase,
    advance_to_cycle_complete,
    start_next_round,
)
from services.gap_report import build_gap_retargets, derive_gap_report
from services.generation_service import GenerationService
from services.phase import resolve_assessment
from services.repositories.base import (
    AssessmentRepository,
    FamilyRepository,
    GapReportRepository,
    QuestionMarkRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


# ---------------------------------------------------------------------------
# POST /cycles/{cycle_id}/variant-b
# ---------------------------------------------------------------------------


@router.post(
    "/{cycle_id}/variant-b",
    response_model=Assessment,
    status_code=status.HTTP_201_CREATED,
    operation_id="generateVariantB",
    summary=(
        "Start round 2 (retest) and generate its assessment from the stored "
        "round-1 assessment + gap report. Advances round 2 to DRAFT_REVIEW, "
        "awaiting parent approval (same as round 1)."
    ),
)
def generate_variant_b(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    assessment_repo: AssessmentRepository = Depends(get_assessment_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
) -> Assessment:
    """Generate (or return the already-generated) round 2 assessment.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. Round 1 is at a settled STUDY_PACK phase, or PUBLISHED (pack skipped)
       — 409 otherwise (``start_next_round`` enforces this generically).
    3. Idempotent: if round 2 already has a B assessment (any round-2
       phase), return it without regenerating or re-advancing the phase.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    existing_b = resolve_assessment(cycle, "B")
    if cycle.round >= 2 and existing_b is not None:
        return existing_b

    if cycle.round == 1:
        # Legal only from a settled STUDY_PACK phase or PUBLISHED (pack
        # skipped) — start_next_round is the single source of truth for
        # this guard (design §5 pin), shared with the generic round
        # machinery in services/cycle.py.
        try:
            cycle = start_next_round(family_repo, cycle_id)
        except IllegalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    elif not (cycle.round >= 2 and cycle.phase is CyclePhase.GENERATING):
        # Round 2 exists but has moved past GENERATING with no B assessment
        # saved — should not occur via normal flow; guard defensively.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is at round {cycle.round}, phase '{cycle.phase.value}'; "
                "Variant B generation requires round 1 to be complete "
                "(settled STUDY_PACK or PUBLISHED), or round 2 to be at "
                "GENERATING with no assessment yet."
            ),
        )

    variant_a = resolve_assessment(cycle, "A")
    if variant_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Variant-A assessment found for this cycle.",
        )

    # Resolve round 1's gap report — stored preferred, derive in-memory
    # otherwise (no persist, no phase transition — mirrors child_results.py's
    # fallback).
    gap_row = gap_repo.get_for_cycle(cycle_id)
    if gap_row is not None:
        report: GapReport = gap_row.report
    else:
        marks = marks_repo.list_for_cycle(cycle_id, "A")
        if not marks:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No graded Variant-A marks found; cannot derive gap report.",
            )
        try:
            report = derive_gap_report(variant_a, marks)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Gap report derivation failed: {exc}",
            ) from exc

    gaps = build_gap_retargets(report)
    request = VariantBRequest(source_assessment=variant_a, gaps=gaps)

    service = GenerationService(claude=FakeClaude())
    result = service.generate_variant_b(request, assessment_id=str(uuid.uuid4()))

    if not result.ok or result.assessment is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": result.error,
                "issues": [issue.model_dump() for issue in result.issues],
            },
        )

    saved = assessment_repo.save(result.assessment)

    # Advance round 2 GENERATING -> DRAFT_REVIEW: the paper is generated but
    # NOT yet child-visible — the parent must approve it first, exactly like
    # round 1 (golden rule 8, design §2 "every round gets DRAFT_REVIEW").
    # The parent then approves via the SAME shared draft-approval endpoint
    # round 1 uses (POST /cycles/{id}/approve) -> PRINTED -> capture ...
    try:
        advance_phase(family_repo, cycle_id, CyclePhase.DRAFT_REVIEW)
    except IllegalTransitionError as exc:
        # Assessment is saved; phase advance failure is non-fatal (e.g. a
        # concurrent retry already advanced it) — log and continue.
        log.warning(
            "generate_variant_b: phase advance to DRAFT_REVIEW failed for cycle %s: %s",
            cycle_id,
            exc,
        )

    log.info("generate_variant_b: cycle=%s assessment_id=%s", cycle_id, saved.assessment_id)
    return saved


# ---------------------------------------------------------------------------
# GET /cycles/{cycle_id}/comparison
# ---------------------------------------------------------------------------


@router.get(
    "/{cycle_id}/comparison",
    response_model=ABComparison,
    status_code=status.HTTP_200_OK,
    operation_id="getAbComparison",
    summary="Derive the round-1-vs-round-2 gap comparison. Requires round 2 to be fully marked.",
)
def get_ab_comparison(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
) -> ABComparison:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.round < 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is at round {cycle.round}; comparison requires round 2 "
                "(Variant B) to have been started."
            ),
        )

    gap_a = _resolve_gap_report(cycle, "A", gap_repo, marks_repo)
    gap_b = _resolve_gap_report(cycle, "B", gap_repo, marks_repo, require_full=True)

    return derive_ab_comparison(gap_a, gap_b)


# ---------------------------------------------------------------------------
# POST /cycles/{cycle_id}/complete
# ---------------------------------------------------------------------------


@router.post(
    "/{cycle_id}/complete",
    response_model=CycleResponse,
    status_code=status.HTTP_200_OK,
    operation_id="completeCycle",
    summary=(
        "Terminal transition to COMPLETE from round 2's PUBLISHED (or settled STUDY_PACK) "
        "phase. Requires round 2 to be fully marked."
    ),
)
def complete_cycle(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
) -> CycleResponse:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.round < 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cycle is at round {cycle.round}; complete requires round 2 to be finished.",
        )

    # Require the comparison to be derivable — i.e. round 2 (Variant B) fully marked.
    _resolve_gap_report(cycle, "A", gap_repo, marks_repo)
    _resolve_gap_report(cycle, "B", gap_repo, marks_repo, require_full=True)

    try:
        return advance_to_cycle_complete(family_repo, cycle_id)
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_gap_report(
    cycle: CycleResponse,
    variant: str,
    gap_repo: GapReportRepository,
    marks_repo: QuestionMarkRepository,
    *,
    require_full: bool = False,
) -> GapReport:
    """Resolve a variant's gap report for comparison purposes.

    Variant A: prefers the stored gap report; derives in-memory otherwise.
    Variant B: ALWAYS derives in-memory from the B assessment + B marks (the
    B gap report is never persisted — locked spec). ``require_full=True``
    raises 409 unless every Variant-B mark has final_marks set.
    """
    assessment = resolve_assessment(cycle, variant)
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Variant-{variant} assessment found for this cycle.",
        )

    if variant == "A":
        gap_row = gap_repo.get_for_cycle(cycle.id)
        if gap_row is not None:
            return gap_row.report

    marks = marks_repo.list_for_cycle(cycle.id, variant)
    if not marks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No graded Variant-{variant} marks found for this cycle.",
        )

    if require_full:
        unresolved = [m.question_id for m in marks if m.final_marks is None]
        if unresolved:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Variant {variant} is not fully marked; "
                    f"{len(unresolved)} question(s) still have final_marks=NULL: {unresolved}."
                ),
            )

    try:
        return derive_gap_report(assessment, marks)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gap report derivation failed for Variant {variant}: {exc}",
        ) from exc
