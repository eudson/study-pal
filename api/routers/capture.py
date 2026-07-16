"""Child answer capture endpoints.

Two endpoints, variant-parameterized (``?variant=A|B``, default ``A``):

    GET  /cycles/{cycle_id}/capture
        Returns a memo-free ChildAssessmentView for the child to work through.
        Guard (per PHASE_CONFIG): Variant A requires APPROVED_PRINTED; Variant
        B requires GENERATING_B.  The cycle and its assessment are resolved
        via RLS in the caller's family.

    POST /cycles/{cycle_id}/submissions
        Accepts the child's responses. For Variant A, advances the cycle to
        ANSWERS_ENTERED. Variant B does NOT advance cycle state (the B phase
        is inferred from data presence, not a new state).
        Server-side guards (NEVER trusting any client mode flag):
          - Cycle resolves in the caller's family (RLS).
          - Cycle is in the variant's legal state (PHASE_CONFIG).
          - The target variant's marks are not already published (409).
          - Submission child_id matches the cycle's subject's child_id.
        Photo paths are stored for audit only; never fed to grading (§10).

Security note (recorded for PROGRESS log):
    The kiosk "child mode" runs under the parent's authenticated session.
    Authorization is therefore the existing family RLS — there is no
    separate child auth token.  This means a parent user could, in theory,
    call these endpoints themselves.  The content-safety boundary is enforced
    by these server-side guards (state check, child_id match) — NOT by a
    client mode flag.  Future work (Phase 3+) may introduce a limited
    child session token if the UX requires it; for MVP the kiosk session
    is the accepted risk (architect decision A).
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_assessment_repository,
    get_family_repository,
    get_submission_repository,
)
from routers.families import _resolve_family_id
from schemas.capture import ChildAssessmentView, SubmissionCreate, SubmissionResponse
from schemas.identity import Identity
from services.auth import get_identity
from services.capture_service import project_for_child
from services.cycle import IllegalTransitionError
from services.phase import PHASE_CONFIG, apply_advance, resolve_assessment
from services.repositories.base import (
    AssessmentRepository,
    FamilyRepository,
    SubmissionRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


@router.get(
    "/{cycle_id}/capture",
    response_model=ChildAssessmentView,
    operation_id="get_capture_view",
    summary=(
        "Return the memo-free child view of the approved assessment "
        "(variant defaults to A; A requires APPROVED_PRINTED, B requires GENERATING_B)."
    ),
)
def get_capture_view(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    assessment_repo: AssessmentRepository = Depends(get_assessment_repository),
) -> ChildAssessmentView:
    """Serve the requested variant's assessment to the child in kiosk mode.

    Guards:
    - Cycle exists in the caller's family (RLS enforced in repo layer).
    - Cycle is in the variant's legal capture state (PHASE_CONFIG) — nothing
      is visible before parent approval (golden rule 8).
    - Assessment for this cycle + variant exists (generation must have
      completed).

    Returns a ``ChildAssessmentView`` that contains NO answer keys, memo text,
    method notes, accepted alternatives, or any other information that would
    reveal the answers.
    """
    _resolve_family_id(identity, family_repo)  # ensures the caller has a family (RLS)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    config = PHASE_CONFIG[variant]
    if not config.capture.is_legal(cycle.state):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                f"Variant {variant} capture view is only available when the cycle is "
                f"{config.capture.label()}."
            ),
        )

    assessment = resolve_assessment(cycle, variant)
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Variant-{variant} assessment found for this cycle.",
        )

    return project_for_child(assessment)


@router.post(
    "/{cycle_id}/submissions",
    response_model=SubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_submission",
    summary=(
        "Submit child answers. Variant A advances APPROVED_PRINTED → ANSWERS_ENTERED; "
        "Variant B does not advance cycle state."
    ),
)
def create_submission(
    cycle_id: uuid.UUID,
    body: SubmissionCreate,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    assessment_repo: AssessmentRepository = Depends(get_assessment_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
) -> SubmissionResponse:
    """Accept the child's responses and persist the submission.

    Server-side guards (none of these trust any client flag):
    1. Cycle exists in the caller's family (RLS).
    2. The target variant's marks are not already published (409) — universal
       write guard, belt-and-suspenders on top of the state guard.
    3. Cycle is in the variant's legal submit state (PHASE_CONFIG).
    4. body.child_id matches the cycle → subject → child_id chain.
    5. All qids in body.responses belong to the variant's assessment.

    On success:
    - Submission is persisted via SubmissionRepository.
    - Variant A: cycle advances APPROVED_PRINTED → ANSWERS_ENTERED via cycle
      service. Variant B: no state advance (inferred from data presence).

    Grading is NOT triggered here (Phase 2).
    proof_photo_paths are stored as-is; NEVER fed to grading (§10).
    """
    _resolve_family_id(identity, family_repo)  # ensures caller has a family (RLS)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    config = PHASE_CONFIG[variant]

    if config.is_published(cycle):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variant {variant} marks are published and immutable.",
        )

    if not config.submit.is_legal(cycle.state):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                f"Variant {variant} submissions are only accepted when the cycle is "
                f"{config.submit.label()}."
            ),
        )

    # Guard: child_id must match the cycle's subject's child_id.
    # We resolve this through the subjects list — the subject carrying the cycle
    # has a child_id field; the cycle carries subject_id.
    subjects = family_repo.list_subjects(cycle.family_id)
    cycle_subject = next((s for s in subjects if s.id == cycle.subject_id), None)
    if cycle_subject is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Subject for this cycle could not be resolved.",
        )

    if body.child_id != cycle_subject.child_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="child_id does not match the child associated with this cycle.",
        )

    # Guard: qids must belong to the variant's assessment.
    assessment = resolve_assessment(cycle, variant)
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Variant-{variant} assessment found for this cycle.",
        )

    valid_qids: set[str] = {q.qid for section in assessment.sections for q in section.questions}
    unknown_qids = [r.qid for r in body.responses if r.qid not in valid_qids]
    if unknown_qids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown question ids in responses: {unknown_qids}",
        )

    # Persist the submission.
    submission = submission_repo.create_submission(
        family_id=cycle.family_id,
        assessment_id=assessment.assessment_id,
        payload=body,
        cycle_id=cycle_id,
    )

    # Advance state (Variant A only — table-driven, see PHASE_CONFIG).
    try:
        apply_advance(config.submit, family_repo, cycle_id, cycle.state)
    except IllegalTransitionError as exc:
        # Submission is already persisted; state advance failure is non-fatal.
        # Log and continue — the submission is the authoritative record.
        log.warning(
            "create_submission: state advance failed for cycle %s variant %s: %s",
            cycle_id,
            variant,
            exc,
        )

    return submission
