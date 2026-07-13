"""Phase 4 — gap report endpoints.

    POST /cycles/{cycle_id}/gap-report
        Derive the gap report from reviewed marks, upsert to gap_reports table,
        return the GapReport.  Idempotent: re-running overwrites the previous row.
        Guard: cycle must be in GAP_REPORT or any later state.
        No cycle state transition (GAP_REPORT is the resting state; the study-pack
        phase handles the next transition).
        operation_id: generate_gap_report

    GET /cycles/{cycle_id}/gap-report
        Return the stored GapReport for the cycle.
        404 if not yet generated.
        operation_id: get_gap_report

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- No Claude call: gap report is derived deterministically from marks only (§6).
- All state transitions via api/services/cycle.py only (none needed here).
- Cycle state guard: GAP_REPORT or later states are acceptable; earlier states
  return 409 (marks not yet published / reviewed).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_family_repository,
    get_gap_report_repository,
    get_question_mark_repository,
)
from routers.families import _resolve_family_id
from schemas.family import CycleState
from schemas.gap_report import GapReportResponse
from schemas.identity import Identity
from services.auth import get_identity
from services.gap_report import derive_gap_report
from services.repositories.base import (
    FamilyRepository,
    GapReportRepository,
    QuestionMarkRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")

# States in which the gap report is valid to generate or fetch.
# GAP_REPORT is the resting state after publish; all subsequent states are also
# acceptable (study pack, Variant B, complete).
# Exported so child_results.py can reuse it without redefining.
GAP_REPORT_VALID_STATES: frozenset[CycleState] = frozenset(
    {
        CycleState.GAP_REPORT,
        CycleState.GENERATING_STUDY_PACK,
        CycleState.STUDY_PACK_DONE,
        CycleState.GENERATING_B,
        CycleState.CYCLE_COMPLETE,
    }
)


@router.post(
    "/{cycle_id}/gap-report",
    response_model=GapReportResponse,
    status_code=status.HTTP_200_OK,
    operation_id="generate_gap_report",
    summary=(
        "Derive the gap report from reviewed marks and upsert to storage. "
        "Idempotent. Cycle must be in GAP_REPORT or a later state."
    ),
)
def generate_gap_report(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
) -> GapReportResponse:
    """Derive and persist the gap report for a cycle.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. Cycle is in GAP_REPORT or a later state (marks published).
    3. The cycle has a Variant-A assessment.
    4. The cycle has at least one graded mark.

    Derivation is deterministic — no Claude call.
    final_marks is guaranteed set by the publish gate; any None value causes a
    500 (invariant violation) rather than silently producing a wrong report.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    if cycle.state not in GAP_REPORT_VALID_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                "gap report requires GAP_REPORT or a later state "
                "(publish marks first via POST /cycles/{cycle_id}/publish)."
            ),
        )

    # Resolve Variant A assessment.
    variant_a = next((a for a in cycle.assessments if a.variant == "A"), None)
    if variant_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Variant-A assessment found for this cycle.",
        )

    # Resolve marks.
    marks = marks_repo.list_for_cycle(cycle_id)
    if not marks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded marks found for this cycle.",
        )

    # Resolve submission_id (needed for the gap_reports row FK).
    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id)
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded submission found for this cycle.",
        )

    # Derive — deterministic, no Claude.
    try:
        report = derive_gap_report(variant_a, marks)
    except ValueError as exc:
        log.error(
            "generate_gap_report: derivation invariant violation for cycle %s: %s",
            cycle_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(f"Gap report derivation failed due to an invariant violation: {exc}"),
        ) from exc

    # Upsert — idempotent.
    family_id = cycle.family_id
    row = gap_repo.upsert(family_id, cycle_id, submission_id, report)

    log.info(
        "generate_gap_report: cycle=%s mastered=%d growing=%d",
        cycle_id,
        report.summary.mastered_count,
        report.summary.growing_count,
    )

    return GapReportResponse(
        cycle_id=row.cycle_id,
        submission_id=row.submission_id,
        report=row.report,
    )


@router.get(
    "/{cycle_id}/gap-report",
    response_model=GapReportResponse,
    status_code=status.HTTP_200_OK,
    operation_id="get_gap_report",
    summary="Return the stored gap report for a cycle. 404 if not yet generated.",
)
def get_gap_report(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
) -> GapReportResponse:
    """Return the persisted gap report.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. Gap report row exists — 404 if not yet generated.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    row = gap_repo.get_for_cycle(cycle_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Gap report not yet generated for this cycle. "
                "Call POST /cycles/{cycle_id}/gap-report first."
            ),
        )

    return GapReportResponse(
        cycle_id=row.cycle_id,
        submission_id=row.submission_id,
        report=row.report,
    )
