"""Routers for families, children, and subjects.

All endpoints are RLS-scoped via ``get_identity`` + ``get_family_repository``.
family_id is NEVER accepted from the client — derived server-side (invariant 3).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import get_family_repository
from schemas.family import (
    BootstrapResponse,
    ChildCreate,
    ChildResponse,
    ChildUpdate,
    FamilyCreate,
    FamilyResponse,
    SubjectCreate,
    SubjectResponse,
)
from schemas.identity import Identity
from services.auth import get_identity
from services.repositories.base import FamilyRepository

router = APIRouter()


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------


@router.post(
    "/families",
    response_model=BootstrapResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="bootstrap_family",
    summary="Bootstrap: create a family + membership (+ optional first child).",
)
def bootstrap_family(
    body: FamilyCreate,
    _identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> BootstrapResponse:
    """Create the caller's family in a single atomic DB call via the
    SECURITY DEFINER function (0003_bootstrap.sql).

    Idempotent: if the caller already has a family, returns it unchanged.
    ``child_name`` and ``grade_label`` must both be provided or both omitted.
    """
    if (body.child_name is None) != (body.grade_label is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="child_name and grade_label must both be provided or both omitted.",
        )

    family, child_id = repo.bootstrap_family(
        body.family_name,
        body.child_name,
        body.grade_label,
    )
    return BootstrapResponse(family=family, child_id=child_id)


@router.get(
    "/families",
    response_model=list[FamilyResponse],
    operation_id="list_families",
    summary="List the families the caller belongs to.",
)
def list_families(
    _identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> list[FamilyResponse]:
    return repo.list_families()


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


@router.post(
    "/children",
    response_model=ChildResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_child",
    summary="Add a child to the caller's family.",
)
def create_child(
    body: ChildCreate,
    identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> ChildResponse:
    """family_id is resolved server-side from the caller's membership."""
    family_id = _resolve_family_id(identity, repo)
    return repo.create_child(
        family_id,
        body.display_name,
        body.grade_label,
        body.visibility_defaults,
    )


@router.get(
    "/children",
    response_model=list[ChildResponse],
    operation_id="list_children",
    summary="List children in the caller's family.",
)
def list_children(
    identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> list[ChildResponse]:
    family_id = _resolve_family_id(identity, repo)
    return repo.list_children(family_id)


@router.patch(
    "/children/{child_id}",
    response_model=ChildResponse,
    operation_id="update_child",
    summary="Partially update a child's profile (name, grade, visibility defaults).",
)
def update_child(
    child_id: uuid.UUID,
    body: ChildUpdate,
    _identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> ChildResponse:
    """Partial update (PATCH semantics): only supplied fields are changed.

    RLS ensures the caller can only update children in their own family.
    Returns 404 if the child is not found or not accessible.
    """
    try:
        return repo.update_child(child_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/children/{child_id}/archive",
    response_model=ChildResponse,
    operation_id="archive_child",
    summary="Archive a child (reversible soft-delete — hides cycles from the child).",
)
def archive_child(
    child_id: uuid.UUID,
    _identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> ChildResponse:
    """Sets archived_at = now().

    The child no longer appears in GET /children (active-only list).
    RLS ensures the caller can only archive children in their own family.
    Returns 404 if the child is not found, already archived, or not accessible.
    """
    try:
        return repo.archive_child(child_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------


@router.post(
    "/subjects",
    response_model=SubjectResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_subject",
    summary="Add a subject for a child.",
)
def create_subject(
    body: SubjectCreate,
    identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> SubjectResponse:
    """family_id is resolved server-side from the caller's membership.

    ``name`` is freeform; the app never interprets it (ARCHITECTURE golden rule 4).
    """
    family_id = _resolve_family_id(identity, repo)
    return repo.create_subject(
        family_id,
        body.child_id,
        body.name,
        body.content_language,
    )


@router.get(
    "/subjects",
    response_model=list[SubjectResponse],
    operation_id="list_subjects",
    summary="List subjects in the caller's family.",
)
def list_subjects(
    identity: Identity = Depends(get_identity),
    repo: FamilyRepository = Depends(get_family_repository),
) -> list[SubjectResponse]:
    family_id = _resolve_family_id(identity, repo)
    return repo.list_subjects(family_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_family_id(identity: Identity, repo: FamilyRepository) -> uuid.UUID:
    """Derive family_id server-side from the authenticated user's membership.

    Raises 409 if the user has no family yet (they should call POST /families
    first).  Raises 409 if the user belongs to multiple families (future
    multi-family support; not expected in MVP).
    """
    families = repo.list_families()
    if not families:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No family found for this user. Call POST /families first.",
        )
    # MVP: single family per user.
    return families[0].id
