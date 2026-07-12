"""Router for cycles and cycle-state transitions.

Cycle state transitions go ONLY through ``api/services/cycle.py``
(ARCHITECTURE.md §5) — never by direct column updates here.

The generate endpoint wires FakeClaude generation into the state machine:
on success the cycle advances SCOPE_UPLOADED → GENERATING_A → PARENT_REVIEWS_DRAFT.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import get_assessment_repository, get_family_repository
from routers.families import _resolve_family_id
from schemas.family import CycleApprove, CycleCreate, CycleResponse
from schemas.generation import GenerateAssessmentRequest, GenerateAssessmentResponse
from schemas.identity import Identity
from services.auth import get_identity
from services.claude_client import FakeClaude
from services.cycle import (
    IllegalTransitionError,
    advance_to_generating,
    advance_to_parent_reviews,
    approve_draft,
)
from services.generation_service import GenerationService
from services.repositories.base import AssessmentRepository, FamilyRepository

router = APIRouter(prefix="/cycles")


@router.post(
    "",
    response_model=CycleResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_cycle",
    summary="Create a new diagnostic cycle for a subject.",
)
def create_cycle(
    body: CycleCreate,
    identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> CycleResponse:
    """Create a cycle in SCOPE_UPLOADED state.

    family_id is resolved server-side from the caller's membership (invariant 3).
    ``scope_text`` is the text-first scope intake for this slice.
    """
    family_id = _resolve_family_id(identity, repo)
    return repo.create_cycle(family_id, body.subject_id, body.scope_text)


@router.get(
    "/{cycle_id}",
    response_model=CycleResponse,
    operation_id="get_cycle",
    summary="Get a cycle by id, including its assessment(s) if present.",
)
def get_cycle(
    cycle_id: uuid.UUID,
    _identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> CycleResponse:
    cycle = repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")
    return cycle


@router.get(
    "",
    response_model=list[CycleResponse],
    operation_id="list_cycles",
    summary="List all cycles for the caller's family.",
)
def list_cycles(
    identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> list[CycleResponse]:
    family_id = _resolve_family_id(identity, repo)
    return repo.list_cycles(family_id)


@router.post(
    "/{cycle_id}/generate",
    response_model=GenerateAssessmentResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="generate_assessment_for_cycle",
    summary="Generate Variant-A assessment for a cycle; advances cycle state.",
)
def generate_for_cycle(
    cycle_id: uuid.UUID,
    body: GenerateAssessmentRequest,
    _identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    assessment_repo: AssessmentRepository = Depends(get_assessment_repository),
) -> GenerateAssessmentResponse:
    """Generate Variant-A using FakeClaude (live Claude is deferred).

    State machine:
      SCOPE_UPLOADED → GENERATING_A  (before generation starts)
      GENERATING_A   → PARENT_REVIEWS_DRAFT  (on success)

    On validation failure after retry, the cycle stays in GENERATING_A
    and a structured error is returned (HTTP 422).
    """
    # Ensure cycle_id in body matches the path param.
    if str(body.cycle_id) != str(cycle_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cycle_id in request body must match the path parameter.",
        )

    # Advance: SCOPE_UPLOADED → GENERATING_A
    try:
        advance_to_generating(family_repo, cycle_id)
    except IllegalTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    service = GenerationService(claude=FakeClaude())
    result = service.generate(body)

    if not result.ok:
        # Leave cycle in GENERATING_A (generation failed; parent must retry or intervene).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": result.error,
                "issues": [issue.model_dump() for issue in result.issues],
            },
        )

    from schemas.assessment_schema import Assessment

    assessment: Assessment = result.assessment  # type: ignore[assignment]
    assessment_repo.save(assessment)

    # Advance: GENERATING_A → PARENT_REVIEWS_DRAFT
    try:
        advance_to_parent_reviews(family_repo, cycle_id)
    except IllegalTransitionError as exc:
        # Assessment is saved; state advance failure is non-fatal — log and continue.
        import logging

        logging.getLogger(__name__).warning(
            "generate_for_cycle: state advance to PARENT_REVIEWS_DRAFT failed: %s", exc
        )

    return result


@router.post(
    "/{cycle_id}/approve",
    response_model=CycleResponse,
    operation_id="approve_cycle_draft",
    summary="Parent approves the draft — advances to APPROVED_PRINTED.",
)
def approve_cycle(
    cycle_id: uuid.UUID,
    body: CycleApprove,
    _identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> CycleResponse:
    """PARENT_REVIEWS_DRAFT → APPROVED_PRINTED.

    Records ``parent_approval_at`` (now()) and optional ``note`` (golden rule 8).
    """
    try:
        return approve_draft(repo, cycle_id, body.note)
    except IllegalTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
