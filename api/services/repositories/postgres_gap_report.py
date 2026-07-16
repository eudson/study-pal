"""Postgres-backed ``GapReportRepository``.

Invariant 1: every query runs as the non-privileged ``authenticated`` role via
``open_authenticated_connection`` so RLS is enforced by the DB.

Invariant 3: ``family_id`` is always derived server-side from the cycles → family
join — never accepted from the client.

Upsert on (cycle_id) unique constraint: re-running derive + upsert overwrites
the previous row so regenerate is idempotent.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from schemas.gap_report import GapReport, GapReportRow
from services.repositories.postgres import DictConn


class PostgresGapReportRepository:
    """Satisfies the ``GapReportRepository`` protocol for Postgres.

    The caller must supply a live psycopg connection already set up by
    ``open_authenticated_connection`` (SET ROLE authenticated + GUC).
    """

    def __init__(self, conn: DictConn) -> None:
        self._conn = conn

    def upsert(
        self,
        family_id: uuid.UUID,
        cycle_id: uuid.UUID,
        submission_id: uuid.UUID,
        report: GapReport,
        round: int = 1,  # noqa: A002
    ) -> GapReportRow:
        """Insert or overwrite the gap report for a cycle + round.

        Uses ON CONFLICT (cycle_id, round) DO UPDATE so re-generation is
        idempotent (design docs/design/round-phase-architecture.md §4.3). The
        row id is preserved on re-runs (via EXCLUDED is ignored in favour of
        the existing primary key on conflict, but created_at is refreshed).

        ``round`` defaults to 1 (round 1 / Variant A, unchanged behaviour);
        round-aware callers (P4) pass it explicitly.
        """
        report_json = report.model_dump_json()

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO gap_reports (
                family_id, cycle_id, submission_id, report, round
            ) VALUES (
                %(family_id)s, %(cycle_id)s, %(submission_id)s, %(report)s::jsonb, %(round)s
            )
            ON CONFLICT (cycle_id, round) DO UPDATE SET
                submission_id = EXCLUDED.submission_id,
                report        = EXCLUDED.report,
                created_at    = now()
            RETURNING id, family_id, cycle_id, submission_id, report, created_at, round
            """,
            {
                "family_id": str(family_id),
                "cycle_id": str(cycle_id),
                "submission_id": str(submission_id),
                "report": report_json,
                "round": round,
            },
        )
        row = cur.fetchone()
        assert row is not None, "upsert returned no row"
        self._conn.commit()
        return _row_to_gap_report_row(row)

    def get_for_cycle(self, cycle_id: uuid.UUID, round: int = 1) -> GapReportRow | None:  # noqa: A002
        """Return the gap report row for a cycle + round, or None if not yet generated.

        ``round`` defaults to 1 (round 1 / Variant A, unchanged behaviour);
        round-aware callers (P4) pass it explicitly.
        """
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT id, family_id, cycle_id, submission_id, report, created_at, round
            FROM gap_reports
            WHERE cycle_id = %s AND round = %s
            """,
            (str(cycle_id), round),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_gap_report_row(row)


def _row_to_gap_report_row(row: dict[str, Any]) -> GapReportRow:
    """Convert a DB row dict to a GapReportRow model."""

    def _to_dt(v: Any) -> datetime:
        if isinstance(v, datetime):
            return v if v.tzinfo is not None else v.replace(tzinfo=UTC)
        return datetime.fromisoformat(str(v)).replace(tzinfo=UTC)

    # The report column comes back as a dict (psycopg parses JSONB automatically)
    # or as a JSON string depending on the psycopg version / row factory.
    raw_report = row["report"]
    if isinstance(raw_report, str):
        raw_report = json.loads(raw_report)

    report = GapReport.model_validate(raw_report)

    return GapReportRow(
        id=uuid.UUID(str(row["id"])),
        family_id=uuid.UUID(str(row["family_id"])),
        cycle_id=uuid.UUID(str(row["cycle_id"])),
        submission_id=uuid.UUID(str(row["submission_id"])),
        report=report,
        created_at=_to_dt(row["created_at"]),
        round=int(row["round"]),
    )
