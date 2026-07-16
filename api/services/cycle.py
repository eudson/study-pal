"""Cycle state machine (ARCHITECTURE.md §5).

All cycle state transitions MUST go through this module — never by direct
column updates elsewhere.  Every child-visible transition records parent
approval + timestamp (golden rule 8).

P2 of the generic (round, phase) redesign
(docs/design/round-phase-architecture.md §5, §7): the state machine now
runs on the generic ``advance_phase`` / ``start_next_round`` primitives,
keyed on ``(round, phase)`` rather than the flat 12-value ``CycleState``.
The historical ``advance_to_*`` functions below are kept — signatures
UNCHANGED — as thin delegating wrappers so ``services/phase.py`` and every
router (unmodified in P2) keep working exactly as before.

``state`` remains the DRIVER column persisted on every write (via
``round_phase_to_state``) — this is the P2-P4 compat shim (design §6.4);
it is dropped only in P6.

Implemented transitions (round 1, unchanged since P1):
    SCOPE_UPLOADED → GENERATING_A
    GENERATING_A   → PARENT_REVIEWS_DRAFT
    PARENT_REVIEWS_DRAFT → APPROVED_PRINTED
    APPROVED_PRINTED → ANSWERS_ENTERED
    ANSWERS_ENTERED  → AUTO_MARKED
    AUTO_MARKED      → PARENT_REVIEW_MARKS
    PARENT_REVIEW_MARKS → GAP_REPORT
    GAP_REPORT → GENERATING_STUDY_PACK
    GENERATING_STUDY_PACK → STUDY_PACK_DONE
    STUDY_PACK_DONE → GENERATING_B
    GENERATING_B → CYCLE_COMPLETE

Every one of the above is still reachable, in the same order, producing the
exact same resulting ``CycleState`` values — the regression net for this
refactor (design §7: "existing A tests, re-expressed in phases, stay
green" is a hard gate).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from schemas.family import (
    CyclePhase,
    CycleResponse,
    CycleState,
    VisibilityDefaults,
    round_phase_to_state,
)
from services.repositories.base import FamilyRepository

# ---------------------------------------------------------------------------
# Generic per-phase transition map (docs/design/round-phase-architecture.md
# §2, §5) — round-independent.  This is the "every round traverses this
# identically" middle of the state machine.  PUBLISHED is reached only via
# ``publish_marks`` (needs a ``VisibilityDefaults`` payload the generic
# advance can't carry), so it is not a valid *target* of ``advance_phase``
# in P2 — it stays its own dedicated entry point.  STUDY_PACK has no
# generic "next phase" here: moving to the next round is the dedicated
# ``start_next_round`` transition, not a phase advance.
# ---------------------------------------------------------------------------

_PHASE_ALLOWED: dict[CyclePhase, CyclePhase] = {
    CyclePhase.SCOPE_UPLOADED: CyclePhase.GENERATING,
    CyclePhase.GENERATING: CyclePhase.DRAFT_REVIEW,
    CyclePhase.DRAFT_REVIEW: CyclePhase.PRINTED,
    CyclePhase.PRINTED: CyclePhase.ANSWERS_ENTERED,
    CyclePhase.ANSWERS_ENTERED: CyclePhase.MARKED,
    CyclePhase.MARKED: CyclePhase.REVIEW_MARKS,
    CyclePhase.REVIEW_MARKS: CyclePhase.PUBLISHED,
    CyclePhase.PUBLISHED: CyclePhase.STUDY_PACK,
}

# Transitions that make content visible to the child and require explicit
# parent approval recorded with a timestamp (golden rule 8).
# PUBLISHED is reached via the publish gate (POST /cycles/{id}/publish) which:
#   - freezes the visibility snapshot in published_visibility (marks_published_at)
#   - requires all final_marks to be set (guard)
# This is the approval record for marks visibility; the phase advance is
# handled by publish_marks() below which records its own timestamp.
_REQUIRES_PARENT_APPROVAL: frozenset[CyclePhase] = frozenset(
    {CyclePhase.PRINTED, CyclePhase.PUBLISHED}
)


class IllegalTransitionError(ValueError):
    """Raised when a requested transition is not legal from the current state."""


def _require_cycle(repo: FamilyRepository, cycle_id: uuid.UUID) -> CycleResponse:
    cycle = repo.get_cycle(cycle_id)
    if cycle is None:
        raise ValueError(f"Cycle {cycle_id} not found or not accessible")
    return cycle


def _legal_next_phase(round_: int, phase: CyclePhase) -> CyclePhase | None:
    """Return the only legal next phase from (round_, phase), or None (terminal).

    CRITICAL COMPAT (design §2): round 2's real intermediate phases
    (DRAFT_REVIEW/PRINTED/…) are not wired until P3 — Variant B's entire
    capture→grade→review sub-loop is still crammed into the single
    ``(2, GENERATING)`` state today.  So round >= 2 collapses straight from
    GENERATING to COMPLETE, exactly matching the legacy
    ``GENERATING_B → CYCLE_COMPLETE`` transition.  Every other round/phase
    combination uses the generic, round-independent map.
    """
    if round_ >= 2 and phase is CyclePhase.GENERATING:
        return CyclePhase.COMPLETE
    return _PHASE_ALLOWED.get(phase)


def _assert_phase_transition(round_: int, phase: CyclePhase, target: CyclePhase) -> None:
    """Raise ``IllegalTransitionError`` if (round_, phase) -> target is not legal."""
    legal_next = _legal_next_phase(round_, phase)
    if legal_next != target:
        next_label = repr(legal_next.value) if legal_next else "'none (terminal)'"
        raise IllegalTransitionError(
            f"Cannot transition cycle round {round_} from phase {phase.value!r} to "
            f"{target.value!r}. Expected next phase: {next_label}."
        )


# ---------------------------------------------------------------------------
# Generic phase-driven advance API (design §5, §7 P2)
# ---------------------------------------------------------------------------


def advance_phase(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
    target_phase: CyclePhase,
    *,
    note: str | None = None,
) -> CycleResponse:
    """Advance the cycle's current ``(round, phase)`` to ``target_phase``.

    Validates the transition against the generic per-phase map (round-2
    GENERATING→COMPLETE collapse aside — see ``_legal_next_phase``), then
    persists the equivalent (shadowed) ``CycleState`` via
    ``round_phase_to_state`` — ``round``/``phase`` are always re-derived from
    that state on read (schemas.family.CycleResponse validator), so they can
    never disagree with it.

    ``DRAFT_REVIEW → PRINTED`` is a child-visible gate (golden rule 8): the
    parent approval is recorded on the cycle row (compat) AND dual-written
    into the per-round ``cycle_round_approvals`` table (design §4.6).

    ``PUBLISHED`` is NOT a legal target here — it always carries a
    ``VisibilityDefaults`` payload and stays behind the dedicated
    ``publish_marks`` entry point.
    """
    if target_phase is CyclePhase.PUBLISHED:
        raise IllegalTransitionError(
            "PUBLISHED is not a legal advance_phase target — it requires a "
            "VisibilityDefaults snapshot; use publish_marks() instead."
        )

    cycle = _require_cycle(repo, cycle_id)
    _assert_phase_transition(cycle.round, cycle.phase, target_phase)
    new_state = round_phase_to_state(cycle.round, target_phase)

    if target_phase is CyclePhase.PRINTED:
        approval_at = datetime.now(tz=UTC)
        updated = repo.update_cycle_state(
            cycle_id,
            new_state,
            parent_approval_at=approval_at,
            parent_approval_note=note,
        )
        repo.record_round_draft_approval(cycle_id, cycle.round, approval_at, note)
        return updated

    return repo.update_cycle_state(cycle_id, new_state)


def start_next_round(repo: FamilyRepository, cycle_id: uuid.UUID) -> CycleResponse:
    """(round, STUDY_PACK | PUBLISHED) → (round + 1, GENERATING).

    Legal from a *settled* STUDY_PACK phase (design §6.4: the legacy
    ``STUDY_PACK_DONE`` state — not the transient ``GENERATING_STUDY_PACK``
    mid-generation state, which the lossy ``round_phase_to_state`` mapping
    cannot distinguish from it by phase alone) or from PUBLISHED (pack
    skipped, design §5 pin).
    """
    cycle = _require_cycle(repo, cycle_id)
    settled_study_pack_state = round_phase_to_state(cycle.round, CyclePhase.STUDY_PACK)
    is_legal = cycle.phase is CyclePhase.PUBLISHED or (
        cycle.phase is CyclePhase.STUDY_PACK and cycle.state == settled_study_pack_state
    )
    if not is_legal:
        raise IllegalTransitionError(
            f"Cannot start next round from state {cycle.state.value!r} "
            f"(round={cycle.round}, phase={cycle.phase.value!r}). Expected a settled "
            "'STUDY_PACK' phase or 'PUBLISHED'."
        )
    new_round = cycle.round + 1
    new_state = round_phase_to_state(new_round, CyclePhase.GENERATING)
    return repo.update_cycle_state(cycle_id, new_state)


# ---------------------------------------------------------------------------
# Public transition functions (compat wrappers — signatures UNCHANGED so
# services/phase.py and every router keep working unmodified in P2)
# ---------------------------------------------------------------------------


def advance_to_generating(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """SCOPE_UPLOADED → GENERATING_A.

    Called when generation is kicked off for a cycle.
    """
    return advance_phase(repo, cycle_id, CyclePhase.GENERATING)


def advance_to_parent_reviews(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """GENERATING_A → PARENT_REVIEWS_DRAFT.

    Called when generation completes successfully and the draft is ready.
    """
    return advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)


def approve_draft(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
    note: str | None = None,
) -> CycleResponse:
    """PARENT_REVIEWS_DRAFT → APPROVED_PRINTED.

    Child-visible gate: records ``parent_approval_at`` timestamp and
    optional ``parent_approval_note`` (golden rule 8), dual-written into
    ``cycle_round_approvals`` (design §4.6).
    """
    return advance_phase(repo, cycle_id, CyclePhase.PRINTED, note=note)


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
    return advance_phase(repo, cycle_id, CyclePhase.ANSWERS_ENTERED)


def advance_to_auto_marked(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """ANSWERS_ENTERED → AUTO_MARKED.

    Called after grading completes successfully.
    This is not a child-visible gate — no parent_approval_at recorded here.
    Parent review of marks happens at the PARENT_REVIEW_MARKS gate (Phase 3).
    """
    return advance_phase(repo, cycle_id, CyclePhase.MARKED)


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
    return advance_phase(repo, cycle_id, CyclePhase.REVIEW_MARKS)


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

    Dual-written into the per-round ``cycle_round_approvals`` table (design
    §4.6) so a later round 2 publish can never clobber round 1's record.

    The caller (router) is responsible for the pre-publish guard:
    every question mark must have final_marks set before this is called.

    This function does NOT check the guard itself — that separation keeps
    the service layer thin and the router in control of the 409 response shape.
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_phase_transition(cycle.round, cycle.phase, CyclePhase.PUBLISHED)
    new_state = round_phase_to_state(cycle.round, CyclePhase.PUBLISHED)
    approval_at = datetime.now(tz=UTC)
    updated = repo.publish_marks(
        cycle_id,
        new_state=new_state,
        marks_published_at=approval_at,
        published_visibility=published_visibility,
    )
    repo.record_round_publish(cycle_id, cycle.round, approval_at, published_visibility)
    return updated


def advance_to_generating_study_pack(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """GAP_REPORT → GENERATING_STUDY_PACK.

    Called when study pack generation is kicked off.
    Not a child-visible gate — the child sees nothing until the pack is
    approved via the approve endpoint (golden rule 8).

    LEGACY (design §6 sub-question 1, to be revisited in P3): the generic
    model has a single ``STUDY_PACK`` phase with status inferred from the
    study_pack row (generation is synchronous — no durable "generating"
    state to represent).  ``GENERATING_STUDY_PACK`` therefore has no
    distinct phase of its own; this wrapper validates the ``PUBLISHED →
    STUDY_PACK`` phase transition generically, then writes the exact
    legacy waypoint state directly (``round_phase_to_state`` only knows the
    settled ``STUDY_PACK_DONE`` value for the ``STUDY_PACK`` phase — design
    §6.4 — so it cannot produce this transient state).
    """
    cycle = _require_cycle(repo, cycle_id)
    _assert_phase_transition(cycle.round, cycle.phase, CyclePhase.STUDY_PACK)
    return repo.update_cycle_state(cycle_id, CycleState.GENERATING_STUDY_PACK)


def advance_to_study_pack_done(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """GENERATING_STUDY_PACK → STUDY_PACK_DONE.

    Called when study pack generation completes successfully.
    Not a child-visible gate — child visibility is gated on
    POST /cycles/{id}/study-pack/approve (golden rule 8).

    LEGACY (design §6 sub-question 1, to be revisited in P3): both the
    before and after state share the single generic ``STUDY_PACK`` phase —
    this is a within-phase legacy sub-status transition, not modelled by
    the generic phase map, so it is validated directly against the exact
    legacy waypoint state rather than via ``advance_phase``.
    """
    cycle = _require_cycle(repo, cycle_id)
    if cycle.phase is not CyclePhase.STUDY_PACK or cycle.state != CycleState.GENERATING_STUDY_PACK:
        raise IllegalTransitionError(
            f"Cannot transition cycle from {cycle.state.value!r} to "
            f"{CycleState.STUDY_PACK_DONE.value!r}. Expected current state: "
            f"{CycleState.GENERATING_STUDY_PACK.value!r}."
        )
    return repo.update_cycle_state(cycle_id, CycleState.STUDY_PACK_DONE)


def advance_to_generating_b(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """STUDY_PACK_DONE → GENERATING_B.

    Called when Variant B generation is kicked off (Week 6 retest tail).
    Not a child-visible gate — Variant B results are deferred (nothing
    child-visible in v1); no parent_approval_at is recorded here.

    This is the generic ``start_next_round`` transition: (1, STUDY_PACK
    settled | PUBLISHED) → (2, GENERATING).
    """
    return start_next_round(repo, cycle_id)


def advance_to_cycle_complete(
    repo: FamilyRepository,
    cycle_id: uuid.UUID,
) -> CycleResponse:
    """GENERATING_B → CYCLE_COMPLETE.

    Terminal transition for the cycle. Called once Variant B has been fully
    captured, graded, reviewed, and the A-vs-B comparison is derivable.
    CYCLE_COMPLETE publishes nothing new to the child, so golden rule 8
    (child-visible transitions require recorded parent approval) is not
    triggered here — no parent_approval_at is recorded.

    LEGACY round-2 path (design §2 "CRITICAL COMPAT", to be revisited in
    P3): round 2's real DRAFT_REVIEW/PRINTED/…/PUBLISHED phases are not
    wired yet, so this is the collapsed ``(2, GENERATING) → (2, COMPLETE)``
    transition, handled by ``_legal_next_phase``'s round >= 2 special case.
    """
    return advance_phase(repo, cycle_id, CyclePhase.COMPLETE)
