"""Cycle state machine (ARCHITECTURE.md §5).

All cycle state transitions MUST go through this module — never by direct
column updates elsewhere.  Every child-visible transition records parent
approval + timestamp (golden rule 8).

Implemented transitions:
    SCOPE_UPLOADED → GENERATING_A
    GENERATING_A   → PARENT_REVIEWS_DRAFT
    PARENT_REVIEWS_DRAFT → APPROVED_PRINTED  (child-visible gate)
    APPROVED_PRINTED → ANSWERS_ENTERED
    ANSWERS_ENTERED  → AUTO_MARKED
    AUTO_MARKED      → PARENT_REVIEW_MARKS   (first mark patch triggers this)
    PARENT_REVIEW_MARKS → GAP_REPORT         (publish gate — child-visible, parent approval)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from schemas.family import CycleResponse, CycleState, VisibilityDefaults
from services.repositories.base import FamilyRepository

# ---------------------------------------------------------------------------
# Legal transitions (ARCHITECTURE.md §5 state machine)
# ---------------------------------------------------------------------------

_ALLOWED: dict[CycleState, CycleState] = {
    CycleState.SCOPE_UPLOADED: CycleState.GENERATING_A,
    CycleState.GENERATING_A: CycleState.PARENT_REVIEWS_DRAFT,
    CycleState.PARENT_REVIEWS_DRAFT: CycleState.APPROVED_PRINTED,
    CycleState.APPROVED_PRINTED: CycleState.ANSWERS_ENTERED,
    CycleState.ANSWERS_ENTERED: CycleState.AUTO_MARKED,
    CycleState.AUTO_MARKED: CycleState.PARENT_REVIEW_MARKS,
    CycleState.PARENT_REVIEW_MARKS: CycleState.GAP_REPORT,
    CycleState.GAP_REPORT: CycleState.GENERATING_STUDY_PACK,
    CycleState.GENERATING_STUDY_PACK: CycleState.STUDY_PACK_DONE,
    CycleState.STUDY_PACK_DONE: CycleState.GENERATING_B,
    CycleState.GENERATING_B: CycleState.CYCLE_COMPLETE,
}

# Transitions that make content visible to the child and require explicit
# parent approval recorded with a timestamp (golden rule 8).
# GAP_REPORT is reached via the publish gate (POST /cycles/{id}/publish) which:
#   - freezes the visibility snapshot in published_visibility (marks_published_at)
#   - requires all final_marks to be set (guard)
# This is the approval record for marks visibility; the state advance is
# handled by publish_marks() below which records its own timestamp.
_REQUIRES_PARENT_APPROVAL: frozenset[CycleState] = frozenset(
    {CycleState.APPROVED_PRINTED, CycleState.GAP_REPORT}
)


class IllegalTransitionError(ValueError):
    """Raised when a requested transition is not legal from the current state."""


def _require_cycle(repo: FamilyRepository, cycle_id: uuid.UUID) -> CycleResponse:
    cycle = repo.get_cycle(cycle_id)
    if cycle is None:
        raise ValueError(f"Cycle {cycle_id} not found or not accessible")
    return cycle


# ---------------------------------------------------------------------------
# Public transition functions
# ---------------------------------------------------------------------------


def advance_to_generating(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """SCOPE_UPLOADED → GENERATING_A.

    Called when generation is kicked off for a cycle.
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_transition(cycle.state, CycleState.GENERATING_A)
    return repo.update_cycle_state(cycle_id, CycleState.GENERATING_A)


def advance_to_parent_reviews(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """GENERATING_A → PARENT_REVIEWS_DRAFT.

    Called when generation completes successfully and the draft is ready.
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_transition(cycle.state, CycleState.PARENT_REVIEWS_DRAFT)
    return repo.update_cycle_state(cycle_id, CycleState.PARENT_REVIEWS_DRAFT)


def approve_draft(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
    note: str | None = None,
) -> CycleResponse:
    """PARENT_REVIEWS_DRAFT → APPROVED_PRINTED.

    Child-visible gate: records ``parent_approval_at`` timestamp and
    optional ``parent_approval_note`` (golden rule 8).
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_transition(cycle.state, CycleState.APPROVED_PRINTED)
    approval_at = datetime.now(tz=UTC)
    return repo.update_cycle_state(
        cycle_id,
        CycleState.APPROVED_PRINTED,
        parent_approval_at=approval_at,
        parent_approval_note=note,
    )


def advance_to_answers_entered(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """APPROVED_PRINTED → ANSWERS_ENTERED.

    Called when the child's submission is persisted successfully.
    This is a child-action consequence, not a parent approval — no
    parent_approval_at is recorded (the parent already approved at
    the APPROVED_PRINTED gate).
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_transition(cycle.state, CycleState.ANSWERS_ENTERED)
    return repo.update_cycle_state(cycle_id, CycleState.ANSWERS_ENTERED)


def advance_to_auto_marked(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """ANSWERS_ENTERED → AUTO_MARKED.

    Called after grading completes successfully.
    This is not a child-visible gate — no parent_approval_at recorded here.
    Parent review of marks happens at the PARENT_REVIEW_MARKS gate (Phase 3).
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_transition(cycle.state, CycleState.AUTO_MARKED)
    return repo.update_cycle_state(cycle_id, CycleState.AUTO_MARKED)


def advance_to_parent_review_marks(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """AUTO_MARKED → PARENT_REVIEW_MARKS.

    Called the first time the parent edits a mark (first PATCH on any question).
    Not a child-visible gate — marks are not yet published.
    Idempotent from the caller's perspective: if already in PARENT_REVIEW_MARKS
    the router skips calling this.
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_transition(cycle.state, CycleState.PARENT_REVIEW_MARKS)
    return repo.update_cycle_state(cycle_id, CycleState.PARENT_REVIEW_MARKS)


def publish_marks(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
    published_visibility: VisibilityDefaults,
) -> CycleResponse:
    """PARENT_REVIEW_MARKS → GAP_REPORT.

    The publish gate (golden rule 8 — child-visible transition with parent
    approval + timestamp).  Records:
    - marks_published_at = now() (the parent approval timestamp for marks)
    - published_visibility = frozen snapshot (immutable after publish)
    - state = GAP_REPORT

    The caller (router) is responsible for the pre-publish guard:
    every question mark must have final_marks set before this is called.

    This function does NOT check the guard itself — that separation keeps
    the service layer thin and the router in control of the 409 response shape.
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_transition(cycle.state, CycleState.GAP_REPORT)
    approval_at = datetime.now(tz=UTC)
    return repo.publish_marks(
        cycle_id,
        new_state=CycleState.GAP_REPORT,
        marks_published_at=approval_at,
        published_visibility=published_visibility,
    )


# ---------------------------------------------------------------------------
# Internal guard
# ---------------------------------------------------------------------------


def _assert_transition(current: CycleState, target: CycleState) -> None:
    """Raise ``IllegalTransitionError`` if the transition is not legal."""
    allowed_next = _ALLOWED.get(current)
    if allowed_next != target:
        next_label = repr(allowed_next.value) if allowed_next else "'none (terminal)'"
        raise IllegalTransitionError(
            f"Cannot transition cycle from {current.value!r} to {target.value!r}. "
            f"Expected next state: {next_label}."
        )
