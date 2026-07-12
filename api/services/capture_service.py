"""Capture service — child-safe assessment projection.

Builds a ``ChildAssessmentView`` from a full ``Assessment`` by explicit
projection.  Every answer-key and memo field is excluded; only structural
and display information reaches the wire.

This is the ONLY code path that may produce a ChildAssessmentView.
The router calls ``project_for_child`` — never accesses Assessment fields
directly when building the child view.
"""

from __future__ import annotations

from schemas.assessment_schema import (
    Assessment,
    CalculationAnswer,
    ExtendedResponseAnswer,
    FillBlankAnswer,
    LabellingAnswer,
    MatchingAnswer,
    McqAnswer,
    OrderingAnswer,
    Question,
    Section,
    ShortAnswerSpec,
    TableCompletionAnswer,
    TrueFalseAnswer,
)
from schemas.capture import (
    ChildAnswerView,
    ChildAssessmentView,
    ChildCalculationView,
    ChildExtendedResponseView,
    ChildFillBlankView,
    ChildLabellingView,
    ChildMatchingView,
    ChildMcqView,
    ChildOrderingView,
    ChildQuestionView,
    ChildSectionView,
    ChildShortAnswerView,
    ChildTableCompletionView,
    ChildTrueFalseView,
)


def _project_answer(question: Question) -> ChildAnswerView:
    """Strip all answer-key information, returning structure-only view."""
    answer = question.answer

    if isinstance(answer, McqAnswer):
        # options text is printed on the paper and safe; correct_index is NOT
        return ChildMcqView(options=list(answer.options))

    if isinstance(answer, TrueFalseAnswer):
        # Nothing safe to expose beyond question_type already on parent model
        return ChildTrueFalseView()

    if isinstance(answer, MatchingAnswer):
        # left/right item text is printed; correct_pairs mapping is NOT
        return ChildMatchingView(left=list(answer.left), right=list(answer.right))

    if isinstance(answer, OrderingAnswer):
        # items as-shuffled are printed; correct_order is NOT
        return ChildOrderingView(items=list(answer.items))

    if isinstance(answer, FillBlankAnswer):
        # blank count and value types are layout info; accepted answers are NOT
        return ChildFillBlankView(
            blank_count=len(answer.blanks),
            value_types=[b.value_type for b in answer.blanks],
        )

    if isinstance(answer, ShortAnswerSpec):
        # Nothing safe beyond question text (no accepted alternatives, no keywords)
        return ChildShortAnswerView()

    if isinstance(answer, CalculationAnswer):
        # Only working_lines_hint (from render_hints) — final_answer/method_steps NOT
        return ChildCalculationView(
            working_lines_hint=question.render_hints.working_lines,
        )

    if isinstance(answer, TableCompletionAnswer):
        # Row/col headers are printed; answer cells' accepted values are NOT.
        # blank_cell_positions exposes only {row, col} coordinates.
        blank_positions = [{"row": c.row, "col": c.col} for c in answer.cells]
        return ChildTableCompletionView(
            row_headers=list(answer.row_headers),
            col_headers=list(answer.col_headers),
            format_example_row=answer.format_example_row,
            blank_cell_positions=blank_positions,
        )

    if isinstance(answer, LabellingAnswer):
        # Position IDs and optional term_bank are printed on paper; correct labels NOT.
        return ChildLabellingView(
            position_ids=list(answer.positions.keys()),
            term_bank=list(answer.term_bank),
            diagram_asset=answer.diagram_asset,
        )

    if isinstance(answer, ExtendedResponseAnswer):
        # model_answer, rubric, required_structure all excluded
        return ChildExtendedResponseView()

    # Exhaustive match on AnswerPayload union — this line should be unreachable.
    raise TypeError(f"Unknown answer type: {type(answer)}")  # pragma: no cover


def _project_question(question: Question) -> ChildQuestionView:
    return ChildQuestionView(
        qid=question.qid,
        number=question.number,
        text=question.text,
        question_type=question.question_type,
        marks_total=question.mark_rules.total,
        render_hints=question.render_hints,
        answer_view=_project_answer(question),
    )


def _project_section(section: Section) -> ChildSectionView:
    return ChildSectionView(
        label=section.label,
        title=section.title,
        instructions=section.instructions,
        declared_marks=section.declared_marks,
        questions=[_project_question(q) for q in section.questions],
    )


def project_for_child(assessment: Assessment) -> ChildAssessmentView:
    """Build the memo-free child view from a full ``Assessment``.

    This is the authoritative projection function.  All answer-key fields
    are excluded by construction — never by omission of a guard.
    """
    return ChildAssessmentView(
        assessment_id=assessment.assessment_id,
        cycle_id=assessment.cycle_id,
        variant=assessment.variant,
        subject=assessment.subject,
        content_language=assessment.content_language,
        grade_label=assessment.grade_label,
        title=assessment.title,
        duration_minutes=assessment.duration_minutes,
        instructions=list(assessment.instructions),
        declared_total_marks=assessment.declared_total_marks,
        sections=[_project_section(s) for s in assessment.sections],
    )
