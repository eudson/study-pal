"""Identity models for the auth dependency seam.

PR-1: stub identity injected via ``X-User-Id`` header.
PR-2: real JWKS-verified JWT replaces the stub; callers are unchanged.

The ``Identity`` model is the only thing that crosses the auth→service
boundary — no bare dict, no raw header string.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, field_validator


class Identity(BaseModel):
    """Verified caller identity.  ``user_id`` is the only value the request
    path needs — the DB resolves tenancy from it via RLS (the family_members
    join, ARCHITECTURE §10 R1), so ``family_id`` is never threaded into queries.

    - Stub path (local/test): ``user_id`` and ``family_id`` come from the
      ``X-User-Id: <user_id>/<family_id>`` header.
    - JWKS path (production): ``user_id`` is the verified JWT ``sub``; a Supabase
      token carries no family, so ``family_id`` stays ``None`` and RLS resolves it.
    """

    user_id: uuid.UUID
    family_id: uuid.UUID | None = None

    @field_validator("user_id", "family_id", mode="before")
    @classmethod
    def _coerce_uuid(cls, v: object) -> uuid.UUID | None:
        if v is None:
            return None
        if isinstance(v, uuid.UUID):
            return v
        if isinstance(v, str):
            return uuid.UUID(v)
        raise ValueError(f"Expected UUID or str, got {type(v)}")


class KioskIdentity(BaseModel):
    """Verified claims from a scoped, short-lived kiosk (child capture/results)
    token — see ``services/kiosk_session.py``.

    ``user_id`` is the owning PARENT's user_id (the tenancy key — ARCHITECTURE
    §10 2026-07-12: RLS resolves ``family_id`` by joining ``user_id`` in
    ``family_members``). ``cycle_id`` / ``child_id`` / ``family_id`` are
    API-layer assertion values ONLY: endpoints compare them against the
    server-resolved cycle/subject/child to authorize the request — they are
    NEVER fed into a DB tenancy claim or a claim-keyed RLS policy.
    """

    user_id: uuid.UUID
    cycle_id: uuid.UUID
    child_id: uuid.UUID
    family_id: uuid.UUID
    scope: Literal["capture", "results"]

    @field_validator("user_id", "cycle_id", "child_id", "family_id", mode="before")
    @classmethod
    def _coerce_uuid(cls, v: object) -> uuid.UUID:
        if isinstance(v, uuid.UUID):
            return v
        if isinstance(v, str):
            return uuid.UUID(v)
        raise ValueError(f"Expected UUID or str, got {type(v)}")


class RequestCaller(BaseModel):
    """Normalized caller for endpoints that accept EITHER a full parent
    ``Identity`` OR a scoped kiosk token (``get_capture_or_results_caller``).

    ``identity`` always carries the tenancy key (``user_id``) that repository
    dependencies use to open the RLS-scoped connection — for a kiosk caller
    this is the token's owning parent ``user_id``, so tenancy resolution is
    byte-for-byte the same ``family_members`` join used for a normal parent
    request. ``kiosk`` is populated only for kiosk-token calls and carries the
    additional scope/cycle/child assertions the endpoint must enforce; it is
    ``None`` for ordinary parent calls.
    """

    identity: Identity
    kiosk: KioskIdentity | None = None
