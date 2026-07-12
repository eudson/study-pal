"""Auth dependency seam (PR-1 stub).

``get_identity`` is the ONLY entry point for caller identity in the request
path.  It is shaped so PR-2 drops in real JWKS JWT verification without
touching any callers:

    async def get_identity(...) -> Identity:
        # PR-2: verify JWT sig/iss/aud/exp; extract sub; return Identity
        ...

PR-1 behaviour:
- Reads the ``X-User-Id`` header (format: ``<user_id>/<family_id>``
  or just ``<user_id>`` — see below).
- Returns 401 when no header is present (deny-by-default, invariant 2).
- Returns 401 on malformed UUIDs.

Header format (PR-1 only):
    X-User-Id: <user_uuid>/<family_uuid>

This lets tests inject arbitrary identities without a real JWT.
The slash-separated pair keeps it unambiguous and parseable in one regex.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from config import Settings, get_settings
from schemas.identity import Identity

# Visible scheme name (shows in OpenAPI docs).
_STUB_SCHEME = APIKeyHeader(name="x-user-id", auto_error=False)


def get_identity(
    raw_header: str | None = Depends(_STUB_SCHEME),
    settings: Settings = Depends(get_settings),
) -> Identity:
    """Return the caller ``Identity`` or raise 401.

    PR-1: parses ``X-User-Id: <user_id>/<family_id>``.
    PR-2: replace body with JWKS JWT verification; signature stays identical.
    """
    if raw_header is None:
        if settings.deny_when_no_identity:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing identity header",
            )
        # Only reached when deny_when_no_identity is False (test override).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing identity header",
        )

    parts = raw_header.strip().split("/")
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed identity header: expected <user_id>/<family_id>",
        )
    try:
        return Identity(user_id=uuid.UUID(parts[0]), family_id=uuid.UUID(parts[1]))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid UUID in identity header: {exc}",
        ) from exc
