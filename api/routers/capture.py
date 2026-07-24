"""Child answer capture endpoints.

Two endpoints, variant-parameterized (``?variant=A|B``, default ``A``); the
variant selects the assessment/round but every guard below is generic,
phase-driven (docs/design/round-phase-architecture.md §3, §5, §7 P4) —
identical for every round (the old per-variant A/B fork is retired):

    GET  /cycles/{cycle_id}/capture
        Returns a memo-free ChildAssessmentView for the child to work through.
        Guard (per PHASE_CONFIG): the cycle must be at PRINTED. The cycle and
        its assessment are resolved via RLS in the caller's family.

    POST /cycles/{cycle_id}/submissions
        Accepts the child's responses. Advances the cycle PRINTED ->
        ANSWERS_ENTERED (table-driven, PHASE_CONFIG).
        Server-side guards (NEVER trusting any client mode flag):
          - Cycle resolves in the caller's family (RLS).
          - Cycle is at the variant's legal phase (PHASE_CONFIG).
          - The target round's marks are not already published (409).
          - Submission child_id matches the cycle's subject's child_id.
        Photo paths are stored for audit only; never fed to grading (§10).

Security note (kiosk hardening, supersedes the prior "accepted risk" note):
    Both endpoints now accept EITHER a parent ``Identity`` (unchanged
    behaviour) OR a scoped kiosk token presented via ``X-Child-Session``
    (``services/kiosk_session.py``, ``get_capture_or_results_caller``).  A
    kiosk token is verified cryptographically, independent of any stub
    header, and MUST satisfy ``scope == "capture"`` plus an exact match on
    both ``cycle_id`` and ``child_id`` (enforced on both the GET and the
    POST — advisor must-fix #4) or the request is 403.  Tenancy for a kiosk
    request resolves through the SAME ``family_members`` RLS join as a
    parent request, keyed on the token's owning parent ``user_id``
    (``dependencies.py``'s ``*_for_caller`` providers) — never a
    claim-keyed policy.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_assessment_repository_for_caller,
    get_family_repository_for_caller,
    get_submission_repository_for_caller,
)
from routers.families import _resolve_family_id
from schemas.capture import ChildAssessmentView, SubmissionCreate, SubmissionResponse
from schemas.family import CycleResponse
from schemas.identity import RequestCaller
from services.capture_service import project_for_child
from services.cycle import IllegalTransitionError
from services.kiosk_session import get_capture_or_results_caller
from services.phase import (
    PHASE_CONFIG,
    apply_advance,
    is_published,
    resolve_assessment,
    round_for_variant,
)
from services.repositories.base import (
    AssessmentRepository,
    FamilyRepository,
    SubmissionRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


def _resolve_cycle_child_id(cycle: CycleResponse, family_repo: FamilyRepository) -> uuid.UUID:
    """Resolve the cycle's child_id server-side via its subject. 409 if unresolvable."""
    subjects = family_repo.list_subjects(cycle.family_id)
    cycle_subject = next((s for s in subjects if s.id == cycle.subject_id), None)
    if cycle_subject is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Subject for this cycle could not be resolved.",
        )
    return cycle_subject.child_id


def _enforce_kiosk_scope(
    caller: RequestCaller,
    cycle_id: uuid.UUID,
    cycle_child_id: uuid.UUID,
    expected_scope: Literal["capture", "results"],
) -> None:
    """No-op for a parent caller. For a kiosk caller, enforce (advisor must-fix
    #4, applied on both GET and POST): the token's scope, cycle_id, and
    child_id all match the server-resolved values — 403 on any mismatch.
    """
    kiosk = caller.kiosk
    if kiosk is None:
        return
    if kiosk.scope != expected_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Kiosk token scope '{kiosk.scope}' does not permit this action.",
        )
    if kiosk.cycle_id != cycle_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kiosk token is not valid for this cycle.",
        )
    if kiosk.child_id != cycle_child_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kiosk token is not valid for this child.",
        )


@router.get(
    "/{cycle_id}/capture",
    response_model=ChildAssessmentView,
    operation_id="get_capture_view",
    summary=(
        "Return the memo-free child view of the approved assessment "
        "(variant defaults to A; every round requires PRINTED). "
        "Accepts a parent Identity or a scope=capture kiosk token."
    ),
)
def get_capture_view(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    caller: RequestCaller = Depends(get_capture_or_results_caller),
    family_repo: FamilyRepository = Depends(get_family_repository_for_caller),
    assessment_repo: AssessmentRepository = Depends(get_assessment_repository_for_caller),
) -> ChildAssessmentView:
    """Serve the requested variant's assessment to the child in kiosk mode.

    Guards:
    - Cycle exists in the caller's family (RLS enforced in repo layer; for a
      kiosk caller this is the token's owning parent's family).
    - A kiosk caller's token must match this cycle_id + child_id and carry
      scope="capture" (advisor must-fix #4) — 403 otherwise.
    - Cycle is in the variant's legal capture state (PHASE_CONFIG) — nothing
      is visible before parent approval (golden rule 8).
    - Assessment for this cycle + variant exists (generation must have
      completed).

    Returns a ``ChildAssessmentView`` that contains NO answer keys, memo text,
    method notes, accepted alternatives, or any other information that would
    reveal the answers.
    """
    _resolve_family_id(caller.identity, family_repo)  # ensures the caller has a family (RLS)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    cycle_child_id = _resolve_cycle_child_id(cycle, family_repo)
    _enforce_kiosk_scope(caller, cycle_id, cycle_child_id, "capture")

    if not PHASE_CONFIG.capture.is_legal(cycle.phase):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in phase '{cycle.phase.value}'; "
                f"Variant {variant} capture view is only available when the cycle is "
                f"{PHASE_CONFIG.capture.label()}."
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
        "Submit child answers. Advances PRINTED → ANSWERS_ENTERED (every round). "
        "Accepts a parent Identity or a scope=capture kiosk token."
    ),
)
def create_submission(
    cycle_id: uuid.UUID,
    body: SubmissionCreate,
    variant: Literal["A", "B"] = "A",
    caller: RequestCaller = Depends(get_capture_or_results_caller),
    family_repo: FamilyRepository = Depends(get_family_repository_for_caller),
    assessment_repo: AssessmentRepository = Depends(get_assessment_repository_for_caller),
    submission_repo: SubmissionRepository = Depends(get_submission_repository_for_caller),
) -> SubmissionResponse:
    """Accept the child's responses and persist the submission.

    Server-side guards (none of these trust any client flag):
    1. Cycle exists in the caller's family (RLS).
    2. A kiosk caller's token must match this cycle_id + child_id and carry
       scope="capture" (advisor must-fix #4) — 403 otherwise.
    3. The target round's marks are not already published (409) — universal
       write guard, belt-and-suspenders on top of the phase guard.
    4. Cycle is at the variant's legal submit phase (PHASE_CONFIG).
    5. body.child_id matches the cycle → subject → child_id chain.
    6. All qids in body.responses belong to the variant's assessment.

    On success:
    - Submission is persisted via SubmissionRepository.
    - Cycle advances PRINTED → ANSWERS_ENTERED via the cycle service
      (table-driven, PHASE_CONFIG — identical for every round).

    Grading is NOT triggered here (Phase 2).
    proof_photo_paths are stored as-is; NEVER fed to grading (§10).
    """
    _resolve_family_id(caller.identity, family_repo)  # ensures caller has a family (RLS)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    # Guard: child_id must match the cycle's subject's child_id.
    # We resolve this through the subjects list — the subject carrying the cycle
    # has a child_id field; the cycle carries subject_id.
    cycle_child_id = _resolve_cycle_child_id(cycle, family_repo)
    _enforce_kiosk_scope(caller, cycle_id, cycle_child_id, "capture")

    round_ = round_for_variant(variant)

    if is_published(family_repo, cycle_id, round_):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variant {variant} marks are published and immutable.",
        )

    if not PHASE_CONFIG.submit.is_legal(cycle.phase):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in phase '{cycle.phase.value}'; "
                f"Variant {variant} submissions are only accepted when the cycle is "
                f"{PHASE_CONFIG.submit.label()}."
            ),
        )

    if body.child_id != cycle_child_id:
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

    # Advance phase (table-driven, see PHASE_CONFIG — identical for every round).
    try:
        apply_advance(PHASE_CONFIG.submit, family_repo, cycle_id, cycle.phase)
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
