"""Capture-side Pydantic models for Phase 1 child answer capture.

Rules enforced here:
- ChildAssessmentView / ChildQuestionView expose ONLY what the child needs.
  Every answer-key, memo, and grading-aid field is excluded by explicit
  projection — not hidden client-side.  A child inspecting the wire response
  must find no answer information.
- SubmissionCreate is the POST body for /cycles/{cycle_id}/submissions.
- SubmissionResponse is returned on success.
- ChildResponsePayload wraps the per-question response the child records;
  the payload dict is persisted without interpretation (grading is Phase 2).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from schemas.assessment_schema import QuestionType, RenderHints

# ---------------------------------------------------------------------------
# Child-safe question view — NO answer/memo fields
# ---------------------------------------------------------------------------


class ChildMcqView(BaseModel):
    """MCQ options text only — correct_index and distractor_notes excluded."""

    kind: str = "mcq"
    options: list[str]


class ChildTrueFalseView(BaseModel):
    """True/False question — is_true, requires_correction, corrected_statement excluded."""

    kind: str = "true_false"


class ChildMatchingView(BaseModel):
    """Left and right item lists only — correct_pairs excluded."""

    kind: str = "matching"
    left: list[str]
    right: list[str]


class ChildOrderingView(BaseModel):
    """Shuffled items only — correct_order excluded."""

    kind: str = "ordering"
    items: list[str]


class ChildFillBlankView(BaseModel):
    """Number of blanks + value types only — accepted answers excluded."""

    kind: str = "fill_blank"
    blank_count: int
    value_types: list[str]  # "word" | "number" per blank, no accepted values


class ChildShortAnswerView(BaseModel):
    """Short answer — accepted alternatives, required_keywords, marker_guidance excluded."""

    kind: str = "short_answer"


class ChildCalculationView(BaseModel):
    """Calculation — final_answer, method_steps, unit, tolerance, number_sentence excluded."""

    kind: str = "calculation"
    working_lines_hint: int = Field(
        default=0,
        description="How many working lines to show (from render_hints); not answer info.",
    )


class ChildTableCompletionView(BaseModel):
    """Table structure only — answer cells excluded.

    row_headers and col_headers are safe (structural); cells accepted values
    are stripped.  format_example_row tells the frontend whether to show the
    pre-filled example row (layout only, not the answers).
    """

    kind: str = "table_completion"
    row_headers: list[str]
    col_headers: list[str]
    format_example_row: bool
    blank_cell_positions: list[dict[str, int]] = Field(
        description="List of {row, col} dicts identifying which cells the child fills. "
        "No accepted-answer info — only cell coordinates.",
    )


class ChildLabellingView(BaseModel):
    """Diagram labelling — correct labels excluded.

    position_ids: the position numbers as printed.
    term_bank: optional word bank (allowed to be shown — it is printed on the paper).
    diagram_asset is included so the frontend can display the diagram.
    Correct label per position is excluded.
    """

    kind: str = "labelling"
    position_ids: list[str]
    term_bank: list[str]
    diagram_asset: str | None = None


class ChildExtendedResponseView(BaseModel):
    """Extended response — model_answer, rubric, required_structure excluded."""

    kind: str = "extended_response"


# Union alias used in ChildQuestionView.answer_view
ChildAnswerView = (
    ChildMcqView
    | ChildTrueFalseView
    | ChildMatchingView
    | ChildOrderingView
    | ChildFillBlankView
    | ChildShortAnswerView
    | ChildCalculationView
    | ChildTableCompletionView
    | ChildLabellingView
    | ChildExtendedResponseView
)


class ChildQuestionView(BaseModel):
    """A single question as visible to the child.

    Fields present: qid, number, text, question_type, marks_total (for display),
    render_hints (layout/working lines), answer_view (structure only — NO answers).
    Fields absent: answer payload answer keys/accepted values, memo, gap_tags,
    difficulty (used internally), grading_path.
    """

    qid: str
    number: str
    text: str
    question_type: QuestionType
    marks_total: float = Field(description="Total marks for this question — displayed to child.")
    render_hints: RenderHints
    answer_view: ChildAnswerView


class ChildSectionView(BaseModel):
    """A section as visible to the child — full structure, no answer keys."""

    label: str
    title: str
    instructions: str | None = None
    declared_marks: float
    questions: list[ChildQuestionView]


class ChildAssessmentView(BaseModel):
    """Full assessment as seen by the child in capture mode.

    Fields present: assessment_id, cycle_id, variant, subject (freeform display),
    content_language, grade_label, title, duration_minutes, instructions (public
    instructions printed at the top of the paper), declared_total_marks, sections
    (ChildSectionView — no answer keys anywhere).

    Fields absent: schema_version (internal), computed_total_marks (internal
    duplicate), and every answer/memo field in every question.
    """

    assessment_id: str
    cycle_id: str
    variant: str
    subject: str
    content_language: str
    grade_label: str
    title: str
    duration_minutes: int
    instructions: list[str]
    declared_total_marks: float
    sections: list[ChildSectionView]


# ---------------------------------------------------------------------------
# Submission create request / response
# ---------------------------------------------------------------------------


class ChildResponseItem(BaseModel):
    """One question's response from the child.

    payload is intentionally typed as dict[str, Any] — it is persisted without
    interpretation in Phase 1.  Grading (Phase 2) will validate shapes then.
    """

    qid: str
    attempted: bool = True
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Shape depends on question_type; not interpreted in Phase 1.",
    )


class SubmissionCreate(BaseModel):
    """POST body for POST /cycles/{cycle_id}/submissions."""

    child_id: uuid.UUID
    responses: list[ChildResponseItem]
    proof_photo_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Supabase Storage paths, supplied by the client for audit purposes only. "
            "NEVER used in grading (ARCHITECTURE.md §10 no-vision-grading decision). "
            "Not fetched or validated server-side this phase."
        ),
    )


class SubmissionResponse(BaseModel):
    """Response from POST /cycles/{cycle_id}/submissions."""

    submission_id: uuid.UUID
    assessment_id: str
    child_id: uuid.UUID
    cycle_id: uuid.UUID
    responses_count: int
    proof_photo_paths: list[str]
    created_at: str = Field(description="ISO 8601 UTC timestamp.")
