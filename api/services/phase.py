"""Per-variant phase configuration for capture/grade/review actions.

Single source of truth for which cycle states legally permit each of the
shared capture/grade/review actions per variant, and the (optional)
state-advance triggered on success (ARCHITECTURE.md §5).  Table-driven by
design: variant is legitimately cycle-phase-relevant (it selects which
sub-flow of the SAME state machine an action belongs to), so keying rules
off it here is not the forbidden `if subject == ...` pattern (golden rule 4)
— but it must stay data-driven, never a scattered `if variant == ...` per
call site.

Also carries the published-immutability predicate, used as a universal
write guard (belt-and-suspenders alongside the state guards) applied before
create_submission / grade / review-PATCH: a variant whose marks are already
published must never be mutated again.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from schemas.assessment_schema import Assessment
from schemas.family import CycleResponse, CycleState
from services.cycle import (
    advance_to_answers_entered,
    advance_to_auto_marked,
    advance_to_parent_review_marks,
)
from services.repositories.base import FamilyRepository

Variant = Literal["A", "B"]


@dataclass(frozen=True)
class StateAdvance:
    """A state advance that fires only when the cycle is currently in
    ``trigger_state`` at the moment the action succeeds."""

    trigger_state: CycleState
    advance_fn: Callable[[FamilyRepository, uuid.UUID], CycleResponse]


@dataclass(frozen=True)
class ActionRule:
    """Legal cycle states for one action, plus its optional state advance.

    ``legal_states=None`` means "no state guard" — this matches Variant A's
    current marks-GET behaviour, which has never checked cycle state.
    """

    legal_states: frozenset[CycleState] | None
    advance: StateAdvance | None = None

    def is_legal(self, state: CycleState) -> bool:
        return self.legal_states is None or state in self.legal_states

    def label(self) -> str:
        if self.legal_states is None:
            return "any state"
        return " or ".join(sorted(s.value for s in self.legal_states))


@dataclass(frozen=True)
class VariantPhaseConfig:
    """All per-action rules for one variant, plus its published predicate."""

    variant: Variant
    capture: ActionRule
    submit: ActionRule
    grade: ActionRule
    review: ActionRule
    marks_get: ActionRule
    is_published: Callable[[CycleResponse], bool]


PHASE_CONFIG: dict[Variant, VariantPhaseConfig] = {
    "A": VariantPhaseConfig(
        variant="A",
        capture=ActionRule(legal_states=frozenset({CycleState.APPROVED_PRINTED})),
        submit=ActionRule(
            legal_states=frozenset({CycleState.APPROVED_PRINTED}),
            advance=StateAdvance(CycleState.APPROVED_PRINTED, advance_to_answers_entered),
        ),
        grade=ActionRule(
            legal_states=frozenset({CycleState.ANSWERS_ENTERED, CycleState.AUTO_MARKED}),
            advance=StateAdvance(CycleState.ANSWERS_ENTERED, advance_to_auto_marked),
        ),
        review=ActionRule(
            legal_states=frozenset({CycleState.AUTO_MARKED, CycleState.PARENT_REVIEW_MARKS}),
            advance=StateAdvance(CycleState.AUTO_MARKED, advance_to_parent_review_marks),
        ),
        # Variant A's marks-GET has never had a cycle-state guard — preserved as-is.
        marks_get=ActionRule(legal_states=None),
        is_published=lambda cycle: cycle.marks_published_at is not None,
    ),
    "B": VariantPhaseConfig(
        variant="B",
        capture=ActionRule(legal_states=frozenset({CycleState.GENERATING_B})),
        submit=ActionRule(legal_states=frozenset({CycleState.GENERATING_B})),
        grade=ActionRule(legal_states=frozenset({CycleState.GENERATING_B})),
        review=ActionRule(legal_states=frozenset({CycleState.GENERATING_B})),
        # Read-only after CYCLE_COMPLETE, matching getVariantBMarks/getAbComparison.
        marks_get=ActionRule(
            legal_states=frozenset({CycleState.GENERATING_B, CycleState.CYCLE_COMPLETE})
        ),
        # Variant B (v1) is never published.
        is_published=lambda _cycle: False,
    ),
}


def resolve_assessment(cycle: CycleResponse, variant: str) -> Assessment | None:
    """Return the cycle's assessment for the given variant, or None."""
    return next((a for a in cycle.assessments if a.variant == variant), None)


def apply_advance(
    rule: ActionRule,
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
    current_state: CycleState,
) -> CycleResponse | None:
    """Call the rule's state advance iff the cycle is in its trigger state.

    Returns the updated cycle, or None if no advance applied (rule has none,
    or the cycle is not currently in the trigger state — e.g. idempotent
    re-grade from AUTO_MARKED).  Propagates ``IllegalTransitionError`` from
    the underlying ``services.cycle`` call; callers decide how to handle it
    (log-and-continue vs. surface as 409), matching each endpoint's existing
    behaviour.
    """
    if rule.advance is None or current_state != rule.advance.trigger_state:
        return None
    return rule.advance.advance_fn(repo, cycle_id)
