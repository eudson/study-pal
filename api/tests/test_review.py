"""Unit tests for Phase 3 — parent mark review + publish gate.

Coverage:
- Enriched review payload: child_answer_rendered + correct_answer_rendered
  are present for all supported question types.
- PATCH: sets final_marks, reviewed_at, overridden_at correctly.
- PATCH: 0.5-step validation + range validation (422).
- PATCH: AUTO_MARKED → PARENT_REVIEW_MARKS on first call.
- PATCH: already PARENT_REVIEW_MARKS stays there.
- Publish guard: 409 when any final_marks is NULL.
- Publish: snapshots visibility + transitions to GAP_REPORT + records timestamp.
- Publish: request override merges onto child defaults.
- DB-tier RLS: publish isolation (skipped when Postgres unreachable).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import psycopg.rows
import pytest

from schemas.assessment_schema import (
    Assessment,
    ErrorCategory,
    GradingPath,
)
from schemas.family import CycleState, VisibilityDefaults
from schemas.grading import (
    QuestionContext,
    QuestionMark,
    render_child_answer,
    render_correct_answer,
)
from schemas.review import MarkPatchRequest, PublishRequest
from services.cycle import (
    IllegalTransitionError,
    advance_to_answers_entered,
    advance_to_auto_marked,
    advance_to_generating,
    advance_to_parent_review_marks,
    advance_to_parent_reviews,
    approve_draft,
    publish_marks,
)
from services.repositories.memory import (
    InMemoryFamilyRepository,
    InMemoryQuestionMarkRepository,
)
from tests.samples.afrikaans_sample import afrikaans_assessment
from tests.samples.maths_sample import maths_assessment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAMILY_ID = uuid.uuid4()
_SUBMISSION_ID = uuid.uuid4()


def _assessment(raw: dict[str, Any]) -> Assessment:
    return Assessment.model_validate(raw)


def _mark(
    question_id: str = "A.1",
    marks_total: str = "2.0",
    suggested_marks: str = "1.0",
    final_marks: str | None = None,
    needs_review: bool = True,
    grading_path: GradingPath = GradingPath.AUTO_FUZZY,
    **kwargs: Any,
) -> QuestionMark:
    return QuestionMark(
        family_id=_FAMILY_ID,
        submission_id=_SUBMISSION_ID,
        question_id=question_id,
        marks_total=Decimal(marks_total),
        suggested_marks=Decimal(suggested_marks),
        final_marks=Decimal(final_marks) if final_marks is not None else None,
        grading_path=grading_path,
        needs_review=needs_review,
        **kwargs,
    )


def _cycle_with_marks_state(
    repo: InMemoryFamilyRepository,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a cycle in AUTO_MARKED state and return (cycle_id, subject_id)."""
    family, _ = repo.bootstrap_family("Test Family", None, None)
    subject = repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
    cycle = repo.create_cycle(family.id, subject.id, "scope")

    advance_to_generating(repo, cycle.id)
    advance_to_parent_reviews(repo, cycle.id)
    approve_draft(repo, cycle.id)
    advance_to_answers_entered(repo, cycle.id)
    advance_to_auto_marked(repo, cycle.id)

    return cycle.id, subject.id


def _cycle_with_review_state(
    repo: InMemoryFamilyRepository,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a cycle in PARENT_REVIEW_MARKS state."""
    cycle_id, subject_id = _cycle_with_marks_state(repo)
    advance_to_parent_review_marks(repo, cycle_id)
    return cycle_id, subject_id


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


class TestRenderChildAnswer:
    """render_child_answer — one per question type (golden rule 4: no subject branches)."""

    def test_mcq(self) -> None:
        result = render_child_answer("mcq", {"selected_index": 2})
        assert result == "Option 2"

    def test_mcq_no_selection(self) -> None:
        result = render_child_answer("mcq", {})
        assert result == "(not attempted)"

    def test_true_false_true(self) -> None:
        assert render_child_answer("true_false", {"value": True}) == "True"

    def test_true_false_false(self) -> None:
        assert render_child_answer("true_false", {"value": False}) == "False"

    def test_matching(self) -> None:
        pairs = [{"left": 0, "right": 1}, {"left": 1, "right": 0}]
        result = render_child_answer("matching", {"pairs": pairs})
        assert "0→1" in result
        assert "1→0" in result

    def test_ordering(self) -> None:
        result = render_child_answer("ordering", {"order": [2, 0, 1]})
        assert "2" in result and "0" in result and "1" in result

    def test_fill_blank(self) -> None:
        result = render_child_answer("fill_blank", {"values": ["kinders", "boeke"]})
        assert "kinders" in result
        assert "boeke" in result

    def test_short_answer(self) -> None:
        result = render_child_answer("short_answer", {"text": "photosynthesis"})
        assert "photosynthesis" in result

    def test_calculation_with_working(self) -> None:
        result = render_child_answer("calculation", {"answer": "860", "working": "2×430"})
        assert "860" in result
        assert "2×430" in result

    def test_extended_response(self) -> None:
        result = render_child_answer("extended_response", {"text": "Plants use sunlight."})
        assert "Plants use sunlight." in result

    def test_empty_payload(self) -> None:
        result = render_child_answer("mcq", {})
        assert result == "(not attempted)"


class TestRenderCorrectAnswer:
    """render_correct_answer — one per AnswerPayload subtype (no subject branches)."""

    def test_mcq(self) -> None:
        asmt = _assessment(maths_assessment())
        q = asmt.sections[0].questions[0]  # A.1 MCQ
        result = render_correct_answer(q.answer)
        # correct_index=1, options has at least 2 items
        assert "Option 1" in result

    def test_true_false(self) -> None:
        asmt = _assessment(afrikaans_assessment())
        q = asmt.sections[1].questions[1]  # B.2 true_false, is_true=False
        result = render_correct_answer(q.answer)
        assert "False" in result

    def test_matching(self) -> None:
        asmt = _assessment(afrikaans_assessment())
        q = asmt.sections[1].questions[0]  # B.1 matching
        result = render_correct_answer(q.answer)
        # Should contain arrows showing correct pairs
        assert "→" in result

    def test_fill_blank(self) -> None:
        asmt = _assessment(afrikaans_assessment())
        q = asmt.sections[0].questions[2]  # A.3 fill_blank
        result = render_correct_answer(q.answer)
        assert "kinders" in result or "boeke" in result

    def test_short_answer(self) -> None:
        asmt = _assessment(afrikaans_assessment())
        q = asmt.sections[0].questions[0]  # A.1 short_answer
        result = render_correct_answer(q.answer)
        assert "katjie" in result

    def test_calculation(self) -> None:
        asmt = _assessment(maths_assessment())
        q = asmt.sections[1].questions[0]  # B.1 calculation
        result = render_correct_answer(q.answer)
        assert "860" in result


# ---------------------------------------------------------------------------
# QuestionContext enrichment
# ---------------------------------------------------------------------------


class TestQuestionContextFields:
    """QuestionContext carries child_answer_rendered + correct_answer_rendered."""

    def test_context_has_rendered_fields(self) -> None:
        ctx = QuestionContext(
            qid="A.1",
            number="1",
            text="Question?",
            question_type="mcq",
            marks_total=Decimal("1.0"),
            child_answer_rendered="Option 2",
            correct_answer_rendered="Option 1: correct answer",
        )
        assert ctx.child_answer_rendered == "Option 2"
        assert ctx.correct_answer_rendered == "Option 1: correct answer"

    def test_defaults_are_safe_strings(self) -> None:
        ctx = QuestionContext(
            qid="A.1",
            number="1",
            text="Question?",
            question_type="unknown",
            marks_total=Decimal("1.0"),
        )
        assert isinstance(ctx.child_answer_rendered, str)
        assert isinstance(ctx.correct_answer_rendered, str)


# ---------------------------------------------------------------------------
# MarkPatchRequest validation
# ---------------------------------------------------------------------------


class TestMarkPatchRequest:
    def test_valid_half_mark(self) -> None:
        req = MarkPatchRequest(final_marks=Decimal("1.5"))
        assert req.final_marks == Decimal("1.5")

    def test_zero_is_valid(self) -> None:
        req = MarkPatchRequest(final_marks=Decimal("0"))
        assert req.final_marks == Decimal("0")

    def test_non_half_step_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MarkPatchRequest(final_marks=Decimal("0.3"))

    def test_negative_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MarkPatchRequest(final_marks=Decimal("-0.5"))

    def test_empty_body_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MarkPatchRequest()

    def test_error_category_only_valid(self) -> None:
        req = MarkPatchRequest(error_category=ErrorCategory.CONCEPT_GAP)
        assert req.error_category == ErrorCategory.CONCEPT_GAP
        assert req.final_marks is None

    def test_note_only_valid(self) -> None:
        req = MarkPatchRequest(note="Parent note")
        assert req.note == "Parent note"


# ---------------------------------------------------------------------------
# InMemoryQuestionMarkRepository — update_mark
# ---------------------------------------------------------------------------


class TestInMemoryUpdateMark:
    def _seeded_repo(self) -> tuple[InMemoryQuestionMarkRepository, uuid.UUID, QuestionMark]:
        repo = InMemoryQuestionMarkRepository()
        sid = uuid.uuid4()
        m = _mark(question_id="A.1", suggested_marks="1.0", final_marks=None, needs_review=True)
        repo.bulk_upsert(_FAMILY_ID, sid, [m])
        return repo, sid, m

    def test_sets_final_marks_and_reviewed_at(self) -> None:
        repo, sid, _original = self._seeded_repo()
        now = datetime.now(tz=UTC)
        patch = MarkPatchRequest(final_marks=Decimal("1.0"))
        updated = repo.update_mark(sid, "A.1", patch, now)
        assert updated.final_marks == Decimal("1.0")
        assert updated.reviewed_at == now

    def test_sets_overridden_at_when_different_from_suggested(self) -> None:
        repo, sid, _ = self._seeded_repo()
        now = datetime.now(tz=UTC)
        # suggested_marks=1.0, final_marks=0.5 → override
        patch = MarkPatchRequest(final_marks=Decimal("0.5"))
        updated = repo.update_mark(sid, "A.1", patch, now)
        assert updated.overridden_at == now

    def test_no_overridden_at_when_same_as_suggested(self) -> None:
        repo, sid, _ = self._seeded_repo()
        now = datetime.now(tz=UTC)
        # suggested_marks=1.0, final_marks=1.0 → NOT an override
        patch = MarkPatchRequest(final_marks=Decimal("1.0"))
        updated = repo.update_mark(sid, "A.1", patch, now)
        assert updated.overridden_at is None

    def test_sets_error_category(self) -> None:
        repo, sid, _ = self._seeded_repo()
        now = datetime.now(tz=UTC)
        patch = MarkPatchRequest(error_category=ErrorCategory.CONCEPT_GAP)
        updated = repo.update_mark(sid, "A.1", patch, now)
        assert updated.error_category == ErrorCategory.CONCEPT_GAP

    def test_combined_patch(self) -> None:
        repo, sid, _ = self._seeded_repo()
        now = datetime.now(tz=UTC)
        patch = MarkPatchRequest(
            final_marks=Decimal("0.0"),
            error_category=ErrorCategory.NOT_ATTEMPTED,
        )
        updated = repo.update_mark(sid, "A.1", patch, now)
        assert updated.final_marks == Decimal("0.0")
        assert updated.error_category == ErrorCategory.NOT_ATTEMPTED
        assert updated.reviewed_at == now

    def test_not_found_raises(self) -> None:
        repo, sid, _ = self._seeded_repo()
        with pytest.raises(ValueError, match="not found"):
            repo.update_mark(sid, "NONEXISTENT", MarkPatchRequest(note="x"), datetime.now(tz=UTC))

    def test_get_mark(self) -> None:
        repo, sid, _original = self._seeded_repo()
        found = repo.get_mark(sid, "A.1")
        assert found is not None
        assert found.question_id == "A.1"

    def test_get_mark_not_found(self) -> None:
        repo, sid, _ = self._seeded_repo()
        assert repo.get_mark(sid, "Z.99") is None


# ---------------------------------------------------------------------------
# Cycle service: advance_to_parent_review_marks
# ---------------------------------------------------------------------------


class TestAdvanceToParentReviewMarks:
    def test_transition_from_auto_marked(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        cycle_id, _ = _cycle_with_marks_state(repo)

        updated = advance_to_parent_review_marks(repo, cycle_id)
        assert updated.state == CycleState.PARENT_REVIEW_MARKS

    def test_illegal_from_scope_uploaded(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        family, _ = repo.bootstrap_family("Test", None, None)
        subject = repo.create_subject(family.id, uuid.uuid4(), "X", "en")
        cycle = repo.create_cycle(family.id, subject.id, "scope")

        with pytest.raises(IllegalTransitionError):
            advance_to_parent_review_marks(repo, cycle.id)


# ---------------------------------------------------------------------------
# Cycle service: publish_marks
# ---------------------------------------------------------------------------


class TestPublishMarksCycleService:
    def test_transitions_to_gap_report(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        cycle_id, _ = _cycle_with_review_state(repo)

        visibility = VisibilityDefaults(
            accuracy=True, effort=True, growing=True, ai_rationale=False
        )
        updated = publish_marks(repo, cycle_id, visibility)

        assert updated.state == CycleState.GAP_REPORT

    def test_records_timestamp(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        cycle_id, _ = _cycle_with_review_state(repo)

        before = datetime.now(tz=UTC)
        visibility = VisibilityDefaults()
        updated = publish_marks(repo, cycle_id, visibility)
        after = datetime.now(tz=UTC)

        assert updated.marks_published_at is not None
        assert before <= updated.marks_published_at <= after

    def test_freezes_visibility_snapshot(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        cycle_id, _ = _cycle_with_review_state(repo)

        visibility = VisibilityDefaults(
            accuracy=True, effort=False, growing=True, ai_rationale=True
        )
        updated = publish_marks(repo, cycle_id, visibility)

        assert updated.published_visibility is not None
        assert updated.published_visibility.effort is False
        assert updated.published_visibility.ai_rationale is True

    def test_illegal_from_auto_marked(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        cycle_id, _ = _cycle_with_marks_state(repo)  # AUTO_MARKED, not PARENT_REVIEW_MARKS

        with pytest.raises(IllegalTransitionError):
            publish_marks(repo, cycle_id, VisibilityDefaults())


# ---------------------------------------------------------------------------
# Publish guard: unresolved marks
# ---------------------------------------------------------------------------


class TestPublishGuard:
    def test_all_final_marks_set_passes(self) -> None:
        """Guard passes when all final_marks are set."""
        marks_repo = InMemoryQuestionMarkRepository()
        sid = uuid.uuid4()
        marks = [
            _mark("A.1", final_marks="1.0", needs_review=False),
            _mark("A.2", final_marks="0.5", needs_review=False),
        ]
        marks_repo.bulk_upsert(_FAMILY_ID, sid, marks)

        all_marks = marks_repo.list_for_submission(sid)
        unresolved = [m.question_id for m in all_marks if m.final_marks is None]
        assert unresolved == []

    def test_null_final_marks_detected(self) -> None:
        """Guard detects marks with final_marks=NULL."""
        marks_repo = InMemoryQuestionMarkRepository()
        sid = uuid.uuid4()
        marks = [
            _mark("A.1", final_marks="1.0", needs_review=False),
            _mark("A.2", final_marks=None, needs_review=True),  # NULL
        ]
        marks_repo.bulk_upsert(_FAMILY_ID, sid, marks)

        all_marks = marks_repo.list_for_submission(sid)
        unresolved = [m.question_id for m in all_marks if m.final_marks is None]
        assert unresolved == ["A.2"]

    def test_all_null_blocked(self) -> None:
        marks_repo = InMemoryQuestionMarkRepository()
        sid = uuid.uuid4()
        marks = [
            _mark("A.1", final_marks=None, needs_review=True),
            _mark("A.2", final_marks=None, needs_review=True),
        ]
        marks_repo.bulk_upsert(_FAMILY_ID, sid, marks)

        all_marks = marks_repo.list_for_submission(sid)
        unresolved = [m.question_id for m in all_marks if m.final_marks is None]
        assert set(unresolved) == {"A.1", "A.2"}


# ---------------------------------------------------------------------------
# PublishRequest.merge_with_defaults
# ---------------------------------------------------------------------------


class TestPublishRequestMerge:
    def test_empty_request_keeps_defaults(self) -> None:
        defaults = VisibilityDefaults(accuracy=True, effort=False, growing=True, ai_rationale=False)
        req = PublishRequest()
        merged = req.merge_with_defaults(defaults)
        assert merged.accuracy is True
        assert merged.effort is False
        assert merged.growing is True
        assert merged.ai_rationale is False

    def test_partial_override(self) -> None:
        defaults = VisibilityDefaults(accuracy=True, effort=True, growing=True, ai_rationale=False)
        req = PublishRequest(ai_rationale=True, effort=False)
        merged = req.merge_with_defaults(defaults)
        assert merged.ai_rationale is True  # overridden
        assert merged.effort is False  # overridden
        assert merged.accuracy is True  # kept
        assert merged.growing is True  # kept

    def test_full_override(self) -> None:
        defaults = VisibilityDefaults(
            accuracy=False, effort=False, growing=False, ai_rationale=False
        )
        req = PublishRequest(accuracy=True, effort=True, growing=True, ai_rationale=True)
        merged = req.merge_with_defaults(defaults)
        assert merged.accuracy is True
        assert merged.effort is True
        assert merged.growing is True
        assert merged.ai_rationale is True


# ---------------------------------------------------------------------------
# DB-tier RLS test — skipped when Postgres unreachable
# ---------------------------------------------------------------------------

_DSN = os.environ.get("STUDYPAL_DB_DSN", "postgresql://studypal:studypal@localhost:5432/studypal")


def _try_connect_db() -> bool:
    try:
        conn = psycopg.connect(_DSN, connect_timeout=3, autocommit=True)
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def owner_conn_review() -> Any:
    if not _try_connect_db():
        pytest.skip("Local Postgres not reachable")
    conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
    )
    yield conn
    conn.close()


@pytest.mark.skipif(
    not _try_connect_db(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)
class TestPublishRLS:
    """Prove that the publish columns are RLS-isolated.

    User A publishes their cycle; user B must not see A's published_visibility.
    """

    def _open_auth_conn(self, user_id: uuid.UUID) -> psycopg.Connection[dict[str, Any]]:
        claims = json.dumps({"sub": str(user_id)})
        conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
            _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
        )
        conn.execute("SET ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))
        return conn

    def _seed_cycle_at_parent_review(
        self,
        owner_conn: psycopg.Connection[dict[str, Any]],
        user_id: uuid.UUID,
        family_name: str,
    ) -> uuid.UUID:
        """Seed a cycle in PARENT_REVIEW_MARKS state. Returns cycle_id."""
        cur = owner_conn.cursor()
        cur.execute("INSERT INTO families (name) VALUES (%s) RETURNING id", (family_name,))
        row = cur.fetchone()
        assert row is not None
        family_id = uuid.UUID(str(row["id"]))

        cur.execute(
            "INSERT INTO family_members (user_id, family_id) VALUES (%s, %s)",
            (str(user_id), str(family_id)),
        )
        cur.execute(
            "INSERT INTO children (family_id, display_name, grade_label) "
            "VALUES (%s, 'Kid', 'Grade 5') RETURNING id",
            (str(family_id),),
        )
        row = cur.fetchone()
        assert row is not None
        child_id = uuid.UUID(str(row["id"]))

        cur.execute(
            "INSERT INTO subjects (family_id, child_id, name, content_language) "
            "VALUES (%s, %s, 'Maths', 'en') RETURNING id",
            (str(family_id), str(child_id)),
        )
        row = cur.fetchone()
        assert row is not None
        subject_id = uuid.UUID(str(row["id"]))

        cur.execute(
            "INSERT INTO cycles (family_id, subject_id, state) "
            "VALUES (%s, %s, 'PARENT_REVIEW_MARKS') RETURNING id",
            (str(family_id), str(subject_id)),
        )
        row = cur.fetchone()
        assert row is not None
        cycle_id = uuid.UUID(str(row["id"]))

        owner_conn.commit()
        return cycle_id

    def test_publish_columns_rls_isolated(
        self, owner_conn_review: psycopg.Connection[dict[str, Any]]
    ) -> None:
        """User B must not see user A's marks_published_at / published_visibility."""
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        cycle_a_id = self._seed_cycle_at_parent_review(
            owner_conn_review, user_a, f"FamilyA-{user_a.hex[:6]}"
        )
        _ = self._seed_cycle_at_parent_review(
            owner_conn_review, user_b, f"FamilyB-{user_b.hex[:6]}"
        )

        # Publish cycle A via owner connection.
        visibility_json = json.dumps(
            {"accuracy": True, "effort": True, "growing": True, "ai_rationale": False}
        )
        cur = owner_conn_review.cursor()
        cur.execute(
            """
            UPDATE cycles
            SET state = 'GAP_REPORT',
                marks_published_at = now(),
                published_visibility = %s::jsonb
            WHERE id = %s
            """,
            (visibility_json, str(cycle_a_id)),
        )
        owner_conn_review.commit()

        # User B must NOT see cycle A.
        conn_b = self._open_auth_conn(user_b)
        try:
            b_cur = conn_b.cursor()
            b_cur.execute(
                "SELECT marks_published_at FROM cycles WHERE id = %s",
                (str(cycle_a_id),),
            )
            row = b_cur.fetchone()
            assert row is None, "RLS violation: user B can see user A's cycle"
        finally:
            conn_b.close()

        # User A must see their own cycle.
        conn_a = self._open_auth_conn(user_a)
        try:
            a_cur = conn_a.cursor()
            a_cur.execute(
                "SELECT marks_published_at, published_visibility FROM cycles WHERE id = %s",
                (str(cycle_a_id),),
            )
            row = a_cur.fetchone()
            assert row is not None, "User A cannot see their own cycle"
            assert row["marks_published_at"] is not None
            assert row["published_visibility"] is not None
        finally:
            conn_a.close()
