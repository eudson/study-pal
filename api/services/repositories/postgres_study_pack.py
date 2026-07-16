"""Postgres-backed ``StudyPackRepository``.

Invariant 1: every query runs as the non-privileged ``authenticated`` role via
``open_authenticated_connection`` so RLS is enforced by the DB.

Invariant 3: ``family_id`` is always derived server-side from the cycles → family
join — never accepted from the client.

Upsert on (cycle_id) unique constraint: re-running generate + upsert overwrites
the previous row so regenerate is idempotent.  ``approved_at`` is preserved on
re-upsert (not overwritten), so approval survives a re-generate.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from schemas.study_pack import StudyPack, StudyPackRow
from services.repositories.postgres import DictConn


class PostgresStudyPackRepository:
    """Satisfies the ``StudyPackRepository`` protocol for Postgres.

    The caller must supply a live psycopg connection already set up by
    ``open_authenticated_connection`` (SET ROLE authenticated + GUC).
    """

    def __init__(self, conn: DictConn) -> None:
        self._conn = conn

    def upsert(
        self,
        family_id: uuid.UUID,
        cycle_id: uuid.UUID,
        pack: StudyPack,
    ) -> StudyPackRow:
        """Insert or overwrite the study pack for a cycle.

        Uses ON CONFLICT (cycle_id, round) DO UPDATE so re-generation is
        idempotent (design docs/design/round-phase-architecture.md §4.4).
        ``approved_at`` is NOT overwritten on conflict — approval survives regeneration.

        P1: no caller threads a round through this Protocol method yet — the
        only existing call sites are the round-1 (Variant A) path, so round
        is hardcoded to 1 here (design §7 P1 scope; round-aware callers land
        in P4).
        """
        pack_json = pack.model_dump_json()

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO study_packs (
                family_id, cycle_id, pack, round
            ) VALUES (
                %(family_id)s, %(cycle_id)s, %(pack)s::jsonb, 1
            )
            ON CONFLICT (cycle_id, round) DO UPDATE SET
                pack       = EXCLUDED.pack,
                created_at = now()
            RETURNING id, family_id, cycle_id, pack, approved_at, created_at
            """,
            {
                "family_id": str(family_id),
                "cycle_id": str(cycle_id),
                "pack": pack_json,
            },
        )
        row = cur.fetchone()
        assert row is not None, "upsert returned no row"
        self._conn.commit()
        return _row_to_study_pack_row(row)

    def get_for_cycle(self, cycle_id: uuid.UUID) -> StudyPackRow | None:
        """Return the round-1 study pack row for a cycle, or None if not yet generated.

        P1: this Protocol method is not yet round-parameterized (design §7 P1
        scope — round-aware callers land in P4), so it is pinned to round=1,
        matching the only round upsert() currently writes.
        """
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT id, family_id, cycle_id, pack, approved_at, created_at
            FROM study_packs
            WHERE cycle_id = %s AND round = 1
            """,
            (str(cycle_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_study_pack_row(row)

    def set_approved_at(
        self,
        cycle_id: uuid.UUID,
        approved_at: datetime,
    ) -> StudyPackRow:
        """Record parent approval: set approved_at on the study pack row (round 1, P1 scope)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE study_packs
            SET approved_at = %(approved_at)s
            WHERE cycle_id = %(cycle_id)s AND round = 1
            RETURNING id, family_id, cycle_id, pack, approved_at, created_at
            """,
            {
                "cycle_id": str(cycle_id),
                "approved_at": approved_at.isoformat(),
            },
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No study pack found for cycle {cycle_id} — generate one first.")
        self._conn.commit()
        return _row_to_study_pack_row(row)


def _row_to_study_pack_row(row: dict[str, Any]) -> StudyPackRow:
    """Convert a DB row dict to a StudyPackRow model."""

    def _to_dt(v: Any) -> datetime:
        if isinstance(v, datetime):
            return v if v.tzinfo is not None else v.replace(tzinfo=UTC)
        return datetime.fromisoformat(str(v)).replace(tzinfo=UTC)

    def _to_dt_opt(v: Any) -> datetime | None:
        if v is None:
            return None
        return _to_dt(v)

    raw_pack = row["pack"]
    if isinstance(raw_pack, str):
        raw_pack = json.loads(raw_pack)

    pack = StudyPack.model_validate(raw_pack)

    return StudyPackRow(
        id=uuid.UUID(str(row["id"])),
        family_id=uuid.UUID(str(row["family_id"])),
        cycle_id=uuid.UUID(str(row["cycle_id"])),
        pack=pack,
        approved_at=_to_dt_opt(row["approved_at"]),
        created_at=_to_dt(row["created_at"]),
    )
