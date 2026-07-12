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
    """Verified caller identity.  Carries the user_id and resolved family_id.

    In PR-1 both values come from the stub header / config default.
    In PR-2 both are derived from the verified JWT (user_id from ``sub``,
    family_id resolved via the family_members join at auth time or left to RLS).
    """

    user_id: uuid.UUID
    family_id: uuid.UUID

    @field_validator("user_id", "family_id", mode="before")
    @classmethod
    def _coerce_uuid(cls, v: object) -> uuid.UUID:
        if isinstance(v, uuid.UUID):
            return v
        if isinstance(v, str):
            return uuid.UUID(v)
        raise ValueError(f"Expected UUID or str, got {type(v)}")
