"""Unit tests for P2/P4 of the generic (round, phase) cycle redesign.

docs/design/round-phase-architecture.md §5 (backend collapse), §7 (P2/P4 scope).

Covers the generic primitives (``advance_phase`` / ``start_next_round``), the
per-round approval dual-write (``cycle_round_approvals``), and confirms every
legacy ``advance_to_*`` wrapper still produces the exact same resulting round
1 ``CycleState`` values as before this redesign (the regression net — design
§7). The P2-era round-2 collapse ("GENERATING -> COMPLETE direct") is retired
in P4 — round 2 now gets real phases uniform with round 1 (design §2); the
tests below exercise that instead (``TestRoundTwoRealPhases``).  ``state`` is
deprecated compat only and is now ROUND-AGNOSTIC (keys off phase alone,
design §5) — round 2's legacy ``state`` values equal round 1's at the same
phase (e.g. both rounds' GENERATING -> ``GENERATING_A``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from typing import Any

import pytest

from schemas.family import CyclePhase, CycleState, VisibilityDefaults
from services.cycle import (
    IllegalTransitionError,
    advance_phase,
    advance_to_answers_entered,
    advance_to_auto_marked,
    advance_to_cycle_complete,
    advance_to_generating,
    advance_to_generating_b,
    advance_to_generating_study_pack,
    advance_to_parent_review_marks,
    advance_to_parent_reviews,
    advance_to_study_pack_done,
    approve_draft,
    publish_marks,
    start_next_round,
)
from services.repositories.memory import InMemoryFamilyRepository


def _new_cycle(repo: InMemoryFamilyRepository) -> uuid.UUID:
    family, _ = repo.bootstrap_family("Test", None, None)
    subject = repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
    cycle = repo.create_cycle(family.id, subject.id, "scope")
    return cycle.id


# ---------------------------------------------------------------------------
# advance_phase — legal transitions
# ---------------------------------------------------------------------------


class TestAdvancePhaseLegal:
    def test_full_round_1_walk_matches_legacy_states(self) -> None:
        """Walking every phase in order lands on the exact legacy CycleState
        at each step — advance_phase is behavior-identical to the old
        _ALLOWED-driven advance_to_* chain."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)

        cycle = advance_phase(repo, cycle_id, CyclePhase.GENERATING)
        assert cycle.state == CycleState.GENERATING_A
        assert cycle.round == 1
        assert cycle.phase == CyclePhase.GENERATING

        cycle = advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)
        assert cycle.state == CycleState.PARENT_REVIEWS_DRAFT

        cycle = advance_phase(repo, cycle_id, CyclePhase.PRINTED)
        assert cycle.state == CycleState.APPROVED_PRINTED
        assert cycle.parent_approval_at is not None

        cycle = advance_phase(repo, cycle_id, CyclePhase.ANSWERS_ENTERED)
        assert cycle.state == CycleState.ANSWERS_ENTERED

        cycle = advance_phase(repo, cycle_id, CyclePhase.MARKED)
        assert cycle.state == CycleState.AUTO_MARKED

        cycle = advance_phase(repo, cycle_id, CyclePhase.REVIEW_MARKS)
        assert cycle.state == CycleState.PARENT_REVIEW_MARKS

    def test_draft_review_to_printed_records_parent_approval_note(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_phase(repo, cycle_id, CyclePhase.GENERATING)
        advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)

        updated = advance_phase(repo, cycle_id, CyclePhase.PRINTED, note="looks good")
        assert updated.parent_approval_note == "looks good"
        assert updated.parent_approval_at is not None


# ---------------------------------------------------------------------------
# advance_phase — illegal transitions
# ---------------------------------------------------------------------------


class TestAdvancePhaseIllegal:
    def test_cannot_skip_phases(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        with pytest.raises(IllegalTransitionError):
            advance_phase(repo, cycle_id, CyclePhase.PRINTED)

    def test_cannot_go_backwards(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_phase(repo, cycle_id, CyclePhase.GENERATING)
        advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)
        with pytest.raises(IllegalTransitionError):
            advance_phase(repo, cycle_id, CyclePhase.GENERATING)

    def test_published_is_not_a_legal_advance_phase_target(self) -> None:
        """PUBLISHED only reachable via publish_marks (needs a visibility payload)."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_phase(repo, cycle_id, CyclePhase.GENERATING)
        advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)
        advance_phase(repo, cycle_id, CyclePhase.PRINTED)
        advance_phase(repo, cycle_id, CyclePhase.ANSWERS_ENTERED)
        advance_phase(repo, cycle_id, CyclePhase.MARKED)
        advance_phase(repo, cycle_id, CyclePhase.REVIEW_MARKS)
        with pytest.raises(IllegalTransitionError):
            advance_phase(repo, cycle_id, CyclePhase.PUBLISHED)


# ---------------------------------------------------------------------------
# Round-2 REAL phases (P4, design §2, §3) — the P2 collapse is retired.
# Round 2 now traverses the exact same ``_PHASE_ALLOWED`` map as round 1.
# ---------------------------------------------------------------------------


def _cycle_at_round_2_generating(repo: InMemoryFamilyRepository) -> uuid.UUID:
    cycle_id = _new_cycle(repo)
    advance_to_generating(repo, cycle_id)
    advance_to_parent_reviews(repo, cycle_id)
    approve_draft(repo, cycle_id)
    advance_to_answers_entered(repo, cycle_id)
    advance_to_auto_marked(repo, cycle_id)
    advance_to_parent_review_marks(repo, cycle_id)
    publish_marks(repo, cycle_id, VisibilityDefaults())
    advance_to_generating_study_pack(repo, cycle_id)
    advance_to_study_pack_done(repo, cycle_id)
    advance_to_generating_b(repo, cycle_id)
    return cycle_id


def _walk_round_2_to_published(repo: InMemoryFamilyRepository, cycle_id: uuid.UUID) -> None:
    """From (2, GENERATING) walk the full real phase sequence to (2, PUBLISHED)."""
    advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)
    advance_phase(repo, cycle_id, CyclePhase.PRINTED)
    advance_phase(repo, cycle_id, CyclePhase.ANSWERS_ENTERED)
    advance_phase(repo, cycle_id, CyclePhase.MARKED)
    advance_phase(repo, cycle_id, CyclePhase.REVIEW_MARKS)
    publish_marks(repo, cycle_id, VisibilityDefaults())


class TestRoundTwoRealPhases:
    def test_round_2_generating_walks_full_phase_sequence(self) -> None:
        """Round 2 traverses DRAFT_REVIEW -> PRINTED -> ... -> PUBLISHED, exactly
        like round 1 — the P2 collapse ("GENERATING -> COMPLETE direct") is gone."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _cycle_at_round_2_generating(repo)
        cycle = repo.get_cycle(cycle_id)
        assert cycle is not None
        assert cycle.round == 2
        assert cycle.phase == CyclePhase.GENERATING

        updated = advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)
        assert updated.round == 2
        assert updated.phase == CyclePhase.DRAFT_REVIEW
        # `state` is deprecated + ROUND-AGNOSTIC (design §5): round 2's
        # DRAFT_REVIEW carries the exact same legacy value as round 1's.
        assert updated.state == CycleState.PARENT_REVIEWS_DRAFT

        updated = advance_phase(repo, cycle_id, CyclePhase.PRINTED)
        assert updated.state == CycleState.APPROVED_PRINTED
        assert updated.parent_approval_at is not None

        updated = advance_phase(repo, cycle_id, CyclePhase.ANSWERS_ENTERED)
        assert updated.state == CycleState.ANSWERS_ENTERED

        updated = advance_phase(repo, cycle_id, CyclePhase.MARKED)
        assert updated.state == CycleState.AUTO_MARKED

        updated = advance_phase(repo, cycle_id, CyclePhase.REVIEW_MARKS)
        assert updated.state == CycleState.PARENT_REVIEW_MARKS

        published = publish_marks(repo, cycle_id, VisibilityDefaults())
        assert published.round == 2
        assert published.phase == CyclePhase.PUBLISHED
        assert published.state == CycleState.GAP_REPORT

    def test_round_2_generating_cannot_skip_to_printed(self) -> None:
        """Round 2 still cannot skip phases — DRAFT_REVIEW must come first."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _cycle_at_round_2_generating(repo)
        with pytest.raises(IllegalTransitionError):
            advance_phase(repo, cycle_id, CyclePhase.PRINTED)

    def test_round_2_draft_review_records_its_own_approval(self) -> None:
        """Round 2's DRAFT_REVIEW -> PRINTED gate records a round-2 approval
        row, distinct from round 1's (golden rule 8, per round)."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _cycle_at_round_2_generating(repo)
        advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)
        approve_draft(repo, cycle_id, note="round 2 approved")

        approval = repo.get_round_approval(cycle_id, 2)
        assert approval is not None
        assert approval.draft_approval_note == "round 2 approved"

        # Round 1's approval row is untouched.
        round_1_approval = repo.get_round_approval(cycle_id, 1)
        assert round_1_approval is not None
        assert round_1_approval.draft_approval_note is None


# ---------------------------------------------------------------------------
# start_next_round
# ---------------------------------------------------------------------------


class TestStartNextRound:
    def test_legal_from_settled_study_pack(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        approve_draft(repo, cycle_id)
        advance_to_answers_entered(repo, cycle_id)
        advance_to_auto_marked(repo, cycle_id)
        advance_to_parent_review_marks(repo, cycle_id)
        publish_marks(repo, cycle_id, VisibilityDefaults())
        advance_to_generating_study_pack(repo, cycle_id)
        advance_to_study_pack_done(repo, cycle_id)

        updated = start_next_round(repo, cycle_id)
        # `state` is deprecated + ROUND-AGNOSTIC (design §5, P4): round 2's
        # GENERATING carries the exact same legacy value as round 1's.
        assert updated.state == CycleState.GENERATING_A
        assert updated.round == 2
        assert updated.phase == CyclePhase.GENERATING

    def test_legal_from_published_pack_skipped(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        approve_draft(repo, cycle_id)
        advance_to_answers_entered(repo, cycle_id)
        advance_to_auto_marked(repo, cycle_id)
        advance_to_parent_review_marks(repo, cycle_id)
        publish_marks(repo, cycle_id, VisibilityDefaults())

        updated = start_next_round(repo, cycle_id)
        assert updated.state == CycleState.GENERATING_A
        assert updated.round == 2

    def test_illegal_mid_study_pack_generation(self) -> None:
        """The transient GENERATING_STUDY_PACK state must NOT be usable to
        start the next round — only the settled STUDY_PACK_DONE state is."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        approve_draft(repo, cycle_id)
        advance_to_answers_entered(repo, cycle_id)
        advance_to_auto_marked(repo, cycle_id)
        advance_to_parent_review_marks(repo, cycle_id)
        publish_marks(repo, cycle_id, VisibilityDefaults())
        advance_to_generating_study_pack(repo, cycle_id)

        with pytest.raises(IllegalTransitionError):
            start_next_round(repo, cycle_id)

    def test_illegal_from_scope_uploaded(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        with pytest.raises(IllegalTransitionError):
            start_next_round(repo, cycle_id)


# ---------------------------------------------------------------------------
# Per-round approval dual-write (design §4.6)
# ---------------------------------------------------------------------------


class TestPerRoundApprovalDualWrite:
    def test_approve_draft_dual_writes_round_approval(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)

        updated = approve_draft(repo, cycle_id, note="approved")

        approval = repo.get_round_approval(cycle_id, 1)
        assert approval is not None
        assert approval.draft_approved_at == updated.parent_approval_at
        assert approval.draft_approval_note == "approved"
        assert approval.marks_published_at is None

    def test_publish_marks_dual_writes_round_approval_without_clobbering_draft(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        approve_draft(repo, cycle_id, note="approved draft")
        advance_to_answers_entered(repo, cycle_id)
        advance_to_auto_marked(repo, cycle_id)
        advance_to_parent_review_marks(repo, cycle_id)

        visibility = VisibilityDefaults(accuracy=True, effort=False, growing=True)
        updated = publish_marks(repo, cycle_id, visibility)

        approval = repo.get_round_approval(cycle_id, 1)
        assert approval is not None
        # Draft-approval half untouched by the publish dual-write.
        assert approval.draft_approval_note == "approved draft"
        assert approval.draft_approved_at is not None
        # Publish half now recorded, matching the single-valued cycle columns.
        assert approval.marks_published_at == updated.marks_published_at
        assert approval.published_visibility == visibility

    def test_round_2_would_not_clobber_round_1_approval_row(self) -> None:
        """The exact golden-rule-8 gap cycle_round_approvals closes: a second
        round's approval must land in its own (cycle_id, round) row."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _cycle_at_round_2_generating(repo)

        round_1_approval = repo.get_round_approval(cycle_id, 1)
        assert round_1_approval is not None
        assert round_1_approval.draft_approved_at is not None
        assert round_1_approval.marks_published_at is not None

        # Round 2 has no approval row yet (no draft/publish gate reached for
        # round 2 in P2 — the collapsed GENERATING->COMPLETE legacy path
        # never calls approve_draft/publish_marks for round 2).
        assert repo.get_round_approval(cycle_id, 2) is None

        # Round 1's row is still intact after round 2 started.
        still_round_1 = repo.get_round_approval(cycle_id, 1)
        assert still_round_1 == round_1_approval


# ---------------------------------------------------------------------------
# Legacy advance_to_* wrappers — exact state preservation (regression net)
# ---------------------------------------------------------------------------


class TestLegacyWrapperCompat:
    def test_study_pack_legacy_states_preserved(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        approve_draft(repo, cycle_id)
        advance_to_answers_entered(repo, cycle_id)
        advance_to_auto_marked(repo, cycle_id)
        advance_to_parent_review_marks(repo, cycle_id)
        publish_marks(repo, cycle_id, VisibilityDefaults())

        generating = advance_to_generating_study_pack(repo, cycle_id)
        assert generating.state == CycleState.GENERATING_STUDY_PACK
        assert generating.phase == CyclePhase.STUDY_PACK

        done = advance_to_study_pack_done(repo, cycle_id)
        assert done.state == CycleState.STUDY_PACK_DONE
        assert done.phase == CyclePhase.STUDY_PACK

    def test_study_pack_done_illegal_without_generating_first(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        approve_draft(repo, cycle_id)
        advance_to_answers_entered(repo, cycle_id)
        advance_to_auto_marked(repo, cycle_id)
        advance_to_parent_review_marks(repo, cycle_id)
        publish_marks(repo, cycle_id, VisibilityDefaults())

        with pytest.raises(IllegalTransitionError):
            advance_to_study_pack_done(repo, cycle_id)

    def test_cycle_complete_matches_legacy_state(self) -> None:
        """P4: COMPLETE is reached from round 2's own PUBLISHED phase — round
        2 no longer collapses straight from GENERATING (design §2, §5 pin)."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _cycle_at_round_2_generating(repo)
        _walk_round_2_to_published(repo, cycle_id)
        updated = advance_to_cycle_complete(repo, cycle_id)
        assert updated.state == CycleState.CYCLE_COMPLETE
        assert updated.round == 2
        assert updated.phase == CyclePhase.COMPLETE

    def test_cycle_complete_legal_directly_from_round_1_when_no_retest(self) -> None:
        """Design §5 pin: COMPLETE is reachable from PUBLISHED/settled STUDY_PACK
        of THE FINAL round — a single-round cycle (no retest) can complete
        directly from round 1, without ever starting round 2."""
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _new_cycle(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        approve_draft(repo, cycle_id)
        advance_to_answers_entered(repo, cycle_id)
        advance_to_auto_marked(repo, cycle_id)
        advance_to_parent_review_marks(repo, cycle_id)
        publish_marks(repo, cycle_id, VisibilityDefaults())
        advance_to_generating_study_pack(repo, cycle_id)
        advance_to_study_pack_done(repo, cycle_id)

        updated = advance_to_cycle_complete(repo, cycle_id)
        assert updated.round == 1
        assert updated.phase == CyclePhase.COMPLETE
        assert updated.state == CycleState.CYCLE_COMPLETE

    def test_cycle_complete_illegal_from_generating(self) -> None:
        repo = InMemoryFamilyRepository(uuid.uuid4())
        cycle_id = _cycle_at_round_2_generating(repo)
        with pytest.raises(IllegalTransitionError):
            advance_to_cycle_complete(repo, cycle_id)


# ---------------------------------------------------------------------------
# DB-tier — cycle_round_approvals dual-write against real Postgres
# (skipped when Postgres is unreachable; mirrors tests/test_child_profile.py)
# ---------------------------------------------------------------------------

_DSN = os.environ.get("STUDYPAL_DB_DSN", "postgresql://studypal:studypal@localhost:5432/studypal")


def _try_connect_pg() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(_DSN, connect_timeout=3, autocommit=True)
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def _pg_owner_conn() -> Generator[Any, None, None]:
    import psycopg

    conn = psycopg.connect(_DSN, autocommit=False)
    yield conn
    conn.close()


def _pg_authed_repo(user_id: uuid.UUID) -> tuple[Any, Any]:
    """Return (conn, PostgresFamilyRepository) for an authenticated user."""
    from config import get_settings
    from schemas.identity import Identity
    from services.repositories.postgres import open_authenticated_connection
    from services.repositories.postgres_family import PostgresFamilyRepository

    settings = get_settings()
    identity = Identity(user_id=user_id)
    conn = open_authenticated_connection(settings.db_dsn, identity)
    repo = PostgresFamilyRepository(conn)
    return conn, repo


@pytest.mark.skipif(not _try_connect_pg(), reason="Postgres not reachable")
class TestPostgresPerRoundApprovalDualWrite:
    """DB-tier: cycle_round_approvals dual-write, against the real 0010 table."""

    def test_approve_and_publish_round_trip(self, _pg_owner_conn: Any) -> None:
        user_id = uuid.uuid4()
        conn, repo = _pg_authed_repo(user_id)
        family_id: uuid.UUID | None = None
        try:
            family, _ = repo.bootstrap_family("PhaseP2Family", None, None)
            family_id = family.id
            child = repo.create_child(family.id, "Kid", "Grade 5")
            subject = repo.create_subject(family.id, child.id, "Maths", "en")
            cycle = repo.create_cycle(family.id, subject.id, "scope")
            cycle_id = cycle.id

            updated = advance_to_generating(repo, cycle_id)
            assert updated.state == CycleState.GENERATING_A
            updated = advance_to_parent_reviews(repo, cycle_id)
            assert updated.state == CycleState.PARENT_REVIEWS_DRAFT

            approved = approve_draft(repo, cycle_id, note="db-tier approval")
            assert approved.state == CycleState.APPROVED_PRINTED
            assert approved.parent_approval_at is not None

            approval = repo.get_round_approval(cycle_id, 1)
            assert approval is not None
            assert approval.round == 1
            assert approval.draft_approved_at == approved.parent_approval_at
            assert approval.draft_approval_note == "db-tier approval"
            assert approval.marks_published_at is None

            advance_to_answers_entered(repo, cycle_id)
            advance_to_auto_marked(repo, cycle_id)
            advance_to_parent_review_marks(repo, cycle_id)
            visibility = VisibilityDefaults(accuracy=True, effort=False, growing=True)
            published = publish_marks(repo, cycle_id, visibility)
            assert published.state == CycleState.GAP_REPORT

            approval = repo.get_round_approval(cycle_id, 1)
            assert approval is not None
            # Draft half untouched by the publish dual-write.
            assert approval.draft_approval_note == "db-tier approval"
            # Publish half matches the (still shadowed) single-valued cycle columns.
            assert approval.marks_published_at == published.marks_published_at
            assert approval.published_visibility == visibility
        finally:
            conn.close()
            if family_id is not None:
                cur = _pg_owner_conn.cursor()
                cur.execute(
                    "DELETE FROM cycle_round_approvals WHERE family_id = %s", (str(family_id),)
                )
                cur.execute(
                    "DELETE FROM cycles WHERE family_id = %s",
                    (str(family_id),),
                )
                cur.execute(
                    "DELETE FROM subjects WHERE family_id = %s",
                    (str(family_id),),
                )
                cur.execute(
                    "DELETE FROM children WHERE family_id = %s",
                    (str(family_id),),
                )
                cur.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
                cur.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
                _pg_owner_conn.commit()
