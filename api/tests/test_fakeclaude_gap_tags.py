"""Regression tests: FakeClaude-seeded ``gap_tags`` -> populated A-vs-B comparison.

Background (docs/PROGRESS.md, live browser re-test 2026-07-23): the full
diagnostic loop runs end to end on FakeClaude, but every generated question
carried ``gap_tags: []``. ``derive_gap_report`` and ``derive_ab_comparison``
are both correct and independently unit-tested (they're exercised elsewhere
with hand-built ``GapReport``s carrying tags directly) — the break was purely
that FakeClaude never gave round-1 questions anything to tag. A child who
went from A 4.5/9 to B 9/9 still saw "0 closed / no comparable areas".

``services/claude_client.py::_seed_deterministic_gap_tags`` fixes this by
giving every round-1 (Variant A) question a stable, deterministic,
structural tag (``{question_type}-{section_label}{index}``). Round-2
(Variant B) generation already propagated a source gap's tag onto its
retargeted questions correctly (``_derive_variant_b_doc``'s round-robin
assignment from ``build_gap_retargets``'s tag list) — that hop needed no fix,
it simply had nothing to propagate before now.

These tests run the ACTUAL FakeClaude generation path end to end (never
hand-construct a ``GapReport`` with tags already present) to prove the whole
chain: generation -> derive_gap_report -> build_gap_retargets ->
generate_variant_b -> derive_gap_report -> derive_ab_comparison.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from schemas.assessment_schema import GradingPath, VariantBRequest
from schemas.gap_report import GapStatus
from schemas.generation import GenerateAssessmentRequest
from schemas.grading import QuestionMark
from services.claude_client import FakeClaude
from services.comparison import derive_ab_comparison
from services.gap_report import build_gap_retargets, derive_gap_report
from services.generation_service import GenerationService


def _mark(
    qid: str, total: str, final: str, *, family_id: uuid.UUID, submission_id: uuid.UUID
) -> QuestionMark:
    return QuestionMark(
        family_id=family_id,
        submission_id=submission_id,
        question_id=qid,
        marks_total=Decimal(total),
        suggested_marks=Decimal(final),
        final_marks=Decimal(final),
        grading_path=GradingPath.AUTO,
        needs_review=False,
    )


class TestFakeClaudeRound1SeedsGapTags:
    """Round-1 generation on FakeClaude must yield non-empty ``gap_tags``,
    and a growing question's tag must surface in the gap report summary."""

    def test_generated_questions_all_carry_gap_tags(self) -> None:
        service = GenerationService(claude=FakeClaude())
        result = service.generate(
            GenerateAssessmentRequest(
                cycle_id=str(uuid.uuid4()),
                scope_text="Grade 5 Mathematics — measurement and fractions",
            )
        )
        assert result.ok
        assessment = result.assessment
        assert assessment is not None

        all_questions = [q for section in assessment.sections for q in section.questions]
        assert all_questions, "sanity: sample assessment has questions"
        for q in all_questions:
            assert q.gap_tags, f"question {q.qid} has empty gap_tags — FakeClaude seeding broken"

        # Deterministic: tags are stable across independent generations.
        result2 = GenerationService(claude=FakeClaude()).generate(
            GenerateAssessmentRequest(
                cycle_id=str(uuid.uuid4()),
                scope_text="Grade 5 Mathematics — measurement and fractions",
            )
        )
        assert result2.assessment is not None
        tags1 = [q.gap_tags for section in assessment.sections for q in section.questions]
        tags2 = [q.gap_tags for section in result2.assessment.sections for q in section.questions]
        assert tags1 == tags2

    def test_growing_question_gap_tag_surfaces_in_gap_report_summary(self) -> None:
        """This is the exact bug: a below-full-marks question's gap_tags must
        be non-empty and must appear in ``summary.growing_gap_tags`` — derived
        from a REAL FakeClaude assessment, not a hand-built one."""
        service = GenerationService(claude=FakeClaude())
        result = service.generate(
            GenerateAssessmentRequest(cycle_id=str(uuid.uuid4()), scope_text="scope")
        )
        assert result.ok
        assessment = result.assessment
        assert assessment is not None

        family_id = uuid.uuid4()
        submission_id = uuid.uuid4()

        # Mark every question as "not attempted" (0 marks) so every question
        # is growing — sufficient to prove the tags flow through; the
        # closed/persisting split is covered by the comparison test below.
        marks = [
            _mark(
                q.qid,
                str(q.mark_rules.total),
                "0",
                family_id=family_id,
                submission_id=submission_id,
            )
            for section in assessment.sections
            for q in section.questions
        ]

        report = derive_gap_report(assessment, marks)
        assert report.summary.growing_count == len(marks)
        assert report.summary.growing_gap_tags, (
            "growing_gap_tags is empty even though every question is growing "
            "and every question has gap_tags — FakeClaude seeding regression"
        )
        for item in report.items:
            assert item.status == GapStatus.GROWING
            assert item.gap_tags, f"{item.question_id}: gap_tags empty on a growing item"


class TestFakeClaudeAbComparisonPartitions:
    """End-to-end A -> B comparison on FakeClaude: a gap the child masters in
    round 2 must land in ``closed``; a gap still missed must land in
    ``persisting``. Reproduces the live scenario (A 4.5/9 -> B 9/9 previously
    showed "0 closed")."""

    def test_closed_and_persisting_populate_from_fakeclaude(self) -> None:
        family_id = uuid.uuid4()

        # --- Round 1: generate on FakeClaude (maths sample, first call). ---
        gen_service = GenerationService(claude=FakeClaude())
        result_a = gen_service.generate(
            GenerateAssessmentRequest(cycle_id=str(uuid.uuid4()), scope_text="scope")
        )
        assert result_a.ok
        assessment_a = result_a.assessment
        assert assessment_a is not None

        # maths sample: A.1 mcq(1.0), A.2 fill_blank(2.0), B.1 calculation(3.0),
        # B.2 table_completion(3.0) -> seeded tags "mcq-a1", "fill_blank-a2",
        # "calculation-b1", "table_completion-b2".
        submission_a = uuid.uuid4()
        marks_a = [
            _mark("A.1", "1.0", "1.0", family_id=family_id, submission_id=submission_a),  # mastered
            _mark("A.2", "2.0", "0", family_id=family_id, submission_id=submission_a),  # growing
            _mark("B.1", "3.0", "1.5", family_id=family_id, submission_id=submission_a),  # growing
            _mark("B.2", "3.0", "3.0", family_id=family_id, submission_id=submission_a),  # mastered
        ]
        report_a = derive_gap_report(assessment_a, marks_a)
        assert report_a.summary.growing_gap_tags == ["calculation-b1", "fill_blank-a2"]

        # --- Round 2: retarget the growing gaps, generate Variant B. ---
        gaps = build_gap_retargets(report_a)
        assert {g.gap_id for g in gaps} == {"calculation-b1", "fill_blank-a2"}

        request_b = VariantBRequest(source_assessment=assessment_a, gaps=gaps)
        result_b = GenerationService(claude=FakeClaude()).generate_variant_b(
            request_b, assessment_id=str(uuid.uuid4())
        )
        assert result_b.ok
        assessment_b = result_b.assessment
        assert assessment_b is not None

        # Every Variant-B question must carry one of the retargeted tags
        # (FakeClaude's round-robin distribution — services/claude_client.py).
        b_tags = {
            tag
            for section in assessment_b.sections
            for q in section.questions
            for tag in q.gap_tags
        }
        assert b_tags == {"calculation-b1", "fill_blank-a2"}

        # Child masters everything tagged "calculation-b1" (mcq + calculation)
        # but still misses the item tagged "fill_blank-a2" that maps to the
        # fill_blank-derived question -> "calculation-b1" closes, "fill_blank-a2" persists.
        submission_b = uuid.uuid4()
        marks_b = [
            _mark("A.1", "1.0", "1.0", family_id=family_id, submission_id=submission_b),  # mastered
            _mark("A.2", "2.0", "0", family_id=family_id, submission_id=submission_b),  # growing
            _mark("B.1", "3.0", "3.0", family_id=family_id, submission_id=submission_b),  # mastered
            _mark("B.2", "3.0", "3.0", family_id=family_id, submission_id=submission_b),  # mastered
        ]
        report_b = derive_gap_report(assessment_b, marks_b)

        comparison = derive_ab_comparison(report_a, report_b)

        closed_tags = {d.gap_tag for d in comparison.closed}
        persisting_tags = {d.gap_tag for d in comparison.persisting}

        assert "calculation-b1" in closed_tags, (
            f"expected 'calculation-b1' closed; got closed={closed_tags} "
            f"persisting={persisting_tags} new={{d.gap_tag for d in comparison.new}}"
        )
        assert "fill_blank-a2" in persisting_tags
        assert comparison.summary.closed_count >= 1
        assert comparison.summary.persisting_count >= 1

    def test_all_gaps_closed_when_round_2_is_perfect(self) -> None:
        """The exact live scenario: A 4.5/9 -> B 9/9 must show every A-growing
        tag as closed, never '0 closed'."""
        family_id = uuid.uuid4()

        gen_service = GenerationService(claude=FakeClaude())
        result_a = gen_service.generate(
            GenerateAssessmentRequest(cycle_id=str(uuid.uuid4()), scope_text="scope")
        )
        assert result_a.ok
        assessment_a = result_a.assessment
        assert assessment_a is not None

        submission_a = uuid.uuid4()
        # A: 4.5 / 9.0 total (mirrors the live re-test's actual A score).
        marks_a = [
            _mark("A.1", "1.0", "1.0", family_id=family_id, submission_id=submission_a),
            _mark("A.2", "2.0", "0", family_id=family_id, submission_id=submission_a),
            _mark("B.1", "3.0", "1.5", family_id=family_id, submission_id=submission_a),
            _mark("B.2", "3.0", "2.0", family_id=family_id, submission_id=submission_a),
        ]
        report_a = derive_gap_report(assessment_a, marks_a)
        assert report_a.summary.total_marks_earned == Decimal("4.5")
        assert report_a.summary.growing_gap_tags, "sanity: A must have growing gaps to close"

        gaps = build_gap_retargets(report_a)
        request_b = VariantBRequest(source_assessment=assessment_a, gaps=gaps)
        result_b = GenerationService(claude=FakeClaude()).generate_variant_b(
            request_b, assessment_id=str(uuid.uuid4())
        )
        assert result_b.ok
        assessment_b = result_b.assessment
        assert assessment_b is not None

        submission_b = uuid.uuid4()
        # B: full marks on every question (9/9).
        marks_b = [
            _mark(
                q.qid,
                str(q.mark_rules.total),
                str(q.mark_rules.total),
                family_id=family_id,
                submission_id=submission_b,
            )
            for section in assessment_b.sections
            for q in section.questions
        ]
        report_b = derive_gap_report(assessment_b, marks_b)
        assert report_b.summary.growing_gap_tags == []

        comparison = derive_ab_comparison(report_a, report_b)

        assert set(report_a.summary.growing_gap_tags) == {d.gap_tag for d in comparison.closed}
        assert comparison.persisting == []
        assert comparison.new == []
        assert comparison.summary.closed_count == len(report_a.summary.growing_gap_tags)
        assert comparison.summary.closed_count > 0, (
            "regression: this is the exact live bug — closed must not be empty "
            "when round 2 is a perfect score"
        )
