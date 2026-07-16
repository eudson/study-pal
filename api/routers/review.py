"""Parent mark review + publish gate endpoints.

Guards are phase-driven and identical for every round (docs/design/
round-phase-architecture.md §3, §5, §7 P4) — ``variant`` only selects which
round's marks are targeted.

    PATCH /cycles/{cycle_id}/marks/{question_id}
        Parent override of a single question mark, variant-parameterized
        (``?variant=A|B``, default ``A``).
        Sets final_marks, reviewed_at, and overridden_at (when marks differ).
        On first call, transitions MARKED → REVIEW_MARKS (every round).
        Guarded by the target round's published-immutability (409 if
        already published — per-round, ``cycle_round_approvals``).
        operation_id: review_question_mark

    POST /cycles/{cycle_id}/publish
        Publish marks to the child — the approval-gated transition (golden rule 8),
        variant-parameterized (``?variant=A|B``, default ``A``) — the SAME
        endpoint publishes any round.
        Guard: every question mark (for this round) must have final_marks set.
        Computes + freezes published_visibility from child defaults + request overrides.
        Transitions REVIEW_MARKS → PUBLISHED.
        operation_id: publish_marks

Security / invariants:
- family_id is NEVER accepted from the client — derived from cycle row (RLS-scoped).
- All state transitions go only through api/services/cycle.py (ARCHITECTURE.md §5).
- Publish records parent approval + timestamp (golden rule 8), per round.
- publish_marks freezes the visibility snapshot — a later change to the child's
  defaults MUST NOT alter what was approved.
- The child results endpoint MUST filter ai_rationale server-side from
  published_visibility (esp. exclude it when toggle is False).  This endpoint
  only stores the snapshot — the child results gate is a separate concern.
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
from schemas.family import CyclePhase, VisibilityDefaults
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
from services.phase import PHASE_CONFIG, apply_advance, is_published, round_for_variant
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
        "On first call, transitions MARKED → REVIEW_MARKS (every round)."
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
    2. The target round's marks are not already published (409).
    3. Cycle is at the variant's legal review phase (PHASE_CONFIG) — MARKED
       or REVIEW_MARKS, identical for every round.
    4. The question mark exists for this cycle's submission + variant.
    5. final_marks (if provided) must be <= marks_total.

    Transition: MARKED → REVIEW_MARKS on the first PATCH (every round).
    Already at REVIEW_MARKS: no transition needed.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    round_ = round_for_variant(variant)

    if is_published(family_repo, cycle_id, round_):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variant {variant} marks are published and immutable.",
        )

    if not PHASE_CONFIG.review.is_legal(cycle.phase):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in phase '{cycle.phase.value}'; "
                f"Variant {variant} mark review requires the cycle to be "
                f"{PHASE_CONFIG.review.label()}."
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

    # Transition (identical for every round, and only from MARKED — table-driven).
    try:
        apply_advance(PHASE_CONFIG.review, family_repo, cycle_id, cycle.phase)
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
        "Transitions REVIEW_MARKS → PUBLISHED (every round)."
    ),
)
def publish_marks_endpoint(
    cycle_id: uuid.UUID,
    body: PublishRequest,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
) -> PublishResponse:
    """Publish marks to the child (approval-gated, golden rule 8).

    ``variant`` selects the round to publish (default ``"A"`` == round 1);
    the SAME endpoint is used for round 2's publish gate (design §5) — the
    guard below is phase-driven and identical for every round.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. Cycle is at REVIEW_MARKS.
    3. Every question mark (for this variant/round) has final_marks set —
       409 with list of unresolved question_ids if not.

    On success:
    - Resolves child's visibility_defaults and merges with request overrides.
    - Freezes the merged result as published_visibility for this round.
    - Records marks_published_at = now() (parent approval timestamp, golden rule 8),
      per round, in ``cycle_round_approvals``.
    - Transitions REVIEW_MARKS → PUBLISHED via cycle.py.

    NOTE: This endpoint does NOT build or return the child results view.
    The child results endpoint MUST:
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

    if cycle.phase != CyclePhase.REVIEW_MARKS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"Cycle is in phase '{cycle.phase.value}'; publish requires REVIEW_MARKS."),
        )

    # Resolve submission_id for this cycle + variant.
    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, variant)
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No graded Variant-{variant} submission found for this cycle.",
        )

    # Guard: every mark must have final_marks set.
    all_marks = marks_repo.list_for_cycle(cycle_id, variant)
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
