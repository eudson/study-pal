"""Study pack endpoints, round-parameterized (P4 of the generic (round, phase)
redesign — docs/design/round-phase-architecture.md §5, §7).

    POST /cycles/{cycle_id}/study-pack
        Generate the study pack from the stored gap report for the target
        variant/round, upsert to study_packs (keyed on (cycle_id, round)),
        return StudyPackResponse.  Idempotent re-run allowed.
        Guard: the target round's marks must already be published.
        Phase transitions via cycle.py only, and only when the round being
        generated is the cycle's CURRENT round (``cycle.round``) — a request
        for an earlier, already-settled round is a pure idempotent
        re-generate/upsert with no phase transition attempted:
          PUBLISHED → STUDY_PACK (generating) → STUDY_PACK (done).
        operation_id: generate_study_pack

    GET /cycles/{cycle_id}/study-pack
        Return the stored study pack for the variant/round (404 if none).
        operation_id: get_study_pack

    POST /cycles/{cycle_id}/study-pack/approve
        Record parent approval (set approved_at) for the variant/round — the
        golden-rule-8 gate before child visibility.  Returns the updated
        StudyPackResponse.
        Guard: study pack must exist for this round (generate first).
        operation_id: approve_study_pack

    GET /cycles/{cycle_id}/study-pack/pdf
        Render and return the WeasyPrint PDF (StreamingResponse, application/pdf)
        for the variant/round.
        Guard: study pack must exist for this round (generate first).
        operation_id: get_study_pack_pdf

``variant`` (default ``"A"`` == round 1) selects the round, mirroring the
sibling capture/grading/review/gap-report endpoints' ``?variant=A|B`` surface.

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- All state transitions via api/services/cycle.py only (golden rule 5).
- Guard: the target round's marks must be published — 409 if not.
- Child-visible gate: approved_at must be set before child-facing exposure
  (this router does not enforce child-vs-parent distinction — it records the
  approval timestamp; the frontend gates visibility on approved_at != null).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from dependencies import (
    get_family_repository,
    get_gap_report_repository,
    get_study_pack_repository,
)
from routers.families import _resolve_family_id
from schemas.family import CyclePhase, CycleState
from schemas.identity import Identity
from schemas.study_pack import StudyPackResponse
from services.auth import get_identity
from services.cycle import (
    IllegalTransitionError,
    advance_to_generating_study_pack,
    advance_to_study_pack_done,
)
from services.phase import is_published, resolve_assessment, round_for_variant
from services.repositories.base import (
    FamilyRepository,
    GapReportRepository,
    StudyPackRepository,
)
from services.study_pack import FakeStudyPack

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")


@router.post(
    "/{cycle_id}/study-pack",
    response_model=StudyPackResponse,
    status_code=status.HTTP_200_OK,
    operation_id="generate_study_pack",
    summary=(
        "Generate (or re-generate) the study pack from the gap report, for "
        "the given variant/round (default A / round 1). Idempotent. The "
        "target round's marks must already be published."
    ),
)
def generate_study_pack(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
    pack_repo: StudyPackRepository = Depends(get_study_pack_repository),
) -> StudyPackResponse:
    """Generate and persist the study pack for a cycle + round.

    Guards (in order):
    1. User has a family (RLS).
    2. Cycle exists and belongs to caller's family (RLS).
    3. The target round's marks are published (per-round, ``cycle_round_approvals``).
    4. The cycle has an assessment for this variant/round.
    5. A stored gap report exists for this round (generate it first if not).

    Idempotent: if the pack already exists, this just re-generates and upserts it.

    Phase transitions (``PUBLISHED → STUDY_PACK`` generating/done) are only
    attempted when the requested round is the cycle's CURRENT round — a
    request for an earlier, already-settled round only re-derives + upserts
    the pack, without touching the cycle's (unrelated, later) current phase.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    round_ = round_for_variant(variant)

    if not is_published(family_repo, cycle_id, round_):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Variant {variant} marks are not yet published; "
                "study pack requires marks to be published first "
                "(publish marks via POST /cycles/{cycle_id}/publish, "
                "then derive the gap report)."
            ),
        )

    # Resolve gap report — must already be stored for this round (the normal
    # flow always generates the gap report before the study pack).
    gap_row = gap_repo.get_for_cycle(cycle_id, round=round_)
    if gap_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Gap report not found for this cycle/round. "
                "Generate it first via POST /cycles/{cycle_id}/gap-report."
            ),
        )
    gap_report = gap_row.report

    # Phase transitions only apply when this round is the cycle's CURRENT
    # round — an idempotent re-run against an earlier, already-settled round
    # must not touch the cycle's (unrelated, later) phase.
    is_current_round = round_ == cycle.round
    if is_current_round and cycle.phase is CyclePhase.PUBLISHED:
        try:
            advance_to_generating_study_pack(family_repo, cycle_id)
        except IllegalTransitionError as exc:
            log.warning("generate_study_pack: unexpected transition error: %s", exc)

    # Generate pack (FakeStudyPack — one call models the single generation artefact).
    generator = FakeStudyPack()
    pack = generator.generate(gap_report)

    # Upsert — idempotent, keyed on (cycle_id, round).
    family_id = cycle.family_id
    row = pack_repo.upsert(family_id, cycle_id, pack, round=round_)

    # Advance to STUDY_PACK done (skip if already there, later, or this round
    # is not the cycle's current round).
    refreshed = family_repo.get_cycle(cycle_id)
    if (
        is_current_round
        and refreshed is not None
        and refreshed.phase is CyclePhase.STUDY_PACK
        and refreshed.state is CycleState.GENERATING_STUDY_PACK
    ):
        try:
            advance_to_study_pack_done(family_repo, cycle_id)
        except IllegalTransitionError as exc:
            log.warning("generate_study_pack: transition to STUDY_PACK_DONE failed: %s", exc)

    log.info(
        "generate_study_pack: cycle=%s round=%d items=%d tags=%d",
        cycle_id,
        round_,
        len(pack.items),
        len(pack.derived_from_gap_tags),
    )

    return StudyPackResponse(
        cycle_id=row.cycle_id,
        pack=row.pack,
        approved_at=row.approved_at,
        round=row.round,
    )


@router.get(
    "/{cycle_id}/study-pack",
    response_model=StudyPackResponse,
    status_code=status.HTTP_200_OK,
    operation_id="get_study_pack",
    summary=(
        "Return the stored study pack for a cycle + variant/round "
        "(default A / round 1). 404 if not yet generated."
    ),
)
def get_study_pack(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    pack_repo: StudyPackRepository = Depends(get_study_pack_repository),
) -> StudyPackResponse:
    """Return the persisted study pack for a cycle + round.

    Guards:
    1. User has a family (RLS).
    2. Cycle exists and belongs to caller's family.
    3. Study pack row exists for this round — 404 if not yet generated.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    round_ = round_for_variant(variant)

    row = pack_repo.get_for_cycle(cycle_id, round=round_)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Study pack not yet generated for this cycle. "
                "Call POST /cycles/{cycle_id}/study-pack first."
            ),
        )

    return StudyPackResponse(
        cycle_id=row.cycle_id,
        pack=row.pack,
        approved_at=row.approved_at,
        round=row.round,
    )


@router.post(
    "/{cycle_id}/study-pack/approve",
    response_model=StudyPackResponse,
    status_code=status.HTTP_200_OK,
    operation_id="approve_study_pack",
    summary=(
        "Record parent approval for the study pack (golden rule 8 gate), for "
        "the given variant/round (default A / round 1). Sets approved_at; "
        "study pack is visible to child only after this call."
    ),
)
def approve_study_pack(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    pack_repo: StudyPackRepository = Depends(get_study_pack_repository),
) -> StudyPackResponse:
    """Record parent approval timestamp on the study pack for a round.

    This is the golden-rule-8 gate: the study pack is exposed to the child
    only after the parent explicitly calls this endpoint.  The approval
    timestamp is stored on the study_packs row (``approved_at``).

    Guards:
    1. User has a family (RLS).
    2. Cycle exists and belongs to caller's family.
    3. Study pack must exist for this round (generate first — 404 if not).

    Idempotent: calling approve more than once updates the timestamp to now().
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    round_ = round_for_variant(variant)

    # Ensure pack exists.
    existing = pack_repo.get_for_cycle(cycle_id, round=round_)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Study pack not yet generated for this cycle. "
                "Call POST /cycles/{cycle_id}/study-pack first."
            ),
        )

    approved_at = datetime.now(tz=UTC)
    try:
        row = pack_repo.set_approved_at(cycle_id, approved_at, round=round_)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    log.info(
        "approve_study_pack: cycle=%s round=%d approved_at=%s",
        cycle_id,
        round_,
        approved_at.isoformat(),
    )

    return StudyPackResponse(
        cycle_id=row.cycle_id,
        pack=row.pack,
        approved_at=row.approved_at,
        round=row.round,
    )


@router.get(
    "/{cycle_id}/study-pack/pdf",
    operation_id="get_study_pack_pdf",
    summary=(
        "Render the study pack to PDF (WeasyPrint) for the variant/round "
        "(default A / round 1). Returns application/pdf."
    ),
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Study pack PDF.",
        }
    },
)
def get_study_pack_pdf(
    cycle_id: uuid.UUID,
    variant: Literal["A", "B"] = "A",
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    pack_repo: StudyPackRepository = Depends(get_study_pack_repository),
) -> StreamingResponse:
    """Render the study pack to PDF and return as a streaming response.

    Guards:
    1. User has a family (RLS).
    2. Cycle exists and belongs to caller's family.
    3. Study pack must exist for this round (generate first — 404 if not).

    The PDF is rendered on every request (no caching) — consistent with the
    assessment PDF pattern.  Content language from the assessment is used when
    available.
    """
    from services.pdf_service import render_study_pack_pdf

    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    round_ = round_for_variant(variant)

    row = pack_repo.get_for_cycle(cycle_id, round=round_)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Study pack not yet generated for this cycle. "
                "Call POST /cycles/{cycle_id}/study-pack first."
            ),
        )

    # Derive subject and grade_label from the cycle's target-variant assessment
    # (subject-agnostic: the value is a freeform string, never branched on).
    assessment = resolve_assessment(cycle, variant)
    subject = assessment.subject if assessment is not None else "General"
    grade_label = assessment.grade_label if assessment is not None else ""
    content_language = assessment.content_language if assessment is not None else "en"

    pdf_bytes = render_study_pack_pdf(
        row.pack,
        subject=subject,
        grade_label=grade_label,
        content_language=content_language,
    )

    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="study-pack-{cycle_id}.pdf"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )
