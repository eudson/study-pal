"""Phase 3 — parent mark review + publish gate endpoints.

    PATCH /cycles/{cycle_id}/marks/{question_id}
        Parent override of a single question mark, variant-parameterized
        (``?variant=A|B``, default ``A``).
        Sets final_marks, reviewed_at, and overridden_at (when marks differ).
        Variant A: on first call, transitions AUTO_MARKED → PARENT_REVIEW_MARKS.
        Variant B: legal only in GENERATING_B; never advances cycle state.
        Guarded by the target variant's published-immutability (409 if
        already published — Variant A only in v1).
        operation_id: review_question_mark

    POST /cycles/{cycle_id}/publish
        Publish marks to the child — the approval-gated transition (golden rule 8).
        Guard: every question mark must have final_marks set.
        Computes + freezes published_visibility from child defaults + request overrides.
        Transitions PARENT_REVIEW_MARKS → GAP_REPORT.
        operation_id: publish_marks

Security / invariants:
- family_id is NEVER accepted from the client — derived from cycle row (RLS-scoped).
- All state transitions go only through api/services/cycle.py (ARCHITECTURE.md §5).
- Publish records parent approval + timestamp (golden rule 8).
- publish_marks freezes the visibility snapshot — a later change to the child's
  defaults MUST NOT alter what was approved.
- The future child results endpoint MUST filter ai_rationale server-side from
  published_visibility (esp. exclude it when toggle is False).  This endpoint
  only stores the snapshot — the child results gate is deferred (Phase 4+).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_family_repository,
    get_question_mark_repository,
    get_submission_repository,
)
from routers.families import _resolve_family_id
from schemas.family import CycleState, VisibilityDefaults
from schemas.identity import Identity
from schemas.review import (
    MarkPatchRequest,
    MarkPatchResponse,
    PublishRequest,
    PublishResponse,
    UnresolvedMarksError,
)
from services.auth import get_identity
from services.cycle import IllegalTransitionError
from services.cycle import (
    publish_marks as cycle_publish_marks,
)
from services.phase import PHASE_CONFIG, apply_advance
from services.repositories.base import (
    FamilyRepository,
    QuestionMarkRepository,
    SubmissionRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


@router.patch(
    "/{cycle_id}/marks/{question_id}",
    response_model=MarkPatchResponse,
    status_code=status.HTTP_200_OK,
    operation_id="review_question_mark",
    summary=(
        "Parent override of a single question mark. "
        "Sets final_marks, reviewed_at, and overridden_at. "
        "On first call, transitions AUTO_MARKED → PARENT_REVIEW_MARKS."
    ),
)
def review_question_mark(
    cycle_id: uuid.UUID,
    question_id: str,
    body: MarkPatchRequest,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
) -> MarkPatchResponse:
    """Apply a parent review patch to a single question mark.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. The target variant's marks are not already published (409).
    3. Cycle is in the variant's legal review state (PHASE_CONFIG). For
       Variant A this is AUTO_MARKED or PARENT_REVIEW_MARKS.
    4. The question mark exists for this cycle's submission + variant.
    5. final_marks (if provided) must be <= marks_total.

    Transition (Variant A only): AUTO_MARKED → PARENT_REVIEW_MARKS on the
    first PATCH. Already in PARENT_REVIEW_MARKS: no transition needed.
    Variant B never advances cycle state.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    config = PHASE_CONFIG[variant]

    if config.is_published(cycle):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variant {variant} marks are published and immutable.",
        )

    if not config.review.is_legal(cycle.state):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                f"Variant {variant} mark review requires the cycle to be "
                f"{config.review.label()}."
            ),
        )

    # Resolve submission_id for this cycle + variant.
    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, variant)
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No graded Variant-{variant} submission found for this cycle.",
        )

    # Fetch the existing mark to validate final_marks upper bound.
    existing_mark = marks_repo.get_mark(submission_id, question_id)
    if existing_mark is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Question mark '{question_id}' not found for this cycle's Variant {variant}.",
        )

    # Validate final_marks <= marks_total.
    if body.final_marks is not None and body.final_marks > existing_mark.marks_total:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"final_marks {body.final_marks} exceeds marks_total "
                f"{existing_mark.marks_total} for question '{question_id}'."
            ),
        )

    now = datetime.now(tz=UTC)

    # Transition (Variant A only, and only from AUTO_MARKED — table-driven).
    try:
        apply_advance(config.review, family_repo, cycle_id, cycle.state)
    except IllegalTransitionError as exc:
        log.warning(
            "review_question_mark: state advance failed for cycle %s variant %s: %s",
            cycle_id,
            variant,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    # Apply the patch.
    updated_mark = marks_repo.update_mark(submission_id, question_id, body, now)

    return MarkPatchResponse(mark=updated_mark)


@router.post(
    "/{cycle_id}/publish",
    response_model=PublishResponse,
    status_code=status.HTTP_200_OK,
    operation_id="publish_marks",
    summary=(
        "Publish marks to the child. "
        "Requires all final_marks to be set. "
        "Freezes visibility snapshot. "
        "Transitions PARENT_REVIEW_MARKS → GAP_REPORT."
    ),
)
def publish_marks_endpoint(
    cycle_id: uuid.UUID,
    body: PublishRequest,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
) -> PublishResponse:
    """Publish marks to the child (approval-gated, golden rule 8).

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. Cycle is in PARENT_REVIEW_MARKS.
    3. Every question mark has final_marks set — 409 with list of unresolved
       question_ids if not.

    On success:
    - Resolves child's visibility_defaults and merges with request overrides.
    - Freezes the merged result as published_visibility in the cycle row.
    - Records marks_published_at = now() (parent approval timestamp, golden rule 8).
    - Transitions PARENT_REVIEW_MARKS → GAP_REPORT via cycle.py.

    NOTE: This endpoint does NOT build or return the child results view.
    The future child results endpoint (Phase 4+) MUST:
    - Read published_visibility server-side.
    - Exclude ai_rationale from the child response when published_visibility.ai_rationale is False.
    - Never expose correct_answer_rendered or memo fields to the child.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    if cycle.state != CycleState.PARENT_REVIEW_MARKS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; publish requires PARENT_REVIEW_MARKS state."
            ),
        )

    # Resolve submission_id for this cycle.
    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, "A")
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded submission found for this cycle.",
        )

    # Guard: every mark must have final_marks set.
    all_marks = marks_repo.list_for_cycle(cycle_id, "A")
    unresolved = [m.question_id for m in all_marks if m.final_marks is None]
    if unresolved:
        error = UnresolvedMarksError(
            detail=(
                f"{len(unresolved)} question mark(s) still have final_marks=NULL. "
                "Set final_marks on every question before publishing."
            ),
            unresolved_question_ids=unresolved,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.model_dump(),
        )

    # Resolve child's visibility_defaults for this cycle's child.
    # The child is identified via: cycle → subject → child.
    child_defaults = _resolve_child_visibility(family_repo, cycle_id)

    # Merge request overrides onto child defaults (request fields take precedence).
    frozen_visibility = body.merge_with_defaults(child_defaults)

    # Transition PARENT_REVIEW_MARKS → GAP_REPORT (records marks_published_at + snapshot).
    try:
        updated_cycle = cycle_publish_marks(family_repo, cycle_id, frozen_visibility)
    except IllegalTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    if updated_cycle.marks_published_at is None:
        # Should never happen — cycle_publish_marks always sets the timestamp.
        log.error(
            "publish_marks_endpoint: marks_published_at is None after publish for cycle %s",
            cycle_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Publish succeeded but marks_published_at was not recorded.",
        )

    return PublishResponse(
        cycle_id=cycle_id,
        state=updated_cycle.state,
        marks_published_at=updated_cycle.marks_published_at,
        published_visibility=frozen_visibility,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_child_visibility(
    family_repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> VisibilityDefaults:
    """Resolve the child's visibility_defaults for a given cycle.

    Path: cycle.subject_id → subject.child_id → child.visibility_defaults.
    Falls back to VisibilityDefaults() (standard defaults) if the child is
    not found (defensive; should not occur in normal flow).
    """
    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        return VisibilityDefaults()

    subjects = family_repo.list_subjects(cycle.family_id)
    subject = next((s for s in subjects if s.id == cycle.subject_id), None)
    if subject is None:
        return VisibilityDefaults()

    children = family_repo.list_children(cycle.family_id)
    child = next((c for c in children if c.id == subject.child_id), None)
    if child is None:
        return VisibilityDefaults()

    return child.visibility_defaults
