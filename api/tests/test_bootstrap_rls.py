"""Bootstrap + RLS isolation tests for the spine CRUD layer.

These tests require a live Postgres instance (the docker ``db`` service).
They are skipped automatically when the DB is unreachable, so they never
block the unit-test tier.

What is proven:
1. app_bootstrap_family atomically creates family + membership (+ child) and
   returns the family_id to the caller.
2. Bootstrap is idempotent: calling it twice returns the same family.
3. A user cannot see another user's family/children/subjects/cycles.
4. The SECURITY DEFINER function works even when the user has no prior
   family_members row (solves the bootstrap deadlock).
5. approve records parent_approval_at (child-visible gate — golden rule 8).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator
from typing import Any

import psycopg
import psycopg.errors
import psycopg.rows
import pytest

_DSN = os.environ.get("STUDYPAL_DB_DSN", "postgresql://studypal:studypal@localhost:5432/studypal")


def _try_connect(dsn: str) -> psycopg.Connection[tuple[object, ...]] | None:
    try:
        return psycopg.connect(dsn, connect_timeout=3, autocommit=True)
    except Exception:
        return None


def _db_available() -> bool:
    conn = _try_connect(_DSN)
    if conn is not None:
        conn.close()
        return True
    return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)


@pytest.fixture(scope="module")
def owner_conn() -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(_DSN, autocommit=False)
    yield conn
    conn.close()


def _authed_dict_conn(user_id: uuid.UUID) -> psycopg.Connection[dict[str, Any]]:
    claims = json.dumps({"sub": str(user_id)})
    conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        _DSN,
        row_factory=psycopg.rows.dict_row,
        autocommit=False,
    )
    conn.execute("SET ROLE authenticated")
    # Session-scope (not SET LOCAL) so the claim survives conn.commit() —
    # see TestApprovalRecorded for the regression this fixes.
    conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))
    return conn


def _parse_bootstrap_result(raw: Any) -> dict[str, Any]:
    """Convert the app_bootstrap_family result to a plain dict."""
    if isinstance(raw, dict):
        return raw
    return json.loads(str(raw))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Bootstrap function
# ---------------------------------------------------------------------------


class TestBootstrapFunction:
    def test_bootstrap_creates_family_and_membership(
        self, owner_conn: psycopg.Connection[tuple[object, ...]]
    ) -> None:
        user_id = uuid.uuid4()
        conn = _authed_dict_conn(user_id)
        try:
            cur = conn.cursor()
            cur.execute("SELECT app_bootstrap_family(%s, NULL, NULL)", ("Test Family",))
            row = cur.fetchone()
            assert row is not None
            result: dict[str, Any] = _parse_bootstrap_result(row["app_bootstrap_family"])
            family_id = uuid.UUID(str(result["family_id"]))
            assert result["child_id"] is None
            conn.commit()

            # Verify family is visible under RLS.
            cur.execute("SELECT name FROM families WHERE id = %s", (str(family_id),))
            frow = cur.fetchone()
            assert frow is not None
            assert frow["name"] == "Test Family"

            # Verify membership exists.
            cur.execute(
                "SELECT family_id FROM family_members WHERE user_id = %s AND family_id = %s",
                (str(user_id), str(family_id)),
            )
            mrow = cur.fetchone()
            assert mrow is not None, "family_members row not created"
        finally:
            # Cleanup
            conn.close()
            cur2 = owner_conn.cursor()
            cur2.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
            cur2.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
            owner_conn.commit()

    def test_bootstrap_creates_child_when_provided(
        self, owner_conn: psycopg.Connection[tuple[object, ...]]
    ) -> None:
        user_id = uuid.uuid4()
        conn = _authed_dict_conn(user_id)
        family_id: uuid.UUID | None = None
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT app_bootstrap_family(%s, %s, %s)",
                ("Family With Child", "Eva", "Grade 4"),
            )
            row = cur.fetchone()
            assert row is not None
            result: dict[str, Any] = _parse_bootstrap_result(row["app_bootstrap_family"])
            family_id = uuid.UUID(str(result["family_id"]))
            child_id = uuid.UUID(str(result["child_id"]))
            conn.commit()

            cur.execute("SELECT display_name FROM children WHERE id = %s", (str(child_id),))
            crow = cur.fetchone()
            assert crow is not None
            assert crow["display_name"] == "Eva"
        finally:
            conn.close()
            if family_id is not None:
                cur2 = owner_conn.cursor()
                cur2.execute("DELETE FROM children WHERE family_id = %s", (str(family_id),))
                cur2.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
                cur2.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
                owner_conn.commit()

    def test_bootstrap_is_idempotent(
        self, owner_conn: psycopg.Connection[tuple[object, ...]]
    ) -> None:
        user_id = uuid.uuid4()
        conn = _authed_dict_conn(user_id)
        family_id: uuid.UUID | None = None
        try:
            cur = conn.cursor()
            cur.execute("SELECT app_bootstrap_family(%s, NULL, NULL)", ("Idem Family",))
            row1 = cur.fetchone()
            assert row1 is not None
            r1: dict[str, Any] = _parse_bootstrap_result(row1["app_bootstrap_family"])
            family_id = uuid.UUID(str(r1["family_id"]))
            conn.commit()

            # Second call with different name — should return the same family.
            cur.execute("SELECT app_bootstrap_family(%s, NULL, NULL)", ("Different Name",))
            row2 = cur.fetchone()
            assert row2 is not None
            r2: dict[str, Any] = _parse_bootstrap_result(row2["app_bootstrap_family"])
            family_id_2 = uuid.UUID(str(r2["family_id"]))
            conn.commit()

            assert family_id == family_id_2, "Bootstrap returned different family on second call"
        finally:
            conn.close()
            if family_id is not None:
                cur2 = owner_conn.cursor()
                cur2.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
                cur2.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
                owner_conn.commit()

    def test_bootstrap_without_identity_raises(self) -> None:
        """Calling the function without auth.uid() set must raise."""
        conn: psycopg.Connection[dict[str, object]] = psycopg.connect(
            _DSN, row_factory=psycopg.rows.dict_row, autocommit=False
        )
        try:
            conn.execute("SET ROLE authenticated")
            # No GUC set → auth.uid() = NULL → should raise.
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute("SELECT app_bootstrap_family('NoAuth', NULL, NULL)")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# RLS isolation for new spine rows
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _two_families(
    owner_conn: psycopg.Connection[tuple[object, ...]],
) -> Generator[dict[str, uuid.UUID], None, None]:
    """Bootstrap two separate families and yield ids; clean up after."""
    user_x = uuid.uuid4()
    user_y = uuid.uuid4()

    conn_x = _authed_dict_conn(user_x)
    conn_y = _authed_dict_conn(user_y)

    cur_x = conn_x.cursor()
    cur_x.execute("SELECT app_bootstrap_family(%s, %s, %s)", ("Family X", "Xander", "Grade 5"))
    rx = cur_x.fetchone()
    assert rx is not None
    raw_x: dict[str, Any] = _parse_bootstrap_result(rx["app_bootstrap_family"])
    family_x = uuid.UUID(str(raw_x["family_id"]))
    child_x = uuid.UUID(str(raw_x["child_id"])) if raw_x.get("child_id") else None
    conn_x.commit()

    cur_y = conn_y.cursor()
    cur_y.execute("SELECT app_bootstrap_family(%s, %s, %s)", ("Family Y", "Yael", "Grade 3"))
    ry = cur_y.fetchone()
    assert ry is not None
    raw_y: dict[str, Any] = _parse_bootstrap_result(ry["app_bootstrap_family"])
    family_y = uuid.UUID(str(raw_y["family_id"]))
    child_y = uuid.UUID(str(raw_y["child_id"])) if raw_y.get("child_id") else None
    conn_y.commit()

    # Add subjects + cycles via owner for test data richness.
    subject_x = uuid.uuid4()
    subject_y = uuid.uuid4()
    cycle_x = uuid.uuid4()
    cycle_y = uuid.uuid4()
    oc = owner_conn.cursor()
    if child_x:
        oc.execute(
            "INSERT INTO subjects (id, family_id, child_id, name, content_language) "
            "VALUES (%s, %s, %s, 'Maths', 'en')",
            (str(subject_x), str(family_x), str(child_x)),
        )
    if child_y:
        oc.execute(
            "INSERT INTO subjects (id, family_id, child_id, name, content_language) "
            "VALUES (%s, %s, %s, 'Science', 'en')",
            (str(subject_y), str(family_y), str(child_y)),
        )
    if child_x:
        oc.execute(
            "INSERT INTO cycles (id, family_id, subject_id) VALUES (%s, %s, %s)",
            (str(cycle_x), str(family_x), str(subject_x)),
        )
    if child_y:
        oc.execute(
            "INSERT INTO cycles (id, family_id, subject_id) VALUES (%s, %s, %s)",
            (str(cycle_y), str(family_y), str(subject_y)),
        )
    owner_conn.commit()

    conn_x.close()
    conn_y.close()

    yield {
        "user_x": user_x,
        "user_y": user_y,
        "family_x": family_x,
        "family_y": family_y,
        "child_x": child_x or uuid.UUID(int=0),
        "child_y": child_y or uuid.UUID(int=0),
        "subject_x": subject_x,
        "subject_y": subject_y,
        "cycle_x": cycle_x,
        "cycle_y": cycle_y,
    }

    # Cleanup.
    oc2 = owner_conn.cursor()
    oc2.execute("DELETE FROM cycles WHERE id IN (%s, %s)", (str(cycle_x), str(cycle_y)))
    oc2.execute("DELETE FROM subjects WHERE id IN (%s, %s)", (str(subject_x), str(subject_y)))
    oc2.execute("DELETE FROM children WHERE family_id IN (%s, %s)", (str(family_x), str(family_y)))
    oc2.execute("DELETE FROM family_members WHERE user_id IN (%s, %s)", (str(user_x), str(user_y)))
    oc2.execute("DELETE FROM families WHERE id IN (%s, %s)", (str(family_x), str(family_y)))
    owner_conn.commit()


class TestSpineRLSIsolation:
    def test_user_x_cannot_see_family_y(self, _two_families: dict[str, uuid.UUID]) -> None:
        user_x = _two_families["user_x"]
        family_y = _two_families["family_y"]
        conn = _authed_dict_conn(user_x)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM families WHERE id = %s", (str(family_y),))
            assert cur.fetchone() is None, "RLS BREACH: user_x can see family_y"
        finally:
            conn.close()

    def test_user_x_cannot_see_children_of_family_y(
        self, _two_families: dict[str, uuid.UUID]
    ) -> None:
        user_x = _two_families["user_x"]
        child_y = _two_families["child_y"]
        conn = _authed_dict_conn(user_x)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM children WHERE id = %s", (str(child_y),))
            assert cur.fetchone() is None, "RLS BREACH: user_x can see child_y"
        finally:
            conn.close()

    def test_user_x_cannot_see_cycles_of_family_y(
        self, _two_families: dict[str, uuid.UUID]
    ) -> None:
        user_x = _two_families["user_x"]
        cycle_y = _two_families["cycle_y"]
        conn = _authed_dict_conn(user_x)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM cycles WHERE id = %s", (str(cycle_y),))
            assert cur.fetchone() is None, "RLS BREACH: user_x can see cycle_y"
        finally:
            conn.close()

    def test_user_x_can_see_own_family(self, _two_families: dict[str, uuid.UUID]) -> None:
        user_x = _two_families["user_x"]
        family_x = _two_families["family_x"]
        conn = _authed_dict_conn(user_x)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM families WHERE id = %s", (str(family_x),))
            assert cur.fetchone() is not None, "user_x cannot see own family_x"
        finally:
            conn.close()

    def test_user_x_can_see_own_cycle(self, _two_families: dict[str, uuid.UUID]) -> None:
        user_x = _two_families["user_x"]
        cycle_x = _two_families["cycle_x"]
        conn = _authed_dict_conn(user_x)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM cycles WHERE id = %s", (str(cycle_x),))
            assert cur.fetchone() is not None, "user_x cannot see own cycle_x"
        finally:
            conn.close()


class TestFamilyMembersInsertBlocked:
    """No authenticated user may directly INSERT a family_members row.

    All family_members writes go through app_bootstrap_family (SECURITY DEFINER).
    Closing the cross-tenant hole: WITH CHECK (user_id = auth.uid()) would let
    any user point their membership at any arbitrary family_id and gain full
    RLS access to that family's children/subjects/cycles.  The INSERT grant and
    policy are intentionally absent from 0003_bootstrap.sql.
    """

    def test_direct_insert_own_family_id_is_rejected(
        self, _two_families: dict[str, uuid.UUID]
    ) -> None:
        """authenticated role has no INSERT grant on family_members at all."""
        family_x = _two_families["family_x"]
        new_user = uuid.uuid4()
        conn = _authed_dict_conn(new_user)
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO family_members (user_id, family_id, role) "
                    "VALUES (%s, %s, 'parent')",
                    (str(new_user), str(family_x)),
                )
        finally:
            conn.close()

    def test_direct_insert_foreign_family_id_is_rejected(
        self, _two_families: dict[str, uuid.UUID]
    ) -> None:
        """An authenticated user cannot join a foreign family by inserting directly."""
        user_x = _two_families["user_x"]
        family_y = _two_families["family_y"]
        conn = _authed_dict_conn(user_x)
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO family_members (user_id, family_id, role) "
                    "VALUES (%s, %s, 'parent')",
                    (str(user_x), str(family_y)),
                )
        finally:
            conn.close()


class TestApprovalRecorded:
    """parent_approval_at is set by approve_draft — golden rule 8."""

    def test_approval_timestamp_persisted(
        self, owner_conn: psycopg.Connection[tuple[object, ...]]
    ) -> None:
        user_id = uuid.uuid4()
        conn = _authed_dict_conn(user_id)
        family_id: uuid.UUID | None = None
        cycle_id: uuid.UUID | None = None

        try:
            cur = conn.cursor()
            # Bootstrap
            cur.execute(
                "SELECT app_bootstrap_family(%s, %s, %s)", ("ApprovalFamily", "Kid", "Grade 5")
            )
            row = cur.fetchone()
            assert row is not None
            res: dict[str, Any] = _parse_bootstrap_result(row["app_bootstrap_family"])
            family_id = uuid.UUID(str(res["family_id"]))
            child_id = uuid.UUID(str(res["child_id"]))
            conn.commit()

            # Create subject + cycle as owner (RLS INSERT on subjects needs family membership).
            subject_id = uuid.uuid4()
            oc = owner_conn.cursor()
            oc.execute(
                "INSERT INTO subjects (id, family_id, child_id, name, content_language) "
                "VALUES (%s, %s, %s, 'Maths', 'en')",
                (str(subject_id), str(family_id), str(child_id)),
            )
            cycle_id = uuid.uuid4()
            oc.execute(
                "INSERT INTO cycles (id, family_id, subject_id, state) "
                "VALUES (%s, %s, %s, 'PARENT_REVIEWS_DRAFT')",
                (str(cycle_id), str(family_id), str(subject_id)),
            )
            owner_conn.commit()

            # Approve via authenticated connection.
            cur.execute(
                """
                UPDATE cycles
                SET state              = 'APPROVED_PRINTED',
                    updated_at         = now(),
                    parent_approval_at = now(),
                    parent_approval_note = %s
                WHERE id = %s
                RETURNING parent_approval_at
                """,
                ("test note", str(cycle_id)),
            )
            row2 = cur.fetchone()
            conn.commit()
            assert row2 is not None
            approval_ts: Any = row2["parent_approval_at"]
            assert approval_ts is not None, "parent_approval_at was not set"

        finally:
            conn.close()
            if cycle_id is not None:
                oc2 = owner_conn.cursor()
                oc2.execute("DELETE FROM cycles WHERE id = %s", (str(cycle_id),))
                owner_conn.commit()
            if family_id is not None:
                oc3 = owner_conn.cursor()
                oc3.execute("DELETE FROM subjects WHERE family_id = %s", (str(family_id),))
                oc3.execute("DELETE FROM children WHERE family_id = %s", (str(family_id),))
                oc3.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
                oc3.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
                owner_conn.commit()


class TestMultiCommitClaimsRegression:
    """Regression: request.jwt.claims must survive multiple commits on one connection.

    Root cause (fixed): ``SET LOCAL request.jwt.claims`` is transaction-scoped
    and is cleared on COMMIT.  The generate flow issues two commits on the same
    per-request connection (advance_to_generating → commit, then
    advance_to_parent_reviews → commit).  After the first commit auth.uid()
    returned NULL, RLS matched no rows, and the cycle appeared not-found.

    Fix: ``set_config('request.jwt.claims', %s, false)`` sets session scope so
    the claim survives every commit for the lifetime of the connection.

    This test must use a REAL Postgres connection (not InMemory) because the
    bug is in Postgres connection semantics, invisible to the in-process mock.
    """

    def test_claims_survive_two_commits(
        self, owner_conn: psycopg.Connection[tuple[object, ...]]
    ) -> None:
        """Claims remain valid after an explicit COMMIT on a dict-row connection."""
        import json as _json

        user_id = uuid.uuid4()
        claims = _json.dumps({"sub": str(user_id)})

        conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
            _DSN,
            row_factory=psycopg.rows.dict_row,
            autocommit=False,
        )
        try:
            conn.execute("SET ROLE authenticated")
            # Use the fixed session-scope set_config (false = not transaction-local).
            conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))

            # First commit — with SET LOCAL this would clear the claim.
            conn.commit()

            # auth.uid() must still return the user_id after the commit.
            cur = conn.cursor()
            cur.execute("SELECT auth.uid() AS uid")
            row = cur.fetchone()
            assert row is not None
            uid_after_commit = row["uid"]
            assert uid_after_commit is not None, (
                "auth.uid() returned NULL after COMMIT — "
                "SET LOCAL regression: claims were cleared by the commit"
            )
            assert str(uid_after_commit) == str(user_id), (
                f"auth.uid() returned {uid_after_commit!r} after commit, expected {user_id}"
            )

            # Second commit — claim must still hold.
            conn.commit()
            cur.execute("SELECT auth.uid() AS uid")
            row2 = cur.fetchone()
            assert row2 is not None
            uid_after_second = row2["uid"]
            assert str(uid_after_second) == str(user_id), (
                f"auth.uid() returned {uid_after_second!r} after second commit"
            )
        finally:
            conn.close()

    def test_generate_flow_advances_to_parent_reviews(
        self, owner_conn: psycopg.Connection[tuple[object, ...]]
    ) -> None:
        """Full generate path: bootstrap → cycle → FakeClaude generate → PARENT_REVIEWS_DRAFT.

        Exercises the two-commit path on a real Postgres connection and asserts
        the cycle ends in PARENT_REVIEWS_DRAFT with the assessment persisted.
        """
        from config import get_settings
        from schemas.family import CycleState
        from schemas.generation import GenerateAssessmentRequest
        from schemas.identity import Identity
        from services.claude_client import FakeClaude
        from services.cycle import advance_to_generating, advance_to_parent_reviews
        from services.generation_service import GenerationService
        from services.repositories.postgres import (
            PostgresAssessmentRepository,
            open_authenticated_connection,
        )
        from services.repositories.postgres_family import PostgresFamilyRepository

        settings = get_settings()
        user_id = uuid.uuid4()
        identity = Identity(user_id=user_id)

        family_id: uuid.UUID | None = None
        cycle_id_uuid: uuid.UUID | None = None

        try:
            conn = open_authenticated_connection(settings.db_dsn, identity)
            try:
                family_repo = PostgresFamilyRepository(conn)
                asmt_repo = PostgresAssessmentRepository(conn)

                # Bootstrap family + child in one call.
                family, child_id = family_repo.bootstrap_family(
                    "RegressionFamily", "TestKid", "Grade 4"
                )
                family_id = family.id
                assert child_id is not None

                # Create subject and cycle.
                subject = family_repo.create_subject(family.id, child_id, "Mathematics", "en")
                cycle = family_repo.create_cycle(family.id, subject.id, "Grade 4 fractions")
                cycle_id_uuid = cycle.id
                assert cycle.state == CycleState.SCOPE_UPLOADED

                # Advance: SCOPE_UPLOADED → GENERATING_A (commit 1).
                advance_to_generating(family_repo, cycle.id)

                # Generate (FakeClaude — no network call).
                request = GenerateAssessmentRequest(
                    cycle_id=str(cycle.id),
                    scope_text="Grade 4 fractions",
                )
                service = GenerationService(claude=FakeClaude(), settings=settings)
                result = service.generate(request)
                assert result.ok, f"Generation failed: {result.error}"
                assert result.assessment is not None
                asmt_repo.save(result.assessment)

                # Advance: GENERATING_A → PARENT_REVIEWS_DRAFT (commit 2).
                # This is the commit that previously failed with SET LOCAL.
                updated = advance_to_parent_reviews(family_repo, cycle.id)
                assert updated.state == CycleState.PARENT_REVIEWS_DRAFT, (
                    f"Expected PARENT_REVIEWS_DRAFT after generate, got {updated.state} — "
                    "SET LOCAL regression: claims were cleared by the first commit"
                )

                # Assessment must be retrievable under the same identity.
                asmt_id = result.assessment.assessment_id
                fetched = asmt_repo.get(asmt_id)
                assert fetched is not None, "Assessment not found after save"
                assert fetched.assessment_id == asmt_id

            finally:
                conn.close()

        finally:
            # Cleanup via owner connection (reverse FK order).
            if cycle_id_uuid is not None:
                oc = owner_conn.cursor()
                oc.execute("DELETE FROM assessments WHERE cycle_id = %s", (str(cycle_id_uuid),))
                oc.execute("DELETE FROM cycles WHERE id = %s", (str(cycle_id_uuid),))
                owner_conn.commit()
            if family_id is not None:
                oc2 = owner_conn.cursor()
                oc2.execute("DELETE FROM subjects WHERE family_id = %s", (str(family_id),))
                oc2.execute("DELETE FROM children WHERE family_id = %s", (str(family_id),))
                oc2.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
                oc2.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
                owner_conn.commit()
