"""Auth dependency seam — dual-mode caller identity.

``get_identity`` is the ONLY entry point for caller identity in the request
path.  It has two modes, selected purely by configuration:

- **JWKS mode** (production): when ``settings.supabase_jwks_url`` is set, a
  ``Authorization: Bearer <jwt>`` is required.  The token is verified against
  Supabase's published JWKS — signature, issuer, audience, expiry — and the
  ``user_id`` is taken from the verified ``sub`` claim.  The ``X-User-Id`` stub
  header is ignored entirely.
- **Stub mode** (local / test / credential-free): when no JWKS URL is
  configured, identity comes from ``X-User-Id: <user_id>/<family_id>`` so tests
  can inject arbitrary identities without minting a real JWT.

Either way the return type is ``Identity``; callers never change.

Security: only asymmetric algorithms are accepted in JWKS mode.  Accepting
HS256 alongside a public JWKS would allow the classic alg-confusion forgery
(sign an HS256 token using the public key as the shared secret), so HS* is
stripped and rejected at construction time.  Verification fails closed — any
error (bad signature, expiry, wrong iss/aud, unreachable JWKS, non-UUID sub)
maps to HTTP 401.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any, Protocol

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPBearer
from fastapi.security.http import HTTPAuthorizationCredentials
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError

from config import Settings, get_settings
from schemas.identity import Identity


class SigningKeyClient(Protocol):
    """Structural type for a JWKS signing-key resolver.

    ``PyJWKClient`` satisfies this; tests inject a fake so verification needs no
    network. The returned object exposes a ``.key`` usable by ``jwt.decode``.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


# Clock-skew tolerance for exp/iat/nbf, in seconds.
_LEEWAY_SECONDS = 30

# The X-User-Id stub is a credential-free convenience for these environments
# only. Anywhere else, a missing JWKS config must fail closed rather than
# silently accept an unauthenticated header (defence against a prod misconfig).
_STUB_ALLOWED_ENVIRONMENTS = frozenset({"dev", "test", "local", "ci"})

# Visible schemes (show in OpenAPI docs). Both optional; get_identity decides.
_BEARER_SCHEME = HTTPBearer(auto_error=False, scheme_name="SupabaseJWT")
_STUB_SCHEME = APIKeyHeader(name="x-user-id", auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


# ---------------------------------------------------------------------------
# JWKS verification
# ---------------------------------------------------------------------------


class SupabaseJwtVerifier:
    """Verifies a Supabase-issued JWT against the project's JWKS.

    Asymmetric algorithms only. The ``jwks_client`` is injectable so tests can
    supply signing keys without a network round-trip.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        algorithms: list[str],
        jwks_client: SigningKeyClient,
    ) -> None:
        asymmetric = [a for a in algorithms if not a.upper().startswith("HS")]
        if not asymmetric:
            raise ValueError(
                "SupabaseJwtVerifier requires at least one asymmetric algorithm "
                "(HS* is refused to prevent JWKS alg-confusion attacks)."
            )
        self._issuer = issuer or None
        self._audience = audience or None
        self._algorithms = asymmetric
        self._jwks_client = jwks_client

    def verify(self, token: str) -> Identity:
        """Return the caller ``Identity`` from a verified token, or raise 401."""
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        except (PyJWKClientError, jwt.PyJWTError) as exc:
            # Unresolvable/rotated/unreachable key → fail closed.
            raise _unauthorized(f"Unable to resolve signing key: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=_LEEWAY_SECONDS,
                options={
                    "require": ["exp", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": self._audience is not None,
                    "verify_iss": self._issuer is not None,
                },
            )
        except jwt.PyJWTError as exc:
            raise _unauthorized(f"Invalid token: {exc}") from exc

        sub = claims.get("sub")
        if not sub or not isinstance(sub, str):
            raise _unauthorized("Token missing 'sub' claim")
        try:
            return Identity(user_id=uuid.UUID(sub))
        except ValueError as exc:
            raise _unauthorized(f"Token 'sub' is not a UUID: {exc}") from exc


@lru_cache
def _build_verifier(
    jwks_url: str,
    issuer: str,
    audience: str,
    algorithms: tuple[str, ...],
) -> SupabaseJwtVerifier:
    """Cached verifier (and its key-caching PyJWKClient) keyed by config."""
    client = PyJWKClient(jwks_url, cache_keys=True)
    return SupabaseJwtVerifier(
        issuer=issuer,
        audience=audience,
        algorithms=list(algorithms),
        jwks_client=client,
    )


def get_jwt_verifier(
    settings: Settings = Depends(get_settings),
) -> SupabaseJwtVerifier | None:
    """Return a verifier when JWKS is configured, else ``None`` (stub mode)."""
    if not settings.supabase_jwks_url:
        return None
    return _build_verifier(
        settings.supabase_jwks_url,
        settings.supabase_jwt_iss,
        settings.supabase_jwt_aud,
        tuple(settings.supabase_jwt_algorithms),
    )


# ---------------------------------------------------------------------------
# Stub identity (local / test / credential-free)
# ---------------------------------------------------------------------------


def identity_from_stub_header(raw_header: str | None, settings: Settings) -> Identity:
    """Parse ``X-User-Id`` into an ``Identity`` or raise 401.

    Accepted formats:
    - ``<user_id>``               — new users who have no family yet
    - ``<user_id>/<family_id>``   — existing users (family already bootstrapped)

    Three or more segments are rejected (extra slashes are almost certainly a
    copy-paste error, not a valid identity).
    """
    if raw_header is None:
        raise _unauthorized("Missing identity header")

    parts = raw_header.strip().split("/")
    if len(parts) == 1:
        try:
            return Identity(user_id=uuid.UUID(parts[0]))
        except (ValueError, AttributeError) as exc:
            raise _unauthorized(f"Invalid UUID in identity header: {exc}") from exc
    if len(parts) == 2:
        try:
            return Identity(user_id=uuid.UUID(parts[0]), family_id=uuid.UUID(parts[1]))
        except (ValueError, AttributeError) as exc:
            raise _unauthorized(f"Invalid UUID in identity header: {exc}") from exc
    raise _unauthorized("Malformed identity header: expected <user_id> or <user_id>/<family_id>")


# ---------------------------------------------------------------------------
# The dependency
# ---------------------------------------------------------------------------


def get_identity(
    bearer: HTTPAuthorizationCredentials | None = Depends(_BEARER_SCHEME),
    raw_header: str | None = Depends(_STUB_SCHEME),
    verifier: SupabaseJwtVerifier | None = Depends(get_jwt_verifier),
    settings: Settings = Depends(get_settings),
) -> Identity:
    """Resolve the caller ``Identity`` or raise 401.

    JWKS mode (verifier present) requires a Bearer JWT; the stub header is
    ignored.  Otherwise the ``X-User-Id`` stub applies.
    """
    if verifier is not None:
        if bearer is None or not bearer.credentials:
            raise _unauthorized("Missing bearer token")
        return verifier.verify(bearer.credentials)

    # Stub mode: only permitted outside production, and only when JWKS is truly
    # unconfigured. A production deployment must set STUDYPAL_SUPABASE_JWKS_URL.
    if settings.environment.lower() not in _STUB_ALLOWED_ENVIRONMENTS:
        raise _unauthorized(
            "Stub auth is disabled in this environment; "
            "STUDYPAL_SUPABASE_JWKS_URL must be configured."
        )
    return identity_from_stub_header(raw_header, settings)
