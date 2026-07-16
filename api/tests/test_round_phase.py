"""Tests for the additive (round, phase) axis (P1 of the round/phase redesign).

docs/design/round-phase-architecture.md §4 (backfill mapping), §7 (P1 scope).

`CycleState` remains the state-machine DRIVER in P1 — these tests assert only
that the mapping functions are total/correct and that `CycleResponse` always
derives consistent round/phase from state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from schemas.family import (
    CyclePhase,
    CycleResponse,
    CycleState,
    round_phase_to_state,
    state_to_round_phase,
)

# ---------------------------------------------------------------------------
# Backfill totality (design §4 backfill table)
# ---------------------------------------------------------------------------


def test_state_to_round_phase_is_total_over_all_states() -> None:
    """Every CycleState member maps to exactly one (round, phase) pair."""
    for state in CycleState:
        round_, phase = state_to_round_phase(state)
        assert isinstance(round_, int)
        assert isinstance(phase, CyclePhase)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (CycleState.SCOPE_UPLOADED, (1, CyclePhase.SCOPE_UPLOADED)),
        (CycleState.GENERATING_A, (1, CyclePhase.GENERATING)),
        (CycleState.PARENT_REVIEWS_DRAFT, (1, CyclePhase.DRAFT_REVIEW)),
        (CycleState.APPROVED_PRINTED, (1, CyclePhase.PRINTED)),
        (CycleState.ANSWERS_ENTERED, (1, CyclePhase.ANSWERS_ENTERED)),
        (CycleState.AUTO_MARKED, (1, CyclePhase.MARKED)),
        (CycleState.PARENT_REVIEW_MARKS, (1, CyclePhase.REVIEW_MARKS)),
        (CycleState.GAP_REPORT, (1, CyclePhase.PUBLISHED)),
        (CycleState.GENERATING_STUDY_PACK, (1, CyclePhase.STUDY_PACK)),
        (CycleState.STUDY_PACK_DONE, (1, CyclePhase.STUDY_PACK)),
        (CycleState.GENERATING_B, (2, CyclePhase.GENERATING)),
        (CycleState.CYCLE_COMPLETE, (2, CyclePhase.COMPLETE)),
    ],
)
def test_state_to_round_phase_matches_design_table(
    state: CycleState, expected: tuple[int, CyclePhase]
) -> None:
    assert state_to_round_phase(state) == expected


def test_state_to_round_phase_endpoints() -> None:
    """Design §4: get the (1, SCOPE_UPLOADED) and (2, COMPLETE) endpoints right."""
    assert state_to_round_phase(CycleState.SCOPE_UPLOADED) == (1, CyclePhase.SCOPE_UPLOADED)
    assert state_to_round_phase(CycleState.CYCLE_COMPLETE) == (2, CyclePhase.COMPLETE)


def test_state_to_round_phase_covers_every_state_exactly_once() -> None:
    """Total + unambiguous: every state maps to exactly one (round, phase)."""
    seen: dict[CycleState, tuple[int, CyclePhase]] = {}
    for state in CycleState:
        result = state_to_round_phase(state)
        assert state not in seen
        seen[state] = result
    assert len(seen) == len(list(CycleState))


# ---------------------------------------------------------------------------
# Reverse mapping (design §6.4 — NOT 1:1; STUDY_PACK collapses two states)
# ---------------------------------------------------------------------------


def test_round_phase_to_state_endpoints() -> None:
    assert round_phase_to_state(1, CyclePhase.SCOPE_UPLOADED) == CycleState.SCOPE_UPLOADED
    assert round_phase_to_state(2, CyclePhase.COMPLETE) == CycleState.CYCLE_COMPLETE


def test_round_phase_to_state_study_pack_canonical_choice() -> None:
    """(1, STUDY_PACK) -> STUDY_PACK_DONE (canonical choice, design §6.4)."""
    assert round_phase_to_state(1, CyclePhase.STUDY_PACK) == CycleState.STUDY_PACK_DONE


@pytest.mark.parametrize("state", list(CycleState))
def test_round_trip_where_defined(state: CycleState) -> None:
    """state -> (round, phase) -> state round-trips for every state except the

    two that collapse into the single STUDY_PACK phase (design §6.4): only
    STUDY_PACK_DONE is the canonical reverse of (1, STUDY_PACK), so
    GENERATING_STUDY_PACK does not round-trip to itself (by design).
    """
    round_, phase = state_to_round_phase(state)
    reconstructed = round_phase_to_state(round_, phase)
    if state is CycleState.GENERATING_STUDY_PACK:
        assert reconstructed == CycleState.STUDY_PACK_DONE
    else:
        assert reconstructed == state


# ---------------------------------------------------------------------------
# CycleResponse always carries round/phase consistent with state
# ---------------------------------------------------------------------------


def _make_cycle_response(state: CycleState) -> CycleResponse:
    now = datetime.now(tz=UTC)
    return CycleResponse(
        id=uuid.uuid4(),
        family_id=uuid.uuid4(),
        subject_id=uuid.uuid4(),
        state=state,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("state", list(CycleState))
def test_cycle_response_derives_round_phase_from_state(state: CycleState) -> None:
    cycle = _make_cycle_response(state)
    expected_round, expected_phase = state_to_round_phase(state)
    assert cycle.round == expected_round
    assert cycle.phase == expected_phase


def test_cycle_response_explicit_round_phase_trusted_when_consistent() -> None:
    """Callers may pass round/phase explicitly; they are trusted as-is."""
    now = datetime.now(tz=UTC)
    cycle = CycleResponse(
        id=uuid.uuid4(),
        family_id=uuid.uuid4(),
        subject_id=uuid.uuid4(),
        state=CycleState.GENERATING_B,
        round=2,
        phase=CyclePhase.GENERATING,
        created_at=now,
        updated_at=now,
    )
    assert cycle.round == 2
    assert cycle.phase == CyclePhase.GENERATING
