"""Generic per-phase action configuration for capture/grade/review (ARCHITECTURE.md §5).

P4 of the generic (round, phase) redesign
(docs/design/round-phase-architecture.md §3, §5, §7): ``PHASE_CONFIG`` is now
a SINGLE table keyed by ``CyclePhase`` — identical for every round. The old
per-variant ``VariantPhaseConfig`` ("A" row / "B" row) collapses into this
one table because round 2 now traverses the exact same phase sequence as
round 1 (design §2). ``variant`` survives only as an API-facing display/
selection label (``round_for_variant``); nothing here branches control flow
on it — every rule below is looked up purely by ``CyclePhase``.

Also carries the published-immutability predicate (``is_published``), used
as a universal write guard (belt-and-suspenders alongside the phase guards)
applied before create_submission / grade / review-PATCH: a round whose
marks are already published must never be mutated again. This now reads the
per-round ``cycle_round_approvals`` row (design §4.6) rather than the
single-valued ``cycles.marks_published_at`` column, so round 2 publishing
can never be confused with round 1's.

``round_config`` carries the one other genuinely per-round datum in this
module's scope: ``results_child_visible`` (design §2 table) — true for round
1, false for round 2+ in v1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from schemas.assessment_schema import Assessment
from schemas.family import CyclePhase, CycleResponse
from services.cycle import advance_phase
from services.repositories.base import FamilyRepository

# `variant` is a UI-facing display/selection label only (round 1 -> "A",
# round 2 -> "B", design §6 sub-question 2) — a fixed data lookup, never a
# control-flow branch.
_VARIANT_TO_ROUND: dict[str, int] = {"A": 1, "B": 2}


def round_for_variant(variant: str) -> int:
    """Map the UI-facing variant label to its round number (data, not control flow)."""
    return _VARIANT_TO_ROUND.get(variant, 1)


@dataclass(frozen=True)
class PhaseAdvance:
    """A phase advance that fires only when the cycle is currently at
    ``trigger_phase`` at the moment the action succeeds."""

    trigger_phase: CyclePhase
    target_phase: CyclePhase


@dataclass(frozen=True)
class ActionRule:
    """Legal cycle phases for one action, plus its optional phase advance.

    ``legal_phases=None`` means "no phase guard" — this matches marks-GET's
    behaviour, which has never checked cycle phase, for any round.
    """

    legal_phases: frozenset[CyclePhase] | None
    advance: PhaseAdvance | None = None

    def is_legal(self, phase: CyclePhase) -> bool:
        return self.legal_phases is None or phase in self.legal_phases

    def label(self) -> str:
        if self.legal_phases is None:
            return "any phase"
        return " or ".join(sorted(p.value for p in self.legal_phases))


@dataclass(frozen=True)
class PhaseActionTable:
    """All per-action rules — one row per action, uniform across every round."""

    capture: ActionRule
    submit: ActionRule
    grade: ActionRule
    review: ActionRule
    marks_get: ActionRule


PHASE_CONFIG = PhaseActionTable(
    capture=ActionRule(legal_phases=frozenset({CyclePhase.PRINTED})),
    submit=ActionRule(
        legal_phases=frozenset({CyclePhase.PRINTED}),
        advance=PhaseAdvance(CyclePhase.PRINTED, CyclePhase.ANSWERS_ENTERED),
    ),
    grade=ActionRule(
        legal_phases=frozenset({CyclePhase.ANSWERS_ENTERED, CyclePhase.MARKED}),
        advance=PhaseAdvance(CyclePhase.ANSWERS_ENTERED, CyclePhase.MARKED),
    ),
    review=ActionRule(
        legal_phases=frozenset({CyclePhase.MARKED, CyclePhase.REVIEW_MARKS}),
        advance=PhaseAdvance(CyclePhase.MARKED, CyclePhase.REVIEW_MARKS),
    ),
    # Marks-GET has never had a cycle-phase guard, for any round — preserved as-is.
    marks_get=ActionRule(legal_phases=None),
)


@dataclass(frozen=True)
class RoundConfig:
    """The one other per-round datum this module cares about (design §2 table)."""

    results_child_visible: bool


def round_config(round: int) -> RoundConfig:  # noqa: A002
    """Round 1 (diagnostic) results are child-visible; round 2+ (retest) are
    parent-only in v1 — a per-round config source, not a hardcoded B=False."""
    return RoundConfig(results_child_visible=round <= 1)


def resolve_assessment(cycle: CycleResponse, variant: str) -> Assessment | None:
    """Return the cycle's assessment for the given variant, or None."""
    return next((a for a in cycle.assessments if a.variant == variant), None)


def is_published(repo: FamilyRepository, cycle_id: uuid.UUID, round: int) -> bool:  # noqa: A002
    """Published-immutability predicate, per round (design §4.6, §5).

    Reads the per-round ``cycle_round_approvals`` row rather than the
    single-valued (and now round-ambiguous) ``cycles.marks_published_at``
    column, so a round 2 publish can never be confused with round 1's.
    """
    approval = repo.get_round_approval(cycle_id, round)
    return approval is not None and approval.marks_published_at is not None


def apply_advance(
    rule: ActionRule,
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
    current_phase: CyclePhase,
) -> CycleResponse | None:
    """Call the rule's phase advance iff the cycle is currently at its trigger phase.

    Returns the updated cycle, or None if no advance applied (rule has none,
    or the cycle is not currently at the trigger phase — e.g. idempotent
    re-grade from MARKED).  Propagates ``IllegalTransitionError`` from the
    underlying ``services.cycle`` call; callers decide how to handle it
    (log-and-continue vs. surface as 409), matching each endpoint's existing
    behaviour.
    """
    if rule.advance is None or current_phase != rule.advance.trigger_phase:
        return None
    return advance_phase(repo, cycle_id, rule.advance.target_phase)
