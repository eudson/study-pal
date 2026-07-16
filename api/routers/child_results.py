"""Child results view endpoint, round-parameterized (P4 of the generic
(round, phase) redesign — docs/design/round-phase-architecture.md §5, §7).

    GET /cycles/{cycle_id}/child-results
        Returns the child-visible published results for the given
        variant/round (default ``"A"`` == round 1).  Server-side filtered
        through the FROZEN per-round ``published_visibility`` snapshot
        stored in ``cycle_round_approvals`` at publish time — never through
        the child's current ``visibility_defaults`` (drift guard), and never
        through the single-valued (round-ambiguous) ``cycles.published_visibility``
        compat column.
        operation_id: get_child_results

``variant`` (default ``"A"`` == round 1) selects the round, mirroring the
sibling capture/grading/review/gap-report/study-pack endpoints' ``?variant=A|B``
surface.

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- The target round's marks must be published (per-round, ``cycle_round_approvals``).
- The target round must be child-visible per ``services.phase.round_config``
  (``results_child_visible`` — true for round 1, false for round 2+ in v1);
  otherwise 404 (round 2's results are parent-only in v1 — no such resource
  exists for the child).
- ``published_visibility`` is read from the FROZEN per-round snapshot, not the
  child's live ``visibility_defaults``.  A post-publish change to the child's
  defaults MUST NOT alter what the child sees.
- No Claude call; no state transition; no persistence.
- Response contains NO memo, correct-answer, accepted-alternative, or
  AnswerPayload-derived fields (structural exclusion in the service layer).
- Guard order mirrors capture.py / gap_report.py (RLS → 404 → published 409 →
  child-visible-round 404 → resolve data → project → return).

Note on kiosk auth (mirrors capture.py):
  The kiosk "child mode" runs under the parent's authenticated session.
  Authorization is the family RLS — there is no separate child auth token.
  Content-safety boundary is enforced by server-side state guards and the
  projection in services/child_results.py, not by a client mode flag.
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
    get_submission_repository,
)
from routers.families import _resolve_family_id
from schemas.capture import ChildResponseItem
from schemas.child_results import ChildResultsView
from schemas.family import VisibilityDefaults
from schemas.identity import Identity
from services.auth import get_identity
from services.child_results import project_results_for_child
from services.gap_report import derive_gap_report
from services.phase import is_published, resolve_assessment, round_config, round_for_variant
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
        "Return the child-visible published results for a cycle + variant/round "
        "(default A / round 1), server-side filtered through the frozen "
        "per-round published_visibility snapshot. The target round's marks "
        "must be published, and the round must be child-visible."
    ),
)
def get_child_results(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
) -> ChildResultsView:
    """Return the child-visible published results for a cycle + round.

    Guards (in order):
    1. Caller has a family (RLS — cross-family cycles are invisible → 404).
    2. Cycle exists in the caller's family; None → 404.
    3. The target round's marks are published → 409 if not.
    4. The target round is child-visible (``round_config(round).results_child_visible``)
       → 404 if not (round 2's results are parent-only in v1).
    5. The round's ``published_visibility`` snapshot exists (``cycle_round_approvals``)
       → 500 if absent (server invariant — publish always freezes it).
    6. Resolve marks + submission responses.  Gap report: use the stored row
       when present; derive in-memory otherwise (no persist, no Claude, no
       state transition).
    7. Project through the frozen snapshot via ``project_results_for_child``.
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

    round_ = round_for_variant(variant)

    # Guard 3: the target round's marks must be published.
    if not is_published(family_repo, cycle_id, round_):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Variant {variant} marks are not yet published; "
                "child results are only available after marks have been published. "
                "Publish marks via POST /cycles/{cycle_id}/publish first."
            ),
        )

    # Guard 4: the target round must be child-visible (design §2 table —
    # round 1 diagnostic results are child-visible; round 2+ retest results
    # are parent-only in v1). Not a hardcoded variant/subject branch — a
    # per-round config lookup.
    if not round_config(round_).results_child_visible:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"Variant {variant} results are not child-visible in this version."),
        )

    # Guard 5: the round's frozen published_visibility snapshot must exist.
    # Read from the per-round cycle_round_approvals row — NEVER from the
    # single-valued (round-ambiguous) cycles.published_visibility compat
    # column, which a later round's publish can silently overwrite.
    approval = family_repo.get_round_approval(cycle_id, round_)
    snapshot: VisibilityDefaults | None = (
        approval.published_visibility if approval is not None else None
    )
    if snapshot is None:
        # This should not occur once is_published() above is True — publish
        # always freezes the snapshot alongside marks_published_at. Treat as
        # a server invariant violation.
        log.error(
            "get_child_results: published_visibility is None for cycle %s round %d",
            cycle_id,
            round_,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Cycle marks appear to be published but the visibility snapshot "
                "is missing. This is a server-side invariant violation."
            ),
        )

    # Guard 6a: resolve the target variant's assessment (needed for in-memory
    # gap derivation and for title extraction).
    assessment = resolve_assessment(cycle, variant)

    # Guard 6b: resolve marks.
    marks = marks_repo.list_for_cycle(cycle_id, variant)
    if not marks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded marks found for this cycle.",
        )

    # Guard 6c: resolve submission responses for render_child_answer.
    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, variant)
    responses: list[ChildResponseItem] = []
    if submission_id is not None:
        responses = _get_responses(submission_repo, submission_id)

    # Guard 6d: gap report — stored row preferred; derive in-memory if absent.
    stored_row = gap_repo.get_for_cycle(cycle_id, round=round_)
    if stored_row is not None:
        report = stored_row.report
    else:
        # Derive in-memory: no persist, no state transition, no Claude.
        if assessment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No Variant-{variant} assessment found for this cycle. "
                    "Cannot derive gap report."
                ),
            )
        try:
            report = derive_gap_report(assessment, marks)
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
        variant=variant,
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
