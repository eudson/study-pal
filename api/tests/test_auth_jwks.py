"""JWKS JWT verification (PR-2, auth B).

Exercises ``SupabaseJwtVerifier`` and the dual-mode ``get_identity`` without a
network round-trip: an ES256 key pair is generated locally, tokens are signed
with the private key, and a fake JWKS client hands the verifier the matching
public key.  Covers the happy path plus every rejection the seam must enforce
(expiry, wrong iss/aud, bad signature, alg-confusion, missing/non-UUID sub).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Generator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from fastapi import HTTPException
from fastapi.security.http import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from config import Settings
from schemas.identity import Identity
from services.auth import (
    SupabaseJwtVerifier,
    get_identity,
    get_jwt_verifier,
)

_ISS = "https://project.supabase.co/auth/v1"
_AUD = "authenticated"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeJwksClient:
    """Returns a fixed public key for any token (structural SigningKeyClient)."""

    def __init__(self, public_key: object) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> Any:  # noqa: ARG002
        return type("SigningKey", (), {"key": self._public_key})()


class _RaisingJwksClient:
    """Simulates an unreachable/unknown-kid JWKS resolver."""

    def get_signing_key_from_jwt(self, token: str) -> Any:  # noqa: ARG002
        from jwt.exceptions import PyJWKClientError

        raise PyJWKClientError("no matching key")


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _make_token(
    private_key: EllipticCurvePrivateKey,
    *,
    sub: str | None = None,
    aud: str = _AUD,
    iss: str = _ISS,
    exp: dt.datetime | None = None,
    include_exp: bool = True,
    alg: str = "ES256",
    key: Any = None,
) -> str:
    claims: dict[str, Any] = {
        "sub": sub if sub is not None else str(uuid.uuid4()),
        "aud": aud,
        "iss": iss,
        "iat": _now(),
    }
    if include_exp:
        claims["exp"] = exp or (_now() + dt.timedelta(hours=1))
    return jwt.encode(claims, key or private_key, algorithm=alg)


@pytest.fixture(scope="module")
def keypair() -> tuple[EllipticCurvePrivateKey, object]:
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv, priv.public_key()


@pytest.fixture
def verifier(keypair: tuple[EllipticCurvePrivateKey, object]) -> SupabaseJwtVerifier:
    _, public_key = keypair
    return SupabaseJwtVerifier(
        issuer=_ISS,
        audience=_AUD,
        algorithms=["ES256"],
        jwks_client=_FakeJwksClient(public_key),
    )


# ---------------------------------------------------------------------------
# Verifier construction
# ---------------------------------------------------------------------------


class TestVerifierConstruction:
    def test_hs256_only_is_refused(self, keypair: tuple[EllipticCurvePrivateKey, object]) -> None:
        """HS* alone must raise — forecloses JWKS public-key alg-confusion."""
        _, public_key = keypair
        with pytest.raises(ValueError, match="asymmetric"):
            SupabaseJwtVerifier(
                issuer=_ISS,
                audience=_AUD,
                algorithms=["HS256"],
                jwks_client=_FakeJwksClient(public_key),
            )

    def test_hs256_is_stripped_from_mixed_list(
        self, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        _, public_key = keypair
        v = SupabaseJwtVerifier(
            issuer=_ISS,
            audience=_AUD,
            algorithms=["HS256", "ES256"],
            jwks_client=_FakeJwksClient(public_key),
        )
        assert v._algorithms == ["ES256"]


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


class TestVerify:
    def test_valid_token_returns_identity(
        self, verifier: SupabaseJwtVerifier, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        priv, _ = keypair
        sub = uuid.uuid4()
        token = _make_token(priv, sub=str(sub))
        identity = verifier.verify(token)
        assert identity == Identity(user_id=sub)
        assert identity.family_id is None  # RLS resolves tenancy from user_id

    def test_expired_token_401(
        self, verifier: SupabaseJwtVerifier, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        priv, _ = keypair
        token = _make_token(priv, exp=_now() - dt.timedelta(hours=1))
        with pytest.raises(HTTPException) as exc:
            verifier.verify(token)
        assert exc.value.status_code == 401

    def test_wrong_audience_401(
        self, verifier: SupabaseJwtVerifier, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        priv, _ = keypair
        token = _make_token(priv, aud="some-other-service")
        with pytest.raises(HTTPException) as exc:
            verifier.verify(token)
        assert exc.value.status_code == 401

    def test_wrong_issuer_401(
        self, verifier: SupabaseJwtVerifier, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        priv, _ = keypair
        token = _make_token(priv, iss="https://evil.example.com/auth/v1")
        with pytest.raises(HTTPException) as exc:
            verifier.verify(token)
        assert exc.value.status_code == 401

    def test_bad_signature_401(self, verifier: SupabaseJwtVerifier) -> None:
        """Token signed by a DIFFERENT key must fail against the JWKS public key."""
        attacker = ec.generate_private_key(ec.SECP256R1())
        token = _make_token(attacker, sub=str(uuid.uuid4()))
        with pytest.raises(HTTPException) as exc:
            verifier.verify(token)
        assert exc.value.status_code == 401

    def test_hs256_token_rejected_by_es256_verifier(self, verifier: SupabaseJwtVerifier) -> None:
        """Alg-confusion defense: an HS256 token is refused because HS256 is not
        in the verifier's allowed algorithms — the decode never treats the JWKS
        public key as an HMAC secret. (The secret value is irrelevant; the alg
        allowlist is the barrier.)"""
        token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "aud": _AUD,
                "iss": _ISS,
                "exp": _now() + dt.timedelta(hours=1),
            },
            "a-shared-secret-of-at-least-thirty-two-bytes",
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            verifier.verify(token)
        assert exc.value.status_code == 401

    def test_missing_exp_401(
        self, verifier: SupabaseJwtVerifier, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        priv, _ = keypair
        token = _make_token(priv, include_exp=False)
        with pytest.raises(HTTPException) as exc:
            verifier.verify(token)
        assert exc.value.status_code == 401

    def test_non_uuid_sub_401(
        self, verifier: SupabaseJwtVerifier, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        priv, _ = keypair
        token = _make_token(priv, sub="not-a-uuid")
        with pytest.raises(HTTPException) as exc:
            verifier.verify(token)
        assert exc.value.status_code == 401

    def test_unresolvable_key_401(self, keypair: tuple[EllipticCurvePrivateKey, object]) -> None:
        priv, _ = keypair
        v = SupabaseJwtVerifier(
            issuer=_ISS,
            audience=_AUD,
            algorithms=["ES256"],
            jwks_client=_RaisingJwksClient(),
        )
        with pytest.raises(HTTPException) as exc:
            v.verify(_make_token(priv))
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------


class TestGetJwtVerifier:
    def test_none_when_unconfigured(self) -> None:
        assert get_jwt_verifier(Settings(supabase_jwks_url="")) is None

    def test_built_when_configured(self) -> None:
        v = get_jwt_verifier(
            Settings(
                supabase_jwks_url="https://project.supabase.co/auth/v1/.well-known/jwks.json",
                supabase_jwt_iss=_ISS,
            )
        )
        assert isinstance(v, SupabaseJwtVerifier)


class TestGetIdentityDualMode:
    def test_jwks_mode_requires_bearer(self, verifier: SupabaseJwtVerifier) -> None:
        with pytest.raises(HTTPException) as exc:
            get_identity(bearer=None, raw_header=None, verifier=verifier, settings=Settings())
        assert exc.value.status_code == 401

    def test_jwks_mode_verifies_bearer(
        self, verifier: SupabaseJwtVerifier, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        priv, _ = keypair
        sub = uuid.uuid4()
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=_make_token(priv, sub=str(sub))
        )
        identity = get_identity(
            bearer=creds, raw_header=None, verifier=verifier, settings=Settings()
        )
        assert identity.user_id == sub

    def test_stub_mode_ignores_bearer_uses_header(self) -> None:
        user_id, family_id = uuid.uuid4(), uuid.uuid4()
        identity = get_identity(
            bearer=None,
            raw_header=f"{user_id}/{family_id}",
            verifier=None,
            settings=Settings(),
        )
        assert identity.user_id == user_id

    def test_stub_mode_disabled_in_production(self) -> None:
        """No JWKS + production env must fail closed, not accept the stub header."""
        with pytest.raises(HTTPException) as exc:
            get_identity(
                bearer=None,
                raw_header=f"{uuid.uuid4()}/{uuid.uuid4()}",
                verifier=None,
                settings=Settings(environment="production"),
            )
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# End-to-end through the protected endpoint (JWKS mode)
# ---------------------------------------------------------------------------


class TestGenerateEndpointJwksMode:
    @pytest.fixture
    def client(self, verifier: SupabaseJwtVerifier) -> Generator[TestClient, None, None]:
        from dependencies import get_assessment_repository
        from main import app
        from services.repositories.memory import InMemoryAssessmentRepository

        repo = InMemoryAssessmentRepository()
        app.dependency_overrides[get_assessment_repository] = lambda: repo
        app.dependency_overrides[get_jwt_verifier] = lambda: verifier
        yield TestClient(app, raise_server_exceptions=True)
        app.dependency_overrides.pop(get_assessment_repository, None)
        app.dependency_overrides.pop(get_jwt_verifier, None)

    def test_no_bearer_401(self, client: TestClient) -> None:
        resp = client.post(
            "/assessments/generate",
            json={"cycle_id": str(uuid.uuid4()), "scope_text": "Grade 5 Maths"},
        )
        assert resp.status_code == 401

    def test_valid_bearer_201(
        self, client: TestClient, keypair: tuple[EllipticCurvePrivateKey, object]
    ) -> None:
        priv, _ = keypair
        token = _make_token(priv, sub=str(uuid.uuid4()))
        resp = client.post(
            "/assessments/generate",
            json={"cycle_id": str(uuid.uuid4()), "scope_text": "Grade 5 Maths"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        assert resp.json()["ok"] is True
