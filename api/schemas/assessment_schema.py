"""
StudyPal — Assessment Schema v1 (single source of truth)
=========================================================
Canonical Pydantic models for everything the generation engine produces and
the capture/grading pipeline consumes. See ARCHITECTURE.md §3, §6.

Design rules encoded here:
- Subject-agnostic: `subject` is freeform; intelligence lives in question types.
- Language-agnostic: `content_language` drives generation/grading language.
- Marks may be half-marks (multiples of 0.5).
- Answer payloads are a discriminated union keyed on `kind`, which must match
  the question's `question_type` (validated).
- Declared totals must equal computed totals (validated) — this is the main
  guardrail against inconsistent Claude output.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, computed_field, model_validator

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class QuestionType(str, Enum):
    MCQ = "mcq"
    TRUE_FALSE = "true_false"
    MATCHING = "matching"
    ORDERING = "ordering"
    FILL_BLANK = "fill_blank"
    SHORT_ANSWER = "short_answer"
    CALCULATION = "calculation"
    TABLE_COMPLETION = "table_completion"
    LABELLING = "labelling"
    EXTENDED_RESPONSE = "extended_response"


class GradingPath(str, Enum):
    AUTO = "auto"                  # deterministic marking from child input
    AUTO_FUZZY = "auto_fuzzy"      # accepted alternatives; low confidence -> parent
    CLAUDE_ASSIST = "claude_assist"  # AI suggestion, parent confirms/edits


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    CHALLENGING = "challenging"    # rendered with a star


class ErrorCategory(str, Enum):
    """Subject-agnostic error taxonomy for gap reports (ARCHITECTURE.md §6)."""
    CONCEPT_GAP = "concept_gap"
    FORMAT_MISREAD = "format_misread"
    CARELESS = "careless"
    NOT_ATTEMPTED = "not_attempted"


# Base grading path per question type. fill_blank is refined at runtime:
# number-only blanks -> AUTO, word blanks -> AUTO_FUZZY.
GRADING_PATHS: dict[QuestionType, GradingPath] = {
    QuestionType.MCQ: GradingPath.AUTO,
    QuestionType.TRUE_FALSE: GradingPath.AUTO,
    QuestionType.MATCHING: GradingPath.AUTO,
    QuestionType.ORDERING: GradingPath.AUTO,
    QuestionType.FILL_BLANK: GradingPath.AUTO_FUZZY,
    QuestionType.SHORT_ANSWER: GradingPath.AUTO_FUZZY,
    QuestionType.CALCULATION: GradingPath.CLAUDE_ASSIST,  # answer AUTO + method assist
    QuestionType.TABLE_COMPLETION: GradingPath.CLAUDE_ASSIST,
    QuestionType.LABELLING: GradingPath.CLAUDE_ASSIST,
    QuestionType.EXTENDED_RESPONSE: GradingPath.CLAUDE_ASSIST,
}


# --------------------------------------------------------------------------
# Answer payloads (discriminated union on `kind`)
# --------------------------------------------------------------------------

class McqAnswer(BaseModel):
    kind: Literal["mcq"] = "mcq"
    options: list[str] = Field(min_length=3, max_length=5)
    correct_index: int = Field(ge=0)
    distractor_notes: dict[int, str] = Field(
        default_factory=dict,
        description="Why each wrong option is a plausible common error (memo aid).",
    )

    @model_validator(mode="after")
    def _index_in_range(self) -> "McqAnswer":
        if self.correct_index >= len(self.options):
            raise ValueError("correct_index out of range")
        return self


class TrueFalseAnswer(BaseModel):
    kind: Literal["true_false"] = "true_false"
    is_true: bool
    requires_correction: bool = Field(
        default=False,
        description="If a false statement must be corrected for the second mark.",
    )
    corrected_statement: Optional[str] = None

    @model_validator(mode="after")
    def _correction_present(self) -> "TrueFalseAnswer":
        if self.requires_correction and not self.is_true and not self.corrected_statement:
            raise ValueError("corrected_statement required when requires_correction")
        return self


class MatchingAnswer(BaseModel):
    kind: Literal["matching"] = "matching"
    left: list[str] = Field(min_length=2)
    right: list[str] = Field(min_length=2)
    correct_pairs: dict[int, int] = Field(
        description="left index -> right index. right may contain distractor extras."
    )

    @model_validator(mode="after")
    def _pairs_valid(self) -> "MatchingAnswer":
        for li, ri in self.correct_pairs.items():
            if li >= len(self.left) or ri >= len(self.right):
                raise ValueError("pair index out of range")
        if len(self.correct_pairs) != len(self.left):
            raise ValueError("every left item needs exactly one pair")
        return self


class OrderingAnswer(BaseModel):
    kind: Literal["ordering"] = "ordering"
    items: list[str] = Field(min_length=3, description="As printed (shuffled).")
    correct_order: list[int] = Field(description="Indices of `items` in correct sequence.")

    @model_validator(mode="after")
    def _is_permutation(self) -> "OrderingAnswer":
        if sorted(self.correct_order) != list(range(len(self.items))):
            raise ValueError("correct_order must be a permutation of item indices")
        return self


class Blank(BaseModel):
    accepted: list[str] = Field(min_length=1, description="All accepted answers/spellings.")
    value_type: Literal["word", "number"] = "word"
    case_sensitive: bool = False


class FillBlankAnswer(BaseModel):
    kind: Literal["fill_blank"] = "fill_blank"
    blanks: list[Blank] = Field(min_length=1)

    @property
    def effective_grading_path(self) -> GradingPath:
        if all(b.value_type == "number" for b in self.blanks):
            return GradingPath.AUTO
        return GradingPath.AUTO_FUZZY


class ShortAnswerSpec(BaseModel):
    kind: Literal["short_answer"] = "short_answer"
    accepted: list[str] = Field(min_length=1, description="Model answers/alternatives.")
    required_keywords: list[str] = Field(default_factory=list)
    marker_guidance: Optional[str] = None


class CalculationAnswer(BaseModel):
    kind: Literal["calculation"] = "calculation"
    final_answer: str
    unit: Optional[str] = None
    tolerance: Optional[float] = Field(default=None, description="Numeric tolerance if any.")
    number_sentence: Optional[str] = Field(
        default=None, description="Full number sentence for the memo (word problems)."
    )
    method_steps: list[str] = Field(
        default_factory=list, description="Worked steps; basis for method marks."
    )


class TableCell(BaseModel):
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    accepted: list[str] = Field(min_length=1)
    half_mark: bool = Field(default=False, description="0.5 mark per cell if True.")


class TableCompletionAnswer(BaseModel):
    kind: Literal["table_completion"] = "table_completion"
    row_headers: list[str]
    col_headers: list[str]
    cells: list[TableCell] = Field(min_length=1, description="Only cells the child fills.")
    format_example_row: bool = Field(
        default=True,
        description="Render one pre-completed row (discovery: table format misreads).",
    )

    @model_validator(mode="after")
    def _cells_in_grid(self) -> "TableCompletionAnswer":
        for c in self.cells:
            if c.row >= len(self.row_headers) or c.col >= len(self.col_headers):
                raise ValueError("cell outside table grid")
        return self


class LabellingAnswer(BaseModel):
    kind: Literal["labelling"] = "labelling"
    positions: dict[str, str] = Field(
        description="Position number (as printed on diagram) -> correct term."
    )
    term_bank: list[str] = Field(default_factory=list, description="Optional word bank.")
    diagram_asset: Optional[str] = Field(
        default=None, description="Storage path or asset id of the diagram."
    )


class RubricPoint(BaseModel):
    point: str
    marks: float = Field(gt=0, multiple_of=0.5)


class ExtendedResponseAnswer(BaseModel):
    kind: Literal["extended_response"] = "extended_response"
    model_answer: str
    rubric: list[RubricPoint] = Field(min_length=1)
    required_structure: Optional[str] = Field(
        default=None, description='e.g. "P.E.E. (Point, Evidence, Explain)".'
    )


AnswerPayload = Annotated[
    Union[
        McqAnswer,
        TrueFalseAnswer,
        MatchingAnswer,
        OrderingAnswer,
        FillBlankAnswer,
        ShortAnswerSpec,
        CalculationAnswer,
        TableCompletionAnswer,
        LabellingAnswer,
        ExtendedResponseAnswer,
    ],
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------
# Marks, rendering, memo
# --------------------------------------------------------------------------

class MarkRules(BaseModel):
    total: float = Field(gt=0, multiple_of=0.5)
    answer_marks: Optional[float] = Field(default=None, multiple_of=0.5)
    method_marks: Optional[float] = Field(default=None, multiple_of=0.5)
    tick_allocation: Optional[str] = Field(
        default=None, description='Memo note, e.g. "1 tick method, 1 tick answer".'
    )

    @model_validator(mode="after")
    def _split_sums(self) -> "MarkRules":
        if self.answer_marks is not None and self.method_marks is not None:
            if self.answer_marks + self.method_marks != self.total:
                raise ValueError("answer_marks + method_marks must equal total")
        return self


class RenderHints(BaseModel):
    working_lines: int = Field(default=0, ge=0, description="Blank working lines to print.")
    layout: Optional[str] = Field(default=None, description='e.g. "two_column", "table".')
    diagram_prompt: Optional[str] = Field(
        default=None,
        description="Image-generation prompt box if a visual aid is needed (no image gen in MVP).",
    )
    page_break_before: bool = False


class Memo(BaseModel):
    worked_solution: Optional[str] = None
    marker_tip: Optional[str] = None


# --------------------------------------------------------------------------
# Question / Section / Assessment
# --------------------------------------------------------------------------

class Question(BaseModel):
    qid: str = Field(description="Stable unique id, e.g. 'A.3' or a uuid.")
    number: str = Field(description='As printed: "3", "3.1", "3a".')
    text: str
    question_type: QuestionType
    difficulty: Difficulty
    answer: AnswerPayload
    mark_rules: MarkRules
    render_hints: RenderHints = Field(default_factory=RenderHints)
    memo: Memo = Field(default_factory=Memo)
    gap_tags: list[str] = Field(
        default_factory=list,
        description="Gap ids this question deliberately retests (Variant B retargeting).",
    )

    @computed_field
    @property
    def grading_path(self) -> GradingPath:
        if isinstance(self.answer, FillBlankAnswer):
            return self.answer.effective_grading_path
        return GRADING_PATHS[self.question_type]

    @model_validator(mode="after")
    def _kind_matches_type(self) -> "Question":
        if self.answer.kind != self.question_type.value:
            raise ValueError(
                f"answer.kind '{self.answer.kind}' does not match "
                f"question_type '{self.question_type.value}'"
            )
        return self


class Section(BaseModel):
    label: str = Field(description='"A", "B", ...')
    title: str
    instructions: Optional[str] = None
    declared_marks: float = Field(gt=0, multiple_of=0.5)
    questions: list[Question] = Field(min_length=1)

    @computed_field
    @property
    def computed_marks(self) -> float:
        return sum(q.mark_rules.total for q in self.questions)

    @model_validator(mode="after")
    def _totals_agree(self) -> "Section":
        if self.computed_marks != self.declared_marks:
            raise ValueError(
                f"Section {self.label}: declared {self.declared_marks} "
                f"!= computed {self.computed_marks}"
            )
        return self


class Assessment(BaseModel):
    schema_version: str = SCHEMA_VERSION
    assessment_id: str
    cycle_id: str
    variant: Literal["A", "B"]
    subject: str = Field(description="Freeform; the app never interprets it.")
    content_language: str = Field(description='ISO 639-1, e.g. "en", "af", "fr", "zu".')
    grade_label: str = Field(description='Freeform, e.g. "Grade 5".')
    title: str
    duration_minutes: int = Field(gt=0)
    instructions: list[str] = Field(default_factory=list)
    declared_total_marks: float = Field(gt=0, multiple_of=0.5)
    sections: list[Section] = Field(min_length=1)

    @computed_field
    @property
    def computed_total_marks(self) -> float:
        return sum(s.computed_marks for s in self.sections)

    @model_validator(mode="after")
    def _grand_total_agrees(self) -> "Assessment":
        if self.computed_total_marks != self.declared_total_marks:
            raise ValueError(
                f"declared_total_marks {self.declared_total_marks} "
                f"!= computed {self.computed_total_marks}"
            )
        return self


# --------------------------------------------------------------------------
# Variant B retargeting input
# --------------------------------------------------------------------------

class GapRetarget(BaseModel):
    """One flagged gap from a gap report, fed into Variant B generation."""
    gap_id: str
    category: ErrorCategory
    description: str = Field(description="Root-cause note, e.g. 'writes plurals in diminutive section'.")
    source_question_ids: list[str] = Field(default_factory=list)


class VariantBRequest(BaseModel):
    source_assessment: Assessment
    gaps: list[GapRetarget]
    note: str = (
        "Same structure and difficulty; all values, names and contexts changed; "
        "every gap deliberately retested via gap_tags on the new questions."
    )


# --------------------------------------------------------------------------
# Capture-side stubs (v1 draft — Week 3/4 will extend, not replace)
# --------------------------------------------------------------------------

class ChildResponse(BaseModel):
    qid: str
    attempted: bool = True
    payload: dict = Field(
        default_factory=dict,
        description="Shape depends on question_type; validated by the grading service.",
    )


class Submission(BaseModel):
    submission_id: str
    assessment_id: str
    child_id: str
    responses: list[ChildResponse]
    proof_photo_paths: list[str] = Field(
        default_factory=list, description="Supabase Storage paths; audit only, never auto-graded."
    )
