"""Week 6 — Variant B retest generation + A/B comparison endpoints.

Variant B is SAME-CYCLE (ARCHITECTURE.md §5): the cycle state machine is
unchanged — ``STUDY_PACK_DONE -> GENERATING_B -> CYCLE_COMPLETE``.

The B capture -> submit -> grade -> review sub-flow itself is served by the
SHARED capture/grade/review endpoints (``routers/capture.py``,
``routers/grading.py``, ``routers/review.py``) via ``?variant=B`` — see
``services/phase.py`` for the per-variant state-guard/advance table. This
router keeps only the genuinely Variant-B-specific endpoints: generation,
the A-vs-B comparison, and the terminal completion transition.

    POST /cycles/{cycle_id}/variant-b
        Generate the Variant B assessment from the stored Variant-A assessment
        + the stored (or in-memory derived) gap report's growing gaps.
        Guard: STUDY_PACK_DONE (idempotent: returns the existing B assessment
        if already GENERATING_B and one exists).
        Advances STUDY_PACK_DONE -> GENERATING_B via cycle.py.
        operation_id: generateVariantB

    GET /cycles/{cycle_id}/comparison
        Derive the A-vs-B comparison (pure, in-memory — never persisted).
        Guard: GENERATING_B or CYCLE_COMPLETE. Requires Variant B to be fully
        marked (409 otherwise).
        operation_id: getAbComparison

    POST /cycles/{cycle_id}/complete
        Terminal transition GENERATING_B -> CYCLE_COMPLETE. Requires the
        comparison to be derivable (Variant B fully marked) — 409 otherwise.
        operation_id: completeCycle

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- All state transitions go only through api/services/cycle.py (ARCHITECTURE.md §5).
- Every marks-repo call here hard-targets an explicit variant ("A" or "B") —
  never inferred by recency (Week 6 guardrail).
- No new cycle states are introduced; the B capture->mark->review sub-flow is
  inferred entirely from data presence while state stays GENERATING_B.
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
from schemas.family import CycleResponse, CycleState
from schemas.gap_report import GapReport
from schemas.identity import Identity
from services.auth import get_identity
from services.claude_client import FakeClaude
from services.comparison import derive_ab_comparison
from services.cycle import (
    IllegalTransitionError,
    advance_to_cycle_complete,
    advance_to_generating_b,
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
        "Generate the Variant B retest from the stored Variant A assessment "
        "+ gap report. Advances STUDY_PACK_DONE -> GENERATING_B."
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
    """Generate (or return the already-generated) Variant B assessment.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. Cycle is STUDY_PACK_DONE, or GENERATING_B with no B assessment yet
       (retry path) — 409 otherwise.
    3. Idempotent: if GENERATING_B and a B assessment already exists, return
       it without regenerating.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    existing_b = resolve_assessment(cycle, "B")
    if cycle.state == CycleState.GENERATING_B and existing_b is not None:
        return existing_b

    if cycle.state not in (CycleState.STUDY_PACK_DONE, CycleState.GENERATING_B):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; Variant B generation requires STUDY_PACK_DONE."
            ),
        )

    variant_a = resolve_assessment(cycle, "A")
    if variant_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Variant-A assessment found for this cycle.",
        )

    # Resolve the gap report — stored preferred, derive in-memory otherwise
    # (no persist, no state transition — mirrors child_results.py's fallback).
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

    # Advance STUDY_PACK_DONE -> GENERATING_B (skip if already there — retry path).
    if cycle.state == CycleState.STUDY_PACK_DONE:
        try:
            advance_to_generating_b(family_repo, cycle_id)
        except IllegalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

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
    summary="Derive the A-vs-B gap comparison. Requires Variant B to be fully marked.",
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

    if cycle.state not in (CycleState.GENERATING_B, CycleState.CYCLE_COMPLETE):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                "comparison requires GENERATING_B or CYCLE_COMPLETE."
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
    summary="Terminal transition GENERATING_B -> CYCLE_COMPLETE. Requires Variant B fully marked.",
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

    if cycle.state != CycleState.GENERATING_B:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cycle is in state '{cycle.state}'; complete requires GENERATING_B.",
        )

    # Require the comparison to be derivable — i.e. Variant B fully marked.
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
