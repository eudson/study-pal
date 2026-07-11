from pydantic import BaseModel, Field


class ValidationIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ValidateAssessmentRequest(BaseModel):
    """The one deliberate raw-dict entry point: an assessment payload of
    unknown validity, submitted for schema validation."""

    assessment: dict[str, object]


class ValidationResult(BaseModel):
    valid: bool
    schema_version: str
    issues: list[ValidationIssue] = Field(default_factory=list)
