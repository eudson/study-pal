"""Pydantic models for Phase 2 grading engine output.

``QuestionMark`` is the single source of truth for a marked question —
it matches the ``question_marks`` DB table column-for-column.

All mark values are ``Decimal`` with 0.5-step enforcement.
Grading attaches to question type, never subject (ARCHITECTURE.md §6, golden rule 4).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from schemas.assessment_schema import ErrorCategory, GradingPath


def _half_step(v: Decimal) -> Decimal:
    """Validate that *v* is a non-negative multiple of 0.5."""
    if v < Decimal("0"):
        raise ValueError(f"Mark value {v} must be >= 0")
    remainder = (v * 2) % 1
    if remainder != 0:
        raise ValueError(f"Mark value {v} must be a multiple of 0.5")
    return v


class QuestionMark(BaseModel):
    """One graded question — mirrors the question_marks table.

    Rules:
    - marks_total, suggested_marks, final_marks are all half-steps (0.5 increments).
    - suggested_marks and final_marks must be <= marks_total.
    - final_marks is NULL until a parent reviews (Phase 3).
    - grading_path is snapshotted at grading time (not re-derived later).
    - For AUTO pure-graded questions, final_marks == suggested_marks at grading time.
    - For CLAUDE_ASSIST/AUTO_FUZZY non-matches, final_marks stays NULL.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    family_id: uuid.UUID
    submission_id: uuid.UUID
    question_id: str

    marks_total: Decimal
    suggested_marks: Decimal
    final_marks: Decimal | None = None

    grading_path: GradingPath
    confidence: Decimal | None = None
    needs_review: bool

    ai_rationale: str | None = None
    matched_alternative: str | None = None
    error_category: ErrorCategory | None = None

    reviewed_at: datetime | None = None
    overridden_at: datetime | None = None
    created_at: datetime | None = None

    @field_validator("marks_total", mode="after")
    @classmethod
    def _validate_marks_total(cls, v: Decimal) -> Decimal:
        return _half_step(v)

    @field_validator("suggested_marks", mode="after")
    @classmethod
    def _validate_suggested_marks(cls, v: Decimal) -> Decimal:
        return _half_step(v)

    @field_validator("final_marks", mode="after")
    @classmethod
    def _validate_final_marks(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        return _half_step(v)

    @field_validator("confidence", mode="after")
    @classmethod
    def _validate_confidence(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        if v < Decimal("0") or v > Decimal("1"):
            raise ValueError(f"confidence {v} must be in [0, 1]")
        return v

    @model_validator(mode="after")
    def _marks_within_total(self) -> QuestionMark:
        if self.suggested_marks > self.marks_total:
            raise ValueError(
                f"suggested_marks {self.suggested_marks} exceeds marks_total {self.marks_total}"
            )
        if self.final_marks is not None and self.final_marks > self.marks_total:
            raise ValueError(
                f"final_marks {self.final_marks} exceeds marks_total {self.marks_total}"
            )
        return self


# ---------------------------------------------------------------------------
# Grading summary response models
# ---------------------------------------------------------------------------


class GradingSummary(BaseModel):
    """Summary counts returned by POST /cycles/{cycle_id}/grade."""

    total_questions: int
    auto_marked: int = Field(description="Questions graded by AUTO path, no review needed.")
    needs_review: int = Field(description="Questions flagged for parent review.")
    not_attempted: int = Field(description="Questions skipped by the child.")


class GradeSubmissionResponse(BaseModel):
    """Response from POST /cycles/{cycle_id}/grade."""

    cycle_id: uuid.UUID
    submission_id: uuid.UUID
    summary: GradingSummary
    marks: list[QuestionMark]


class ListMarksResponse(BaseModel):
    """Response from GET /cycles/{cycle_id}/marks.

    Includes both the marks and the question context needed for the
    parent review screen (Phase 3).
    """

    cycle_id: uuid.UUID
    submission_id: uuid.UUID
    marks: list[QuestionMark]


# ---------------------------------------------------------------------------
# Question context for the review screen
# ---------------------------------------------------------------------------


class QuestionContext(BaseModel):
    """Minimal question context attached to a mark for the review screen.

    Carries text/type/mark_rules so the parent can evaluate without
    re-fetching the full assessment.
    """

    qid: str
    number: str
    text: str
    question_type: str
    marks_total: Decimal
    answer_marks: Decimal | None = None
    method_marks: Decimal | None = None


class QuestionMarkWithContext(BaseModel):
    """QuestionMark + its question context, for Phase 3 review."""

    mark: QuestionMark
    question: QuestionContext


class ListMarksWithContextResponse(BaseModel):
    """Extended response from GET /cycles/{cycle_id}/marks, with question context."""

    cycle_id: uuid.UUID
    submission_id: uuid.UUID
    items: list[QuestionMarkWithContext]
