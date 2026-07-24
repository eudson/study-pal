"""Mint a scoped, short-lived kiosk (child capture/results) token.

POST /cycles/{cycle_id}/child-session
    Parent-authenticated (``get_identity`` — asymmetric JWKS or the
    dev/test stub; a kiosk token can never mint another kiosk token).
    Resolves the cycle and its child SERVER-SIDE (never trusts the
    client) and infers the token's ``scope`` from the cycle's CURRENT
    phase:
      - ``PRINTED``                                   -> "capture"
      - marks published for the cycle's current round -> "results"
      - anything else                                 -> 409

    The capture token is minted at PRINTED; the results grant is a
    SEPARATE mint issued once marks are published — never one token
    stretched to cover both (advisor must-fix #5).

See ``services/kiosk_session.py`` for the token itself and
``routers/capture.py`` / ``routers/child_results.py`` for how it is
consumed.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from config import Settings, get_settings
from dependencies import get_family_repository
from routers.families import _resolve_family_id
from schemas.family import CyclePhase
from schemas.identity import Identity
from schemas.kiosk_session import ChildSessionResponse
from services.auth import get_identity
from services.kiosk_session import mint_kiosk_token, resolve_kiosk_secret
from services.phase import is_published, round_config
from services.repositories.base import FamilyRepository

router = APIRouter(prefix="/cycles")


@router.post(
    "/{cycle_id}/child-session",
    response_model=ChildSessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="mintChildSession",
    summary=(
        "Mint a short-lived scoped kiosk token for child capture or results "
        "(scope inferred from the cycle's current phase)."
    ),
)
def mint_child_session(
    cycle_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    family_repo: FamilyRepository = Depends(get_family_repository),
    settings: Settings = Depends(get_settings),
) -> ChildSessionResponse:
    """Mint a kiosk token scoped to this cycle, its child, and one purpose.

    Guards:
    - Cycle exists in the caller's family (RLS via ``get_family_repository`` —
      a non-owner/other-family parent gets 404, never a token).
    - Scope is inferred server-side from the cycle's phase — never trusted
      from the client:
        * ``PRINTED``                                      -> "capture"
        * current round's marks published AND that round is
          child-visible (``services.phase.round_config``)   -> "results"
        * otherwise                                         -> 409
    - ``child_id`` is resolved from the cycle's subject, never accepted from
      the client.
    """
    _resolve_family_id(identity, family_repo)  # ensures the caller has a family (RLS)

    cycle = family_repo.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        )

    scope: Literal["capture", "results"]
    if cycle.phase == CyclePhase.PRINTED:
        scope = "capture"
    elif (
        is_published(family_repo, cycle_id, cycle.round)
        and round_config(cycle.round).results_child_visible
    ):
        scope = "results"
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is in phase '{cycle.phase.value}' (round {cycle.round}); "
                "a child session can only be minted when the cycle is PRINTED "
                "(capture) or once that round's marks are published and "
                "child-visible (results)."
            ),
        )

    subjects = family_repo.list_subjects(cycle.family_id)
    cycle_subject = next((s for s in subjects if s.id == cycle.subject_id), None)
    if cycle_subject is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Subject for this cycle could not be resolved.",
        )

    secret = resolve_kiosk_secret(settings)
    token, expires_at = mint_kiosk_token(
        secret=secret,
        user_id=identity.user_id,
        cycle_id=cycle_id,
        child_id=cycle_subject.child_id,
        family_id=cycle.family_id,
        scope=scope,
    )
    return ChildSessionResponse(token=token, scope=scope, expires_at=expires_at)
