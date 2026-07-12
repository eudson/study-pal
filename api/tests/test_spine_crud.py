"""Unit tests for the spine CRUD endpoints (families, children, subjects, cycles).

Uses InMemoryFamilyRepository — no Postgres required for this tier.
State-machine transition tests are also included here (no DB needed).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dependencies import get_assessment_repository, get_family_repository
from main import app
from schemas.family import CycleState
from services.cycle import (
    IllegalTransitionError,
    advance_to_generating,
    advance_to_parent_reviews,
    approve_draft,
)
from services.repositories.base import FamilyRepository
from services.repositories.memory import InMemoryAssessmentRepository, InMemoryFamilyRepository

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_STUB_HEADER = str(_USER_ID)  # no family yet — single-UUID format


def _make_family_repo() -> InMemoryFamilyRepository:
    return InMemoryFamilyRepository(user_id=_USER_ID)


@pytest.fixture()
def client_no_family() -> Generator[TestClient, None, None]:
    """TestClient where the user has no family yet."""
    family_repo = _make_family_repo()
    asmt_repo = InMemoryAssessmentRepository()

    def _family() -> FamilyRepository:
        return family_repo

    def _asmt() -> InMemoryAssessmentRepository:
        return asmt_repo

    app.dependency_overrides[get_family_repository] = _family
    app.dependency_overrides[get_assessment_repository] = _asmt
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_family_repository, None)
    app.dependency_overrides.pop(get_assessment_repository, None)


@pytest.fixture()
def client_with_family() -> Generator[tuple[TestClient, dict[str, Any]], None, None]:
    """TestClient where the user already has a bootstrapped family + child + subject."""
    family_repo = _make_family_repo()
    asmt_repo = InMemoryAssessmentRepository()

    # Bootstrap inline so we have ids to return.
    family, child_id = family_repo.bootstrap_family("Smith Family", "Alice", "Grade 5")
    assert child_id is not None
    subject = family_repo.create_subject(family.id, child_id, "Mathematics", "en")

    ids = {
        "family_id": str(family.id),
        "child_id": str(child_id),
        "subject_id": str(subject.id),
    }

    def _family() -> FamilyRepository:
        return family_repo

    def _asmt() -> InMemoryAssessmentRepository:
        return asmt_repo

    app.dependency_overrides[get_family_repository] = _family
    app.dependency_overrides[get_assessment_repository] = _asmt
    with TestClient(app) as c:
        yield c, ids
    app.dependency_overrides.pop(get_family_repository, None)
    app.dependency_overrides.pop(get_assessment_repository, None)


# ---------------------------------------------------------------------------
# Bootstrap / families
# ---------------------------------------------------------------------------


class TestBootstrapFamily:
    def test_bootstrap_creates_family_and_child(self, client_no_family: TestClient) -> None:
        resp = client_no_family.post(
            "/families",
            json={"family_name": "Jones Family", "child_name": "Bob", "grade_label": "Grade 3"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["family"]["name"] == "Jones Family"
        assert body["child_id"] is not None

    def test_bootstrap_without_child(self, client_no_family: TestClient) -> None:
        resp = client_no_family.post(
            "/families",
            json={"family_name": "Solo Family"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["family"]["name"] == "Solo Family"
        assert body["child_id"] is None

    def test_bootstrap_is_idempotent(self, client_no_family: TestClient) -> None:
        """Second call returns the existing family, no error."""
        for _ in range(2):
            resp = client_no_family.post(
                "/families",
                json={"family_name": "Dup Family"},
                headers={"x-user-id": _STUB_HEADER},
            )
            assert resp.status_code == 201

        resp1 = client_no_family.get("/families", headers={"x-user-id": _STUB_HEADER})
        assert resp1.status_code == 200
        assert len(resp1.json()) == 1

    def test_bootstrap_requires_identity(self, client_no_family: TestClient) -> None:
        resp = client_no_family.post(
            "/families",
            json={"family_name": "No Auth"},
        )
        assert resp.status_code == 401

    def test_bootstrap_child_name_without_grade_label_rejected(
        self, client_no_family: TestClient
    ) -> None:
        resp = client_no_family.post(
            "/families",
            json={"family_name": "X", "child_name": "Kid"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 422

    def test_list_families(self, client_no_family: TestClient) -> None:
        client_no_family.post(
            "/families",
            json={"family_name": "Listed Family"},
            headers={"x-user-id": _STUB_HEADER},
        )
        resp = client_no_family.get("/families", headers={"x-user-id": _STUB_HEADER})
        assert resp.status_code == 200
        assert any(f["name"] == "Listed Family" for f in resp.json())


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


class TestChildren:
    def test_create_child(self, client_with_family: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_with_family
        resp = client.post(
            "/children",
            json={"display_name": "Charlie", "grade_label": "Grade 2"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["display_name"] == "Charlie"
        assert body["family_id"] == ids["family_id"]

    def test_list_children(self, client_with_family: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_with_family
        resp = client.get("/children", headers={"x-user-id": _STUB_HEADER})
        assert resp.status_code == 200
        # Alice was created during bootstrap fixture.
        names = [c["display_name"] for c in resp.json()]
        assert "Alice" in names

    def test_create_child_no_family_returns_409(self, client_no_family: TestClient) -> None:
        resp = client_no_family.post(
            "/children",
            json={"display_name": "Orphan", "grade_label": "Grade 1"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------


class TestSubjects:
    def test_create_subject(self, client_with_family: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_with_family
        resp = client.post(
            "/subjects",
            json={
                "child_id": ids["child_id"],
                "name": "Science",
                "content_language": "en",
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Science"
        assert body["content_language"] == "en"

    def test_list_subjects(self, client_with_family: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_with_family
        resp = client.get("/subjects", headers={"x-user-id": _STUB_HEADER})
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()]
        assert "Mathematics" in names

    def test_invalid_language_code_rejected(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        resp = client.post(
            "/subjects",
            json={
                "child_id": ids["child_id"],
                "name": "Art",
                "content_language": "ENGLISH",  # not 2-3 lowercase letters
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Cycles — CRUD
# ---------------------------------------------------------------------------


class TestCycles:
    def test_create_cycle(self, client_with_family: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_with_family
        resp = client.post(
            "/cycles",
            json={"subject_id": ids["subject_id"], "scope_text": "Grade 5 fractions"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["state"] == "SCOPE_UPLOADED"
        assert body["scope_text"] == "Grade 5 fractions"

    def test_get_cycle(self, client_with_family: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_with_family
        create_resp = client.post(
            "/cycles",
            json={"subject_id": ids["subject_id"], "scope_text": "Fractions"},
            headers={"x-user-id": _STUB_HEADER},
        )
        cycle_id = create_resp.json()["id"]
        get_resp = client.get(f"/cycles/{cycle_id}", headers={"x-user-id": _STUB_HEADER})
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == cycle_id

    def test_get_nonexistent_cycle_returns_404(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, _ = client_with_family
        resp = client.get(f"/cycles/{uuid.uuid4()}", headers={"x-user-id": _STUB_HEADER})
        assert resp.status_code == 404

    def test_list_cycles(self, client_with_family: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_with_family
        client.post(
            "/cycles",
            json={"subject_id": ids["subject_id"], "scope_text": "Geometry"},
            headers={"x-user-id": _STUB_HEADER},
        )
        resp = client.get("/cycles", headers={"x-user-id": _STUB_HEADER})
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


# ---------------------------------------------------------------------------
# Cycle approval
# ---------------------------------------------------------------------------


class TestCycleApproval:
    def _create_and_advance_to_reviews(self, client: TestClient, ids: dict[str, Any]) -> Any:
        """Create a cycle and manually advance it to PARENT_REVIEWS_DRAFT."""
        create_resp = client.post(
            "/cycles",
            json={"subject_id": ids["subject_id"], "scope_text": "Algebra"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert create_resp.status_code == 201
        cycle_id = create_resp.json()["id"]

        # Generate advances state machine SCOPE_UPLOADED → GENERATING_A → PARENT_REVIEWS_DRAFT
        gen_resp = client.post(
            f"/cycles/{cycle_id}/generate",
            json={"cycle_id": cycle_id, "scope_text": "Algebra"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert gen_resp.status_code == 201, gen_resp.text
        return cycle_id

    def test_approve_advances_state_and_records_approval(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        cycle_id = self._create_and_advance_to_reviews(client, ids)

        approve_resp = client.post(
            f"/cycles/{cycle_id}/approve",
            json={"note": "Looks great"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert approve_resp.status_code == 200, approve_resp.text
        body = approve_resp.json()
        assert body["state"] == "APPROVED_PRINTED"
        assert body["parent_approval_at"] is not None
        assert body["parent_approval_note"] == "Looks great"

    def test_approve_without_note(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        cycle_id = self._create_and_advance_to_reviews(client, ids)
        resp = client.post(
            f"/cycles/{cycle_id}/approve",
            json={},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "APPROVED_PRINTED"
        assert resp.json()["parent_approval_at"] is not None

    def test_approve_wrong_state_returns_409(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """Cannot approve a cycle that is still in SCOPE_UPLOADED."""
        client, ids = client_with_family
        create_resp = client.post(
            "/cycles",
            json={"subject_id": ids["subject_id"], "scope_text": "Scope"},
            headers={"x-user-id": _STUB_HEADER},
        )
        cycle_id = create_resp.json()["id"]

        resp = client.post(
            f"/cycles/{cycle_id}/approve",
            json={},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 409

    def test_double_approve_returns_409(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """Approving an already-APPROVED_PRINTED cycle is illegal."""
        client, ids = client_with_family
        cycle_id = self._create_and_advance_to_reviews(client, ids)
        client.post(
            f"/cycles/{cycle_id}/approve",
            json={},
            headers={"x-user-id": _STUB_HEADER},
        )
        resp = client.post(
            f"/cycles/{cycle_id}/approve",
            json={},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Generation wires into state machine
# ---------------------------------------------------------------------------


class TestGenerateAdvancesState:
    def test_generate_advances_cycle_to_parent_reviews(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        create_resp = client.post(
            "/cycles",
            json={"subject_id": ids["subject_id"], "scope_text": "Fractions test"},
            headers={"x-user-id": _STUB_HEADER},
        )
        cycle_id = create_resp.json()["id"]
        assert create_resp.json()["state"] == "SCOPE_UPLOADED"

        gen_resp = client.post(
            f"/cycles/{cycle_id}/generate",
            json={"cycle_id": cycle_id, "scope_text": "Fractions test"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert gen_resp.status_code == 201, gen_resp.text
        assert gen_resp.json()["ok"] is True

        get_resp = client.get(f"/cycles/{cycle_id}", headers={"x-user-id": _STUB_HEADER})
        assert get_resp.json()["state"] == "PARENT_REVIEWS_DRAFT"

    def test_generate_mismatched_cycle_id_rejected(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        create_resp = client.post(
            "/cycles",
            json={"subject_id": ids["subject_id"], "scope_text": "Scope"},
            headers={"x-user-id": _STUB_HEADER},
        )
        cycle_id = create_resp.json()["id"]

        gen_resp = client.post(
            f"/cycles/{cycle_id}/generate",
            json={"cycle_id": str(uuid.uuid4()), "scope_text": "Scope"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert gen_resp.status_code == 422

    def test_generate_already_generating_returns_409(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """Calling generate twice on the same cycle is an illegal transition."""
        client, ids = client_with_family
        create_resp = client.post(
            "/cycles",
            json={"subject_id": ids["subject_id"], "scope_text": "Scope"},
            headers={"x-user-id": _STUB_HEADER},
        )
        cycle_id = create_resp.json()["id"]

        # First generate succeeds.
        client.post(
            f"/cycles/{cycle_id}/generate",
            json={"cycle_id": cycle_id, "scope_text": "Scope"},
            headers={"x-user-id": _STUB_HEADER},
        )

        # Second generate should be rejected (cycle is now PARENT_REVIEWS_DRAFT,
        # not SCOPE_UPLOADED).
        resp = client.post(
            f"/cycles/{cycle_id}/generate",
            json={"cycle_id": cycle_id, "scope_text": "Scope"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# State-machine unit tests (no HTTP)
# ---------------------------------------------------------------------------


class TestCycleStateMachine:
    def _make_repo(self) -> InMemoryFamilyRepository:
        return InMemoryFamilyRepository(user_id=uuid.uuid4())

    def _bootstrap(self, repo: InMemoryFamilyRepository) -> tuple[uuid.UUID, uuid.UUID]:
        family, child_id = repo.bootstrap_family("F", "C", "G1")
        assert child_id is not None
        subj = repo.create_subject(family.id, child_id, "Math", "en")
        cycle = repo.create_cycle(family.id, subj.id, "scope")
        return family.id, cycle.id

    def test_scope_uploaded_to_generating(self) -> None:
        repo = self._make_repo()
        _, cycle_id = self._bootstrap(repo)
        result = advance_to_generating(repo, cycle_id)
        assert result.state == CycleState.GENERATING_A

    def test_generating_to_parent_reviews(self) -> None:
        repo = self._make_repo()
        _, cycle_id = self._bootstrap(repo)
        advance_to_generating(repo, cycle_id)
        result = advance_to_parent_reviews(repo, cycle_id)
        assert result.state == CycleState.PARENT_REVIEWS_DRAFT

    def test_approve_draft_records_timestamp_and_note(self) -> None:
        repo = self._make_repo()
        _, cycle_id = self._bootstrap(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        result = approve_draft(repo, cycle_id, note="all good")
        assert result.state == CycleState.APPROVED_PRINTED
        assert result.parent_approval_at is not None
        assert result.parent_approval_note == "all good"

    def test_illegal_transition_raises(self) -> None:
        repo = self._make_repo()
        _, cycle_id = self._bootstrap(repo)
        # Jump straight to approve — illegal.
        with pytest.raises(IllegalTransitionError):
            approve_draft(repo, cycle_id)

    def test_illegal_transition_skip_state(self) -> None:
        repo = self._make_repo()
        _, cycle_id = self._bootstrap(repo)
        advance_to_generating(repo, cycle_id)
        # Try to go directly to APPROVED_PRINTED without PARENT_REVIEWS_DRAFT.
        with pytest.raises(IllegalTransitionError):
            approve_draft(repo, cycle_id)

    def test_approve_records_approval_without_note(self) -> None:
        repo = self._make_repo()
        _, cycle_id = self._bootstrap(repo)
        advance_to_generating(repo, cycle_id)
        advance_to_parent_reviews(repo, cycle_id)
        result = approve_draft(repo, cycle_id)
        assert result.state == CycleState.APPROVED_PRINTED
        assert result.parent_approval_at is not None
        assert result.parent_approval_note is None
