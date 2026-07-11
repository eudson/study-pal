from fastapi import APIRouter

from schemas.validation import ValidateAssessmentRequest, ValidationResult
from services.validation_service import validate_assessment

router = APIRouter(prefix="/assessments")


@router.post(
    "/validate",
    response_model=ValidationResult,
    operation_id="validate_assessment",
)
def validate(request: ValidateAssessmentRequest) -> ValidationResult:
    """Validate a raw assessment payload against the canonical schema.

    Always returns HTTP 200 — the validation outcome (valid or not) is the
    payload, not an HTTP error.
    """
    return validate_assessment(request.assessment)
