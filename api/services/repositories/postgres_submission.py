"""Postgres-backed ``SubmissionRepository``.

Invariant 1: every query runs as the non-privileged ``authenticated`` role
via ``open_authenticated_connection`` so RLS is enforced by the DB.

Invariant 3: ``family_id`` is always derived server-side from the assessments
/ cycles join — never accepted from the client.

Photo paths are stored as-is in the JSONB; they are NEVER used for grading
(ARCHITECTURE.md §10 no-vision-grading decision).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from schemas.capture import ChildResponseItem, SubmissionCreate, SubmissionResponse
from services.repositories.postgres import DictConn


class PostgresSubmissionRepository:
    """Satisfies the ``SubmissionRepository`` protocol for Postgres.

    The caller must supply a live *psycopg* connection already set up by
    ``open_authenticated_connection`` (SET ROLE authenticated + GUC).
    """

    def __init__(self, conn: DictConn) -> None:
        self._conn = conn

    def create_submission(
        self,
        family_id: uuid.UUID,
        assessment_id: str,
        payload: SubmissionCreate,
        cycle_id: uuid.UUID,
    ) -> SubmissionResponse:
        """INSERT a new submission row and return the populated response model."""
        submission_doc = {
            "child_id": str(payload.child_id),
            "responses": [r.model_dump() for r in payload.responses],
            # proof_photo_paths stored for audit; NEVER fed to grading.
            "proof_photo_paths": list(payload.proof_photo_paths),
        }

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO submissions (family_id, assessment_id, child_id, submission)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING id, created_at
            """,
            (
                str(family_id),
                assessment_id,
                str(payload.child_id),
                json.dumps(submission_doc),
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO submissions returned no row")
        self._conn.commit()

        created_at_raw = row["created_at"]
        if isinstance(created_at_raw, datetime):
            created_dt = (
                created_at_raw
                if created_at_raw.tzinfo is not None
                else created_at_raw.replace(tzinfo=UTC)
            )
        else:
            created_dt = datetime.fromisoformat(str(created_at_raw)).replace(tzinfo=UTC)

        return SubmissionResponse(
            submission_id=uuid.UUID(str(row["id"])),
            assessment_id=assessment_id,
            child_id=payload.child_id,
            cycle_id=cycle_id,
            responses_count=len(payload.responses),
            proof_photo_paths=list(payload.proof_photo_paths),
            created_at=created_dt.isoformat(),
        )

    def _get_responses_for_grading(self, submission_id: uuid.UUID) -> list[ChildResponseItem]:
        """Fetch the full ChildResponseItem list for grading.

        NOT part of the SubmissionRepository protocol — only used by the
        grading router via duck-typing.  Returns the parsed response list
        from the submission JSONB.
        """
        cur = self._conn.cursor()
        cur.execute(
            "SELECT submission FROM submissions WHERE id = %s",
            (str(submission_id),),
        )
        row = cur.fetchone()
        if row is None:
            return []
        doc_raw = row["submission"]
        doc: dict[str, object] = doc_raw if isinstance(doc_raw, dict) else json.loads(str(doc_raw))
        responses_raw = doc.get("responses", [])
        results: list[ChildResponseItem] = []
        if isinstance(responses_raw, list):
            for r in responses_raw:
                if isinstance(r, dict):
                    results.append(ChildResponseItem.model_validate(r))
        return results

    def get_submission(self, submission_id: uuid.UUID) -> SubmissionResponse | None:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT s.id, s.assessment_id, s.child_id, s.submission, s.created_at,
                   a.cycle_id
            FROM submissions s
            JOIN assessments a ON a.id = s.assessment_id
            WHERE s.id = %s
            """,
            (str(submission_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None

        doc_raw = row["submission"]
        doc: dict[str, object] = doc_raw if isinstance(doc_raw, dict) else json.loads(str(doc_raw))
        responses_raw = doc.get("responses", [])
        responses: list[ChildResponseItem] = []
        if isinstance(responses_raw, list):
            for r in responses_raw:
                if isinstance(r, dict):
                    responses.append(ChildResponseItem.model_validate(r))

        proof_paths: list[str] = []
        pp_raw = doc.get("proof_photo_paths", [])
        if isinstance(pp_raw, list):
            proof_paths = [str(p) for p in pp_raw]

        created_at_raw = row["created_at"]
        if isinstance(created_at_raw, datetime):
            created_dt = (
                created_at_raw
                if created_at_raw.tzinfo is not None
                else created_at_raw.replace(tzinfo=UTC)
            )
        else:
            created_dt = datetime.fromisoformat(str(created_at_raw)).replace(tzinfo=UTC)

        return SubmissionResponse(
            submission_id=uuid.UUID(str(row["id"])),
            assessment_id=str(row["assessment_id"]),
            child_id=uuid.UUID(str(row["child_id"])),
            cycle_id=uuid.UUID(str(row["cycle_id"])),
            responses_count=len(responses),
            proof_photo_paths=proof_paths,
            created_at=created_dt.isoformat(),
        )
