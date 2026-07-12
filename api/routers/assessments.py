from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import get_assessment_repository
from schemas.assessment_schema import Assessment
from schemas.generation import GenerateAssessmentRequest, GenerateAssessmentResponse
from schemas.identity import Identity
from schemas.validation import ValidateAssessmentRequest, ValidationResult
from services.auth import get_identity
from services.claude_client import FakeClaude
from services.generation_service import GenerationService
from services.repositories.base import AssessmentRepository
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


@router.post(
    "/generate",
    response_model=GenerateAssessmentResponse,
    operation_id="generate_assessment",
    status_code=status.HTTP_201_CREATED,
)
def generate(
    request: GenerateAssessmentRequest,
    identity: Identity = Depends(get_identity),
    repo: AssessmentRepository = Depends(get_assessment_repository),
) -> GenerateAssessmentResponse:
    """Generate a schema-valid Variant-A assessment from a scope text.

    - Requires a valid caller identity (``X-User-Id`` header in PR-1).
    - Routes output through the single validation gate.
    - On success, persists to Postgres under the caller's family tenancy.
    - On validation failure after one repair retry, returns a structured error
      (HTTP 422 with issues list).
    """
    service = GenerationService(claude=FakeClaude())
    result = service.generate(request)

    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": result.error,
                "issues": [issue.model_dump() for issue in result.issues],
            },
        )

    assessment: Assessment = result.assessment  # type: ignore[assignment]
    repo.save(assessment)

    return result
