"""Week 6 — Variant B retest + A/B comparison endpoints.

Variant B is SAME-CYCLE (ARCHITECTURE.md §5): the cycle state machine is
unchanged — ``STUDY_PACK_DONE -> GENERATING_B -> CYCLE_COMPLETE`` — and every
handler here hard-targets ``variant="B"`` on the relevant repo calls so
Variant A's stored data (submission, marks, gap report) is never touched.

    POST /cycles/{cycle_id}/variant-b
        Generate the Variant B assessment from the stored Variant-A assessment
        + the stored (or in-memory derived) gap report's growing gaps.
        Guard: STUDY_PACK_DONE (idempotent: returns the existing B assessment
        if already GENERATING_B and one exists).
        Advances STUDY_PACK_DONE -> GENERATING_B via cycle.py.
        operation_id: generateVariantB

    GET /cycles/{cycle_id}/variant-b/capture
        Memo-free child view of the Variant-B assessment (reuses
        ``project_for_child`` — same projection as Variant A).
        Guard: GENERATING_B.
        operation_id: getVariantBCaptureView

    POST /cycles/{cycle_id}/variant-b/submissions
        Accept the child's Variant-B responses. Does NOT advance cycle state
        (cycle stays GENERATING_B; the B phase is inferred from data
        presence, not a state enum — ARCHITECTURE.md §5 has no new states).
        Guard: GENERATING_B.
        operation_id: createVariantBSubmission

    POST /cycles/{cycle_id}/variant-b/grade
        Grade the Variant-B submission; upserts question_marks scoped to the
        B submission. Does NOT advance cycle state.
        Guard: GENERATING_B.
        operation_id: gradeVariantB

    GET /cycles/{cycle_id}/variant-b/marks
        List Variant-B marks with question context (parent review screen).
        Guard: GENERATING_B.
        operation_id: getVariantBMarks

    PATCH /cycles/{cycle_id}/variant-b/marks/{question_id}
        Parent override of a single Variant-B question mark.
        Guard: GENERATING_B.
        operation_id: reviewVariantBMark

    GET /cycles/{cycle_id}/comparison
        Derive the A-vs-B comparison (pure, in-memory — never persisted).
        Guard: GENERATING_B or CYCLE_COMPLETE. Requires Variant B to be fully
        marked (409 otherwise).
        operation_id: getAbComparison

    POST /cycles/{cycle_id}/complete
        Terminal transition GENERATING_B -> CYCLE_COMPLETE. Requires the
        comparison to be derivable (Variant B fully marked) — 409 otherwise.
        operation_id: completeCycle

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- All state transitions go only through api/services/cycle.py (ARCHITECTURE.md §5).
- Every marks-repo call here hard-targets variant="B" (or "A" for the source
  side of the comparison) — never inferred by recency (Week 6 guardrail).
- No new cycle states are introduced; the B capture->mark->review sub-flow is
  inferred entirely from data presence while state stays GENERATING_B.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_assessment_repository,
    get_family_repository,
    get_gap_report_repository,
    get_question_mark_repository,
    get_submission_repository,
)
from routers.families import _resolve_family_id

# Re-use the parent-only rendering helpers (grading.py's private helpers are
# duplicated here in miniature via the shared QuestionContext builder below).
from routers.grading import _build_context, _get_responses
from schemas.assessment_schema import Assessment, VariantBRequest
from schemas.capture import (
    ChildAssessmentView,
    ChildResponseItem,
    SubmissionCreate,
    SubmissionResponse,
)
from schemas.comparison import ABComparison
from schemas.family import CycleResponse, CycleState
from schemas.gap_report import GapReport
from schemas.grading import (
    GradeSubmissionResponse,
    GradingSummary,
    ListMarksWithContextResponse,
    QuestionContext,
    QuestionMarkWithContext,
)
from schemas.identity import Identity
from schemas.review import MarkPatchRequest, MarkPatchResponse
from services.auth import get_identity
from services.capture_service import project_for_child
from services.claude_client import FakeClaude
from services.comparison import derive_ab_comparison
from services.cycle import (
    IllegalTransitionError,
    advance_to_cycle_complete,
    advance_to_generating_b,
)
from services.gap_report import build_gap_retargets, derive_gap_report
from services.generation_service import GenerationService
from services.grading import FakeGrader, grade_submission
from services.repositories.base import (
    AssessmentRepository,
    FamilyRepository,
    GapReportRepository,
    QuestionMarkRepository,
    SubmissionRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


def _variant(cycle: CycleResponse, variant: str) -> Assessment | None:
    return next((a for a in cycle.assessments if a.variant == variant), None)


# ---------------------------------------------------------------------------
# POST /cycles/{cycle_id}/variant-b
# ---------------------------------------------------------------------------


@router.post(
    "/{cycle_id}/variant-b",
    response_model=Assessment,
    status_code=status.HTTP_201_CREATED,
    operation_id="generateVariantB",
    summary=(
        "Generate the Variant B retest from the stored Variant A assessment "
        "+ gap report. Advances STUDY_PACK_DONE -> GENERATING_B."
    ),
)
def generate_variant_b(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    assessment_repo: AssessmentRepository = Depends(get_assessment_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
) -> Assessment:
    """Generate (or return the already-generated) Variant B assessment.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. Cycle is STUDY_PACK_DONE, or GENERATING_B with no B assessment yet
       (retry path) — 409 otherwise.
    3. Idempotent: if GENERATING_B and a B assessment already exists, return
       it without regenerating.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    existing_b = _variant(cycle, "B")
    if cycle.state == CycleState.GENERATING_B and existing_b is not None:
        return existing_b

    if cycle.state not in (CycleState.STUDY_PACK_DONE, CycleState.GENERATING_B):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; Variant B generation requires STUDY_PACK_DONE."
            ),
        )

    variant_a = _variant(cycle, "A")
    if variant_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Variant-A assessment found for this cycle.",
        )

    # Resolve the gap report — stored preferred, derive in-memory otherwise
    # (no persist, no state transition — mirrors child_results.py's fallback).
    gap_row = gap_repo.get_for_cycle(cycle_id)
    if gap_row is not None:
        report: GapReport = gap_row.report
    else:
        marks = marks_repo.list_for_cycle(cycle_id, "A")
        if not marks:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No graded Variant-A marks found; cannot derive gap report.",
            )
        try:
            report = derive_gap_report(variant_a, marks)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Gap report derivation failed: {exc}",
            ) from exc

    gaps = build_gap_retargets(report)
    request = VariantBRequest(source_assessment=variant_a, gaps=gaps)

    # Advance STUDY_PACK_DONE -> GENERATING_B (skip if already there — retry path).
    if cycle.state == CycleState.STUDY_PACK_DONE:
        try:
            advance_to_generating_b(family_repo, cycle_id)
        except IllegalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    service = GenerationService(claude=FakeClaude())
    result = service.generate_variant_b(request, assessment_id=str(uuid.uuid4()))

    if not result.ok or result.assessment is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": result.error,
                "issues": [issue.model_dump() for issue in result.issues],
            },
        )

    saved = assessment_repo.save(result.assessment)
    log.info("generate_variant_b: cycle=%s assessment_id=%s", cycle_id, saved.assessment_id)
    return saved


# ---------------------------------------------------------------------------
# GET /cycles/{cycle_id}/variant-b/capture
# ---------------------------------------------------------------------------


@router.get(
    "/{cycle_id}/variant-b/capture",
    response_model=ChildAssessmentView,
    operation_id="getVariantBCaptureView",
    summary=(
        "Return the memo-free child view of the Variant B assessment (cycle must be GENERATING_B)."
    ),
)
def get_variant_b_capture_view(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
) -> ChildAssessmentView:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.state != CycleState.GENERATING_B:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                "Variant B capture view is only available when GENERATING_B."
            ),
        )

    variant_b = _variant(cycle, "B")
    if variant_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Variant-B assessment found for this cycle.",
        )

    return project_for_child(variant_b)


# ---------------------------------------------------------------------------
# POST /cycles/{cycle_id}/variant-b/submissions
# ---------------------------------------------------------------------------


@router.post(
    "/{cycle_id}/variant-b/submissions",
    response_model=SubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createVariantBSubmission",
    summary="Submit child answers for Variant B. Cycle state is NOT advanced.",
)
def create_variant_b_submission(
    cycle_id: uuid.UUID,
    body: SubmissionCreate,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
) -> SubmissionResponse:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.state != CycleState.GENERATING_B:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                "Variant B submissions are only accepted when GENERATING_B."
            ),
        )

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

    variant_b = _variant(cycle, "B")
    if variant_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Variant-B assessment found for this cycle.",
        )

    valid_qids: set[str] = {q.qid for section in variant_b.sections for q in section.questions}
    unknown_qids = [r.qid for r in body.responses if r.qid not in valid_qids]
    if unknown_qids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown question ids in responses: {unknown_qids}",
        )

    # Persist. Cycle state is NOT advanced (B phase inferred from data presence).
    return submission_repo.create_submission(
        family_id=cycle.family_id,
        assessment_id=variant_b.assessment_id,
        payload=body,
        cycle_id=cycle_id,
    )


# ---------------------------------------------------------------------------
# POST /cycles/{cycle_id}/variant-b/grade
# ---------------------------------------------------------------------------


@router.post(
    "/{cycle_id}/variant-b/grade",
    response_model=GradeSubmissionResponse,
    status_code=status.HTTP_200_OK,
    operation_id="gradeVariantB",
    summary="Grade the Variant B submission; upserts marks. Cycle state is NOT advanced.",
)
def grade_variant_b(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
) -> GradeSubmissionResponse:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.state != CycleState.GENERATING_B:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cycle is in state '{cycle.state}'; grading Variant B requires GENERATING_B.",
        )

    variant_b = _variant(cycle, "B")
    if variant_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Variant-B assessment found for this cycle.",
        )

    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, "B")
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No Variant-B submission found for this cycle. "
                "Submit answers via POST /cycles/{cycle_id}/variant-b/submissions first."
            ),
        )

    submission = submission_repo.get_submission(submission_id)
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found.")

    responses: list[ChildResponseItem] = _get_responses(submission_repo, submission_id)

    grader = FakeGrader()
    marks = grade_submission(
        variant_b,
        responses,
        family_id=cycle.family_id,
        submission_id=submission_id,
        grader=grader,
    )

    persisted = marks_repo.bulk_upsert(cycle.family_id, submission_id, marks)

    auto_marked = sum(1 for m in persisted if not m.needs_review)
    needs_review = sum(1 for m in persisted if m.needs_review)
    not_attempted = sum(
        1
        for m in persisted
        if m.error_category is not None and m.error_category.value == "not_attempted"
    )
    summary = GradingSummary(
        total_questions=len(persisted),
        auto_marked=auto_marked,
        needs_review=needs_review,
        not_attempted=not_attempted,
    )

    return GradeSubmissionResponse(
        cycle_id=cycle_id,
        submission_id=submission_id,
        summary=summary,
        marks=persisted,
    )


# ---------------------------------------------------------------------------
# GET /cycles/{cycle_id}/variant-b/marks
# ---------------------------------------------------------------------------


@router.get(
    "/{cycle_id}/variant-b/marks",
    response_model=ListMarksWithContextResponse,
    operation_id="getVariantBMarks",
    summary="List Variant B marks with question context (parent review screen).",
)
def get_variant_b_marks(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
) -> ListMarksWithContextResponse:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.state != CycleState.GENERATING_B:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cycle is in state '{cycle.state}'; Variant B marks require GENERATING_B.",
        )

    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, "B")
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded Variant-B submission found for this cycle.",
        )

    raw_marks = marks_repo.list_for_cycle(cycle_id, "B")

    child_responses: list[ChildResponseItem] = _get_responses(submission_repo, submission_id)
    child_payload_map: dict[str, dict[str, object]] = {r.qid: r.payload for r in child_responses}

    variant_b = _variant(cycle, "B")
    q_context: dict[str, QuestionContext] = {}
    if variant_b is not None:
        for section in variant_b.sections:
            for q in section.questions:
                payload = child_payload_map.get(q.qid, {})
                q_context[q.qid] = _build_context(q, payload)

    items = [
        QuestionMarkWithContext(
            mark=m,
            question=q_context.get(
                m.question_id,
                QuestionContext(
                    qid=m.question_id,
                    number="?",
                    text="(question not found)",
                    question_type="unknown",
                    marks_total=m.marks_total,
                ),
            ),
        )
        for m in raw_marks
    ]

    return ListMarksWithContextResponse(cycle_id=cycle_id, submission_id=submission_id, items=items)


# ---------------------------------------------------------------------------
# PATCH /cycles/{cycle_id}/variant-b/marks/{question_id}
# ---------------------------------------------------------------------------


@router.patch(
    "/{cycle_id}/variant-b/marks/{question_id}",
    response_model=MarkPatchResponse,
    status_code=status.HTTP_200_OK,
    operation_id="reviewVariantBMark",
    summary="Parent override of a single Variant B question mark.",
)
def review_variant_b_mark(
    cycle_id: uuid.UUID,
    question_id: str,
    body: MarkPatchRequest,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
) -> MarkPatchResponse:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.state != CycleState.GENERATING_B:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; Variant B mark review requires GENERATING_B."
            ),
        )

    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, "B")
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No graded Variant-B submission found for this cycle.",
        )

    existing_mark = marks_repo.get_mark(submission_id, question_id)
    if existing_mark is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Question mark '{question_id}' not found for this cycle's Variant B.",
        )

    if body.final_marks is not None and body.final_marks > existing_mark.marks_total:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"final_marks {body.final_marks} exceeds marks_total "
                f"{existing_mark.marks_total} for question '{question_id}'."
            ),
        )

    now = datetime.now(tz=UTC)
    updated_mark = marks_repo.update_mark(submission_id, question_id, body, now)
    return MarkPatchResponse(mark=updated_mark)


# ---------------------------------------------------------------------------
# GET /cycles/{cycle_id}/comparison
# ---------------------------------------------------------------------------


@router.get(
    "/{cycle_id}/comparison",
    response_model=ABComparison,
    status_code=status.HTTP_200_OK,
    operation_id="getAbComparison",
    summary="Derive the A-vs-B gap comparison. Requires Variant B to be fully marked.",
)
def get_ab_comparison(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
) -> ABComparison:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.state not in (CycleState.GENERATING_B, CycleState.CYCLE_COMPLETE):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                "comparison requires GENERATING_B or CYCLE_COMPLETE."
            ),
        )

    gap_a = _resolve_gap_report(cycle, "A", gap_repo, marks_repo)
    gap_b = _resolve_gap_report(cycle, "B", gap_repo, marks_repo, require_full=True)

    return derive_ab_comparison(gap_a, gap_b)


# ---------------------------------------------------------------------------
# POST /cycles/{cycle_id}/complete
# ---------------------------------------------------------------------------


@router.post(
    "/{cycle_id}/complete",
    response_model=CycleResponse,
    status_code=status.HTTP_200_OK,
    operation_id="completeCycle",
    summary="Terminal transition GENERATING_B -> CYCLE_COMPLETE. Requires Variant B fully marked.",
)
def complete_cycle(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
) -> CycleResponse:
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found.")

    if cycle.state != CycleState.GENERATING_B:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cycle is in state '{cycle.state}'; complete requires GENERATING_B.",
        )

    # Require the comparison to be derivable — i.e. Variant B fully marked.
    _resolve_gap_report(cycle, "A", gap_repo, marks_repo)
    _resolve_gap_report(cycle, "B", gap_repo, marks_repo, require_full=True)

    try:
        return advance_to_cycle_complete(family_repo, cycle_id)
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_gap_report(
    cycle: CycleResponse,
    variant: str,
    gap_repo: GapReportRepository,
    marks_repo: QuestionMarkRepository,
    *,
    require_full: bool = False,
) -> GapReport:
    """Resolve a variant's gap report for comparison purposes.

    Variant A: prefers the stored gap report; derives in-memory otherwise.
    Variant B: ALWAYS derives in-memory from the B assessment + B marks (the
    B gap report is never persisted — locked spec). ``require_full=True``
    raises 409 unless every Variant-B mark has final_marks set.
    """
    assessment = _variant(cycle, variant)
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Variant-{variant} assessment found for this cycle.",
        )

    if variant == "A":
        gap_row = gap_repo.get_for_cycle(cycle.id)
        if gap_row is not None:
            return gap_row.report

    marks = marks_repo.list_for_cycle(cycle.id, variant)
    if not marks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No graded Variant-{variant} marks found for this cycle.",
        )

    if require_full:
        unresolved = [m.question_id for m in marks if m.final_marks is None]
        if unresolved:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Variant {variant} is not fully marked; "
                    f"{len(unresolved)} question(s) still have final_marks=NULL: {unresolved}."
                ),
            )

    try:
        return derive_gap_report(assessment, marks)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gap report derivation failed for Variant {variant}: {exc}",
        ) from exc
