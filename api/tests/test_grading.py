"""Unit tests for the Phase 2 grading engine.

Coverage:
- Per-type correctness: mcq, true_false, matching, ordering, fill_blank
  (numeric AUTO + word AUTO_FUZZY), short_answer, calculation (CLAUDE_ASSIST),
  table_completion (CLAUDE_ASSIST), labelling (CLAUDE_ASSIST),
  extended_response (CLAUDE_ASSIST).
- AUTO_FUZZY: no auto-zero (needs_review=True when unmatched).
- Numeric fill_blank: comma AND dot decimal, strip spaces/thousand-separators.
- Matching partial credit.
- Ordering exact-position.
- Skip → not_attempted (error_category, suggested_marks=0, needs_review=False).
- Half-mark legality (0.5-step).
- FakeGrader: determinism + batched shape.
- State transition: ANSWERS_ENTERED → AUTO_MARKED via advance_to_auto_marked.
- DB-tier RLS test for question_marks (skipped when Postgres unreachable).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import psycopg
import psycopg.rows
import pytest

from schemas.assessment_schema import Assessment, ErrorCategory, GradingPath
from schemas.capture import ChildResponseItem
from schemas.grading import QuestionMark
from services.grading import FakeGrader, FakeGraderSuggestion, _normalize, grade_submission
from services.repositories.memory import InMemoryFamilyRepository, InMemoryQuestionMarkRepository
from tests.samples.afrikaans_sample import afrikaans_assessment
from tests.samples.maths_sample import maths_assessment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAMILY_ID = uuid.uuid4()
_SUBMISSION_ID = uuid.uuid4()


def _assessment(raw: dict[str, Any]) -> Assessment:
    return Assessment.model_validate(raw)


def _response(qid: str, payload: dict[str, Any], attempted: bool = True) -> ChildResponseItem:
    return ChildResponseItem(qid=qid, attempted=attempted, payload=payload)


def _skip(qid: str) -> ChildResponseItem:
    """A skipped (not attempted) response."""
    return ChildResponseItem(qid=qid, attempted=False, payload={})


def _grade(assessment: Assessment, responses: list[ChildResponseItem]) -> list[QuestionMark]:
    return grade_submission(
        assessment,
        responses,
        family_id=_FAMILY_ID,
        submission_id=_SUBMISSION_ID,
    )


def _mark_for(marks: list[QuestionMark], qid: str) -> QuestionMark:
    for m in marks:
        if m.question_id == qid:
            return m
    raise KeyError(f"No mark for qid={qid}")


# ---------------------------------------------------------------------------
# MCQ
# ---------------------------------------------------------------------------


class TestMcq:
    def _mcq_assessment(self) -> Assessment:
        return _assessment(maths_assessment())  # A.1 is MCQ, correct_index=1

    def test_correct_answer(self) -> None:
        asmt = self._mcq_assessment()
        marks = _grade(asmt, [_response("A.1", {"selected_index": 1})])
        m = _mark_for(marks, "A.1")
        assert m.grading_path == GradingPath.AUTO
        assert m.suggested_marks == Decimal("1.0")
        assert m.final_marks == Decimal("1.0")
        assert not m.needs_review
        assert m.error_category is None

    def test_wrong_answer(self) -> None:
        asmt = self._mcq_assessment()
        marks = _grade(asmt, [_response("A.1", {"selected_index": 0})])
        m = _mark_for(marks, "A.1")
        assert m.suggested_marks == Decimal("0")
        assert m.final_marks == Decimal("0")
        assert not m.needs_review  # AUTO: fully determined
        assert m.error_category == ErrorCategory.CARELESS

    def test_skip_not_attempted(self) -> None:
        asmt = self._mcq_assessment()
        marks = _grade(asmt, [_skip("A.1")])
        m = _mark_for(marks, "A.1")
        assert m.suggested_marks == Decimal("0")
        assert m.final_marks == Decimal("0")
        assert not m.needs_review
        assert m.error_category == ErrorCategory.NOT_ATTEMPTED


# ---------------------------------------------------------------------------
# True/False
# ---------------------------------------------------------------------------


class TestTrueFalse:
    def _tf_assessment(self) -> Assessment:
        # Afrikaans B.2: is_true=False
        return _assessment(afrikaans_assessment())

    def test_correct_false(self) -> None:
        asmt = self._tf_assessment()
        marks = _grade(asmt, [_response("B.2", {"value": False})])
        m = _mark_for(marks, "B.2")
        assert m.grading_path == GradingPath.AUTO
        assert m.suggested_marks == Decimal("2.0")
        assert m.final_marks == Decimal("2.0")
        assert not m.needs_review

    def test_wrong_value(self) -> None:
        asmt = self._tf_assessment()
        marks = _grade(asmt, [_response("B.2", {"value": True})])
        m = _mark_for(marks, "B.2")
        assert m.suggested_marks == Decimal("0")
        assert m.error_category == ErrorCategory.CARELESS

    def test_skip(self) -> None:
        asmt = self._tf_assessment()
        marks = _grade(asmt, [_skip("B.2")])
        m = _mark_for(marks, "B.2")
        assert m.error_category == ErrorCategory.NOT_ATTEMPTED


# ---------------------------------------------------------------------------
# Matching — partial credit
# ---------------------------------------------------------------------------


class TestMatching:
    def _matching_assessment(self) -> Assessment:
        # Afrikaans B.1: 4 pairs × 1.0 mark each = 4.0 total
        return _assessment(afrikaans_assessment())

    def test_all_correct(self) -> None:
        asmt = self._matching_assessment()
        # correct_pairs: {0:1, 1:3, 2:0, 3:2}
        pairs = [
            {"left": 0, "right": 1},
            {"left": 1, "right": 3},
            {"left": 2, "right": 0},
            {"left": 3, "right": 2},
        ]
        marks = _grade(asmt, [_response("B.1", {"pairs": pairs})])
        m = _mark_for(marks, "B.1")
        assert m.suggested_marks == Decimal("4.0")
        assert m.final_marks == Decimal("4.0")
        assert not m.needs_review

    def test_partial_credit_two_correct(self) -> None:
        asmt = self._matching_assessment()
        # Only 2 correct pairs.
        pairs = [
            {"left": 0, "right": 1},  # correct
            {"left": 1, "right": 0},  # wrong (should be 3)
            {"left": 2, "right": 0},  # correct
            {"left": 3, "right": 1},  # wrong
        ]
        marks = _grade(asmt, [_response("B.1", {"pairs": pairs})])
        m = _mark_for(marks, "B.1")
        # 2 correct out of 4 → 2.0 marks (1.0 per pair)
        assert m.suggested_marks == Decimal("2.0")
        assert not m.needs_review

    def test_all_wrong(self) -> None:
        asmt = self._matching_assessment()
        pairs = [
            {"left": 0, "right": 0},
            {"left": 1, "right": 1},
            {"left": 2, "right": 2},
            {"left": 3, "right": 3},
        ]
        marks = _grade(asmt, [_response("B.1", {"pairs": pairs})])
        m = _mark_for(marks, "B.1")
        assert m.suggested_marks == Decimal("0")

    def test_skip(self) -> None:
        asmt = self._matching_assessment()
        marks = _grade(asmt, [_skip("B.1")])
        m = _mark_for(marks, "B.1")
        assert m.error_category == ErrorCategory.NOT_ATTEMPTED


# ---------------------------------------------------------------------------
# Ordering — exact position per slot
# ---------------------------------------------------------------------------


class TestOrdering:
    """Uses an inline assessment with an ordering question."""

    def _ordering_assessment(self) -> Assessment:
        raw: dict[str, Any] = {
            "assessment_id": "asmt-ordering-001",
            "cycle_id": "cycle-ordering-001",
            "variant": "A",
            "subject": "Test",
            "content_language": "en",
            "grade_label": "Grade 5",
            "title": "Ordering Test",
            "duration_minutes": 30,
            "instructions": [],
            "declared_total_marks": 3.0,
            "sections": [
                {
                    "label": "A",
                    "title": "Section A",
                    "declared_marks": 3.0,
                    "questions": [
                        {
                            "qid": "A.1",
                            "number": "1",
                            "text": "Put in order.",
                            "question_type": "ordering",
                            "difficulty": "easy",
                            "answer": {
                                "kind": "ordering",
                                "items": ["B", "C", "A"],  # shuffled
                                "correct_order": [2, 0, 1],  # A, B, C
                            },
                            "mark_rules": {"total": 3.0},
                        }
                    ],
                }
            ],
        }
        return _assessment(raw)

    def test_all_correct(self) -> None:
        asmt = self._ordering_assessment()
        marks = _grade(asmt, [_response("A.1", {"order": [2, 0, 1]})])
        m = _mark_for(marks, "A.1")
        assert m.suggested_marks == Decimal("3.0")
        assert not m.needs_review

    def test_one_correct_slot(self) -> None:
        asmt = self._ordering_assessment()
        # Only slot 0 correct (2 is correct in position 0)
        marks = _grade(asmt, [_response("A.1", {"order": [2, 1, 0]})])
        m = _mark_for(marks, "A.1")
        # 1 of 3 correct → 1.0 mark (1.0 per slot)
        assert m.suggested_marks == Decimal("1.0")

    def test_all_wrong(self) -> None:
        asmt = self._ordering_assessment()
        marks = _grade(asmt, [_response("A.1", {"order": [1, 2, 0]})])
        m = _mark_for(marks, "A.1")
        assert m.suggested_marks == Decimal("0")


# ---------------------------------------------------------------------------
# Fill blank — numeric (AUTO) + word (AUTO_FUZZY)
# ---------------------------------------------------------------------------


class TestFillBlankNumeric:
    def _numeric_assessment(self) -> Assessment:
        # Maths A.2: two number blanks, total=2.0
        return _assessment(maths_assessment())

    def test_correct_dot_decimal(self) -> None:
        asmt = self._numeric_assessment()
        marks = _grade(asmt, [_response("A.2", {"values": ["3000", "4.5"]})])
        m = _mark_for(marks, "A.2")
        assert m.grading_path == GradingPath.AUTO
        assert m.suggested_marks == Decimal("2.0")
        assert m.final_marks == Decimal("2.0")
        assert not m.needs_review

    def test_correct_comma_decimal(self) -> None:
        """Comma as decimal separator must be accepted."""
        asmt = self._numeric_assessment()
        marks = _grade(asmt, [_response("A.2", {"values": ["3000", "4,5"]})])
        m = _mark_for(marks, "A.2")
        assert m.suggested_marks == Decimal("2.0")

    def test_correct_spaced_thousands(self) -> None:
        """3 000 (space as thousands separator) must be accepted."""
        asmt = self._numeric_assessment()
        marks = _grade(asmt, [_response("A.2", {"values": ["3 000", "4.5"]})])
        m = _mark_for(marks, "A.2")
        assert m.suggested_marks == Decimal("2.0")

    def test_partial_credit(self) -> None:
        asmt = self._numeric_assessment()
        marks = _grade(asmt, [_response("A.2", {"values": ["3000", "9.9"]})])
        m = _mark_for(marks, "A.2")
        # 1 of 2 correct → 1.0 (marks_per_blank = 1.0)
        assert m.suggested_marks == Decimal("1.0")
        assert m.final_marks == Decimal("1.0")  # AUTO finalises
        assert not m.needs_review

    def test_wrong_values(self) -> None:
        asmt = self._numeric_assessment()
        marks = _grade(asmt, [_response("A.2", {"values": ["999", "99"]})])
        m = _mark_for(marks, "A.2")
        assert m.suggested_marks == Decimal("0")


class TestFillBlankWord:
    def _word_assessment(self) -> Assessment:
        # Afrikaans A.3: two word blanks, total=2.0
        return _assessment(afrikaans_assessment())

    def test_correct_match(self) -> None:
        asmt = self._word_assessment()
        marks = _grade(asmt, [_response("A.3", {"values": ["kinders", "boeke"]})])
        m = _mark_for(marks, "A.3")
        assert m.grading_path == GradingPath.AUTO_FUZZY
        assert m.suggested_marks == Decimal("2.0")
        assert m.final_marks == Decimal("2.0")
        assert not m.needs_review

    def test_case_insensitive(self) -> None:
        asmt = self._word_assessment()
        marks = _grade(asmt, [_response("A.3", {"values": ["Kinders", "BOEKE"]})])
        m = _mark_for(marks, "A.3")
        assert m.suggested_marks == Decimal("2.0")

    def test_no_match_needs_review_not_auto_zero(self) -> None:
        """Unmatched word blank → needs_review=True, final_marks=None (NEVER auto-finalize zero)."""
        asmt = self._word_assessment()
        marks = _grade(asmt, [_response("A.3", {"values": ["wrong", "boeke"]})])
        m = _mark_for(marks, "A.3")
        assert m.grading_path == GradingPath.AUTO_FUZZY
        assert m.suggested_marks == Decimal("0")
        assert m.final_marks is None  # not auto-finalized
        assert m.needs_review

    def test_skip(self) -> None:
        asmt = self._word_assessment()
        marks = _grade(asmt, [_skip("A.3")])
        m = _mark_for(marks, "A.3")
        assert m.error_category == ErrorCategory.NOT_ATTEMPTED
        assert m.suggested_marks == Decimal("0")
        assert not m.needs_review


# ---------------------------------------------------------------------------
# Short answer (AUTO_FUZZY)
# ---------------------------------------------------------------------------


class TestShortAnswer:
    def _short_answer_assessment(self) -> Assessment:
        # Afrikaans A.1: accepted=["katjie"], total=1.0
        return _assessment(afrikaans_assessment())

    def test_exact_match(self) -> None:
        asmt = self._short_answer_assessment()
        marks = _grade(asmt, [_response("A.1", {"text": "katjie"})])
        m = _mark_for(marks, "A.1")
        assert m.grading_path == GradingPath.AUTO_FUZZY
        assert m.suggested_marks == Decimal("1.0")
        assert m.final_marks == Decimal("1.0")
        assert not m.needs_review
        assert m.matched_alternative == "katjie"

    def test_case_insensitive(self) -> None:
        asmt = self._short_answer_assessment()
        marks = _grade(asmt, [_response("A.1", {"text": "Katjie"})])
        m = _mark_for(marks, "A.1")
        assert m.suggested_marks == Decimal("1.0")

    def test_trailing_punctuation_stripped(self) -> None:
        asmt = self._short_answer_assessment()
        marks = _grade(asmt, [_response("A.1", {"text": "katjie."})])
        m = _mark_for(marks, "A.1")
        assert m.suggested_marks == Decimal("1.0")

    def test_no_match_needs_review(self) -> None:
        asmt = self._short_answer_assessment()
        marks = _grade(asmt, [_response("A.1", {"text": "katje"})])
        m = _mark_for(marks, "A.1")
        assert m.suggested_marks == Decimal("0")
        assert m.final_marks is None  # NOT auto-finalized
        assert m.needs_review

    def test_skip(self) -> None:
        asmt = self._short_answer_assessment()
        marks = _grade(asmt, [_skip("A.1")])
        m = _mark_for(marks, "A.1")
        assert m.error_category == ErrorCategory.NOT_ATTEMPTED


# ---------------------------------------------------------------------------
# Calculation (CLAUDE_ASSIST via FakeGrader)
# ---------------------------------------------------------------------------


class TestCalculation:
    def _calc_assessment(self) -> Assessment:
        # Maths B.1: final_answer="860", answer_marks=1.0, method_marks=2.0, total=3.0
        return _assessment(maths_assessment())

    def test_correct_answer_suggests_answer_marks(self) -> None:
        asmt = self._calc_assessment()
        marks = _grade(asmt, [_response("B.1", {"answer": "860", "working": "2×430"})])
        m = _mark_for(marks, "B.1")
        assert m.grading_path == GradingPath.CLAUDE_ASSIST
        # FakeGrader: correct final answer → suggest answer_marks (1.0)
        assert m.suggested_marks == Decimal("1.0")
        assert m.final_marks is None  # never auto-promoted
        assert m.needs_review
        assert m.ai_rationale is not None

    def test_wrong_answer_suggests_zero(self) -> None:
        asmt = self._calc_assessment()
        marks = _grade(asmt, [_response("B.1", {"answer": "999", "working": "wrong"})])
        m = _mark_for(marks, "B.1")
        assert m.suggested_marks == Decimal("0")
        assert m.final_marks is None
        assert m.needs_review

    def test_skip(self) -> None:
        asmt = self._calc_assessment()
        marks = _grade(asmt, [_skip("B.1")])
        m = _mark_for(marks, "B.1")
        assert m.error_category == ErrorCategory.NOT_ATTEMPTED
        assert m.suggested_marks == Decimal("0")
        assert not m.needs_review  # skipped questions do NOT need review

    def test_never_auto_promotes_to_auto(self) -> None:
        """CLAUDE_ASSIST must never be promoted to AUTO even when answer looks exact."""
        asmt = self._calc_assessment()
        marks = _grade(asmt, [_response("B.1", {"answer": "860"})])
        m = _mark_for(marks, "B.1")
        assert m.grading_path == GradingPath.CLAUDE_ASSIST
        assert m.final_marks is None


# ---------------------------------------------------------------------------
# Table completion (CLAUDE_ASSIST)
# ---------------------------------------------------------------------------


class TestTableCompletion:
    def _table_assessment(self) -> Assessment:
        # Maths B.2: table_completion, total=2.0
        return _assessment(maths_assessment())

    def test_table_is_claude_assist(self) -> None:
        asmt = self._table_assessment()
        cells = [{"row": 1, "col": 0, "value": "500"}]
        marks = _grade(asmt, [_response("B.2", {"cells": cells})])
        m = _mark_for(marks, "B.2")
        assert m.grading_path == GradingPath.CLAUDE_ASSIST
        assert m.needs_review
        assert m.final_marks is None

    def test_skip(self) -> None:
        asmt = self._table_assessment()
        marks = _grade(asmt, [_skip("B.2")])
        m = _mark_for(marks, "B.2")
        assert m.error_category == ErrorCategory.NOT_ATTEMPTED


# ---------------------------------------------------------------------------
# FakeGrader: determinism + batched shape
# ---------------------------------------------------------------------------


class TestFakeGrader:
    def _calc_questions(self) -> tuple[Any, Any]:
        """Return (question, response) for the maths calculation question."""
        asmt = _assessment(maths_assessment())
        section = asmt.sections[1]  # Section B
        calc_q = section.questions[0]  # B.1
        resp = _response("B.1", {"answer": "860", "working": "2×430"})
        return calc_q, resp

    def test_deterministic_same_result_twice(self) -> None:
        grader = FakeGrader()
        q, r = self._calc_questions()
        sub_id = uuid.uuid4()
        result1, _ = grader.grade_batch([(q, r)], sub_id)
        result2, _ = grader.grade_batch([(q, r)], sub_id)
        assert result1[0].suggested_marks == result2[0].suggested_marks
        assert result1[0].ai_rationale == result2[0].ai_rationale

    def test_batch_returns_one_per_question(self) -> None:
        grader = FakeGrader()
        q, r = self._calc_questions()
        sub_id = uuid.uuid4()
        suggestions, call_log = grader.grade_batch([(q, r)], sub_id)
        assert len(suggestions) == 1
        assert isinstance(suggestions[0], FakeGraderSuggestion)

    def test_call_log_shape(self) -> None:
        from schemas.generation import CallLog

        grader = FakeGrader()
        q, r = self._calc_questions()
        _, call_log = grader.grade_batch([(q, r)], uuid.uuid4())
        assert isinstance(call_log, CallLog)
        assert call_log.prompt_tokens == 0
        assert call_log.completion_tokens == 0
        assert call_log.model == "fake-grader"
        assert call_log.latency_ms >= 0

    def test_batched_shape_multiple_questions(self) -> None:
        """One batched call handles multiple CLAUDE_ASSIST questions."""
        asmt = _assessment(maths_assessment())
        b_section = asmt.sections[1]
        calc_q = b_section.questions[0]  # B.1 calculation
        table_q = b_section.questions[1]  # B.2 table_completion

        grader = FakeGrader()
        sub_id = uuid.uuid4()
        suggestions, _ = grader.grade_batch(
            [
                (calc_q, _response("B.1", {"answer": "860"})),
                (table_q, _response("B.2", {"cells": []})),
            ],
            sub_id,
        )
        assert len(suggestions) == 2
        qids = {s.question_id for s in suggestions}
        assert "B.1" in qids
        assert "B.2" in qids


# ---------------------------------------------------------------------------
# Full submission grade: all questions in maths sample
# ---------------------------------------------------------------------------


class TestFullMathsGrade:
    def test_all_questions_marked(self) -> None:
        asmt = _assessment(maths_assessment())
        all_qids = {q.qid for s in asmt.sections for q in s.questions}
        responses = [
            _response("A.1", {"selected_index": 1}),
            _response("A.2", {"values": ["3000", "4.5"]}),
            _response("B.1", {"answer": "860", "working": "step1"}),
            _response("B.2", {"cells": [{"row": 1, "col": 0, "value": "500"}]}),
        ]
        marks = _grade(asmt, responses)
        assert len(marks) == 4
        marked_qids = {m.question_id for m in marks}
        assert marked_qids == all_qids

    def test_half_mark_legality(self) -> None:
        """All suggested_marks must be multiples of 0.5."""
        asmt = _assessment(maths_assessment())
        responses = [
            _response("A.1", {"selected_index": 0}),  # wrong
            _response("A.2", {"values": ["3000", "0.0"]}),  # partial
            _response("B.1", {"answer": "0", "working": ""}),
            _response("B.2", {"cells": []}),
        ]
        marks = _grade(asmt, responses)
        for m in marks:
            remainder = (m.suggested_marks * 2) % 1
            assert remainder == 0, f"{m.question_id}: {m.suggested_marks} is not a half-step"


# ---------------------------------------------------------------------------
# Full submission grade: all questions in afrikaans sample
# ---------------------------------------------------------------------------


class TestFullAfrikaansGrade:
    def test_all_questions_marked(self) -> None:
        asmt = _assessment(afrikaans_assessment())
        all_qids = {q.qid for s in asmt.sections for q in s.questions}
        responses = [
            _response("A.1", {"text": "katjie"}),
            _response("A.2", {"text": "Vandag is dit sonnig."}),
            _response("A.3", {"values": ["kinders", "boeke"]}),
            _response(
                "B.1",
                {
                    "pairs": [
                        {"left": 0, "right": 1},
                        {"left": 1, "right": 3},
                        {"left": 2, "right": 0},
                        {"left": 3, "right": 2},
                    ]
                },
            ),
            _response("B.2", {"value": False}),
        ]
        marks = _grade(asmt, responses)
        assert len(marks) == 5
        marked_qids = {m.question_id for m in marks}
        assert marked_qids == all_qids


# ---------------------------------------------------------------------------
# State transition via cycle service
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_advance_to_auto_marked(self) -> None:
        from schemas.family import CycleState
        from services.cycle import advance_to_auto_marked

        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        family, _ = repo.bootstrap_family("Test Family", None, None)
        subject = repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = repo.create_cycle(family.id, subject.id, "scope text")

        # Manually advance to ANSWERS_ENTERED to set up the precondition.
        from services.cycle import (
            advance_to_answers_entered,
            advance_to_generating,
            advance_to_parent_reviews,
            approve_draft,
        )

        advance_to_generating(repo, cycle.id)
        advance_to_parent_reviews(repo, cycle.id)
        approve_draft(repo, cycle.id)
        advance_to_answers_entered(repo, cycle.id)

        # Now advance to AUTO_MARKED.
        updated = advance_to_auto_marked(repo, cycle.id)
        assert updated.state == CycleState.AUTO_MARKED

    def test_illegal_transition_rejected(self) -> None:
        from services.cycle import IllegalTransitionError, advance_to_auto_marked

        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        family, _ = repo.bootstrap_family("Test Family", None, None)
        subject = repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = repo.create_cycle(family.id, subject.id, "scope")

        # Cycle is in SCOPE_UPLOADED — cannot jump to AUTO_MARKED.
        with pytest.raises(IllegalTransitionError):
            advance_to_auto_marked(repo, cycle.id)


# ---------------------------------------------------------------------------
# Normalisation helper
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_casefold(self) -> None:
        assert _normalize("ABC") == "abc"

    def test_strip_whitespace(self) -> None:
        assert _normalize("  hello  ") == "hello"

    def test_collapse_internal_spaces(self) -> None:
        assert _normalize("hello   world") == "hello world"

    def test_strip_terminal_punctuation(self) -> None:
        assert _normalize("hello.") == "hello"
        assert _normalize("hello,") == "hello"
        assert _normalize("hello!") == "hello"

    def test_unicode_nfc(self) -> None:
        # Precomposed vs decomposed forms must normalise identically.
        composed = "é"  # é (precomposed)
        decomposed = "é"  # e + combining acute
        assert _normalize(composed) == _normalize(decomposed)


# ---------------------------------------------------------------------------
# QuestionMark schema: half-mark and bounds validation
# ---------------------------------------------------------------------------


class TestQuestionMarkSchema:
    def _base_mark(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "family_id": str(uuid.uuid4()),
            "submission_id": str(uuid.uuid4()),
            "question_id": "A.1",
            "marks_total": "2.0",
            "suggested_marks": "1.0",
            "grading_path": "auto",
            "needs_review": False,
        }
        base.update(overrides)
        return base

    def test_valid_half_mark(self) -> None:
        mark = QuestionMark.model_validate(self._base_mark(suggested_marks="0.5"))
        assert mark.suggested_marks == Decimal("0.5")

    def test_suggested_over_total_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            QuestionMark.model_validate(self._base_mark(marks_total="1.0", suggested_marks="1.5"))

    def test_non_half_step_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            QuestionMark.model_validate(self._base_mark(marks_total="1.0", suggested_marks="0.3"))

    def test_negative_marks_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            QuestionMark.model_validate(self._base_mark(marks_total="1.0", suggested_marks="-0.5"))


# ---------------------------------------------------------------------------
# InMemory QuestionMarkRepository
# ---------------------------------------------------------------------------


class TestInMemoryMarkRepository:
    def test_bulk_upsert_and_list(self) -> None:
        repo = InMemoryQuestionMarkRepository()
        fid = uuid.uuid4()
        sid = uuid.uuid4()

        marks = [
            QuestionMark(
                family_id=fid,
                submission_id=sid,
                question_id="A.1",
                marks_total=Decimal("1.0"),
                suggested_marks=Decimal("1.0"),
                final_marks=Decimal("1.0"),
                grading_path=GradingPath.AUTO,
                needs_review=False,
            ),
            QuestionMark(
                family_id=fid,
                submission_id=sid,
                question_id="A.2",
                marks_total=Decimal("2.0"),
                suggested_marks=Decimal("0"),
                final_marks=None,
                grading_path=GradingPath.AUTO_FUZZY,
                needs_review=True,
            ),
        ]

        persisted = repo.bulk_upsert(fid, sid, marks)
        assert len(persisted) == 2

        listed = repo.list_for_submission(sid)
        assert len(listed) == 2
        qids = {m.question_id for m in listed}
        assert qids == {"A.1", "A.2"}

    def test_upsert_is_idempotent(self) -> None:
        repo = InMemoryQuestionMarkRepository()
        fid = uuid.uuid4()
        sid = uuid.uuid4()

        mark = QuestionMark(
            family_id=fid,
            submission_id=sid,
            question_id="A.1",
            marks_total=Decimal("1.0"),
            suggested_marks=Decimal("0"),
            final_marks=Decimal("0"),
            grading_path=GradingPath.AUTO,
            needs_review=False,
        )
        repo.bulk_upsert(fid, sid, [mark])

        updated = mark.model_copy(
            update={"suggested_marks": Decimal("1.0"), "final_marks": Decimal("1.0")}
        )
        repo.bulk_upsert(fid, sid, [updated])

        listed = repo.list_for_submission(sid)
        assert len(listed) == 1  # not doubled
        assert listed[0].suggested_marks == Decimal("1.0")


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


pytestmark_db = pytest.mark.skipif(
    not _try_connect_db(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)


@pytest.fixture(scope="module")
def owner_conn_marks() -> Generator[psycopg.Connection[dict[str, Any]], None, None]:
    conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
    )
    yield conn
    conn.close()


@pytest.mark.skipif(
    not _try_connect_db(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)
class TestQuestionMarksRLS:
    """Prove that question_marks is RLS-isolated by family_id.

    Two users (family A, family B) each grade their own submission.
    User A's authenticated connection must NOT see user B's marks.
    """

    def _open_auth_conn(self, user_id: uuid.UUID) -> psycopg.Connection[dict[str, Any]]:
        claims = json.dumps({"sub": str(user_id)})
        conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
            _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
        )
        conn.execute("SET ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))
        return conn

    def _seed_family(
        self,
        owner_conn: psycopg.Connection[dict[str, Any]],
        user_id: uuid.UUID,
        family_name: str,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        """Seed family → child → subject → cycle → assessment → submission.

        Returns (family_id, child_id, assessment_id, submission_id).
        """
        cur = owner_conn.cursor()
        # family
        cur.execute("INSERT INTO families (name) VALUES (%s) RETURNING id", (family_name,))
        row = cur.fetchone()
        assert row is not None
        family_id = uuid.UUID(str(row["id"]))

        # membership
        cur.execute(
            "INSERT INTO family_members (user_id, family_id) VALUES (%s, %s)",
            (str(user_id), str(family_id)),
        )

        # child
        cur.execute(
            "INSERT INTO children (family_id, display_name, grade_label) "
            "VALUES (%s, 'Kid', 'Grade 5') RETURNING id",
            (str(family_id),),
        )
        row = cur.fetchone()
        assert row is not None
        child_id = uuid.UUID(str(row["id"]))

        # subject
        cur.execute(
            "INSERT INTO subjects (family_id, child_id, name, content_language) "
            "VALUES (%s, %s, 'Maths', 'en') RETURNING id",
            (str(family_id), str(child_id)),
        )
        row = cur.fetchone()
        assert row is not None
        subject_id = uuid.UUID(str(row["id"]))

        # cycle
        cur.execute(
            "INSERT INTO cycles (family_id, subject_id, state) "
            "VALUES (%s, %s, 'ANSWERS_ENTERED') RETURNING id",
            (str(family_id), str(subject_id)),
        )
        row = cur.fetchone()
        assert row is not None
        cycle_id = uuid.UUID(str(row["id"]))

        # assessment
        asmt_raw = maths_assessment()
        asmt_raw["cycle_id"] = str(cycle_id)
        asmt = Assessment.model_validate(asmt_raw)
        import json as _json

        asmt_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO assessments (
                id, family_id, cycle_id, variant, subject, content_language,
                declared_total_marks, computed_total_marks, assessment, schema_version
            ) VALUES (%s, %s, %s, 'A', 'Maths', 'en', 8.0, 8.0, %s::jsonb, '1.0')
            """,
            (
                asmt_id,
                str(family_id),
                str(cycle_id),
                _json.dumps(asmt.model_dump()),
            ),
        )

        # submission
        submission_doc = _json.dumps(
            {
                "child_id": str(child_id),
                "responses": [
                    {"qid": "A.1", "attempted": True, "payload": {"selected_index": 1}},
                ],
                "proof_photo_paths": [],
            }
        )
        cur.execute(
            """
            INSERT INTO submissions (family_id, assessment_id, child_id, submission)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (str(family_id), asmt_id, str(child_id), submission_doc),
        )
        row = cur.fetchone()
        assert row is not None
        submission_id = uuid.UUID(str(row["id"]))

        owner_conn.commit()
        return family_id, child_id, uuid.UUID(asmt_id), submission_id

    def test_cross_tenant_isolation(
        self, owner_conn_marks: psycopg.Connection[dict[str, Any]]
    ) -> None:
        """Family A's marks must not be visible to family B's authenticated connection."""
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        family_a_id, _, _, submission_a_id = self._seed_family(
            owner_conn_marks, user_a, f"Family-A-{user_a.hex[:6]}"
        )
        family_b_id, _, _, submission_b_id = self._seed_family(
            owner_conn_marks, user_b, f"Family-B-{user_b.hex[:6]}"
        )

        # Insert marks for family A via owner.
        mark_id = uuid.uuid4()
        cur = owner_conn_marks.cursor()
        cur.execute(
            """
            INSERT INTO question_marks (
                id, family_id, submission_id, question_id,
                marks_total, suggested_marks, grading_path, needs_review
            ) VALUES (%s, %s, %s, 'A.1', 1.0, 1.0, 'auto', false)
            """,
            (str(mark_id), str(family_a_id), str(submission_a_id)),
        )
        owner_conn_marks.commit()

        # User B's authenticated connection must NOT see family A's mark.
        conn_b = self._open_auth_conn(user_b)
        try:
            b_cur = conn_b.cursor()
            b_cur.execute(
                "SELECT id FROM question_marks WHERE id = %s",
                (str(mark_id),),
            )
            row = b_cur.fetchone()
            assert row is None, "RLS violation: user B can see family A's mark"
        finally:
            conn_b.close()

        # User A must see their own mark.
        conn_a = self._open_auth_conn(user_a)
        try:
            a_cur = conn_a.cursor()
            a_cur.execute(
                "SELECT id FROM question_marks WHERE id = %s",
                (str(mark_id),),
            )
            row = a_cur.fetchone()
            assert row is not None, "User A cannot see their own mark"
        finally:
            conn_a.close()
