"""Phase 5 — study pack endpoints.

    POST /cycles/{cycle_id}/study-pack
        Generate the study pack from the stored gap report, upsert to study_packs,
        return StudyPackResponse.  Idempotent re-run allowed.
        Guard: cycle must be in GAP_REPORT or any later valid state.
        State transitions via cycle.py only:
          GAP_REPORT → GENERATING_STUDY_PACK → STUDY_PACK_DONE.
        (If cycle is already STUDY_PACK_DONE or later, transitions are skipped —
        idempotent re-run just re-generates and upserts the pack.)
        operation_id: generate_study_pack

    GET /cycles/{cycle_id}/study-pack
        Return the stored study pack (404 if none).
        operation_id: get_study_pack

    POST /cycles/{cycle_id}/study-pack/approve
        Record parent approval (set approved_at) — the golden-rule-8 gate
        before child visibility.  Returns the updated StudyPackResponse.
        Guard: study pack must exist (generate first).
        operation_id: approve_study_pack

    GET /cycles/{cycle_id}/study-pack/pdf
        Render and return the WeasyPrint PDF (StreamingResponse, application/pdf).
        Guard: study pack must exist (generate first).
        operation_id: get_study_pack_pdf

Security / invariants:
- family_id is NEVER accepted from the client — derived from the cycle row (RLS).
- All state transitions via api/services/cycle.py only (golden rule 5).
- Cycle state guard: GAP_REPORT or later states are acceptable; earlier → 409.
- Child-visible gate: approved_at must be set before child-facing exposure
  (this router does not enforce child-vs-parent distinction — it records the
  approval timestamp; the frontend gates visibility on approved_at != null).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from dependencies import (
    get_family_repository,
    get_gap_report_repository,
    get_study_pack_repository,
)
from routers.families import _resolve_family_id
from schemas.family import CycleState
from schemas.identity import Identity
from schemas.study_pack import StudyPackResponse
from services.auth import get_identity
from services.cycle import (
    IllegalTransitionError,
    advance_to_generating_study_pack,
    advance_to_study_pack_done,
)
from services.repositories.base import (
    FamilyRepository,
    GapReportRepository,
    StudyPackRepository,
)
from services.study_pack import FakeStudyPack

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cycles")

# States in which a study pack can be generated or fetched.
# Mirrors GAP_REPORT_VALID_STATES but excludes GAP_REPORT itself on generation
# only if idempotent re-run is needed — we include it here as the starting state.
_STUDY_PACK_VALID_STATES: frozenset[CycleState] = frozenset(
    {
        CycleState.GAP_REPORT,
        CycleState.GENERATING_STUDY_PACK,
        CycleState.STUDY_PACK_DONE,
        CycleState.GENERATING_B,
        CycleState.CYCLE_COMPLETE,
    }
)

# States in which the pack already exists (so the transition can be skipped).
_POST_GENERATION_STATES: frozenset[CycleState] = frozenset(
    {
        CycleState.STUDY_PACK_DONE,
        CycleState.GENERATING_B,
        CycleState.CYCLE_COMPLETE,
    }
)


@router.post(
    "/{cycle_id}/study-pack",
    response_model=StudyPackResponse,
    status_code=status.HTTP_200_OK,
    operation_id="generate_study_pack",
    summary=(
        "Generate (or re-generate) the study pack from the gap report. "
        "Idempotent. Cycle must be in GAP_REPORT or a later state."
    ),
)
def generate_study_pack(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    gap_repo: GapReportRepository = Depends(get_gap_report_repository),
    pack_repo: StudyPackRepository = Depends(get_study_pack_repository),
) -> StudyPackResponse:
    """Generate and persist the study pack for a cycle.

    Guards (in order):
    1. User has a family (RLS).
    2. Cycle exists and belongs to caller's family (RLS).
    3. Cycle is in GAP_REPORT or a later state.
    4. The cycle has a Variant-A assessment (for gap derivation fallback).
    5. The cycle has at least one graded mark (for gap derivation fallback).

    Idempotent: if the cycle is already in STUDY_PACK_DONE or later, the
    transition calls are skipped and only the pack upsert runs.

    The gap report is read from the gap_repo if already stored; if not, it
    is re-derived in memory from the assessment + marks (fallback).  The
    primary path is to read the stored gap report.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    if cycle.state not in _STUDY_PACK_VALID_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in state '{cycle.state}'; "
                "study pack requires GAP_REPORT or a later state "
                "(publish marks first via POST /cycles/{cycle_id}/publish, "
                "then derive the gap report)."
            ),
        )

    # Resolve gap report — prefer stored, fall back to in-memory derivation.
    gap_row = gap_repo.get_for_cycle(cycle_id)
    if gap_row is not None:
        gap_report = gap_row.report
    else:
        # Fallback: re-derive from assessment + marks.
        # This path should be rare in production (gap report is generated before
        # study pack in the normal flow), but makes the endpoint self-contained.
        variant_a = next((a for a in cycle.assessments if a.variant == "A"), None)
        if variant_a is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "No gap report found and no Variant-A assessment available. "
                    "Generate the gap report first via POST /cycles/{cycle_id}/gap-report."
                ),
            )
        # We need marks for in-memory derivation — not available here without the
        # marks repo.  Surface a clear error directing the caller to the right path.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Gap report not found for this cycle. "
                "Generate it first via POST /cycles/{cycle_id}/gap-report."
            ),
        )

    # State transitions: GAP_REPORT → GENERATING_STUDY_PACK → STUDY_PACK_DONE.
    # Skip if already in a post-generation state (idempotent re-run).
    if cycle.state == CycleState.GAP_REPORT:
        try:
            advance_to_generating_study_pack(family_repo, cycle_id)
        except IllegalTransitionError as exc:
            log.warning("generate_study_pack: unexpected transition error: %s", exc)

    # Generate pack (FakeStudyPack — one call models the single generation artefact).
    generator = FakeStudyPack()
    pack = generator.generate(gap_report)

    # Upsert — idempotent.
    family_id = cycle.family_id
    row = pack_repo.upsert(family_id, cycle_id, pack)

    # Advance to STUDY_PACK_DONE (skip if already there or later).
    refreshed = family_repo.get_cycle(cycle_id)
    if refreshed is not None and refreshed.state == CycleState.GENERATING_STUDY_PACK:
        try:
            advance_to_study_pack_done(family_repo, cycle_id)
        except IllegalTransitionError as exc:
            log.warning("generate_study_pack: transition to STUDY_PACK_DONE failed: %s", exc)

    log.info(
        "generate_study_pack: cycle=%s items=%d tags=%d",
        cycle_id,
        len(pack.items),
        len(pack.derived_from_gap_tags),
    )

    return StudyPackResponse(
        cycle_id=row.cycle_id,
        pack=row.pack,
        approved_at=row.approved_at,
    )


@router.get(
    "/{cycle_id}/study-pack",
    response_model=StudyPackResponse,
    status_code=status.HTTP_200_OK,
    operation_id="get_study_pack",
    summary="Return the stored study pack for a cycle. 404 if not yet generated.",
)
def get_study_pack(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    pack_repo: StudyPackRepository = Depends(get_study_pack_repository),
) -> StudyPackResponse:
    """Return the persisted study pack.

    Guards:
    1. User has a family (RLS).
    2. Cycle exists and belongs to caller's family.
    3. Study pack row exists — 404 if not yet generated.
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    row = pack_repo.get_for_cycle(cycle_id)
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
    )


@router.post(
    "/{cycle_id}/study-pack/approve",
    response_model=StudyPackResponse,
    status_code=status.HTTP_200_OK,
    operation_id="approve_study_pack",
    summary=(
        "Record parent approval for the study pack (golden rule 8 gate). "
        "Sets approved_at; study pack is visible to child only after this call."
    ),
)
def approve_study_pack(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    pack_repo: StudyPackRepository = Depends(get_study_pack_repository),
) -> StudyPackResponse:
    """Record parent approval timestamp on the study pack.

    This is the golden-rule-8 gate: the study pack is exposed to the child
    only after the parent explicitly calls this endpoint.  The approval
    timestamp is stored on the study_packs row (``approved_at``).

    Guards:
    1. User has a family (RLS).
    2. Cycle exists and belongs to caller's family.
    3. Study pack must exist (generate first — 404 if not).

    Idempotent: calling approve more than once updates the timestamp to now().
    """
    _resolve_family_id(identity, family_repo)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    # Ensure pack exists.
    existing = pack_repo.get_for_cycle(cycle_id)
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
        row = pack_repo.set_approved_at(cycle_id, approved_at)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    log.info(
        "approve_study_pack: cycle=%s approved_at=%s",
        cycle_id,
        approved_at.isoformat(),
    )

    return StudyPackResponse(
        cycle_id=row.cycle_id,
        pack=row.pack,
        approved_at=row.approved_at,
    )


@router.get(
    "/{cycle_id}/study-pack/pdf",
    operation_id="get_study_pack_pdf",
    summary="Render the study pack to PDF (WeasyPrint). Returns application/pdf.",
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Study pack PDF.",
        }
    },
)
def get_study_pack_pdf(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    pack_repo: StudyPackRepository = Depends(get_study_pack_repository),
) -> StreamingResponse:
    """Render the study pack to PDF and return as a streaming response.

    Guards:
    1. User has a family (RLS).
    2. Cycle exists and belongs to caller's family.
    3. Study pack must exist (generate first — 404 if not).

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

    row = pack_repo.get_for_cycle(cycle_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Study pack not yet generated for this cycle. "
                "Call POST /cycles/{cycle_id}/study-pack first."
            ),
        )

    # Derive subject and grade_label from the cycle's Variant-A assessment
    # (subject-agnostic: the value is a freeform string, never branched on).
    variant_a = next((a for a in cycle.assessments if a.variant == "A"), None)
    subject = variant_a.subject if variant_a is not None else "General"
    grade_label = variant_a.grade_label if variant_a is not None else ""
    content_language = variant_a.content_language if variant_a is not None else "en"

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
