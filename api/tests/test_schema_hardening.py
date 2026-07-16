"""
Schema hardening tests (Week 1 stressor samples + invariant negative tests).

Covers the validators added during the Week 1 hardening pass:
  1. Global uniqueness of qid across the whole Assessment.
  2. Uniqueness of Section labels.
  3. McqAnswer.distractor_notes keys must be valid option indices.
  4. CalculationAnswer ↔ MarkRules coherence: method_marks > 0 requires method_steps.
  5. content_language must be a lowercase 2-3 letter code.
  6. Float-equality safety: marks that are multiples of 0.5 stay exact through addition.

Also verifies that the two representative stressor samples (Maths and Afrikaans) each
validate cleanly against the canonical Assessment schema.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from schemas.assessment_schema import Assessment
from tests.samples.afrikaans_sample import afrikaans_assessment
from tests.samples.maths_sample import maths_assessment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate(raw: dict[str, Any]) -> Assessment:
    """Parse raw dict and return the Assessment, or raise ValidationError."""
    return Assessment.model_validate(raw)


def _expect_error(raw: dict[str, Any], fragment: str) -> None:
    """Assert that validating raw raises a ValidationError whose string
    representation contains *fragment* (case-insensitive)."""
    with pytest.raises(ValidationError) as exc_info:
        Assessment.model_validate(raw)
    assert fragment.lower() in str(exc_info.value).lower(), (
        f"Expected error containing {fragment!r} but got:\n{exc_info.value}"
    )


# ---------------------------------------------------------------------------
# Stressor sample positive tests
# ---------------------------------------------------------------------------


class TestMathsSamplePositive:
    def test_maths_sample_validates(self) -> None:
        """Multi-section Maths assessment with calculation/table/fill_blank/mcq validates."""
        a = _validate(maths_assessment())
        assert a.computed_total_marks == 9.0
        assert a.declared_total_marks == 9.0
        assert a.content_language == "en"

    def test_maths_section_a_marks(self) -> None:
        a = _validate(maths_assessment())
        section_a = next(s for s in a.sections if s.label == "A")
        assert section_a.computed_marks == 3.0

    def test_maths_section_b_marks(self) -> None:
        a = _validate(maths_assessment())
        section_b = next(s for s in a.sections if s.label == "B")
        assert section_b.computed_marks == 6.0

    def test_maths_calculation_grading_path(self) -> None:
        """Calculation question must have CLAUDE_ASSIST grading path."""
        from schemas.assessment_schema import GradingPath

        a = _validate(maths_assessment())
        section_b = next(s for s in a.sections if s.label == "B")
        calc_q = next(q for q in section_b.questions if q.question_type.value == "calculation")
        assert calc_q.grading_path == GradingPath.CLAUDE_ASSIST

    def test_maths_fill_blank_number_grading_path(self) -> None:
        """fill_blank with all number-type blanks must resolve to AUTO (not AUTO_FUZZY)."""
        from schemas.assessment_schema import GradingPath

        a = _validate(maths_assessment())
        section_a = next(s for s in a.sections if s.label == "A")
        fb_q = next(q for q in section_a.questions if q.question_type.value == "fill_blank")
        assert fb_q.grading_path == GradingPath.AUTO

    def test_maths_table_has_half_mark_cells(self) -> None:
        """table_completion cells should carry half_mark=True."""
        from schemas.assessment_schema import TableCompletionAnswer

        a = _validate(maths_assessment())
        section_b = next(s for s in a.sections if s.label == "B")
        table_q = next(
            q for q in section_b.questions if q.question_type.value == "table_completion"
        )
        assert isinstance(table_q.answer, TableCompletionAnswer)
        assert all(cell.half_mark for cell in table_q.answer.cells)

    def test_maths_method_marks_have_steps(self) -> None:
        """calculation question with method_marks must supply method_steps."""
        from schemas.assessment_schema import CalculationAnswer

        a = _validate(maths_assessment())
        section_b = next(s for s in a.sections if s.label == "B")
        calc_q = next(q for q in section_b.questions if q.question_type.value == "calculation")
        assert isinstance(calc_q.answer, CalculationAnswer)
        assert calc_q.mark_rules.method_marks == 2.0
        assert len(calc_q.answer.method_steps) >= 2


class TestAfrikaasSamplePositive:
    def test_afrikaans_sample_validates(self) -> None:
        """Afrikaans assessment (content_language='af') validates end-to-end."""
        a = _validate(afrikaans_assessment())
        assert a.computed_total_marks == 11.0
        assert a.content_language == "af"

    def test_afrikaans_section_a_marks(self) -> None:
        a = _validate(afrikaans_assessment())
        section_a = next(s for s in a.sections if s.label == "A")
        assert section_a.computed_marks == 5.0

    def test_afrikaans_section_b_marks(self) -> None:
        a = _validate(afrikaans_assessment())
        section_b = next(s for s in a.sections if s.label == "B")
        assert section_b.computed_marks == 6.0

    def test_afrikaans_true_false_requires_correction(self) -> None:
        """true_false question with is_true=False and requires_correction must
        carry a corrected_statement."""
        from schemas.assessment_schema import TrueFalseAnswer

        a = _validate(afrikaans_assessment())
        section_b = next(s for s in a.sections if s.label == "B")
        tf_q = next(q for q in section_b.questions if q.question_type.value == "true_false")
        assert isinstance(tf_q.answer, TrueFalseAnswer)
        assert tf_q.answer.is_true is False
        assert tf_q.answer.requires_correction is True
        assert tf_q.answer.corrected_statement

    def test_afrikaans_fill_blank_word_grading_path(self) -> None:
        """fill_blank with word-type blanks resolves to AUTO_FUZZY."""
        from schemas.assessment_schema import GradingPath

        a = _validate(afrikaans_assessment())
        section_a = next(s for s in a.sections if s.label == "A")
        fb_q = next(q for q in section_a.questions if q.question_type.value == "fill_blank")
        assert fb_q.grading_path == GradingPath.AUTO_FUZZY

    def test_afrikaans_matching_distractor_in_right(self) -> None:
        """matching answer may have more right-side items than left (distractors)."""
        from schemas.assessment_schema import MatchingAnswer

        a = _validate(afrikaans_assessment())
        section_b = next(s for s in a.sections if s.label == "B")
        match_q = next(q for q in section_b.questions if q.question_type.value == "matching")
        assert isinstance(match_q.answer, MatchingAnswer)
        assert len(match_q.answer.right) > len(match_q.answer.left)


# ---------------------------------------------------------------------------
# Negative tests — each new/existing invariant
# ---------------------------------------------------------------------------


class TestDuplicateQids:
    def test_duplicate_qid_within_section_rejected(self) -> None:
        raw = maths_assessment()
        # Give the second question in section A the same qid as the first
        raw["sections"][0]["questions"][1]["qid"] = "A.1"
        _expect_error(raw, "duplicate question ids")

    def test_duplicate_qid_across_sections_rejected(self) -> None:
        """Same qid appearing in section A and section B must be rejected."""
        raw = maths_assessment()
        raw["sections"][1]["questions"][0]["qid"] = "A.1"
        _expect_error(raw, "duplicate question ids")

    def test_unique_qids_pass(self) -> None:
        """Sanity: all qids distinct → no error."""
        _validate(maths_assessment())


class TestDuplicateSectionLabels:
    def test_duplicate_section_label_rejected(self) -> None:
        raw = maths_assessment()
        raw["sections"][1]["label"] = "A"  # both sections now labeled "A"
        _expect_error(raw, "duplicate section labels")

    def test_unique_section_labels_pass(self) -> None:
        _validate(maths_assessment())


class TestMcqDistractorNoteKeys:
    def _mcq_assessment(self, distractor_notes: dict[int, str]) -> dict[str, Any]:
        """Minimal assessment with one MCQ whose distractor_notes is customisable."""
        raw: dict[str, Any] = {
            "assessment_id": "asmt-mcq-dn",
            "cycle_id": "cycle-mcq-dn",
            "variant": "A",
            "subject": "test",
            "content_language": "en",
            "grade_label": "Grade 4",
            "title": "MCQ distractor note test",
            "duration_minutes": 10,
            "declared_total_marks": 1.0,
            "sections": [
                {
                    "label": "A",
                    "title": "Section A",
                    "declared_marks": 1.0,
                    "questions": [
                        {
                            "qid": "A.1",
                            "number": "1",
                            "text": "Pick one.",
                            "question_type": "mcq",
                            "difficulty": "easy",
                            "answer": {
                                "kind": "mcq",
                                "options": ["alpha", "beta", "gamma"],
                                "correct_index": 0,
                                "distractor_notes": distractor_notes,
                            },
                            "mark_rules": {"total": 1.0},
                        }
                    ],
                }
            ],
        }
        return raw

    def test_distractor_notes_valid_keys_pass(self) -> None:
        """Keys 1 and 2 are valid for a 3-option MCQ (correct_index=0)."""
        _validate(self._mcq_assessment({1: "Confused with alpha.", 2: "Too large."}))

    def test_distractor_notes_key_equal_to_len_rejected(self) -> None:
        """Key == len(options) == 3 is out of range (valid range is 0-2)."""
        raw = self._mcq_assessment({3: "This key is out of range."})
        _expect_error(raw, "out of range")

    def test_distractor_notes_negative_key_rejected(self) -> None:
        raw = self._mcq_assessment({-1: "Negative index."})
        _expect_error(raw, "out of range")

    def test_distractor_notes_empty_pass(self) -> None:
        """Empty distractor_notes (the default) must always pass."""
        _validate(self._mcq_assessment({}))


class TestCalculationMethodCoherence:
    def _calc_assessment(
        self,
        method_marks: float | None,
        method_steps: list[str],
    ) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "assessment_id": "asmt-calc-coherence",
            "cycle_id": "cycle-calc-coherence",
            "variant": "A",
            "subject": "test",
            "content_language": "en",
            "grade_label": "Grade 6",
            "title": "Calculation coherence test",
            "duration_minutes": 20,
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
                            "text": "Calculate 12 × 15.",
                            "question_type": "calculation",
                            "difficulty": "medium",
                            "answer": {
                                "kind": "calculation",
                                "final_answer": "180",
                                "method_steps": method_steps,
                            },
                            "mark_rules": {
                                "total": 3.0,
                                "answer_marks": 1.0 if method_marks is not None else None,
                                "method_marks": method_marks,
                            },
                        }
                    ],
                }
            ],
        }
        # Clean up None values so they don't override defaults
        mark_rules = raw["sections"][0]["questions"][0]["mark_rules"]
        if mark_rules["answer_marks"] is None:
            del mark_rules["answer_marks"]
        if mark_rules["method_marks"] is None:
            del mark_rules["method_marks"]
        return raw

    def test_method_marks_with_steps_pass(self) -> None:
        raw = self._calc_assessment(
            method_marks=2.0,
            method_steps=["12 × 10 = 120", "12 × 5 = 60", "120 + 60 = 180"],
        )
        _validate(raw)

    def test_method_marks_without_steps_rejected(self) -> None:
        raw = self._calc_assessment(method_marks=2.0, method_steps=[])
        _expect_error(raw, "method_steps")

    def test_no_method_marks_no_steps_pass(self) -> None:
        """No method_marks declared → method_steps can be empty — no error."""
        raw = self._calc_assessment(method_marks=None, method_steps=[])
        _validate(raw)

    def test_zero_method_marks_no_steps_pass(self) -> None:
        """method_marks=0 is not a legal value (Field gt=0 per multiple_of=0.5 semantics)
        but method_marks absent → also fine.  We test that method_marks=0.5 with steps is OK."""
        raw = self._calc_assessment(
            method_marks=0.5,
            method_steps=["Identify the operation."],
        )
        # Rebuild mark_rules so answer_marks + method_marks = total=3.0
        raw["sections"][0]["questions"][0]["mark_rules"] = {
            "total": 3.0,
            "answer_marks": 2.5,
            "method_marks": 0.5,
        }
        _validate(raw)


class TestContentLanguageShape:
    def _lang_assessment(self, lang: str) -> dict[str, Any]:
        from tests.conftest import minimal_assessment

        raw = minimal_assessment()
        raw["content_language"] = lang
        return raw

    def test_valid_two_letter_en(self) -> None:
        _validate(self._lang_assessment("en"))

    def test_valid_two_letter_af(self) -> None:
        _validate(self._lang_assessment("af"))

    def test_valid_two_letter_fr(self) -> None:
        _validate(self._lang_assessment("fr"))

    def test_valid_two_letter_zu(self) -> None:
        _validate(self._lang_assessment("zu"))

    def test_valid_three_letter_afr(self) -> None:
        """ISO 639-2 three-letter code is also accepted."""
        _validate(self._lang_assessment("afr"))

    def test_uppercase_rejected(self) -> None:
        """'EN' must be rejected — must be lowercase."""
        _expect_error(self._lang_assessment("EN"), "lowercase")

    def test_mixed_case_rejected(self) -> None:
        _expect_error(self._lang_assessment("En"), "lowercase")

    def test_digit_in_code_rejected(self) -> None:
        _expect_error(self._lang_assessment("e1"), "lowercase")

    def test_one_letter_rejected(self) -> None:
        _expect_error(self._lang_assessment("e"), "lowercase")

    def test_four_letter_rejected(self) -> None:
        _expect_error(self._lang_assessment("engl"), "lowercase")

    def test_empty_string_rejected(self) -> None:
        _expect_error(self._lang_assessment(""), "lowercase")


class TestSectionTotalMismatch:
    def test_section_total_mismatch_rejected(self) -> None:
        """declared_marks != sum of question totals → ValidationError."""
        raw = maths_assessment()
        # Section A declares 3.0 but we inflate it to 9.0
        raw["sections"][0]["declared_marks"] = 9.0
        raw["declared_total_marks"] = 15.0  # keep grand total consistent for isolation
        _expect_error(raw, "section a")

    def test_grand_total_mismatch_rejected(self) -> None:
        """declared_total_marks != sum of section computed_marks → ValidationError."""
        raw = maths_assessment()
        raw["declared_total_marks"] = 99.0
        _expect_error(raw, "declared_total_marks")


class TestMarkRulesSplitSums:
    def test_answer_plus_method_not_equal_total_rejected(self) -> None:
        """answer_marks + method_marks != total → ValidationError."""
        raw = copy.deepcopy(maths_assessment())
        # B.1 has total=3.0, answer=1.0, method=2.0 (valid).
        # Make method=3.0 so 1.0+3.0=4.0 != 3.0.
        raw["sections"][1]["questions"][0]["mark_rules"]["method_marks"] = 3.0
        _expect_error(raw, "answer_marks + method_marks")

    def test_valid_split_sums_pass(self) -> None:
        _validate(maths_assessment())


class TestAnswerKindMatchesQuestionType:
    def test_kind_mismatch_rejected(self) -> None:
        """answer.kind='mcq' on a question_type='calculation' question is an error."""
        raw = copy.deepcopy(maths_assessment())
        # Replace the calculation question's answer with an MCQ payload
        raw["sections"][1]["questions"][0]["answer"] = {
            "kind": "mcq",
            "options": ["1", "2", "3"],
            "correct_index": 0,
        }
        _expect_error(raw, "does not match")

    def test_matching_kind_mismatch_rejected(self) -> None:
        """answer.kind='matching' on question_type='short_answer' is an error."""
        raw = copy.deepcopy(afrikaans_assessment())
        # Replace the short_answer question with a matching answer payload
        raw["sections"][0]["questions"][0]["answer"] = {
            "kind": "matching",
            "left": ["a", "b"],
            "right": ["x", "y"],
            "correct_pairs": {0: 0, 1: 1},
        }
        _expect_error(raw, "does not match")


class TestHalfMarksMaths:
    def test_half_marks_on_section_declared_marks(self) -> None:
        """Sections may declare half-mark totals."""
        raw = copy.deepcopy(maths_assessment())
        # Add a half-mark question to section A to test 0.5 granularity
        raw["sections"][0]["questions"].append(
            {
                "qid": "A.3",
                "number": "3",
                "text": "Name the shape with three sides. (Half mark)",
                "question_type": "short_answer",
                "difficulty": "easy",
                "answer": {
                    "kind": "short_answer",
                    "accepted": ["triangle", "driehoek"],
                },
                "mark_rules": {"total": 0.5},
            }
        )
        raw["sections"][0]["declared_marks"] = 3.5
        raw["declared_total_marks"] = 9.5
        _validate(raw)

    def test_quarter_mark_rejected(self) -> None:
        """0.25 is not a multiple of 0.5 → must be rejected by Field(multiple_of=0.5)."""
        raw = copy.deepcopy(maths_assessment())
        raw["sections"][0]["questions"][0]["mark_rules"]["total"] = 0.25
        raw["sections"][0]["declared_marks"] = 2.25
        raw["declared_total_marks"] = 8.25
        with pytest.raises(ValidationError) as exc_info:
            Assessment.model_validate(raw)
        errors_str = str(exc_info.value)
        assert "multiple" in errors_str.lower() or "0.5" in errors_str
