"""Gap report endpoints, round-parameterized (P4 of the generic (round, phase)
redesign — docs/design/round-phase-architecture.md §5, §7).

    POST /cycles/{cycle_id}/gap-report
        Derive the gap report from reviewed marks, upsert to gap_reports table
        under the target round, return the GapReport.  Idempotent: re-running
        overwrites the previous row for that (cycle_id, round).
        Guard: the target round's marks must already be published (per-round,
        ``cycle_round_approvals`` — see ``services/phase.is_published``).
        No cycle phase transition (PUBLISHED is the resting phase; the
        study-pack phase handles the next transition).
        operation_id: generate_gap_report

    GET /cycles/{cycle_id}/gap-report
        Return the stored GapReport for the cycle + round.
        404 if not yet generated.
        operation_id: get_gap_report

``variant`` (default ``"A"`` == round 1) selects the round, mirroring the
sibling capture/grading/review endpoints' ``?variant=A|B`` surface (P4-1) —
kept consistent rather than introducing a separate ``round`` query param.

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- No Claude call: gap report is derived deterministically from marks only (§6).
- All state transitions via api/services/cycle.py only (none needed here).
- Guard: the target round's marks must be published — 409 if not (marks not
  yet published / reviewed for that round).
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_family_repository,
    get_gap_report_repository,
    get_question_mark_repository,
)
from routers.families import _resolve_family_id
from schemas.gap_report import GapReportResponse
from schemas.identity import Identity
from services.auth import get_identity
from services.gap_report import derive_gap_report
from services.phase import is_published, resolve_assessment, round_for_variant
from services.repositories.base import (
    FamilyRepository,
    GapReportRepository,
    QuestionMarkRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


@router.post(
    "/{cycle_id}/gap-report",
    response_model=GapReportResponse,
    status_code=status.HTTP_200_OK,
    operation_id="generate_gap_report",
    summary=(
        "Derive the gap report from reviewed marks and upsert to storage, for "
        "the given variant/round (default A / round 1). Idempotent. The "
        "target round's marks must already be published."
    ),
)
def generate_gap_report(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
) -> GapReportResponse:
    """Derive and persist the gap report for a cycle + round.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. The target round's marks are published (per-round, ``cycle_round_approvals``).
    3. The cycle has an assessment for this variant/round.
    4. The cycle has at least one graded mark for this variant/round.

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

    round_ = round_for_variant(variant)

    if not is_published(family_repo, cycle_id, round_):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Variant {variant} marks are not yet published; "
                "gap report requires marks to be published first "
                "(publish marks via POST /cycles/{cycle_id}/publish)."
            ),
        )

    # Resolve the target variant's assessment.
    assessment = resolve_assessment(cycle, variant)
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Variant-{variant} assessment found for this cycle.",
        )

    # Resolve marks.
    marks = marks_repo.list_for_cycle(cycle_id, variant)
    if not marks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded marks found for this cycle.",
        )

    # Resolve submission_id (needed for the gap_reports row FK).
    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, variant)
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded submission found for this cycle.",
        )

    # Derive — deterministic, no Claude.
    try:
        report = derive_gap_report(assessment, marks)
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

    # Upsert — idempotent, keyed on (cycle_id, round).
    family_id = cycle.family_id
    row = gap_repo.upsert(family_id, cycle_id, submission_id, report, round=round_)

    log.info(
        "generate_gap_report: cycle=%s round=%d mastered=%d growing=%d",
        cycle_id,
        round_,
        report.summary.mastered_count,
        report.summary.growing_count,
    )

    return GapReportResponse(
        cycle_id=row.cycle_id,
        submission_id=row.submission_id,
        report=row.report,
        round=row.round,
    )


@router.get(
    "/{cycle_id}/gap-report",
    response_model=GapReportResponse,
    status_code=status.HTTP_200_OK,
    operation_id="get_gap_report",
    summary=(
        "Return the stored gap report for a cycle + variant/round "
        "(default A / round 1). 404 if not yet generated."
    ),
)
def get_gap_report(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
) -> GapReportResponse:
    """Return the persisted gap report for a cycle + round.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. Gap report row exists for this round — 404 if not yet generated.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    round_ = round_for_variant(variant)

    row = gap_repo.get_for_cycle(cycle_id, round=round_)
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
        round=row.round,
    )
