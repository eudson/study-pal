"""RLS isolation tier — Local Postgres tests.

These tests require a live Postgres instance (the docker ``db`` service).
They are skipped automatically when ``STUDYPAL_DB_DSN`` is unset or the
DB is unreachable, so they never block the unit-test tier (no docker).

What is proven:
1. Family A's rows are invisible to family B (cross-tenant isolation).
2. Deny-by-default: authenticated role with no ``request.jwt.claims`` set
   cannot read any rows.
3. Request-path role (``authenticated``) cannot bypass RLS; only the owner
   can (confirming the service-role/owner connection is NOT used on request path).
4. Promoted columns match the JSONB document (invariant 5 round-trip).

Invariant 1 enforcement mechanism:
- Tests open two connections: one as the owner (migration role) to insert
  baseline data, one as ``authenticated`` with the GUC set to family A's
  user_id.  Queries in the authenticated connection must NOT see family B's rows.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator

import psycopg
import psycopg.errors
import pytest

from tests.samples.maths_sample import maths_assessment

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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
    """Owner connection (bypasses RLS — migration role).  Module-scoped."""
    conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(_DSN, autocommit=False)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def _baseline(
    owner_conn: psycopg.Connection[tuple[object, ...]],
) -> Generator[dict[str, uuid.UUID], None, None]:
    """Insert two families + users + cycles + assessments; yield ids; clean up."""
    cur = owner_conn.cursor()

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    family_a = uuid.uuid4()
    family_b = uuid.uuid4()
    cycle_a = uuid.uuid4()
    cycle_b = uuid.uuid4()
    child_a = uuid.uuid4()
    child_b = uuid.uuid4()
    subject_a = uuid.uuid4()
    subject_b = uuid.uuid4()
    asmt_a = uuid.uuid4()
    asmt_b = uuid.uuid4()

    # families
    cur.execute("INSERT INTO families (id, name) VALUES (%s, 'Family A')", (str(family_a),))
    cur.execute("INSERT INTO families (id, name) VALUES (%s, 'Family B')", (str(family_b),))
    # family_members
    cur.execute(
        "INSERT INTO family_members (user_id, family_id) VALUES (%s, %s)",
        (str(user_a), str(family_a)),
    )
    cur.execute(
        "INSERT INTO family_members (user_id, family_id) VALUES (%s, %s)",
        (str(user_b), str(family_b)),
    )
    # children
    cur.execute(
        "INSERT INTO children (id, family_id, display_name, grade_label) "
        "VALUES (%s, %s, 'Child A', 'Grade 5')",
        (str(child_a), str(family_a)),
    )
    cur.execute(
        "INSERT INTO children (id, family_id, display_name, grade_label) "
        "VALUES (%s, %s, 'Child B', 'Grade 5')",
        (str(child_b), str(family_b)),
    )
    # subjects
    cur.execute(
        "INSERT INTO subjects (id, family_id, child_id, name, content_language) "
        "VALUES (%s, %s, %s, 'Maths', 'en')",
        (str(subject_a), str(family_a), str(child_a)),
    )
    cur.execute(
        "INSERT INTO subjects (id, family_id, child_id, name, content_language) "
        "VALUES (%s, %s, %s, 'Maths', 'en')",
        (str(subject_b), str(family_b), str(child_b)),
    )
    # cycles (state on cycles only)
    cur.execute(
        "INSERT INTO cycles (id, family_id, subject_id) VALUES (%s, %s, %s)",
        (str(cycle_a), str(family_a), str(subject_a)),
    )
    cur.execute(
        "INSERT INTO cycles (id, family_id, subject_id) VALUES (%s, %s, %s)",
        (str(cycle_b), str(family_b), str(subject_b)),
    )
    # assessments
    raw_a = maths_assessment()
    raw_a["assessment_id"] = str(asmt_a)
    raw_a["cycle_id"] = str(cycle_a)
    raw_b = maths_assessment()
    raw_b["assessment_id"] = str(asmt_b)
    raw_b["cycle_id"] = str(cycle_b)
    raw_b["subject"] = "Family-B-Subject"

    cur.execute(
        """
        INSERT INTO assessments (
            id, family_id, cycle_id, variant, subject, content_language,
            declared_total_marks, computed_total_marks, assessment
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(asmt_a),
            str(family_a),
            str(cycle_a),
            "A",
            "Mathematics",
            "en",
            9.0,
            9.0,
            json.dumps(raw_a),
        ),
    )
    cur.execute(
        """
        INSERT INTO assessments (
            id, family_id, cycle_id, variant, subject, content_language,
            declared_total_marks, computed_total_marks, assessment
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(asmt_b),
            str(family_b),
            str(cycle_b),
            "A",
            "Family-B-Subject",
            "en",
            9.0,
            9.0,
            json.dumps(raw_b),
        ),
    )
    owner_conn.commit()

    yield {
        "user_a": user_a,
        "user_b": user_b,
        "family_a": family_a,
        "family_b": family_b,
        "cycle_a": cycle_a,
        "cycle_b": cycle_b,
        "asmt_a": asmt_a,
        "asmt_b": asmt_b,
    }

    # Cleanup (reverse FK order)
    cur.execute("DELETE FROM assessments WHERE id IN (%s, %s)", (str(asmt_a), str(asmt_b)))
    cur.execute("DELETE FROM cycles WHERE id IN (%s, %s)", (str(cycle_a), str(cycle_b)))
    cur.execute("DELETE FROM subjects WHERE id IN (%s, %s)", (str(subject_a), str(subject_b)))
    cur.execute("DELETE FROM children WHERE id IN (%s, %s)", (str(child_a), str(child_b)))
    cur.execute(
        "DELETE FROM family_members WHERE user_id IN (%s, %s)",
        (str(user_a), str(user_b)),
    )
    cur.execute("DELETE FROM families WHERE id IN (%s, %s)", (str(family_a), str(family_b)))
    owner_conn.commit()


def _authed_conn(user_id: uuid.UUID) -> psycopg.Connection[tuple[object, ...]]:
    """Non-privileged connection with claims set for user_id."""
    claims = json.dumps({"sub": str(user_id)})
    conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(_DSN, autocommit=False)
    conn.execute("SET ROLE authenticated")
    # SET LOCAL does not support parameterized values; use safe string formatting.
    # The value is JSON from json.dumps — no SQL-injectable content possible.
    conn.execute(f"SET LOCAL request.jwt.claims = '{claims}'")  # noqa: S608
    return conn


# ---------------------------------------------------------------------------
# Isolation tests
# ---------------------------------------------------------------------------


class TestRLSCrossTenantIsolation:
    """Family B cannot read family A's rows."""

    def test_family_b_cannot_see_family_a_assessment(self, _baseline: dict[str, uuid.UUID]) -> None:
        asmt_a_id = str(_baseline["asmt_a"])
        user_b = _baseline["user_b"]

        conn = _authed_conn(user_b)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM assessments WHERE id = %s", (asmt_a_id,))
            row = cur.fetchone()
            assert row is None, (
                f"RLS BREACH: Family B user can see family A's assessment {asmt_a_id}"
            )
        finally:
            conn.close()

    def test_family_a_cannot_see_family_b_assessment(self, _baseline: dict[str, uuid.UUID]) -> None:
        asmt_b_id = str(_baseline["asmt_b"])
        user_a = _baseline["user_a"]

        conn = _authed_conn(user_a)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM assessments WHERE id = %s", (asmt_b_id,))
            row = cur.fetchone()
            assert row is None, (
                f"RLS BREACH: Family A user can see family B's assessment {asmt_b_id}"
            )
        finally:
            conn.close()

    def test_family_a_can_see_own_assessment(self, _baseline: dict[str, uuid.UUID]) -> None:
        asmt_a_id = str(_baseline["asmt_a"])
        user_a = _baseline["user_a"]

        conn = _authed_conn(user_a)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM assessments WHERE id = %s", (asmt_a_id,))
            row = cur.fetchone()
            assert row is not None, "Family A user cannot see its own assessment"
        finally:
            conn.close()

    def test_family_b_can_see_own_assessment(self, _baseline: dict[str, uuid.UUID]) -> None:
        asmt_b_id = str(_baseline["asmt_b"])
        user_b = _baseline["user_b"]

        conn = _authed_conn(user_b)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM assessments WHERE id = %s", (asmt_b_id,))
            row = cur.fetchone()
            assert row is not None, "Family B user cannot see its own assessment"
        finally:
            conn.close()

    def test_list_returns_only_own_family_rows(self, _baseline: dict[str, uuid.UUID]) -> None:
        user_a = _baseline["user_a"]
        asmt_a_id = str(_baseline["asmt_a"])
        asmt_b_id = str(_baseline["asmt_b"])

        conn = _authed_conn(user_a)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM assessments")
            ids = {str(row[0]) for row in cur.fetchall()}
            assert asmt_a_id in ids, "Family A's own assessment missing from list"
            assert asmt_b_id not in ids, (
                "Family B's assessment visible in family A's list (RLS BREACH)"
            )
        finally:
            conn.close()


class TestRLSDenyByDefault:
    """No identity → no rows visible (deny-by-default, invariant 2/4)."""

    def test_no_claims_guc_returns_no_rows(self, _baseline: dict[str, uuid.UUID]) -> None:
        """``authenticated`` role with no GUC set → auth.uid() = NULL → 0 rows."""
        conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(_DSN, autocommit=False)
        try:
            conn.execute("SET ROLE authenticated")
            # No SET LOCAL request.jwt.claims → auth.uid() returns NULL.
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM assessments")
            row = cur.fetchone()
            count = row[0] if row else 0
            assert count == 0, f"Deny-by-default BREACH: got {count} rows with no identity set"
        finally:
            conn.close()

    def test_empty_claims_guc_returns_no_rows(self, _baseline: dict[str, uuid.UUID]) -> None:
        """Empty JSON claims → sub=NULL → auth.uid()=NULL → 0 rows."""
        conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(_DSN, autocommit=False)
        try:
            conn.execute("SET ROLE authenticated")
            conn.execute("SET LOCAL request.jwt.claims = '{}'")  # noqa: S608
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM assessments")
            row = cur.fetchone()
            count = row[0] if row else 0
            assert count == 0, f"Deny-by-default BREACH: empty claims yielded {count} rows"
        finally:
            conn.close()


class TestRLSNonPrivilegedRole:
    """Prove the request-path role (``authenticated``) cannot bypass RLS.

    Invariant 1: service-role/owner is NEVER used on the request path.
    """

    def test_authenticated_role_cannot_drop_table(self, _baseline: dict[str, uuid.UUID]) -> None:
        """``authenticated`` role cannot DROP tables — only the owner can."""
        conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(_DSN, autocommit=False)
        try:
            conn.execute("SET ROLE authenticated")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DROP TABLE assessments")
        finally:
            conn.close()

    def test_authenticated_role_cannot_create_table(self, _baseline: dict[str, uuid.UUID]) -> None:
        """``authenticated`` role has no CREATE privilege — it is non-privileged."""
        conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(_DSN, autocommit=False)
        try:
            conn.execute("SET ROLE authenticated")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE rls_escape_hatch (id int)")
        finally:
            conn.close()


class TestPromotedColumnRoundTrip:
    """Promoted columns equal the corresponding JSONB fields (invariant 5)."""

    def test_variant_promoted_column_equals_jsonb(self, _baseline: dict[str, uuid.UUID]) -> None:
        asmt_a_id = str(_baseline["asmt_a"])
        user_a = _baseline["user_a"]

        conn = _authed_conn(user_a)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT variant, assessment->>'variant' AS jsonb_variant "
                "FROM assessments WHERE id = %s",
                (asmt_a_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == row[1], f"Promoted variant '{row[0]}' != JSONB variant '{row[1]}'"
        finally:
            conn.close()

    def test_subject_promoted_column_equals_jsonb(self, _baseline: dict[str, uuid.UUID]) -> None:
        asmt_a_id = str(_baseline["asmt_a"])
        user_a = _baseline["user_a"]

        conn = _authed_conn(user_a)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT subject, assessment->>'subject' AS jsonb_subject "
                "FROM assessments WHERE id = %s",
                (asmt_a_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == row[1]
        finally:
            conn.close()

    def test_content_language_promoted_column_equals_jsonb(
        self, _baseline: dict[str, uuid.UUID]
    ) -> None:
        asmt_a_id = str(_baseline["asmt_a"])
        user_a = _baseline["user_a"]

        conn = _authed_conn(user_a)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT content_language, assessment->>'content_language' "
                "FROM assessments WHERE id = %s",
                (asmt_a_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == row[1]
        finally:
            conn.close()

    def test_declared_total_marks_promoted_column_equals_jsonb(
        self, _baseline: dict[str, uuid.UUID]
    ) -> None:
        asmt_a_id = str(_baseline["asmt_a"])
        user_a = _baseline["user_a"]

        conn = _authed_conn(user_a)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT declared_total_marks, "
                "       (assessment->>'declared_total_marks')::numeric "
                "FROM assessments WHERE id = %s",
                (asmt_a_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert float(str(row[0])) == float(str(row[1]))
        finally:
            conn.close()


class TestFamilyMembersIsolation:
    """family_members is RLS-scoped: a user sees only its own membership rows.

    Without RLS on family_members, any authenticated session could read the
    entire user->family graph across tenants. This proves the self-view policy.
    """

    def test_user_sees_only_own_membership(self, _baseline: dict[str, uuid.UUID]) -> None:
        user_a = _baseline["user_a"]
        user_b = _baseline["user_b"]
        family_a = _baseline["family_a"]

        conn = _authed_conn(user_b)
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id, family_id FROM family_members")
            rows = cur.fetchall()
        finally:
            conn.close()

        user_ids = {str(r[0]) for r in rows}
        family_ids = {str(r[1]) for r in rows}
        assert str(user_b) in user_ids
        assert str(user_a) not in user_ids
        assert str(family_a) not in family_ids

    def test_user_cannot_see_other_family_membership(self, _baseline: dict[str, uuid.UUID]) -> None:
        user_a = _baseline["user_a"]
        user_b = _baseline["user_b"]

        conn = _authed_conn(user_b)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(*) FROM family_members WHERE user_id = %s",
                (str(user_a),),
            )
            row = cur.fetchone()
            assert row is not None
            assert int(str(row[0])) == 0
        finally:
            conn.close()
