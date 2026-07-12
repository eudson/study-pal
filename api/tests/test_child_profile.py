"""Tests for child profile endpoints: update, archive, visibility_defaults.

Tier 1 — unit tests using InMemoryFamilyRepository (no Postgres required).
Tier 2 — DB integration tests (skipped when Postgres is unreachable).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dependencies import get_assessment_repository, get_family_repository
from main import app
from schemas.family import ChildUpdate, VisibilityDefaults
from services.repositories.base import FamilyRepository
from services.repositories.memory import InMemoryAssessmentRepository, InMemoryFamilyRepository

# ---------------------------------------------------------------------------
# Shared helpers / constants
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_STUB_HEADER = str(_USER_ID)


def _make_family_repo() -> InMemoryFamilyRepository:
    return InMemoryFamilyRepository(user_id=_USER_ID)


@pytest.fixture()
def client_with_family() -> Generator[tuple[TestClient, dict[str, Any]], None, None]:
    """TestClient with a bootstrapped family + one child."""
    family_repo = _make_family_repo()
    asmt_repo = InMemoryAssessmentRepository()

    family, child_id = family_repo.bootstrap_family("Test Family", "Alice", "Grade 4")
    assert child_id is not None

    ids: dict[str, Any] = {
        "family_id": str(family.id),
        "child_id": str(child_id),
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


# ===========================================================================
# TIER 1 — InMemory unit tests (no DB)
# ===========================================================================


class TestVisibilityDefaultsModel:
    """VisibilityDefaults schema — defaults and round-trip."""

    def test_standard_defaults(self) -> None:
        vd = VisibilityDefaults()
        assert vd.accuracy is True
        assert vd.effort is True
        assert vd.growing is True
        assert vd.ai_rationale is False

    def test_custom_values_round_trip(self) -> None:
        vd = VisibilityDefaults(accuracy=False, effort=False, growing=True, ai_rationale=True)
        dumped = vd.model_dump()
        loaded = VisibilityDefaults.model_validate(dumped)
        assert loaded == vd

    def test_json_round_trip(self) -> None:
        vd = VisibilityDefaults(ai_rationale=True)
        as_json = vd.model_dump_json()
        loaded = VisibilityDefaults.model_validate_json(as_json)
        assert loaded.ai_rationale is True
        assert loaded.accuracy is True


class TestCreateChildWithVisibility:
    """POST /children accepts and persists visibility_defaults."""

    def test_create_child_default_visibility(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, _ = client_with_family
        resp = client.post(
            "/children",
            json={"display_name": "Bob", "grade_label": "Grade 3"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201
        body = resp.json()
        vd = body["visibility_defaults"]
        assert vd["accuracy"] is True
        assert vd["effort"] is True
        assert vd["growing"] is True
        assert vd["ai_rationale"] is False

    def test_create_child_custom_visibility(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, _ = client_with_family
        resp = client.post(
            "/children",
            json={
                "display_name": "Charlie",
                "grade_label": "Grade 5",
                "visibility_defaults": {
                    "accuracy": True,
                    "effort": False,
                    "growing": True,
                    "ai_rationale": True,
                },
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201
        body = resp.json()
        vd = body["visibility_defaults"]
        assert vd["effort"] is False
        assert vd["ai_rationale"] is True

    def test_child_response_includes_new_fields(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """archived_at is None and visibility_defaults is present on a fresh child."""
        client, ids = client_with_family
        resp = client.get("/children", headers={"x-user-id": _STUB_HEADER})
        assert resp.status_code == 200
        child = next(c for c in resp.json() if c["id"] == ids["child_id"])
        assert child["archived_at"] is None
        assert "visibility_defaults" in child


class TestUpdateChild:
    """PATCH /children/{id} — partial update semantics."""

    def test_update_display_name(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        resp = client.patch(
            f"/children/{ids['child_id']}",
            json={"display_name": "Alicia"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Alicia"

    def test_update_grade_label(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        resp = client.patch(
            f"/children/{ids['child_id']}",
            json={"grade_label": "Grade 5"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 200
        assert resp.json()["grade_label"] == "Grade 5"

    def test_update_visibility_defaults(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        resp = client.patch(
            f"/children/{ids['child_id']}",
            json={
                "visibility_defaults": {
                    "accuracy": True,
                    "effort": True,
                    "growing": False,
                    "ai_rationale": True,
                }
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 200
        vd = resp.json()["visibility_defaults"]
        assert vd["growing"] is False
        assert vd["ai_rationale"] is True

    def test_update_partial_only_changes_named_fields(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """Sending only display_name must not alter grade_label."""
        client, ids = client_with_family
        # Verify baseline grade_label from bootstrap.
        before = client.get("/children", headers={"x-user-id": _STUB_HEADER}).json()
        original_grade = next(c for c in before if c["id"] == ids["child_id"])["grade_label"]

        resp = client.patch(
            f"/children/{ids['child_id']}",
            json={"display_name": "Renamed"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "Renamed"
        assert body["grade_label"] == original_grade

    def test_update_empty_body_returns_200(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """Empty PATCH body is legal (no-op)."""
        client, ids = client_with_family
        resp = client.patch(
            f"/children/{ids['child_id']}",
            json={},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 200

    def test_update_nonexistent_child_returns_404(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, _ = client_with_family
        resp = client.patch(
            f"/children/{uuid.uuid4()}",
            json={"display_name": "Ghost"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 404


class TestArchiveChild:
    """POST /children/{id}/archive — soft-delete."""

    def test_archive_hides_child_from_list(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_with_family
        # Create a second child to archive.
        create_resp = client.post(
            "/children",
            json={"display_name": "ToArchive", "grade_label": "Grade 1"},
            headers={"x-user-id": _STUB_HEADER},
        )
        assert create_resp.status_code == 201
        archive_id = create_resp.json()["id"]

        # Archive it.
        archive_resp = client.post(
            f"/children/{archive_id}/archive",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert archive_resp.status_code == 200
        body = archive_resp.json()
        assert body["archived_at"] is not None

        # Must not appear in list.
        list_resp = client.get("/children", headers={"x-user-id": _STUB_HEADER})
        ids_in_list = [c["id"] for c in list_resp.json()]
        assert archive_id not in ids_in_list

    def test_archive_returns_archived_at_timestamp(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, _ = client_with_family
        create_resp = client.post(
            "/children",
            json={"display_name": "TimestampKid", "grade_label": "Grade 2"},
            headers={"x-user-id": _STUB_HEADER},
        )
        cid = create_resp.json()["id"]
        resp = client.post(f"/children/{cid}/archive", headers={"x-user-id": _STUB_HEADER})
        assert resp.status_code == 200
        assert resp.json()["archived_at"] is not None

    def test_archive_nonexistent_returns_404(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, _ = client_with_family
        resp = client.post(
            f"/children/{uuid.uuid4()}/archive",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 404

    def test_archive_already_archived_returns_404(
        self, client_with_family: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """Archiving an already-archived child raises 404 (idempotent guard)."""
        client, _ = client_with_family
        create_resp = client.post(
            "/children",
            json={"display_name": "DoubleArchive", "grade_label": "Grade 3"},
            headers={"x-user-id": _STUB_HEADER},
        )
        cid = create_resp.json()["id"]
        client.post(f"/children/{cid}/archive", headers={"x-user-id": _STUB_HEADER})
        resp = client.post(f"/children/{cid}/archive", headers={"x-user-id": _STUB_HEADER})
        assert resp.status_code == 404


class TestInMemoryDirectRepo:
    """Direct InMemory repo method tests (no HTTP layer)."""

    def _repo(self) -> InMemoryFamilyRepository:
        return InMemoryFamilyRepository(user_id=uuid.uuid4())

    def _bootstrap(self, repo: InMemoryFamilyRepository) -> tuple[uuid.UUID, uuid.UUID]:
        family, child_id = repo.bootstrap_family("F", "Kid", "G1")
        assert child_id is not None
        return family.id, child_id

    def test_create_child_with_visibility(self) -> None:
        repo = self._repo()
        family_id, _ = self._bootstrap(repo)
        vd = VisibilityDefaults(ai_rationale=True, growing=False)
        child = repo.create_child(family_id, "NewKid", "Grade 3", vd)
        assert child.visibility_defaults.ai_rationale is True
        assert child.visibility_defaults.growing is False

    def test_update_partial_only_named_fields(self) -> None:
        repo = self._repo()
        family_id, child_id = self._bootstrap(repo)
        original = repo.list_children(family_id)[0]

        updated = repo.update_child(child_id, ChildUpdate(display_name="Renamed"))
        assert updated.display_name == "Renamed"
        assert updated.grade_label == original.grade_label
        assert updated.visibility_defaults == original.visibility_defaults

    def test_archive_hides_from_list(self) -> None:
        repo = self._repo()
        family_id, child_id = self._bootstrap(repo)
        assert len(repo.list_children(family_id)) == 1

        repo.archive_child(child_id)
        assert len(repo.list_children(family_id)) == 0

    def test_archive_sets_archived_at(self) -> None:
        repo = self._repo()
        _, child_id = self._bootstrap(repo)
        archived = repo.archive_child(child_id)
        assert archived.archived_at is not None

    def test_update_not_found_raises(self) -> None:
        repo = self._repo()
        with pytest.raises(ValueError, match="not found"):
            repo.update_child(uuid.uuid4(), ChildUpdate(display_name="X"))

    def test_archive_not_found_raises(self) -> None:
        repo = self._repo()
        with pytest.raises(ValueError, match="not found"):
            repo.archive_child(uuid.uuid4())


# ===========================================================================
# TIER 2 — Postgres DB integration tests
# ===========================================================================

_DSN = os.environ.get("STUDYPAL_DB_DSN", "postgresql://studypal:studypal@localhost:5432/studypal")


def _try_connect_pg() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(_DSN, connect_timeout=3, autocommit=True)
        conn.close()
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _try_connect_pg(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)


@pytest.fixture(scope="module")
def _pg_owner_conn() -> Generator[Any, None, None]:
    import psycopg

    conn = psycopg.connect(_DSN, autocommit=False)
    yield conn
    conn.close()


def _pg_authed_repo(user_id: uuid.UUID) -> tuple[Any, Any]:
    """Return (conn, PostgresFamilyRepository) for an authenticated user."""
    import psycopg  # noqa: F401

    from config import get_settings
    from schemas.identity import Identity
    from services.repositories.postgres import open_authenticated_connection
    from services.repositories.postgres_family import PostgresFamilyRepository

    settings = get_settings()
    identity = Identity(user_id=user_id)
    conn = open_authenticated_connection(settings.db_dsn, identity)
    repo = PostgresFamilyRepository(conn)
    return conn, repo


@pytest.mark.skipif(not _try_connect_pg(), reason="Postgres not reachable")
class TestPostgresChildProfile:
    """DB-tier tests — require live Postgres."""

    def test_update_persists(self, _pg_owner_conn: Any) -> None:
        user_id = uuid.uuid4()
        conn, repo = _pg_authed_repo(user_id)
        family_id: uuid.UUID | None = None
        try:
            family, child_id = repo.bootstrap_family("UpdateFamily", "Kid", "Grade 3")
            family_id = family.id
            assert child_id is not None

            updated = repo.update_child(
                child_id,
                ChildUpdate(
                    display_name="Renamed",
                    grade_label="Grade 4",
                    visibility_defaults=VisibilityDefaults(ai_rationale=True, growing=False),
                ),
            )
            assert updated.display_name == "Renamed"
            assert updated.grade_label == "Grade 4"
            assert updated.visibility_defaults.ai_rationale is True
            assert updated.visibility_defaults.growing is False
        finally:
            conn.close()
            if family_id is not None:
                cur = _pg_owner_conn.cursor()
                cur.execute("DELETE FROM children WHERE family_id = %s", (str(family_id),))
                cur.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
                cur.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
                _pg_owner_conn.commit()

    def test_rls_user_x_cannot_update_user_y_child(self, _pg_owner_conn: Any) -> None:
        user_x = uuid.uuid4()
        user_y = uuid.uuid4()
        family_x_id: uuid.UUID | None = None
        family_y_id: uuid.UUID | None = None
        conn_x, repo_x = _pg_authed_repo(user_x)
        conn_y, repo_y = _pg_authed_repo(user_y)
        try:
            family_x, child_x = repo_x.bootstrap_family("FamilyX", "ChildX", "Grade 1")
            family_x_id = family_x.id
            assert child_x is not None

            family_y, child_y = repo_y.bootstrap_family("FamilyY", "ChildY", "Grade 2")
            family_y_id = family_y.id
            assert child_y is not None

            # user_x trying to update user_y's child must raise (RLS returns no row).
            with pytest.raises(ValueError):
                repo_x.update_child(child_y, ChildUpdate(display_name="Hacked"))
        finally:
            conn_x.close()
            conn_y.close()
            oc = _pg_owner_conn.cursor()
            for fid, uid in [(family_x_id, user_x), (family_y_id, user_y)]:
                if fid is not None:
                    oc.execute("DELETE FROM children WHERE family_id = %s", (str(fid),))
                    oc.execute("DELETE FROM family_members WHERE user_id = %s", (str(uid),))
                    oc.execute("DELETE FROM families WHERE id = %s", (str(fid),))
            _pg_owner_conn.commit()

    def test_archive_persists_and_child_drops_from_list(self, _pg_owner_conn: Any) -> None:
        user_id = uuid.uuid4()
        conn, repo = _pg_authed_repo(user_id)
        family_id: uuid.UUID | None = None
        try:
            family, child_id = repo.bootstrap_family("ArchiveFamily", "ToArchive", "Grade 2")
            family_id = family.id
            assert child_id is not None

            # Child is visible before archive.
            active = repo.list_children(family.id)
            assert any(c.id == child_id for c in active)

            archived = repo.archive_child(child_id)
            assert archived.archived_at is not None

            # Must drop from active list.
            active_after = repo.list_children(family.id)
            assert not any(c.id == child_id for c in active_after)
        finally:
            conn.close()
            if family_id is not None:
                cur = _pg_owner_conn.cursor()
                cur.execute("DELETE FROM children WHERE family_id = %s", (str(family_id),))
                cur.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
                cur.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
                _pg_owner_conn.commit()

    def test_visibility_defaults_round_trip_postgres(self, _pg_owner_conn: Any) -> None:
        user_id = uuid.uuid4()
        conn, repo = _pg_authed_repo(user_id)
        family_id: uuid.UUID | None = None
        try:
            family, _ = repo.bootstrap_family("VDFamily", None, None)
            family_id = family.id
            vd_in = VisibilityDefaults(
                accuracy=False, effort=True, growing=False, ai_rationale=True
            )
            child = repo.create_child(family.id, "VDKid", "Grade 5", vd_in)
            assert child.visibility_defaults.accuracy is False
            assert child.visibility_defaults.ai_rationale is True
            assert child.visibility_defaults.growing is False
        finally:
            conn.close()
            if family_id is not None:
                cur = _pg_owner_conn.cursor()
                cur.execute("DELETE FROM children WHERE family_id = %s", (str(family_id),))
                cur.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
                cur.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
                _pg_owner_conn.commit()
