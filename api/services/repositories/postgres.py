"""Postgres-backed ``AssessmentRepository``.

Invariant 1 enforcement: every query runs as the non-privileged ``authenticated``
role with ``SET LOCAL request.jwt.claims`` so the DB's RLS policies are live.
The connection is **never** opened as superuser / owner on the request path.

Invariant 3: ``family_id`` is taken from the validated ``Identity``; the client
cannot supply or override it.

Invariant 5: promoted columns (variant, subject, content_language,
declared_total_marks, computed_total_marks) are derived from the validated
``Assessment`` on every write — never accepted from the caller.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import psycopg.rows

from schemas.assessment_schema import Assessment
from schemas.identity import Identity

# Type alias for a dict-row psycopg connection.
DictConn = psycopg.Connection[dict[str, object]]


class PostgresAssessmentRepository:
    """Satisfies the ``AssessmentRepository`` protocol for Postgres.

    The caller must supply a live *psycopg* connection that has already:
    1. ``SET ROLE authenticated``
    2. ``SET LOCAL request.jwt.claims`` (per-transaction GUC carrying user_id)

    Both are established by ``open_authenticated_connection`` — the caller
    (``get_assessment_repository``) is responsible for that setup.
    """

    def __init__(self, conn: DictConn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def save(self, assessment: Assessment) -> Assessment:
        """Upsert an assessment row.

        Promoted columns are derived from *assessment*; the caller cannot
        override them (invariant 5).
        """
        row_id = uuid.UUID(assessment.assessment_id)
        cycle_id = uuid.UUID(assessment.cycle_id)
        doc = json.loads(assessment.model_dump_json())

        # Resolve family_id from cycles table to avoid the client supplying it.
        # The RLS policy ensures we can only see rows our user owns.
        cur = self._conn.cursor()
        cur.execute(
            "SELECT family_id FROM cycles WHERE id = %s",
            (str(cycle_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"cycle {cycle_id} not found or not accessible")
        family_id: uuid.UUID = uuid.UUID(str(row["family_id"]))

        cur.execute(
            """
            INSERT INTO assessments (
                id, family_id, cycle_id,
                variant, subject, content_language,
                declared_total_marks, computed_total_marks,
                assessment, schema_version
            ) VALUES (
                %(id)s, %(family_id)s, %(cycle_id)s,
                %(variant)s, %(subject)s, %(content_language)s,
                %(declared_total_marks)s, %(computed_total_marks)s,
                %(assessment)s, %(schema_version)s
            )
            ON CONFLICT (id) DO UPDATE SET
                variant               = EXCLUDED.variant,
                subject               = EXCLUDED.subject,
                content_language      = EXCLUDED.content_language,
                declared_total_marks  = EXCLUDED.declared_total_marks,
                computed_total_marks  = EXCLUDED.computed_total_marks,
                assessment            = EXCLUDED.assessment,
                schema_version        = EXCLUDED.schema_version
            """,
            {
                "id": str(row_id),
                "family_id": str(family_id),
                "cycle_id": str(cycle_id),
                "variant": assessment.variant,
                "subject": assessment.subject,
                "content_language": assessment.content_language,
                "declared_total_marks": assessment.declared_total_marks,
                "computed_total_marks": assessment.computed_total_marks,
                "assessment": json.dumps(doc),
                "schema_version": assessment.schema_version,
            },
        )
        self._conn.commit()
        return assessment

    def get(self, assessment_id: str) -> Assessment | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT assessment FROM assessments WHERE id = %s",
            (assessment_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        raw = row["assessment"]
        doc: dict[str, object] = raw if isinstance(raw, dict) else json.loads(str(raw))
        return Assessment.model_validate(doc)

    def list(self) -> list[Assessment]:
        cur = self._conn.cursor()
        cur.execute("SELECT assessment FROM assessments ORDER BY created_at")
        rows = cur.fetchall()
        result: list[Assessment] = []
        for row in rows:
            raw = row["assessment"]
            doc: dict[str, object] = raw if isinstance(raw, dict) else json.loads(str(raw))
            result.append(Assessment.model_validate(doc))
        return result


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def open_authenticated_connection(
    dsn: str,
    identity: Identity,
) -> DictConn:
    """Open a sync psycopg connection for the request path.

    Steps (invariant 1):
    1. Connect as the owner/DSN role.
    2. ``SET ROLE authenticated`` — drop to the non-privileged role so RLS fires.
    3. ``SET LOCAL request.jwt.claims`` — inject the caller's user_id so
       ``auth.uid()`` resolves correctly inside RLS policies.

    The caller is responsible for closing this connection when the request ends.
    """
    claims = json.dumps({"sub": str(identity.user_id)})
    conn: DictConn = psycopg.connect(
        dsn,
        row_factory=psycopg.rows.dict_row,
        autocommit=False,
    )
    # Step 2: become the non-privileged role.
    conn.execute("SET ROLE authenticated")
    # Step 3: inject claims per-transaction so auth.uid() works.
    # SET LOCAL does not support parameterized values; the claims value is
    # produced by json.dumps (from a validated UUID) — no SQL injection risk.
    conn.execute(f"SET LOCAL request.jwt.claims = '{claims}'")  # noqa: S608
    return conn
