"""Validates raw assessment payloads against the canonical schema.

This is the ONLY place a bare ``dict`` is allowed to cross a service
boundary (ARCHITECTURE.md §3, §8) — it is the deliberate entry point for
data of unknown validity (e.g. Claude output, or an uploaded document)
before it becomes a trusted ``Assessment`` model.
"""

from pydantic import ValidationError

from schemas.assessment_schema import SCHEMA_VERSION, Assessment
from schemas.validation import ValidationIssue, ValidationResult


def validate_assessment(raw: dict[str, object]) -> ValidationResult:
    try:
        Assessment.model_validate(raw)
    except ValidationError as e:
        issues = [
            ValidationIssue(
                loc=[str(part) if not isinstance(part, int) else part for part in err["loc"]],
                msg=err["msg"],
                type=err["type"],
            )
            for err in e.errors()
        ]
        return ValidationResult(valid=False, schema_version=SCHEMA_VERSION, issues=issues)
    return ValidationResult(valid=True, schema_version=SCHEMA_VERSION, issues=[])
