"""Pydantic v2 models for the child results view (Phase 4+).

``ChildResultsView`` is the read-only, security-sensitive response that lets
a child see their PUBLISHED results, server-side-filtered through the frozen
``published_visibility`` snapshot from the cycle's publish row.

Security boundary (ARCHITECTURE.md §5, golden rule 8):
- MUST NOT contain any memo, correct-answer, accepted-alternative, or
  AnswerPayload-derived field.  The projection enforcing this is in
  ``services/child_results.py``.
- ``marks_earned`` / ``marks_total`` are ``Decimal`` so half-mark values
  (rule 7) round-trip without float error.
- All four visibility gates (``accuracy``, ``effort``, ``growing``,
  ``ai_rationale``) are applied by leaving gated fields ``None``; Pydantic
  serialises ``None`` as ``null`` / omits it — never present with a fake zero.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from schemas.gap_report import GapStatus


class ChildResultItem(BaseModel):
    """One question's child-visible result.

    Fields gated by ``published_visibility``:
    - ``marks_earned`` / ``marks_total``: present only when ``snapshot.accuracy`` is True.
    - ``status``: present only when ``snapshot.growing`` is True.
    - ``ai_rationale``: present only when ``snapshot.ai_rationale`` is True.

    Fields ABSOLUTELY EXCLUDED (structural exclusion, not runtime hiding):
    error_category, gap_tags, suggested_marks, confidence, needs_review,
    correct_answer_rendered, and any AnswerPayload-derived field.
    """

    question_id: str = Field(description="qid from the original assessment question.")
    number: str = Field(description='Question number as printed, e.g. "3", "3.1".')
    text: str = Field(description="The full question text/label.")
    child_answer_rendered: str = Field(
        description=(
            "Human-readable rendering of the child's submitted response. "
            "Rendered via render_child_answer — memo-free. "
            "Returns '(not attempted)' for unanswered questions."
        ),
    )
    marks_earned: Decimal | None = Field(
        default=None,
        description=(
            "Marks the child earned for this question. "
            "None when published_visibility.accuracy is False. "
            "Decimal to preserve 0.5-step half-marks (ARCHITECTURE.md rule 7)."
        ),
    )
    marks_total: Decimal | None = Field(
        default=None,
        description=(
            "Maximum marks available for this question. "
            "None when published_visibility.accuracy is False."
        ),
    )
    status: GapStatus | None = Field(
        default=None,
        description=(
            "mastered or growing. "
            "None when published_visibility.growing is False. "
            "'growing' is used instead of 'wrong'/'failed' (ARCHITECTURE.md §10 design rule)."
        ),
    )
    ai_rationale: str | None = Field(
        default=None,
        description=(
            "Claude's grading rationale, if present on the QuestionMark. "
            "None unless published_visibility.ai_rationale is True. "
            "Never present when the toggle is off — not filtered client-side."
        ),
    )


class ChildResultsSummary(BaseModel):
    """Aggregate summary of the child's published results.

    Fields are gated by ``published_visibility`` toggles exactly as the
    per-item fields are — ``None`` means the parent chose not to share that
    dimension with the child for this cycle.
    """

    total_questions: int = Field(description="Total number of questions in the assessment.")
    attempted_count: int | None = Field(
        default=None,
        description=(
            "Number of questions the child attempted (payload non-empty). "
            "None when published_visibility.effort is False."
        ),
    )
    mastered_count: int | None = Field(
        default=None,
        description=(
            "Number of questions fully mastered (full marks). "
            "None when published_visibility.growing is False."
        ),
    )
    growing_count: int | None = Field(
        default=None,
        description=(
            "Number of questions in 'growing' status (below full marks). "
            "None when published_visibility.growing is False."
        ),
    )
    marks_earned: Decimal | None = Field(
        default=None,
        description=(
            "Total marks earned across all questions. "
            "None when published_visibility.accuracy is False."
        ),
    )
    marks_available: Decimal | None = Field(
        default=None,
        description=(
            "Total marks available across all questions. "
            "None when published_visibility.accuracy is False."
        ),
    )


class ChildResultsView(BaseModel):
    """Complete child-visible results for a published cycle.

    Produced by ``services.child_results.project_results_for_child`` from the
    frozen ``published_visibility`` snapshot.  The snapshot is set at publish
    time and is never affected by subsequent changes to the child's
    ``visibility_defaults`` (drift test: the freeze is the contract).

    This model contains NO memo, correct-answer, or answer-key fields.
    """

    cycle_id: uuid.UUID = Field(description="The cycle these results belong to.")
    title: str = Field(description="Assessment title as printed on the paper.")
    published_at: datetime = Field(
        description="UTC timestamp when the parent published marks (marks_published_at)."
    )
    summary: ChildResultsSummary
    items: list[ChildResultItem] = Field(
        description=(
            "One item per question that has a reviewed mark, "
            "ordered to match assessment section/question order."
        ),
    )
