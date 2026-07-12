"""Identity models for the auth dependency seam.

PR-1: stub identity injected via ``X-User-Id`` header.
PR-2: real JWKS-verified JWT replaces the stub; callers are unchanged.

The ``Identity`` model is the only thing that crosses the auth→service
boundary — no bare dict, no raw header string.
"""

from __future__ import annotations

import uuid

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
