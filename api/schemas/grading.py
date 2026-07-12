"""Pydantic models for Phase 2 grading engine output and Phase 3 review.

``QuestionMark`` is the single source of truth for a marked question —
it matches the ``question_marks`` DB table column-for-column.

All mark values are ``Decimal`` with 0.5-step enforcement.
Grading attaches to question type, never subject (ARCHITECTURE.md §6, golden rule 4).

Phase 3 additions:
- ``QuestionContext`` carries the child's submitted response (human-readable) and
  the correct answer / memo for the parent review screen.  This endpoint is
  parent-only — memo exposure here is correct and intentional (ARCHITECTURE.md §5).
- ``ChildAnswerRendered`` / ``CorrectAnswerRendered`` are the rendered string forms
  of the child's payload and the assessment's answer payload respectively,
  keyed on ``question_type`` — never on ``subject`` (golden rule 4).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from schemas.assessment_schema import (
    AnswerPayload,
    CalculationAnswer,
    ErrorCategory,
    ExtendedResponseAnswer,
    FillBlankAnswer,
    GradingPath,
    LabellingAnswer,
    MatchingAnswer,
    McqAnswer,
    OrderingAnswer,
    ShortAnswerSpec,
    TableCompletionAnswer,
    TrueFalseAnswer,
)


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
# Human-readable rendering of child and correct answers for the review screen
# ---------------------------------------------------------------------------


def render_child_answer(question_type: str, payload: dict[str, Any]) -> str:
    """Render a child's raw response payload into a human-readable string.

    Keyed on ``question_type`` — never on ``subject`` (golden rule 4).
    Used for the parent review screen; never shown to the child.

    Returns a compact, readable representation suitable for display.
    Returns "(not attempted)" when payload is empty / missing.
    """
    if not payload:
        return "(not attempted)"

    qt = question_type

    if qt == "mcq":
        idx = payload.get("selected_index")
        if idx is None:
            return "(no selection)"
        return f"Option {idx}"

    if qt == "true_false":
        val = payload.get("value")
        if val is None:
            return "(no answer)"
        return "True" if val else "False"

    if qt == "matching":
        pairs = payload.get("pairs", [])
        if not pairs:
            return "(no pairs)"
        rendered = ", ".join(
            f"{p.get('left')}→{p.get('right')}" for p in pairs if isinstance(p, dict)
        )
        return rendered or "(no pairs)"

    if qt == "ordering":
        order = payload.get("order", [])
        if not order:
            return "(no order)"
        return " → ".join(str(i) for i in order)

    if qt == "fill_blank":
        values = payload.get("values", [])
        if not values:
            return "(no values)"
        return " | ".join(str(v) for v in values)

    if qt == "short_answer":
        text = payload.get("text", "")
        return str(text) if text else "(no answer)"

    if qt == "calculation":
        answer = payload.get("answer", "")
        working = payload.get("working", "")
        parts = [f"Answer: {answer}"] if answer else ["(no answer)"]
        if working:
            parts.append(f"Working: {working}")
        return "; ".join(parts)

    if qt == "table_completion":
        cells = payload.get("cells", [])
        if not cells:
            return "(no cells)"
        rendered_cells = [
            f"[{c.get('row')},{c.get('col')}]={c.get('value', '')}"
            for c in cells
            if isinstance(c, dict)
        ]
        return "; ".join(rendered_cells) or "(no cells)"

    if qt == "labelling":
        labels = payload.get("labels", {})
        if not labels:
            return "(no labels)"
        if isinstance(labels, dict):
            return "; ".join(f"{k}={v}" for k, v in labels.items())
        return str(labels)

    if qt == "extended_response":
        text = payload.get("text", "")
        return str(text) if text else "(no answer)"

    # Unknown type — render raw keys (never crashes, never leaks answer info).
    keys = list(payload.keys())
    return f"(raw: {', '.join(keys)})"


def render_correct_answer(answer: AnswerPayload) -> str:
    """Render the assessment's answer payload into a human-readable string.

    This is the MEMO view — only the parent may see this.  The child results
    endpoint must never call this function (ARCHITECTURE.md §5, capture.py
    child-view projection principle).

    Keyed on AnswerPayload subtype — never on subject (golden rule 4).
    """
    if isinstance(answer, McqAnswer):
        correct = answer.options[answer.correct_index]
        return f"Option {answer.correct_index}: {correct}"

    if isinstance(answer, TrueFalseAnswer):
        val = "True" if answer.is_true else "False"
        if answer.requires_correction and answer.corrected_statement:
            return f"{val} — Correction: {answer.corrected_statement}"
        return val

    if isinstance(answer, MatchingAnswer):
        pairs = []
        for li, ri in sorted(answer.correct_pairs.items()):
            left_text = answer.left[li] if li < len(answer.left) else str(li)
            right_text = answer.right[ri] if ri < len(answer.right) else str(ri)
            pairs.append(f"{left_text} → {right_text}")
        return "; ".join(pairs) if pairs else "(no pairs)"

    if isinstance(answer, OrderingAnswer):
        ordered = [answer.items[i] for i in answer.correct_order if i < len(answer.items)]
        return " → ".join(ordered) if ordered else "(no order)"

    if isinstance(answer, FillBlankAnswer):
        accepted_per_blank = ["/".join(b.accepted) for b in answer.blanks]
        return " | ".join(accepted_per_blank) if accepted_per_blank else "(no blanks)"

    if isinstance(answer, ShortAnswerSpec):
        primary = answer.accepted[0] if answer.accepted else "(none)"
        alts = answer.accepted[1:]
        base = f"Answer: {primary}"
        if alts:
            base += f" (also: {', '.join(alts)})"
        if answer.marker_guidance:
            base += f" — {answer.marker_guidance}"
        return base

    if isinstance(answer, CalculationAnswer):
        parts = [f"Answer: {answer.final_answer}"]
        if answer.unit:
            parts[0] += f" {answer.unit}"
        if answer.number_sentence:
            parts.append(f"Equation: {answer.number_sentence}")
        if answer.method_steps:
            steps = "; ".join(answer.method_steps)
            parts.append(f"Steps: {steps}")
        return " | ".join(parts)

    if isinstance(answer, TableCompletionAnswer):
        cell_answers = []
        for cell in answer.cells:
            rh = (
                answer.row_headers[cell.row]
                if cell.row < len(answer.row_headers)
                else str(cell.row)
            )
            ch = (
                answer.col_headers[cell.col]
                if cell.col < len(answer.col_headers)
                else str(cell.col)
            )
            accepted = "/".join(cell.accepted)
            cell_answers.append(f"[{rh},{ch}]={accepted}")
        return "; ".join(cell_answers) if cell_answers else "(no cells)"

    if isinstance(answer, LabellingAnswer):
        labels = [f"{pos}={term}" for pos, term in sorted(answer.positions.items())]
        return "; ".join(labels) if labels else "(no positions)"

    if isinstance(answer, ExtendedResponseAnswer):
        rubric_summary = "; ".join(f"{rp.point} ({rp.marks}m)" for rp in answer.rubric)
        return f"Model answer (summary): {rubric_summary}"

    # Unreachable — exhaustive by AnswerPayload union.
    return "(unknown answer type)"


# ---------------------------------------------------------------------------
# Question context for the review screen
# ---------------------------------------------------------------------------


class QuestionContext(BaseModel):
    """Question context attached to a mark for the parent review screen.

    Carries text/type/mark_rules, the child's submitted response (rendered as
    a human-readable string), and the correct answer / memo (also rendered).

    This model is parent-only — memo exposure here is correct (ARCHITECTURE.md §5).
    The child-facing view must never include ``child_answer_rendered`` (it would
    be a no-op anyway since the child sees a separate capture endpoint), but more
    importantly it must NEVER include ``correct_answer_rendered``.

    ``child_answer_rendered`` and ``correct_answer_rendered`` are plain strings,
    rendered via ``render_child_answer`` and ``render_correct_answer`` at query
    time.  The raw payload is NOT re-exposed here to keep the boundary clean.
    """

    qid: str
    number: str
    text: str
    question_type: str
    marks_total: Decimal
    answer_marks: Decimal | None = None
    method_marks: Decimal | None = None

    # Phase 3 additions — parent-only memo exposure.
    child_answer_rendered: str = Field(
        default="(not attempted)",
        description=(
            "Human-readable rendering of the child's submitted response for this question. "
            "Rendered server-side; safe to display directly on the review screen. "
            "PARENT-ONLY — must not appear in any child-facing response."
        ),
    )
    correct_answer_rendered: str = Field(
        default="(no answer key)",
        description=(
            "Human-readable rendering of the correct answer / memo for this question. "
            "Derived from the assessment's AnswerPayload. "
            "PARENT-ONLY — MUST NOT be included in any child-facing response. "
            "The future child results endpoint must never call render_correct_answer."
        ),
    )


class QuestionMarkWithContext(BaseModel):
    """QuestionMark + its enriched question context, for Phase 3 review."""

    mark: QuestionMark
    question: QuestionContext


class ListMarksWithContextResponse(BaseModel):
    """Extended response from GET /cycles/{cycle_id}/marks, with question context."""

    cycle_id: uuid.UUID
    submission_id: uuid.UUID
    items: list[QuestionMarkWithContext]
