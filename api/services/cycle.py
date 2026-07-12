"""Cycle state machine (ARCHITECTURE.md §5).

All cycle state transitions MUST go through this module — never by direct
column updates elsewhere.  Every child-visible transition records parent
approval + timestamp (golden rule 8).

Implemented transitions for this slice (scope-in → draft → approve):
    SCOPE_UPLOADED → GENERATING_A
    GENERATING_A   → PARENT_REVIEWS_DRAFT
    PARENT_REVIEWS_DRAFT → APPROVED_PRINTED  (child-visible gate)

The remaining transitions (ANSWERS_ENTERED → … → CYCLE_COMPLETE) are
defined in the allowed-transitions table so illegal transitions are
rejected consistently, but their service functions will be added in
later slices.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from schemas.family import CycleResponse, CycleState
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
_REQUIRES_PARENT_APPROVAL: frozenset[CycleState] = frozenset({CycleState.APPROVED_PRINTED})


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
