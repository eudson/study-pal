"""Tests for the scoped kiosk (child capture/results) session token.

Sections:
1. Unit tests for ``services/kiosk_session.py`` — mint/verify round trip and
   every rejection path (tamper, alg:none, RS256, expired, missing/invalid
   claims, secret gating). No app, no DB.
2. ``get_capture_or_results_caller`` unit-level isolation: stub/bearer never
   forge a kiosk identity; the kiosk header is checked in complete isolation.
3. HTTP tests for ``POST /cycles/{id}/child-session`` (mint endpoint) —
   InMemory repos, phase → scope inference, 409s, auth.
4. Isolation matrix: a kiosk token presented to a strictly parent-only
   endpoint is rejected; a parent stub credential presented as
   ``X-Child-Session`` is rejected.
5. DB tier: proves a kiosk request resolves data via the SAME
   ``family_members`` RLS join as an ordinary parent request, and that a
   validly-signed token whose ``family_id``/``cycle_id``/``child_id`` claims
   point at another family's cycle still cannot reach it — the ``user_id``
   join is authoritative, not the token's assertion claims. Skipped cleanly
   when Postgres is unreachable.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from config import Settings, get_settings
from main import app
from schemas.family import CycleState
from services.cycle import advance_to_generating, advance_to_parent_reviews, approve_draft
from services.kiosk_session import (
    KIOSK_ISSUER,
    KIOSK_TOKEN_TYPE,
    get_capture_or_results_caller,
    mint_kiosk_token,
    resolve_kiosk_secret,
    verify_kiosk_token,
)
from services.repositories.memory import InMemoryAssessmentRepository, InMemoryFamilyRepository

_TEST_SECRET = "unit-test-kiosk-secret-at-least-32-characters-long"


def _ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


# ---------------------------------------------------------------------------
# 1. mint_kiosk_token / verify_kiosk_token — unit tier
# ---------------------------------------------------------------------------


class TestMintVerifyRoundTrip:
    def test_valid_token_round_trips(self) -> None:
        user_id, cycle_id, child_id, family_id = _ids()
        token, expires_at = mint_kiosk_token(
            secret=_TEST_SECRET,
            user_id=user_id,
            cycle_id=cycle_id,
            child_id=child_id,
            family_id=family_id,
            scope="capture",
        )
        identity = verify_kiosk_token(token, _TEST_SECRET)
        assert identity.user_id == user_id
        assert identity.cycle_id == cycle_id
        assert identity.child_id == child_id
        assert identity.family_id == family_id
        assert identity.scope == "capture"
        assert expires_at > datetime.now(UTC)

    def test_results_scope_round_trips(self) -> None:
        user_id, cycle_id, child_id, family_id = _ids()
        token, _ = mint_kiosk_token(
            secret=_TEST_SECRET,
            user_id=user_id,
            cycle_id=cycle_id,
            child_id=child_id,
            family_id=family_id,
            scope="results",
        )
        identity = verify_kiosk_token(token, _TEST_SECRET)
        assert identity.scope == "results"

    def test_expiry_is_short_lived(self) -> None:
        user_id, cycle_id, child_id, family_id = _ids()
        now = datetime.now(UTC)
        _token, expires_at = mint_kiosk_token(
            secret=_TEST_SECRET,
            user_id=user_id,
            cycle_id=cycle_id,
            child_id=child_id,
            family_id=family_id,
            scope="capture",
            now=now,
        )
        assert expires_at == now + timedelta(hours=4)


class TestVerifyRejections:
    def test_tampered_signature_401(self) -> None:
        user_id, cycle_id, child_id, family_id = _ids()
        token, _ = mint_kiosk_token(
            secret=_TEST_SECRET,
            user_id=user_id,
            cycle_id=cycle_id,
            child_id=child_id,
            family_id=family_id,
            scope="capture",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, "a-completely-different-secret-of-32-plus-chars")
        assert exc.value.status_code == 401

    def test_alg_none_rejected(self) -> None:
        forged = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "cycle_id": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "capture",
                "iss": KIOSK_ISSUER,
                "token_type": KIOSK_TOKEN_TYPE,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(forged, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_asymmetric_rs256_rejected(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "cycle_id": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "capture",
                "iss": KIOSK_ISSUER,
                "token_type": KIOSK_TOKEN_TYPE,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            private_key,
            algorithm="RS256",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_expired_token_401(self) -> None:
        user_id, cycle_id, child_id, family_id = _ids()
        token, _ = mint_kiosk_token(
            secret=_TEST_SECRET,
            user_id=user_id,
            cycle_id=cycle_id,
            child_id=child_id,
            family_id=family_id,
            scope="capture",
            now=datetime.now(UTC) - timedelta(hours=5),
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_wrong_scope_value_401(self) -> None:
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "cycle_id": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "admin",
                "iss": KIOSK_ISSUER,
                "token_type": KIOSK_TOKEN_TYPE,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_missing_token_type_401(self) -> None:
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "cycle_id": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "capture",
                "iss": KIOSK_ISSUER,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_wrong_token_type_401(self) -> None:
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "cycle_id": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "capture",
                "iss": KIOSK_ISSUER,
                "token_type": "parent",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_missing_iss_401(self) -> None:
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "cycle_id": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "capture",
                "token_type": KIOSK_TOKEN_TYPE,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_wrong_iss_401(self) -> None:
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "cycle_id": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "capture",
                "iss": "some-other-issuer",
                "token_type": KIOSK_TOKEN_TYPE,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_missing_cycle_id_claim_401(self) -> None:
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "capture",
                "iss": KIOSK_ISSUER,
                "token_type": KIOSK_TOKEN_TYPE,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401

    def test_missing_exp_401(self) -> None:
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "cycle_id": str(uuid.uuid4()),
                "child_id": str(uuid.uuid4()),
                "family_id": str(uuid.uuid4()),
                "scope": "capture",
                "iss": KIOSK_ISSUER,
                "token_type": KIOSK_TOKEN_TYPE,
                "iat": datetime.now(UTC),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            verify_kiosk_token(token, _TEST_SECRET)
        assert exc.value.status_code == 401


class TestResolveKioskSecret:
    def test_short_secret_rejected_in_dev(self) -> None:
        with pytest.raises(RuntimeError):
            resolve_kiosk_secret(Settings(environment="dev", child_session_secret="too-short"))

    def test_short_secret_rejected_in_production(self) -> None:
        with pytest.raises(RuntimeError):
            resolve_kiosk_secret(
                Settings(environment="production", child_session_secret="too-short")
            )

    def test_dev_default_accepted_in_dev(self) -> None:
        secret = resolve_kiosk_secret(Settings(environment="dev"))
        assert len(secret) >= 32

    def test_dev_default_rejected_in_production(self) -> None:
        """The unmodified dev-only default must fail closed outside dev/test/local/ci."""
        with pytest.raises(RuntimeError):
            resolve_kiosk_secret(Settings(environment="production"))

    def test_custom_secret_accepted_in_production(self) -> None:
        secret = resolve_kiosk_secret(
            Settings(
                environment="production",
                child_session_secret="a-real-production-secret-that-is-32-plus-chars",
            )
        )
        assert len(secret) >= 32


# ---------------------------------------------------------------------------
# 2. get_capture_or_results_caller — isolation between credential planes
# ---------------------------------------------------------------------------


class TestCombinedCallerIsolation:
    def test_kiosk_header_present_ignores_stub_and_bearer(self) -> None:
        """When X-Child-Session is present, stub/bearer are never consulted —
        even a well-formed stub header must not influence the result."""
        user_id, cycle_id, child_id, family_id = _ids()
        settings = Settings(environment="dev", child_session_secret=_TEST_SECRET)
        secret = resolve_kiosk_secret(settings)
        token, _ = mint_kiosk_token(
            secret=secret,
            user_id=user_id,
            cycle_id=cycle_id,
            child_id=child_id,
            family_id=family_id,
            scope="capture",
        )
        caller = get_capture_or_results_caller(
            child_session=token,
            bearer=None,
            raw_header=f"{uuid.uuid4()}/{uuid.uuid4()}",  # must be ignored
            verifier=None,
            settings=settings,
        )
        assert caller.kiosk is not None
        assert caller.identity.user_id == user_id

    def test_no_kiosk_header_falls_back_to_parent_identity(self) -> None:
        user_id = uuid.uuid4()
        settings = Settings(environment="dev")
        caller = get_capture_or_results_caller(
            child_session=None,
            bearer=None,
            raw_header=str(user_id),
            verifier=None,
            settings=settings,
        )
        assert caller.kiosk is None
        assert caller.identity.user_id == user_id

    def test_no_credentials_at_all_401(self) -> None:
        settings = Settings(environment="dev")
        with pytest.raises(HTTPException) as exc:
            get_capture_or_results_caller(
                child_session=None,
                bearer=None,
                raw_header=None,
                verifier=None,
                settings=settings,
            )
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 3. POST /cycles/{id}/child-session — mint endpoint (InMemory repos)
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_STUB_HEADER = str(_USER_ID)


def _make_family_repo() -> InMemoryFamilyRepository:
    return InMemoryFamilyRepository(user_id=_USER_ID)


def _bootstrap_cycle_at(
    family_repo: InMemoryFamilyRepository,
    target: CycleState,
) -> uuid.UUID:
    family, child_id = family_repo.bootstrap_family("Mint Family", "Kid", "Grade 5")
    assert child_id is not None
    subject = family_repo.create_subject(family.id, child_id, "Maths", "en")
    cycle = family_repo.create_cycle(family.id, subject.id, "scope")

    if target == CycleState.SCOPE_UPLOADED:
        return cycle.id
    advance_to_generating(family_repo, cycle.id)
    if target == CycleState.GENERATING_A:
        return cycle.id
    advance_to_parent_reviews(family_repo, cycle.id)
    if target == CycleState.PARENT_REVIEWS_DRAFT:
        return cycle.id
    approve_draft(family_repo, cycle.id)
    # target == APPROVED_PRINTED (mapped phase PRINTED)
    return cycle.id


@pytest.fixture()
def mint_client() -> Any:
    family_repo = _make_family_repo()
    asmt_repo = InMemoryAssessmentRepository()

    from dependencies import get_assessment_repository, get_family_repository

    app.dependency_overrides[get_family_repository] = lambda: family_repo
    app.dependency_overrides[get_assessment_repository] = lambda: asmt_repo
    with TestClient(app) as c:
        yield c, family_repo
    app.dependency_overrides.pop(get_family_repository, None)
    app.dependency_overrides.pop(get_assessment_repository, None)


class TestMintEndpoint:
    def test_mint_at_printed_returns_capture_scope(self, mint_client: Any) -> None:
        client, family_repo = mint_client
        cycle_id = _bootstrap_cycle_at(family_repo, CycleState.APPROVED_PRINTED)
        resp = client.post(
            f"/cycles/{cycle_id}/child-session",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["scope"] == "capture"
        assert body["token"]
        assert body["expires_at"]

    def test_mint_after_publish_returns_results_scope(self, mint_client: Any) -> None:
        client, family_repo = mint_client
        cycle_id = _bootstrap_cycle_at(family_repo, CycleState.APPROVED_PRINTED)
        from schemas.family import VisibilityDefaults
        from services.cycle import (
            advance_to_answers_entered,
            advance_to_auto_marked,
            advance_to_parent_review_marks,
            publish_marks,
        )

        advance_to_answers_entered(family_repo, cycle_id)
        advance_to_auto_marked(family_repo, cycle_id)
        advance_to_parent_review_marks(family_repo, cycle_id)
        publish_marks(family_repo, cycle_id, VisibilityDefaults())

        resp = client.post(
            f"/cycles/{cycle_id}/child-session",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["scope"] == "results"

    @pytest.mark.parametrize(
        "state",
        [
            CycleState.SCOPE_UPLOADED,
            CycleState.GENERATING_A,
            CycleState.PARENT_REVIEWS_DRAFT,
        ],
    )
    def test_mint_returns_409_at_other_phases(self, mint_client: Any, state: CycleState) -> None:
        client, family_repo = mint_client
        cycle_id = _bootstrap_cycle_at(family_repo, state)
        resp = client.post(
            f"/cycles/{cycle_id}/child-session",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 409

    def test_mint_returns_404_for_nonexistent_cycle(self, mint_client: Any) -> None:
        client, family_repo = mint_client
        # Caller must have a family (else _resolve_family_id 409s first) —
        # bootstrap one, then request a cycle_id that doesn't exist in it.
        family_repo.bootstrap_family("Mint Family", None, None)
        resp = client.post(
            f"/cycles/{uuid.uuid4()}/child-session",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 404

    def test_mint_requires_auth(self, mint_client: Any) -> None:
        client, family_repo = mint_client
        cycle_id = _bootstrap_cycle_at(family_repo, CycleState.APPROVED_PRINTED)
        resp = client.post(f"/cycles/{cycle_id}/child-session")
        assert resp.status_code == 401

    def test_mint_ignores_child_session_header(self, mint_client: Any) -> None:
        """The mint endpoint uses get_identity only — a kiosk token cannot
        mint another kiosk token, even if presented alongside no other creds."""
        client, family_repo = mint_client
        cycle_id = _bootstrap_cycle_at(family_repo, CycleState.APPROVED_PRINTED)
        resp = client.post(
            f"/cycles/{cycle_id}/child-session",
            headers={"x-child-session": "irrelevant-not-consulted"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4. Isolation matrix: kiosk token on parent endpoints, stub as kiosk header
# ---------------------------------------------------------------------------


class TestIsolationMatrix:
    def test_kiosk_token_rejected_by_mint_endpoint(self, mint_client: Any) -> None:
        client, family_repo = mint_client
        cycle_id = _bootstrap_cycle_at(family_repo, CycleState.APPROVED_PRINTED)
        settings = get_settings()
        secret = resolve_kiosk_secret(settings)
        token, _ = mint_kiosk_token(
            secret=secret,
            user_id=_USER_ID,
            cycle_id=cycle_id,
            child_id=uuid.uuid4(),
            family_id=uuid.uuid4(),
            scope="capture",
        )
        resp = client.post(
            f"/cycles/{cycle_id}/child-session",
            headers={"x-child-session": token},
        )
        assert resp.status_code == 401

    def test_kiosk_token_rejected_by_list_cycles(self, mint_client: Any) -> None:
        client, family_repo = mint_client
        cycle_id = _bootstrap_cycle_at(family_repo, CycleState.APPROVED_PRINTED)
        settings = get_settings()
        secret = resolve_kiosk_secret(settings)
        token, _ = mint_kiosk_token(
            secret=secret,
            user_id=_USER_ID,
            cycle_id=cycle_id,
            child_id=uuid.uuid4(),
            family_id=uuid.uuid4(),
            scope="capture",
        )
        resp = client.get("/cycles", headers={"x-child-session": token})
        assert resp.status_code == 401

    def test_kiosk_token_rejected_by_approve_endpoint(self, mint_client: Any) -> None:
        client, family_repo = mint_client
        cycle_id = _bootstrap_cycle_at(family_repo, CycleState.PARENT_REVIEWS_DRAFT)
        settings = get_settings()
        secret = resolve_kiosk_secret(settings)
        token, _ = mint_kiosk_token(
            secret=secret,
            user_id=_USER_ID,
            cycle_id=cycle_id,
            child_id=uuid.uuid4(),
            family_id=uuid.uuid4(),
            scope="capture",
        )
        resp = client.post(
            f"/cycles/{cycle_id}/approve",
            json={},
            headers={"x-child-session": token},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5. DB tier — tenancy proof (skipped cleanly when Postgres unreachable)
# ---------------------------------------------------------------------------

_DSN = os.environ.get("STUDYPAL_DB_DSN", "postgresql://studypal:studypal@localhost:5432/studypal")


def _db_available() -> bool:
    try:
        conn = psycopg.connect(_DSN, connect_timeout=3, autocommit=True)
        conn.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _db_available(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)
class TestKioskTenancyDBTier:
    """Full round trip through the real API + real Postgres.

    Proves: (a) a kiosk request resolves data via the SAME family_members
    RLS join as a parent request (mint -> capture succeeds end to end);
    (b) a validly-signed token whose family_id/cycle_id/child_id claims
    point at ANOTHER family's cycle still cannot reach it, because tenancy
    is resolved from the token's `sub` (user_id) via RLS — never from the
    token's own assertion claims.
    """

    @pytest.fixture()
    def owner_conn(self) -> Any:
        conn = psycopg.connect(_DSN, autocommit=False)
        yield conn
        conn.close()

    def _cleanup_family(self, owner_conn: Any, family_id: str) -> None:
        cur = owner_conn.cursor()
        cur.execute("DELETE FROM assessments WHERE family_id = %s", (family_id,))
        cur.execute("DELETE FROM cycles WHERE family_id = %s", (family_id,))
        cur.execute("DELETE FROM subjects WHERE family_id = %s", (family_id,))
        cur.execute("DELETE FROM children WHERE family_id = %s", (family_id,))
        cur.execute("DELETE FROM family_members WHERE family_id = %s", (family_id,))
        cur.execute("DELETE FROM families WHERE id = %s", (family_id,))
        owner_conn.commit()

    def _create_cycle_at_printed(self, client: TestClient, user_id: uuid.UUID) -> tuple[str, str]:
        """Bootstrap a family+child+subject+cycle for user_id and advance to
        PRINTED via the real endpoints. Returns (cycle_id, child_id)."""
        headers = {"x-user-id": str(user_id)}
        boot = client.post(
            "/families",
            json={"family_name": "DB Kiosk Family", "child_name": "Kid", "grade_label": "Grade 5"},
            headers=headers,
        )
        assert boot.status_code == 201, boot.text
        child_id = boot.json()["child_id"]

        subj = client.post(
            "/subjects",
            json={"child_id": child_id, "name": "Maths", "content_language": "en"},
            headers=headers,
        )
        assert subj.status_code == 201, subj.text
        subject_id = subj.json()["id"]

        cyc = client.post(
            "/cycles",
            json={"subject_id": subject_id, "scope_text": "Grade 5 fractions"},
            headers=headers,
        )
        assert cyc.status_code == 201, cyc.text
        cycle_id = cyc.json()["id"]

        gen = client.post(
            f"/cycles/{cycle_id}/generate",
            json={"cycle_id": cycle_id, "scope_text": "Grade 5 fractions"},
            headers=headers,
        )
        assert gen.status_code == 201, gen.text

        appr = client.post(f"/cycles/{cycle_id}/approve", json={}, headers=headers)
        assert appr.status_code == 200, appr.text

        return cycle_id, child_id

    def test_kiosk_mint_and_capture_round_trip(self, owner_conn: Any) -> None:
        user_id = uuid.uuid4()
        client = TestClient(app, raise_server_exceptions=True)
        cycle_id, _child_id = self._create_cycle_at_printed(client, user_id)

        # Get the family_id for cleanup.
        get_resp = client.get(f"/cycles/{cycle_id}", headers={"x-user-id": str(user_id)})
        family_id = get_resp.json()["family_id"]

        try:
            mint = client.post(
                f"/cycles/{cycle_id}/child-session",
                headers={"x-user-id": str(user_id)},
            )
            assert mint.status_code == 201, mint.text
            assert mint.json()["scope"] == "capture"
            token = mint.json()["token"]

            capture = client.get(
                f"/cycles/{cycle_id}/capture",
                headers={"x-child-session": token},
            )
            assert capture.status_code == 200, capture.text
        finally:
            self._cleanup_family(owner_conn, family_id)

    def test_swapped_family_claims_cannot_reach_another_family(self, owner_conn: Any) -> None:
        """A validly-signed token whose family_id/cycle_id/child_id claims
        point at family B's cycle, but whose sub is family A's user, must
        NOT be able to reach family B's cycle — the user_id RLS join is
        authoritative, the token's own claims are inert for tenancy."""
        client = TestClient(app, raise_server_exceptions=True)

        user_a = uuid.uuid4()
        user_b = uuid.uuid4()
        cycle_a, child_a = self._create_cycle_at_printed(client, user_a)
        cycle_b, child_b = self._create_cycle_at_printed(client, user_b)

        family_a = client.get(f"/cycles/{cycle_a}", headers={"x-user-id": str(user_a)}).json()[
            "family_id"
        ]
        family_b = client.get(f"/cycles/{cycle_b}", headers={"x-user-id": str(user_b)}).json()[
            "family_id"
        ]

        try:
            settings = get_settings()
            secret = resolve_kiosk_secret(settings)
            # Forged token: sub=user_a (family A's own credential), but every
            # assertion claim points at family B's cycle/child/family.
            forged_token, _ = mint_kiosk_token(
                secret=secret,
                user_id=user_a,
                cycle_id=uuid.UUID(cycle_b),
                child_id=uuid.UUID(child_b),
                family_id=uuid.UUID(family_b),
                scope="capture",
            )

            resp = client.get(
                f"/cycles/{cycle_b}/capture",
                headers={"x-child-session": forged_token},
            )
            # family A's RLS-scoped connection cannot see family B's cycle at
            # all — 404, never a successful read of family B's data.
            assert resp.status_code == 404, resp.text
        finally:
            self._cleanup_family(owner_conn, family_a)
            self._cleanup_family(owner_conn, family_b)

    def test_non_owner_cannot_mint_for_another_familys_cycle(self, owner_conn: Any) -> None:
        """A parent from family B cannot mint a kiosk session for family A's
        cycle_id (RLS makes it invisible -> 404, never a token)."""
        client = TestClient(app, raise_server_exceptions=True)
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()
        cycle_a, _child_a = self._create_cycle_at_printed(client, user_a)
        # Bootstrap user_b's own (unrelated) family so they have SOME family
        # (mint's _resolve_family_id would otherwise 409, not 404).
        client.post(
            "/families",
            json={"family_name": "Family B"},
            headers={"x-user-id": str(user_b)},
        )

        family_a = client.get(f"/cycles/{cycle_a}", headers={"x-user-id": str(user_a)}).json()[
            "family_id"
        ]
        try:
            resp = client.post(
                f"/cycles/{cycle_a}/child-session",
                headers={"x-user-id": str(user_b)},
            )
            assert resp.status_code == 404
        finally:
            self._cleanup_family(owner_conn, family_a)
            cur = owner_conn.cursor()
            cur.execute("SELECT family_id FROM family_members WHERE user_id = %s", (str(user_b),))
            row = cur.fetchone()
            if row is not None:
                self._cleanup_family(owner_conn, str(row[0]))
