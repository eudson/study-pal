"""Auth stub (B2) tests.

Verifies deny-by-default and correct identity extraction from the
``X-User-Id`` header (PR-1 seam).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from config import get_settings
from services.auth import identity_from_stub_header

# ---------------------------------------------------------------------------
# Unit tests for the stub identity parser (credential-free path)
# ---------------------------------------------------------------------------


class TestGetIdentityUnit:
    def test_valid_header_returns_identity(self) -> None:
        user_id = uuid.uuid4()
        family_id = uuid.uuid4()
        header = f"{user_id}/{family_id}"
        identity = identity_from_stub_header(header, get_settings())
        assert identity.user_id == user_id
        assert identity.family_id == family_id

    def test_missing_header_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            identity_from_stub_header(None, get_settings())
        assert exc.value.status_code == 401

    def test_malformed_header_no_slash_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            identity_from_stub_header(str(uuid.uuid4()), get_settings())
        assert exc.value.status_code == 401

    def test_invalid_uuid_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            identity_from_stub_header("not-a-uuid/also-not-a-uuid", get_settings())
        assert exc.value.status_code == 401

    def test_three_slash_parts_raises_401(self) -> None:
        """Three segments (extra slash) is rejected."""
        with pytest.raises(HTTPException) as exc:
            identity_from_stub_header(
                f"{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}",
                get_settings(),
            )
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# HTTP integration tests (via TestClient + overridden dependency)
# ---------------------------------------------------------------------------


def _make_header(user_id: uuid.UUID | None = None, family_id: uuid.UUID | None = None) -> str:
    u = user_id or uuid.uuid4()
    f = family_id or uuid.uuid4()
    return f"{u}/{f}"


class TestGenerateEndpointAuth:
    """POST /assessments/generate requires valid identity header."""

    @pytest.fixture()
    def client_with_fake_repo(self) -> Generator[TestClient, None, None]:
        """TestClient that overrides the repo dependency to avoid Postgres."""
        from dependencies import get_assessment_repository
        from main import app
        from services.repositories.memory import InMemoryAssessmentRepository

        repo = InMemoryAssessmentRepository()

        def _fake_repo() -> InMemoryAssessmentRepository:
            return repo

        app.dependency_overrides[get_assessment_repository] = _fake_repo
        client = TestClient(app, raise_server_exceptions=True)
        yield client
        app.dependency_overrides.pop(get_assessment_repository, None)

    def test_missing_identity_returns_401(self, client_with_fake_repo: TestClient) -> None:
        resp = client_with_fake_repo.post(
            "/assessments/generate",
            json={"cycle_id": str(uuid.uuid4()), "scope_text": "Grade 5 Maths"},
        )
        assert resp.status_code == 401

    def test_valid_identity_returns_201(self, client_with_fake_repo: TestClient) -> None:
        resp = client_with_fake_repo.post(
            "/assessments/generate",
            json={"cycle_id": str(uuid.uuid4()), "scope_text": "Grade 5 Maths"},
            headers={"x-user-id": _make_header()},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert body["assessment"] is not None

    def test_malformed_identity_returns_401(self, client_with_fake_repo: TestClient) -> None:
        resp = client_with_fake_repo.post(
            "/assessments/generate",
            json={"cycle_id": str(uuid.uuid4()), "scope_text": "Grade 5 Maths"},
            headers={"x-user-id": "bad-value"},
        )
        assert resp.status_code == 401
