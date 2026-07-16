"""Grading endpoints, variant-parameterized (``?variant=A|B``, default ``A``).

    POST /cycles/{cycle_id}/grade
        Loads the submission + assessment, runs ``grade_submission``, upserts
        ``question_marks``. Variant A transitions ANSWERS_ENTERED → AUTO_MARKED
        (idempotent: re-running from AUTO_MARKED just regrades via upsert).
        Variant B never advances cycle state (legal only in GENERATING_B).
        operation_id: grade_submission_marks

    GET /cycles/{cycle_id}/marks
        Returns all marks for the cycle's submission (for the given variant)
        together with question context (text, type, mark_rules) the parent
        review screen needs.
        operation_id: list_question_marks

Security / invariants:
- family_id is NEVER accepted from the client; it comes from the cycle row
  (which is already RLS-scoped to the caller's family).
- Proof photos are NEVER accessed (ARCHITECTURE.md §10 no-vision-grading).
- Cycle state transitions go only through ``api/services/cycle.py``
  (ARCHITECTURE.md §5), driven by the table in ``services/phase.py``.
- Before grading, the target variant's marks must not already be published
  (409) — universal write guard, belt-and-suspenders on top of the state
  guard (for Variant A: ``marks_published_at is not None``; Variant B is
  never published in v1).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import (
    get_family_repository,
    get_question_mark_repository,
    get_submission_repository,
)
from routers.families import _resolve_family_id
from schemas.assessment_schema import Question
from schemas.capture import ChildResponseItem
from schemas.grading import (
    GradeSubmissionResponse,
    GradingSummary,
    ListMarksWithContextResponse,
    QuestionContext,
    QuestionMarkWithContext,
    render_child_answer,
    render_correct_answer,
)
from schemas.identity import Identity
from services.auth import get_identity
from services.cycle import IllegalTransitionError
from services.grading import FakeGrader, grade_submission
from services.phase import PHASE_CONFIG, apply_advance, resolve_assessment
from services.repositories.base import (
    FamilyRepository,
    QuestionMarkRepository,
    SubmissionRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


@router.post(
    "/{cycle_id}/grade",
    response_model=GradeSubmissionResponse,
    status_code=status.HTTP_200_OK,
    operation_id="grade_submission_marks",
    summary=(
        "Grade the cycle's submission for the given variant; upserts question_marks. "
        "Variant A advances ANSWERS_ENTERED → AUTO_MARKED; Variant B does not advance."
    ),
)
def grade_submission_endpoint(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
) -> GradeSubmissionResponse:
    """Grade the submission and upsert marks.

    Guards:
    1. Cycle exists in the caller's family (RLS).
    2. The target variant's marks are not already published (409).
    3. Cycle is in the variant's legal grade state (PHASE_CONFIG). For
       Variant A this is ANSWERS_ENTERED or AUTO_MARKED (idempotent re-grade).
    4. Submission exists for the cycle + variant.
    5. Assessment exists for the cycle + variant.

    Proof photos are NEVER read (ARCHITECTURE.md §10).
    """
    _resolve_family_id(identity, family_repo)

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

    if not config.grade.is_legal(cycle.state):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                f"grading Variant {variant} requires the cycle to be {config.grade.label()}."
            ),
        )

    family_id = cycle.family_id

    assessment = resolve_assessment(cycle, variant)
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Variant-{variant} assessment found for this cycle.",
        )

    # Resolve the submission for this cycle + variant via the marks repo JOIN query.
    # Returns None if no submission exists yet.
    submission_id_from_repo = marks_repo.get_submission_id_for_cycle(cycle_id, variant)

    if submission_id_from_repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No Variant-{variant} submission found for this cycle. "
                "Submit answers via POST /cycles/{cycle_id}/submissions first."
            ),
        )

    submission = submission_repo.get_submission(submission_id_from_repo)
    if submission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Submission not found.",
        )

    # Fetch full response payloads via duck-typed helper on the Postgres repo.
    # Falls back to empty list for InMemory (marks everything not-attempted).
    responses: list[ChildResponseItem] = _get_responses(submission_repo, submission_id_from_repo)

    # Run the grading engine.
    grader = FakeGrader()
    marks = grade_submission(
        assessment,
        responses,
        family_id=family_id,
        submission_id=submission_id_from_repo,
        grader=grader,
    )

    # Upsert marks.
    persisted = marks_repo.bulk_upsert(family_id, submission_id_from_repo, marks)

    # Advance cycle state (Variant A only, and only from ANSWERS_ENTERED —
    # idempotent re-grade from AUTO_MARKED does not re-advance).
    try:
        apply_advance(config.grade, family_repo, cycle_id, cycle.state)
    except IllegalTransitionError as exc:
        log.warning(
            "grade_submission_endpoint: state advance failed for cycle %s variant %s: %s",
            cycle_id,
            variant,
            exc,
        )

    # Build summary.
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
        submission_id=submission_id_from_repo,
        summary=summary,
        marks=persisted,
    )


@router.get(
    "/{cycle_id}/marks",
    response_model=ListMarksWithContextResponse,
    operation_id="list_question_marks",
    summary=(
        "List all question marks for the cycle + variant, with question context for parent review."
    ),
)
def list_question_marks(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    marks_repo: QuestionMarkRepository = Depends(get_question_mark_repository),
    submission_repo: SubmissionRepository = Depends(get_submission_repository),
) -> ListMarksWithContextResponse:
    """Return all marks for the cycle's submission (given variant).

    Includes question context (text, type, mark_rules) so the parent review
    screen (Phase 3) can render each mark without re-fetching the assessment.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    config = PHASE_CONFIG[variant]
    if not config.marks_get.is_legal(cycle.state):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                f"Variant {variant} marks require the cycle to be {config.marks_get.label()}."
            ),
        )

    # Resolve the submission_id for this cycle + variant.
    submission_id = marks_repo.get_submission_id_for_cycle(cycle_id, variant)
    if submission_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No graded Variant-{variant} submission found for this cycle.",
        )

    raw_marks = marks_repo.list_for_cycle(cycle_id, variant)

    # Fetch child responses for rendering child_answer_rendered.
    child_responses: list[ChildResponseItem] = _get_responses(submission_repo, submission_id)
    child_payload_map: dict[str, dict[str, object]] = {r.qid: r.payload for r in child_responses}

    # Build question context map from the assessment.
    assessment = resolve_assessment(cycle, variant)
    q_context: dict[str, QuestionContext] = {}
    if assessment is not None:
        for section in assessment.sections:
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

    return ListMarksWithContextResponse(
        cycle_id=cycle_id,
        submission_id=submission_id,
        items=items,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_context(q: Question, child_payload: dict[str, object] | None = None) -> QuestionContext:
    mr = q.mark_rules
    payload: dict[str, object] = child_payload or {}
    return QuestionContext(
        qid=q.qid,
        number=q.number,
        text=q.text,
        question_type=q.question_type.value,
        marks_total=Decimal(str(mr.total)),
        answer_marks=(Decimal(str(mr.answer_marks)) if mr.answer_marks is not None else None),
        method_marks=(Decimal(str(mr.method_marks)) if mr.method_marks is not None else None),
        child_answer_rendered=render_child_answer(q.question_type.value, payload),
        correct_answer_rendered=render_correct_answer(q.answer),
    )


def _get_responses(
    submission_repo: SubmissionRepository,
    submission_id: uuid.UUID,
) -> list[ChildResponseItem]:
    """Extract ChildResponseItem list from a submission.

    The current SubmissionResponse model only carries responses_count, not
    the full payload list.  The Postgres implementation of get_submission
    parses the JSONB internally but returns a trimmed SubmissionResponse.

    This helper uses duck-typing to call get_full_responses() if the repo
    exposes it (Postgres tier), falling back to an empty list otherwise.
    The Postgres repo exposes this via its own _get_responses helper.
    """
    # Duck-type: check if the Postgres repo exposes full response fetching.
    getter = getattr(submission_repo, "_get_responses_for_grading", None)
    if callable(getter):
        result: list[ChildResponseItem] = getter(submission_id)
        return result
    # InMemory: the repo stores SubmissionResponse, not the full payload.
    # Callers using InMemory should inject pre-built marks or use Postgres.
    return []
