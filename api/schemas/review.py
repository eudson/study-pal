"""Phase 3 — parent mark review + publish gate schemas.

All types here are Pydantic v2 models — no bare dicts cross service
boundaries (ARCHITECTURE.md §8).

``MarkPatchRequest``  — PATCH /cycles/{cycle_id}/marks/{question_id} body.
``MarkPatchResponse`` — updated mark returned from the PATCH endpoint.
``PublishRequest``    — POST /cycles/{cycle_id}/publish body.
``PublishResponse``   — cycle state + publish metadata returned on success.

Mark values: Decimal, 0.5-step everywhere (golden rule 7).
Subject-agnostic: no if-subject branches (golden rule 4).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from schemas.assessment_schema import ErrorCategory
from schemas.family import CycleState, VisibilityDefaults
from schemas.grading import QuestionMark, _half_step

# ---------------------------------------------------------------------------
# PATCH /cycles/{cycle_id}/marks/{question_id}
# ---------------------------------------------------------------------------


class MarkPatchRequest(BaseModel):
    """Parent override of a single question mark.

    At least one of final_marks / error_category / note must be provided.

    Rules (enforced here AND by the DB check constraint):
    - final_marks must be in [0, marks_total] in 0.5 steps.
    - marks_total is not known at parse time so the 0 lower-bound and
      0.5-step are the only static constraints here; the upper-bound is
      checked in the service layer against the stored marks_total.
    """

    final_marks: Decimal | None = Field(
        default=None,
        description=(
            "Parent's confirmed mark. Must be a non-negative multiple of 0.5. "
            "Upper bound checked against marks_total in the service layer."
        ),
    )
    error_category: ErrorCategory | None = Field(
        default=None,
        description="Gap-report error category (§6 enum).",
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional parent note (not persisted in this phase — reserved for future).",
    )

    @field_validator("final_marks", mode="after")
    @classmethod
    def _validate_final_marks(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        return _half_step(v)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> MarkPatchRequest:
        if self.final_marks is None and self.error_category is None and self.note is None:
            raise ValueError(
                "At least one of final_marks, error_category, or note must be provided."
            )
        return self


class MarkPatchResponse(BaseModel):
    """Updated QuestionMark returned from PATCH /cycles/{cycle_id}/marks/{question_id}."""

    mark: QuestionMark


# ---------------------------------------------------------------------------
# POST /cycles/{cycle_id}/publish
# ---------------------------------------------------------------------------


class PublishRequest(BaseModel):
    """Per-cycle visibility override merged with the child's visibility_defaults.

    All fields are optional — omitted fields fall back to the child's stored
    visibility_defaults.  The merged result is frozen as ``published_visibility``
    in the cycle row (golden rule 8: approval + timestamp).
    """

    accuracy: bool | None = Field(
        default=None,
        description=(
            "Override the child's accuracy toggle for this cycle's published view. "
            "Defaults to child's visibility_defaults.accuracy when omitted."
        ),
    )
    effort: bool | None = Field(
        default=None,
        description="Override effort toggle. Defaults to child's visibility_defaults.effort.",
    )
    growing: bool | None = Field(
        default=None,
        description="Override growing toggle. Defaults to child's visibility_defaults.growing.",
    )
    ai_rationale: bool | None = Field(
        default=None,
        description=(
            "Override ai_rationale toggle. Defaults to child's visibility_defaults.ai_rationale. "
            "NOTE: the future child results endpoint MUST filter ai_rationale server-side "
            "based on published_visibility.ai_rationale — never expose it when False."
        ),
    )

    def merge_with_defaults(self, defaults: VisibilityDefaults) -> VisibilityDefaults:
        """Return a new VisibilityDefaults merging overrides onto child defaults.

        Fields present in the request take precedence; absent fields keep the
        child's stored default.  The result is the frozen published_visibility.
        """
        return VisibilityDefaults(
            accuracy=self.accuracy if self.accuracy is not None else defaults.accuracy,
            effort=self.effort if self.effort is not None else defaults.effort,
            growing=self.growing if self.growing is not None else defaults.growing,
            ai_rationale=(
                self.ai_rationale if self.ai_rationale is not None else defaults.ai_rationale
            ),
        )


class PublishResponse(BaseModel):
    """Response from POST /cycles/{cycle_id}/publish.

    Carries the updated cycle state and the frozen visibility snapshot.
    The child results endpoint (future phase) MUST read published_visibility
    server-side and exclude ai_rationale when its toggle is False.
    """

    cycle_id: uuid.UUID
    state: CycleState
    marks_published_at: datetime
    published_visibility: VisibilityDefaults


# ---------------------------------------------------------------------------
# Unresolved marks error detail (409 guard for publish)
# ---------------------------------------------------------------------------


class UnresolvedMarksError(BaseModel):
    """Structured 409 body returned when publish is blocked by unresolved marks.

    unresolved_question_ids: the question_ids whose final_marks is still NULL.
    The parent must set final_marks on every question before publishing.
    """

    detail: str
    unresolved_question_ids: list[str]
