"""Scoped, short-lived child "kiosk" session token.

Hardens child kiosk mode, which previously ran entirely under the parent's
own authenticated session (the accepted-risk note in ``routers/capture.py``
and ``routers/child_results.py``, now superseded by this module).

Design (ARCHITECTURE.md §10, 2026-07-12 RLS decision — the single load-bearing
constraint for this whole module):

    RLS tenancy resolves ``family_id`` by joining the authenticated
    ``user_id`` in ``family_members``
    (``services/repositories/postgres.py::open_authenticated_connection``
    sets ``request.jwt.claims = {"sub": <user_id>}`` and runs as the
    non-privileged ``authenticated`` role). Tenancy is keyed on ``user_id``,
    NOT ``family_id``, NOT a custom claim.

Therefore the kiosk token's ``sub`` is the owning PARENT's ``user_id`` — the
exact same value ``get_identity`` would have produced for that parent — and a
kiosk request is authorized by feeding that ``user_id`` into the EXISTING
``open_authenticated_connection`` claims path, completely unchanged (see
``dependencies.py``'s ``*_for_caller`` providers). ``cycle_id`` / ``child_id``
/ ``family_id`` carried in the token are API-layer assertion values ONLY —
endpoints compare them against the server-resolved cycle/subject/child and
403 on mismatch. They are NEVER fed into a DB tenancy claim, and there is NO
claim-keyed RLS policy anywhere in this module — that would reopen the §10
decision.

Isolation from ``services/auth.py``:
- Symmetric HS256 only, pinned exactly (``algorithms=["HS256"]``) — this
  alone forecloses ``alg: none`` and any RS*/ES* asymmetric token (PyJWT
  refuses to verify a token whose header ``alg`` is not in the allow-list,
  regardless of what "key" is supplied).
- A dedicated secret (``settings.child_session_secret``), never the Supabase
  JWKS/public key, never the DB/service-role key, never derived from either.
- A distinct ``X-Child-Session`` header (never ``Authorization: Bearer``) so
  the two credential planes can never be confused by a client or a proxy.
- ``get_kiosk_identity`` / the combined caller dependency below verify the
  kiosk header cryptographically FIRST and, when present, never fall back to
  or consult the stub ``X-User-Id`` path — a stub header can never forge a
  kiosk identity, even in dev/test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from fastapi.security.http import HTTPAuthorizationCredentials

from config import Settings, get_settings
from schemas.identity import Identity, KioskIdentity, RequestCaller
from services.auth import (
    _BEARER_SCHEME,
    _STUB_ALLOWED_ENVIRONMENTS,
    _STUB_SCHEME,
    SupabaseJwtVerifier,
    get_identity,
    get_jwt_verifier,
)

# Fixed marker claims that identify a kiosk token — never shared with any
# other token family in this codebase.
KIOSK_ISSUER = "studypal-kiosk"
KIOSK_TOKEN_TYPE = "kiosk"

# Short-lived by design (architect decision: stateless, no revocation table —
# a compromised token self-expires quickly rather than requiring revocation).
KIOSK_TOKEN_TTL = timedelta(hours=4)

_ALGORITHM = "HS256"
_MIN_SECRET_LENGTH = 32

# Ships as the ``Settings.child_session_secret`` default so dev/test work with
# no env configured. Deliberately rejected as a real secret outside
# _STUB_ALLOWED_ENVIRONMENTS (fail closed on a prod misconfig that left the
# dev default in place).
_DEV_DEFAULT_SECRET = "studypal-dev-only-kiosk-secret-do-not-use-in-prod"

# The kiosk credential's own header — physically distinct from both
# ``Authorization: Bearer`` (Supabase JWKS) and ``X-User-Id`` (stub).
# ``scheme_name`` is set explicitly so this gets its OWN OpenAPI security
# scheme entry — without it, FastAPI derives the scheme name from the class
# (``APIKeyHeader``), which collides with ``services.auth``'s stub
# ``X-User-Id`` scheme (same class, no explicit name) and one silently
# shadows the other in the generated spec/SDK.
_CHILD_SESSION_SCHEME = APIKeyHeader(
    name="x-child-session", scheme_name="ChildSessionToken", auto_error=False
)

_REQUIRED_CLAIMS = [
    "exp",
    "iat",
    "sub",
    "scope",
    "token_type",
    "iss",
    "cycle_id",
    "child_id",
    "family_id",
]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


# ---------------------------------------------------------------------------
# Secret resolution — fail closed outside dev/test/local/ci (mirrors
# services.auth._STUB_ALLOWED_ENVIRONMENTS gating).
# ---------------------------------------------------------------------------


def resolve_kiosk_secret(settings: Settings) -> str:
    """Return the configured kiosk secret, or raise if it is unsafe to use.

    - Any environment: a secret shorter than 32 chars is always rejected.
    - Outside ``_STUB_ALLOWED_ENVIRONMENTS`` (i.e. a real deployment): the
      unmodified dev-only default is ALSO rejected — a prod deployment must
      set ``STUDYPAL_CHILD_SESSION_SECRET`` explicitly.
    """
    secret = settings.child_session_secret
    if len(secret) < _MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"STUDYPAL_CHILD_SESSION_SECRET must be at least {_MIN_SECRET_LENGTH} characters."
        )
    if (
        settings.environment.lower() not in _STUB_ALLOWED_ENVIRONMENTS
        and secret == _DEV_DEFAULT_SECRET
    ):
        raise RuntimeError(
            "STUDYPAL_CHILD_SESSION_SECRET must be configured in this "
            "environment; the dev-only default is refused outside "
            f"{sorted(_STUB_ALLOWED_ENVIRONMENTS)}."
        )
    return secret


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------


def mint_kiosk_token(
    *,
    secret: str,
    user_id: uuid.UUID,
    cycle_id: uuid.UUID,
    child_id: uuid.UUID,
    family_id: uuid.UUID,
    scope: Literal["capture", "results"],
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Mint a signed kiosk token. Returns ``(token, expires_at)``.

    ``user_id`` is the owning parent's user_id (the token's ``sub`` / tenancy
    key). ``cycle_id`` / ``child_id`` / ``family_id`` are API-layer assertion
    values only.
    """
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + KIOSK_TOKEN_TTL
    claims: dict[str, object] = {
        "sub": str(user_id),
        "cycle_id": str(cycle_id),
        "child_id": str(child_id),
        "family_id": str(family_id),
        "scope": scope,
        "iss": KIOSK_ISSUER,
        "token_type": KIOSK_TOKEN_TYPE,
        "iat": issued_at,
        "exp": expires_at,
    }
    token = jwt.encode(claims, secret, algorithm=_ALGORITHM)
    return token, expires_at


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_kiosk_token(token: str, secret: str) -> KioskIdentity:
    """Verify a kiosk token and return its ``KioskIdentity``, or raise 401.

    Pins ``algorithms=["HS256"]`` EXACTLY: PyJWT refuses to verify a token
    whose header ``alg`` is not in this list, so ``alg: none`` and any
    RS*/ES* (asymmetric) token are rejected before any key material is even
    consulted. Requires ``exp``, ``scope``, ``token_type == "kiosk"``, and the
    kiosk ``iss`` — anything else fails closed.
    """
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            issuer=KIOSK_ISSUER,
            options={
                "require": _REQUIRED_CLAIMS,
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
            },
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized(f"Invalid kiosk token: {exc}") from exc

    if claims.get("token_type") != KIOSK_TOKEN_TYPE:
        raise _unauthorized("Not a kiosk token")

    scope = claims.get("scope")
    if scope not in ("capture", "results"):
        raise _unauthorized(f"Invalid kiosk token scope: {scope!r}")

    try:
        return KioskIdentity(
            user_id=claims["sub"],
            cycle_id=claims["cycle_id"],
            child_id=claims["child_id"],
            family_id=claims["family_id"],
            scope=scope,
        )
    except ValueError as exc:
        raise _unauthorized(f"Malformed kiosk token claims: {exc}") from exc


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_kiosk_identity(
    child_session: str | None = Depends(_CHILD_SESSION_SCHEME),
    settings: Settings = Depends(get_settings),
) -> KioskIdentity:
    """Resolve a ``KioskIdentity`` from ``X-Child-Session``, or raise 401.

    Always verifies cryptographically — independent of stub/JWKS mode. A
    missing header is a 401, never a silent "no kiosk" fallthrough (callers
    that want the parent-OR-kiosk union should use
    ``get_capture_or_results_caller`` instead).
    """
    if child_session is None:
        raise _unauthorized("Missing X-Child-Session header")
    secret = resolve_kiosk_secret(settings)
    return verify_kiosk_token(child_session, secret)


def get_capture_or_results_caller(
    child_session: str | None = Depends(_CHILD_SESSION_SCHEME),
    bearer: HTTPAuthorizationCredentials | None = Depends(_BEARER_SCHEME),
    raw_header: str | None = Depends(_STUB_SCHEME),
    verifier: SupabaseJwtVerifier | None = Depends(get_jwt_verifier),
    settings: Settings = Depends(get_settings),
) -> RequestCaller:
    """Normalize either a parent ``Identity`` or a scoped kiosk token.

    ``X-Child-Session`` is checked FIRST and, when present, is verified
    cryptographically in complete isolation from the parent auth path — the
    stub ``X-User-Id`` / bearer JWT are never consulted, so neither can forge
    a kiosk identity. When the header is absent, this falls back to the
    normal ``get_identity`` resolution (JWKS or stub, per existing rules),
    exactly as every other endpoint behaves.

    The returned ``RequestCaller.identity`` always carries the tenancy
    ``user_id`` — for a kiosk caller this is the token's owning parent, so
    repository dependencies built from it resolve data via the SAME
    ``family_members`` RLS join as an ordinary parent request.
    """
    if child_session is not None:
        secret = resolve_kiosk_secret(settings)
        kiosk = verify_kiosk_token(child_session, secret)
        return RequestCaller(identity=Identity(user_id=kiosk.user_id), kiosk=kiosk)

    identity = get_identity(
        bearer=bearer, raw_header=raw_header, verifier=verifier, settings=settings
    )
    return RequestCaller(identity=identity)
