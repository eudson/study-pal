"""Postgres-backed ``FamilyRepository``.

Invariant 1: every query runs as the non-privileged ``authenticated`` role
via ``open_authenticated_connection`` so RLS is enforced by the DB.

Invariant 3: ``family_id`` is always derived server-side (from the authenticated
user's membership row, or from the cycles/subjects tables) — never accepted
from the client.

Bootstrap invariant: creating a new family requires the SECURITY DEFINER
function ``app_bootstrap_family`` (0003_bootstrap.sql) because the
``families_tenant_insert`` RLS policy creates a deadlock for new users who
have no membership row yet.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from schemas.assessment_schema import Assessment
from schemas.family import (
    ChildResponse,
    ChildUpdate,
    CycleResponse,
    CycleState,
    FamilyResponse,
    SubjectResponse,
    VisibilityDefaults,
)
from services.repositories.postgres import DictConn


class PostgresFamilyRepository:
    """Satisfies the ``FamilyRepository`` protocol for Postgres.

    The caller must supply a live *psycopg* connection already set up by
    ``open_authenticated_connection`` (SET ROLE authenticated + GUC).
    """

    def __init__(self, conn: DictConn) -> None:
        self._conn = conn

    # -- Family --

    def bootstrap_family(
        self,
        family_name: str,
        child_name: str | None,
        grade_label: str | None,
    ) -> tuple[FamilyResponse, uuid.UUID | None]:
        """Call the SECURITY DEFINER function to atomically bootstrap.

        The function is idempotent: if the caller already belongs to a family,
        it returns the existing family_id with child_id=None.
        """
        cur = self._conn.cursor()
        cur.execute(
            "SELECT app_bootstrap_family(%s, %s, %s)",
            (family_name, child_name, grade_label),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("app_bootstrap_family returned no result")
        result_raw = row["app_bootstrap_family"]
        result: dict[str, object] = (
            result_raw if isinstance(result_raw, dict) else json.loads(str(result_raw))
        )

        family_id = uuid.UUID(str(result["family_id"]))
        child_id_raw = result.get("child_id")
        child_id = uuid.UUID(str(child_id_raw)) if child_id_raw else None

        # Fetch the family row (RLS now allows it because membership exists).
        cur.execute("SELECT id, name, created_at FROM families WHERE id = %s", (str(family_id),))
        frow = cur.fetchone()
        if frow is None:
            raise RuntimeError(f"Family {family_id} not found after bootstrap")
        family = FamilyResponse(
            id=uuid.UUID(str(frow["id"])),
            name=str(frow["name"]),
            created_at=_parse_dt(frow["created_at"]),
        )
        self._conn.commit()
        return family, child_id

    def list_families(self) -> list[FamilyResponse]:
        cur = self._conn.cursor()
        cur.execute("SELECT id, name, created_at FROM families ORDER BY created_at")
        return [
            FamilyResponse(
                id=uuid.UUID(str(r["id"])),
                name=str(r["name"]),
                created_at=_parse_dt(r["created_at"]),
            )
            for r in cur.fetchall()
        ]

    # -- Child --

    def create_child(
        self,
        family_id: uuid.UUID,
        display_name: str,
        grade_label: str,
        visibility_defaults: VisibilityDefaults | None = None,
    ) -> ChildResponse:
        vd = (visibility_defaults or VisibilityDefaults()).model_dump_json()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO children (family_id, display_name, grade_label, visibility_defaults)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING id, family_id, display_name, grade_label, created_at,
                      archived_at, visibility_defaults
            """,
            (str(family_id), display_name, grade_label, vd),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO children returned no row")
        self._conn.commit()
        return _row_to_child(row)

    def list_children(self, family_id: uuid.UUID) -> list[ChildResponse]:
        """Return active children only (archived_at IS NULL)."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, family_id, display_name, grade_label, created_at, "
            "       archived_at, visibility_defaults "
            "FROM children "
            "WHERE family_id = %s AND archived_at IS NULL "
            "ORDER BY created_at",
            (str(family_id),),
        )
        return [_row_to_child(r) for r in cur.fetchall()]

    def update_child(self, child_id: uuid.UUID, payload: ChildUpdate) -> ChildResponse:
        """Partial UPDATE — only provided (non-None) fields are written.

        Raises ValueError if the child is not found or not accessible (RLS).
        """
        cur = self._conn.cursor()
        # Build SET clauses dynamically from non-None payload fields.
        set_parts: list[str] = []
        params: list[object] = []

        if payload.display_name is not None:
            set_parts.append("display_name = %s")
            params.append(payload.display_name)
        if payload.grade_label is not None:
            set_parts.append("grade_label = %s")
            params.append(payload.grade_label)
        if payload.visibility_defaults is not None:
            set_parts.append("visibility_defaults = %s::jsonb")
            params.append(payload.visibility_defaults.model_dump_json())

        if not set_parts:
            # Nothing to update — fetch and return current state.
            cur.execute(
                "SELECT id, family_id, display_name, grade_label, created_at, "
                "       archived_at, visibility_defaults "
                "FROM children WHERE id = %s",
                (str(child_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Child {child_id} not found or not accessible")
            return _row_to_child(row)

        params.append(str(child_id))
        sql = (
            "UPDATE children SET " + ", ".join(set_parts) + " WHERE id = %s "
            "RETURNING id, family_id, display_name, grade_label, created_at, "
            "          archived_at, visibility_defaults"
        )
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Child {child_id} not found or not accessible")
        self._conn.commit()
        return _row_to_child(row)

    def archive_child(self, child_id: uuid.UUID) -> ChildResponse:
        """Set archived_at = now().

        The child will no longer appear in list_children() (active-only).
        Raises ValueError if the child is not found or not accessible (RLS).
        """
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE children
            SET archived_at = now()
            WHERE id = %s AND archived_at IS NULL
            RETURNING id, family_id, display_name, grade_label, created_at,
                      archived_at, visibility_defaults
            """,
            (str(child_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Child {child_id} not found, already archived, or not accessible")
        self._conn.commit()
        return _row_to_child(row)

    # -- Subject --

    def create_subject(
        self,
        family_id: uuid.UUID,
        child_id: uuid.UUID,
        name: str,
        content_language: str,
    ) -> SubjectResponse:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO subjects (family_id, child_id, name, content_language)
            VALUES (%s, %s, %s, %s)
            RETURNING id, family_id, child_id, name, content_language, created_at
            """,
            (str(family_id), str(child_id), name, content_language),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO subjects returned no row")
        self._conn.commit()
        return SubjectResponse(
            id=uuid.UUID(str(row["id"])),
            family_id=uuid.UUID(str(row["family_id"])),
            child_id=uuid.UUID(str(row["child_id"])),
            name=str(row["name"]),
            content_language=str(row["content_language"]),
            created_at=_parse_dt(row["created_at"]),
        )

    def list_subjects(self, family_id: uuid.UUID) -> list[SubjectResponse]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, family_id, child_id, name, content_language, created_at "
            "FROM subjects WHERE family_id = %s ORDER BY created_at",
            (str(family_id),),
        )
        return [
            SubjectResponse(
                id=uuid.UUID(str(r["id"])),
                family_id=uuid.UUID(str(r["family_id"])),
                child_id=uuid.UUID(str(r["child_id"])),
                name=str(r["name"]),
                content_language=str(r["content_language"]),
                created_at=_parse_dt(r["created_at"]),
            )
            for r in cur.fetchall()
        ]

    # -- Cycle --

    def create_cycle(
        self,
        family_id: uuid.UUID,
        subject_id: uuid.UUID,
        scope_text: str,
    ) -> CycleResponse:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO cycles (family_id, subject_id, scope_text, state)
            VALUES (%s, %s, %s, 'SCOPE_UPLOADED')
            RETURNING id, family_id, subject_id, state, scope_text,
                      parent_approval_at, parent_approval_note, created_at, updated_at
            """,
            (str(family_id), str(subject_id), scope_text),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO cycles returned no row")
        self._conn.commit()
        return _row_to_cycle(row)

    def get_cycle(self, cycle_id: uuid.UUID) -> CycleResponse | None:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT c.id, c.family_id, c.subject_id, c.state, c.scope_text,
                   c.parent_approval_at, c.parent_approval_note, c.created_at, c.updated_at,
                   COALESCE(
                       json_agg(a.assessment ORDER BY a.created_at)
                       FILTER (WHERE a.id IS NOT NULL),
                       '[]'
                   ) AS assessments
            FROM cycles c
            LEFT JOIN assessments a ON a.cycle_id = c.id
            WHERE c.id = %s
            GROUP BY c.id
            """,
            (str(cycle_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_cycle(row)

    def list_cycles(self, family_id: uuid.UUID) -> list[CycleResponse]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT id, family_id, subject_id, state, scope_text,
                   parent_approval_at, parent_approval_note, created_at, updated_at
            FROM cycles
            WHERE family_id = %s
            ORDER BY created_at
            """,
            (str(family_id),),
        )
        return [_row_to_cycle(r) for r in cur.fetchall()]

    def update_cycle_state(
        self,
        cycle_id: uuid.UUID,
        new_state: CycleState,
        parent_approval_at: datetime | None = None,
        parent_approval_note: str | None = None,
    ) -> CycleResponse:
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE cycles
            SET state                = %s,
                updated_at           = now(),
                parent_approval_at   = COALESCE(%s, parent_approval_at),
                parent_approval_note = COALESCE(%s, parent_approval_note)
            WHERE id = %s
            RETURNING id, family_id, subject_id, state, scope_text,
                      parent_approval_at, parent_approval_note, created_at, updated_at
            """,
            (
                new_state.value,
                parent_approval_at,
                parent_approval_note,
                str(cycle_id),
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Cycle {cycle_id} not found or not accessible")
        self._conn.commit()
        return _row_to_cycle(row)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_child(row: dict[str, object]) -> ChildResponse:
    vd_raw = row.get("visibility_defaults")
    if isinstance(vd_raw, dict):
        vd = VisibilityDefaults.model_validate(vd_raw)
    elif isinstance(vd_raw, str):
        vd = VisibilityDefaults.model_validate_json(vd_raw)
    else:
        vd = VisibilityDefaults()

    archived_raw = row.get("archived_at")
    archived_at = _parse_dt(archived_raw) if archived_raw is not None else None

    return ChildResponse(
        id=uuid.UUID(str(row["id"])),
        family_id=uuid.UUID(str(row["family_id"])),
        display_name=str(row["display_name"]),
        grade_label=str(row["grade_label"]),
        created_at=_parse_dt(row["created_at"]),
        archived_at=archived_at,
        visibility_defaults=vd,
    )


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


def _row_to_cycle(row: dict[str, object]) -> CycleResponse:
    assessments_raw = row.get("assessments", [])
    raw_list: list[object]
    if isinstance(assessments_raw, list):
        raw_list = list(assessments_raw)
    elif isinstance(assessments_raw, str):
        loaded = json.loads(assessments_raw)
        raw_list = list(loaded) if isinstance(loaded, list) else []
    else:
        raw_list = []

    # Validate each stored JSONB document into the canonical Assessment model.
    # The JSONB was written through the generation validate gate, so round-trips
    # cleanly.  On schema mismatch the ValidationError propagates — never silently
    # swallow bad stored data.
    assessments: list[Assessment] = [Assessment.model_validate(doc) for doc in raw_list]

    return CycleResponse(
        id=uuid.UUID(str(row["id"])),
        family_id=uuid.UUID(str(row["family_id"])),
        subject_id=uuid.UUID(str(row["subject_id"])),
        state=CycleState(str(row["state"])),
        scope_text=str(row["scope_text"]) if row.get("scope_text") else None,
        parent_approval_at=(
            _parse_dt(row["parent_approval_at"]) if row.get("parent_approval_at") else None
        ),
        parent_approval_note=(
            str(row["parent_approval_note"]) if row.get("parent_approval_note") else None
        ),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        assessments=assessments,
    )
