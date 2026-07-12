"""Postgres-backed ``QuestionMarkRepository``.

Invariant 1: every query runs as the non-privileged ``authenticated`` role
via ``open_authenticated_connection`` so RLS is enforced by the DB.

Invariant 3: ``family_id`` is always derived server-side from the
submissions → assessments → cycles join — never accepted from the client.

Bulk upsert uses INSERT ... ON CONFLICT (submission_id, question_id) DO UPDATE
so re-grading (idempotent) works without stale data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from schemas.assessment_schema import ErrorCategory, GradingPath
from schemas.grading import QuestionMark
from schemas.review import MarkPatchRequest
from services.repositories.postgres import DictConn


class PostgresQuestionMarkRepository:
    """Satisfies the ``QuestionMarkRepository`` protocol for Postgres.

    The caller must supply a live psycopg connection already set up by
    ``open_authenticated_connection`` (SET ROLE authenticated + GUC).
    """

    def __init__(self, conn: DictConn) -> None:
        self._conn = conn

    def bulk_upsert(
        self,
        family_id: uuid.UUID,
        submission_id: uuid.UUID,
        marks: list[QuestionMark],
    ) -> list[QuestionMark]:
        """Upsert all marks for a submission in one batch.

        Uses ON CONFLICT (submission_id, question_id) DO UPDATE so that
        re-grading (idempotent POST /grade) replaces stale marks.

        Returns the marks as persisted (with created_at filled from DB).
        """
        if not marks:
            return []

        cur = self._conn.cursor()
        persisted: list[QuestionMark] = []

        for mark in marks:
            cur.execute(
                """
                INSERT INTO question_marks (
                    id, family_id, submission_id, question_id,
                    marks_total, suggested_marks, final_marks,
                    grading_path, confidence, needs_review,
                    ai_rationale, matched_alternative, error_category,
                    reviewed_at, overridden_at
                ) VALUES (
                    %(id)s, %(family_id)s, %(submission_id)s, %(question_id)s,
                    %(marks_total)s, %(suggested_marks)s, %(final_marks)s,
                    %(grading_path)s, %(confidence)s, %(needs_review)s,
                    %(ai_rationale)s, %(matched_alternative)s, %(error_category)s,
                    %(reviewed_at)s, %(overridden_at)s
                )
                ON CONFLICT (submission_id, question_id) DO UPDATE SET
                    marks_total        = EXCLUDED.marks_total,
                    suggested_marks    = EXCLUDED.suggested_marks,
                    final_marks        = EXCLUDED.final_marks,
                    grading_path       = EXCLUDED.grading_path,
                    confidence         = EXCLUDED.confidence,
                    needs_review       = EXCLUDED.needs_review,
                    ai_rationale       = EXCLUDED.ai_rationale,
                    matched_alternative = EXCLUDED.matched_alternative,
                    error_category     = EXCLUDED.error_category,
                    reviewed_at        = EXCLUDED.reviewed_at,
                    overridden_at      = EXCLUDED.overridden_at
                RETURNING id, created_at
                """,
                {
                    "id": str(mark.id),
                    "family_id": str(family_id),
                    "submission_id": str(submission_id),
                    "question_id": mark.question_id,
                    "marks_total": str(mark.marks_total),
                    "suggested_marks": str(mark.suggested_marks),
                    "final_marks": (
                        str(mark.final_marks) if mark.final_marks is not None else None
                    ),
                    "grading_path": mark.grading_path.value,
                    "confidence": (str(mark.confidence) if mark.confidence is not None else None),
                    "needs_review": mark.needs_review,
                    "ai_rationale": mark.ai_rationale,
                    "matched_alternative": mark.matched_alternative,
                    "error_category": (
                        mark.error_category.value if mark.error_category is not None else None
                    ),
                    "reviewed_at": mark.reviewed_at,
                    "overridden_at": mark.overridden_at,
                },
            )
            row = cur.fetchone()
            if row is not None:
                created_raw = row["created_at"]
                if isinstance(created_raw, datetime):
                    created_dt = (
                        created_raw
                        if created_raw.tzinfo is not None
                        else created_raw.replace(tzinfo=UTC)
                    )
                else:
                    created_dt = datetime.fromisoformat(str(created_raw)).replace(tzinfo=UTC)

                persisted.append(mark.model_copy(update={"created_at": created_dt}))
            else:
                persisted.append(mark)

        self._conn.commit()
        return persisted

    def list_for_submission(self, submission_id: uuid.UUID) -> list[QuestionMark]:
        """Return all marks for a submission, ordered by created_at."""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT id, family_id, submission_id, question_id,
                   marks_total, suggested_marks, final_marks,
                   grading_path, confidence, needs_review,
                   ai_rationale, matched_alternative, error_category,
                   reviewed_at, overridden_at, created_at
            FROM question_marks
            WHERE submission_id = %s
            ORDER BY created_at
            """,
            (str(submission_id),),
        )
        rows = cur.fetchall()
        return [_row_to_mark(row) for row in rows]

    def list_for_cycle(
        self,
        cycle_id: uuid.UUID,
    ) -> list[QuestionMark]:
        """Return all marks for the most recent submission of a cycle.

        Joins through submissions → assessments to find the cycle's submission.
        """
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT qm.id, qm.family_id, qm.submission_id, qm.question_id,
                   qm.marks_total, qm.suggested_marks, qm.final_marks,
                   qm.grading_path, qm.confidence, qm.needs_review,
                   qm.ai_rationale, qm.matched_alternative, qm.error_category,
                   qm.reviewed_at, qm.overridden_at, qm.created_at
            FROM question_marks qm
            JOIN submissions s ON s.id = qm.submission_id
            JOIN assessments a ON a.id = s.assessment_id
            WHERE a.cycle_id = %s
            ORDER BY qm.created_at
            """,
            (str(cycle_id),),
        )
        rows = cur.fetchall()
        return [_row_to_mark(row) for row in rows]

    def get_submission_id_for_cycle(self, cycle_id: uuid.UUID) -> uuid.UUID | None:
        """Find the submission_id for a cycle (returns the most recent one)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT s.id
            FROM submissions s
            JOIN assessments a ON a.id = s.assessment_id
            WHERE a.cycle_id = %s
            ORDER BY s.created_at DESC
            LIMIT 1
            """,
            (str(cycle_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return uuid.UUID(str(row["id"]))

    def get_mark(
        self,
        submission_id: uuid.UUID,
        question_id: str,
    ) -> QuestionMark | None:
        """Fetch a single mark by (submission_id, question_id)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT id, family_id, submission_id, question_id,
                   marks_total, suggested_marks, final_marks,
                   grading_path, confidence, needs_review,
                   ai_rationale, matched_alternative, error_category,
                   reviewed_at, overridden_at, created_at
            FROM question_marks
            WHERE submission_id = %s AND question_id = %s
            """,
            (str(submission_id), question_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_mark(row)

    def update_mark(
        self,
        submission_id: uuid.UUID,
        question_id: str,
        patch: MarkPatchRequest,
        now: datetime,
    ) -> QuestionMark:
        """Apply parent review patch: final_marks, reviewed_at, overridden_at, error_category.

        Raises ValueError if the mark is not found or not accessible (RLS).
        Sets overridden_at when final_marks differs from suggested_marks.
        """
        # Fetch current suggested_marks to determine if this is an override.
        cur = self._conn.cursor()
        cur.execute(
            "SELECT suggested_marks FROM question_marks "
            "WHERE submission_id = %s AND question_id = %s",
            (str(submission_id), question_id),
        )
        existing_row = cur.fetchone()
        if existing_row is None:
            raise ValueError(
                f"Mark not found: submission_id={submission_id} question_id={question_id}"
            )
        suggested = Decimal(str(existing_row["suggested_marks"]))

        # Build dynamic SET clause.
        set_parts: list[str] = ["reviewed_at = %s"]
        params: list[Any] = [now]

        if patch.final_marks is not None:
            set_parts.append("final_marks = %s")
            params.append(str(patch.final_marks))
            if patch.final_marks != suggested:
                set_parts.append("overridden_at = %s")
                params.append(now)

        if patch.error_category is not None:
            set_parts.append("error_category = %s")
            params.append(patch.error_category.value)

        params.extend([str(submission_id), question_id])

        sql = (
            "UPDATE question_marks SET "
            + ", ".join(set_parts)
            + " WHERE submission_id = %s AND question_id = %s "
            "RETURNING id, family_id, submission_id, question_id, "
            "marks_total, suggested_marks, final_marks, "
            "grading_path, confidence, needs_review, "
            "ai_rationale, matched_alternative, error_category, "
            "reviewed_at, overridden_at, created_at"
        )
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"Mark not found after update: submission_id={submission_id} "
                f"question_id={question_id}"
            )
        self._conn.commit()
        return _row_to_mark(row)


def _row_to_mark(row: dict[str, Any]) -> QuestionMark:
    """Convert a DB row dict to a QuestionMark model."""

    def _to_decimal(v: Any) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v))

    def _to_dt(v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo is not None else v.replace(tzinfo=UTC)
        return datetime.fromisoformat(str(v)).replace(tzinfo=UTC)

    grading_path_raw = str(row["grading_path"])
    grading_path = GradingPath(grading_path_raw)

    error_cat_raw = row.get("error_category")
    error_cat = ErrorCategory(str(error_cat_raw)) if error_cat_raw is not None else None

    marks_total_d = _to_decimal(row["marks_total"])
    assert marks_total_d is not None
    suggested_d = _to_decimal(row["suggested_marks"])
    assert suggested_d is not None

    return QuestionMark(
        id=uuid.UUID(str(row["id"])),
        family_id=uuid.UUID(str(row["family_id"])),
        submission_id=uuid.UUID(str(row["submission_id"])),
        question_id=str(row["question_id"]),
        marks_total=marks_total_d,
        suggested_marks=suggested_d,
        final_marks=_to_decimal(row["final_marks"]),
        grading_path=grading_path,
        confidence=_to_decimal(row.get("confidence")),
        needs_review=bool(row["needs_review"]),
        ai_rationale=(str(row["ai_rationale"]) if row.get("ai_rationale") is not None else None),
        matched_alternative=(
            str(row["matched_alternative"]) if row.get("matched_alternative") is not None else None
        ),
        error_category=error_cat,
        reviewed_at=_to_dt(row.get("reviewed_at")),
        overridden_at=_to_dt(row.get("overridden_at")),
        created_at=_to_dt(row.get("created_at")),
    )
