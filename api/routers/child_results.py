"""Child results view endpoint (Phase 4+).

    GET /cycles/{cycle_id}/child-results
        Returns the child-visible published results for a cycle.
        Server-side filtered through the FROZEN ``published_visibility``
        snapshot stored at publish time — never through the child's current
        ``visibility_defaults`` (drift guard).
        operation_id: get_child_results

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- Cycle must be in GAP_REPORT or a later state (marks published).
- ``published_visibility`` is read from the FROZEN cycle snapshot, not the
  child's live ``visibility_defaults``.  A post-publish change to the child's
  defaults MUST NOT alter what the child sees.
- No Claude call; no state transition; no persistence.
- Response contains NO memo, correct-answer, accepted-alternative, or
  AnswerPayload-derived fields (structural exclusion in the service layer).
- Guard order mirrors capture.py / gap_report.py (RLS → 404 → state 409 →
  snapshot 409/500 → resolve data → project → return).

Note on kiosk auth (mirrors capture.py):
  The kiosk "child mode" runs under the parent's authenticated session.
  Authorization is the family RLS — there is no separate child auth token.
  Content-safety boundary is enforced by server-side state guards and the
  projection in services/child_results.py, not by a client mode flag.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_family_repository,
    get_gap_report_repository,
    get_question_mark_repository,
    get_submission_repository,
)
from routers.families import _resolve_family_id
from routers.gap_report import GAP_REPORT_VALID_STATES
from schemas.capture import ChildResponseItem
from schemas.child_results import ChildResultsView
from schemas.family import VisibilityDefaults
from schemas.identity import Identity
from services.auth import get_identity
from services.child_results import project_results_for_child
from services.gap_report import derive_gap_report
from services.repositories.base import (
    FamilyRepository,
    GapReportRepository,
    QuestionMarkRepository,
    SubmissionRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


@router.get(
    "/{cycle_id}/child-results",
    response_model=ChildResultsView,
    status_code=status.HTTP_200_OK,
    operation_id="get_child_results",
    summary=(
        "Return the child-visible published results for a cycle, "
        "server-side filtered through the frozen published_visibility snapshot. "
        "Cycle must be in GAP_REPORT or a later state."
    ),
)
def get_child_results(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
) -> ChildResultsView:
    """Return the child-visible published results.

    Guards (in order):
    1. Caller has a family (RLS — cross-family cycles are invisible → 404).
    2. Cycle exists in the caller's family; None → 404.
    3. Cycle is in GAP_REPORT or a later state → 409 if not.
    4. ``published_visibility`` snapshot exists on the cycle → 409/500 if absent.
    5. Resolve marks + submission responses.  Gap report: use the stored row
       when present; derive in-memory otherwise (no persist, no Claude, no
       state transition).
    6. Project through the frozen snapshot via ``project_results_for_child``.
    """
    # Guard 1: caller has a family (RLS seam).
    _resolve_family_id(identity, family_repo)

    # Guard 2: cycle exists in the caller's family.
    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    # Guard 3: cycle must be published (GAP_REPORT or later).
    if cycle.state not in GAP_REPORT_VALID_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                "child results are only available after marks have been published "
                "(cycle must be in GAP_REPORT or a later state). "
                "Publish marks via POST /cycles/{cycle_id}/publish first."
            ),
        )

    # Guard 4: published_visibility snapshot must be frozen on this cycle.
    snapshot: VisibilityDefaults | None = cycle.published_visibility
    if snapshot is None:
        # This should not occur for a cycle in a valid post-publish state —
        # the publish endpoint always freezes the snapshot.  Treat as a server
        # invariant violation.
        log.error(
            "get_child_results: published_visibility is None for cycle %s in state %s",
            cycle_id,
            cycle.state,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Cycle marks appear to be published but the visibility snapshot "
                "is missing. This is a server-side invariant violation."
            ),
        )

    # Guard 5a: resolve Variant-A assessment (needed for in-memory gap derivation
    # and for title extraction).
    variant_a = next((a for a in cycle.assessments if a.variant == "A"), None)

    # Guard 5b: resolve marks.
    marks = marks_repo.list_for_cycle(cycle_id)
    if not marks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded marks found for this cycle.",
        )

    # Guard 5c: resolve submission responses for render_child_answer.
    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id)
    responses: list[ChildResponseItem] = []
    if submission_id is not None:
        responses = _get_responses(submission_repo, submission_id)

    # Guard 5d: gap report — stored row preferred; derive in-memory if absent.
    stored_row = gap_repo.get_for_cycle(cycle_id)
    if stored_row is not None:
        report = stored_row.report
    else:
        # Derive in-memory: no persist, no state transition, no Claude.
        if variant_a is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("No Variant-A assessment found for this cycle. Cannot derive gap report."),
            )
        try:
            report = derive_gap_report(variant_a, marks)
        except ValueError as exc:
            log.error(
                "get_child_results: gap report derivation failed for cycle %s: %s",
                cycle_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Gap report derivation failed: {exc}",
            ) from exc

    # Project through the frozen snapshot — never through the child's live defaults.
    return project_results_for_child(
        cycle=cycle,
        report=report,
        marks=marks,
        responses=responses,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_responses(
    submission_repo: SubmissionRepository,
    submission_id: uuid.UUID,
) -> list[ChildResponseItem]:
    """Extract the child's full response payload list from the submission repo.

    Mirrors grading.py ``_get_responses``: duck-types on the Postgres repo's
    ``_get_responses_for_grading`` helper; falls back to empty list for
    InMemory (marks everything as "(not attempted)").

    This is safe because ``render_child_answer`` handles an empty/missing
    payload by returning "(not attempted)" — never crashes, never leaks.
    """
    getter = getattr(submission_repo, "_get_responses_for_grading", None)
    if callable(getter):
        result: list[ChildResponseItem] = getter(submission_id)
        return result
    return []
