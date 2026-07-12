"""Request/response models for assessment generation (C2/C3).

All models use Pydantic v2 idioms.  No bare dict crosses service boundaries.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from schemas.assessment_schema import Assessment
from schemas.validation import ValidationIssue


class GenerateAssessmentRequest(BaseModel):
    """Caller-supplied scope for a new Variant-A assessment."""

    cycle_id: str = Field(description="The cycle this assessment belongs to.")
    scope_text: str = Field(
        description="Educational scope (topic, grade, curriculum notes). Treated as untrusted."
    )


class GenerateAssessmentResponse(BaseModel):
    """Result of a generation call — either a valid assessment or a structured error."""

    ok: bool
    assessment: Assessment | None = None
    # Populated when ok=False.
    issues: list[ValidationIssue] = Field(default_factory=list)
    error: str | None = None


class CallLog(BaseModel):
    """Token usage + latency record for one Claude call (invariant 6 / §8)."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    attempt: int  # 1 = first call, 2 = repair retry
