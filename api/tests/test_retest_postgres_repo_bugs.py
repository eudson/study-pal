"""DB-tier regression tests for the two Postgres-repo bugs that blocked the
round-2 ("Variant B") retest kickoff, found in the 2026-07-23 live browser
re-test (docs/PROGRESS.md, "Round 2 (retest) — BLOCKED by two live
Postgres-repo bugs").

Both bugs were invisible to the in-memory tier (``services/repositories/
memory.py``), which is why the 447/487 no-DB + DB-tier suites were green
while the live stack 404'd/500'd:

- **Bug A** (404 "No Variant-A assessment found"): ``postgres_family.py``'s
  ``update_cycle_state`` / ``publish_marks`` ``UPDATE ... RETURNING`` clauses
  never joined the ``assessments`` table, unlike ``get_cycle``'s
  ``json_agg`` LEFT JOIN. So ``start_next_round`` (which calls
  ``update_cycle_state``) returned a ``CycleResponse`` with
  ``assessments=[]``, and ``routers/retest.py::generate_variant_b`` ->
  ``resolve_assessment(cycle, "A")`` -> ``None`` -> 404. The in-memory
  repo's ``update_cycle_state`` uses ``cycle.model_copy(...)``, which
  preserves ``assessments`` — the bug only exists in the Postgres layer.
  Fixed by making ``update_cycle_state``/``publish_marks`` do the same
  join-and-aggregate ``get_cycle`` does, so EVERY consumer of a
  cycle-transition return value (not just retest) sees a consistent
  ``CycleResponse`` — this also covers ``approve_cycle``/``complete_cycle``,
  which return the transition's ``CycleResponse`` directly to the API
  caller.

- **Bug B** (500 duplicate key): ``postgres.py::AssessmentRepository.save``'s
  ``INSERT INTO assessments`` never set ``round`` (migration
  ``0013_round_phase_assessments.sql`` added ``round int NOT NULL DEFAULT 1``
  + ``UNIQUE(cycle_id, round)``), so a round-2 assessment always defaulted to
  ``round=1`` and collided with round 1's row. Fixed by deriving ``round``
  from the owning cycle row in the same query that resolves ``family_id`` —
  ``variant`` stays a display label only, never branched on.

These tests require a live Postgres instance (skipped automatically when
unreachable, mirroring ``tests/test_bootstrap_rls.py`` / ``test_phase_p2.py``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import psycopg
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

from schemas.assessment_schema import Assessment, GradingPath
from schemas.capture import SubmissionCreate
from schemas.family import CyclePhase, VisibilityDefaults
from schemas.grading import QuestionMark
from services.cycle import (
    advance_to_answers_entered,
    advance_to_auto_marked,
    advance_to_generating,
    advance_to_parent_review_marks,
    advance_to_parent_reviews,
    approve_draft,
    publish_marks,
    start_next_round,
)
from services.phase import resolve_assessment
from tests.samples.maths_sample import maths_assessment

_DSN = os.environ.get("STUDYPAL_DB_DSN", "postgresql://studypal:studypal@localhost:5432/studypal")


def _try_connect_pg() -> bool:
    try:
        conn = psycopg.connect(_DSN, connect_timeout=3, autocommit=True)
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def _pg_owner_conn() -> Generator[Any, None, None]:
    conn = psycopg.connect(_DSN, row_factory=psycopg.rows.dict_row, autocommit=False)
    yield conn
    conn.close()


def _pg_repos(user_id: uuid.UUID) -> Any:
    """Return (conn, family_repo, assessment_repo, submission_repo, marks_repo)."""
    from config import get_settings
    from schemas.identity import Identity
    from services.repositories.postgres import (
        PostgresAssessmentRepository,
        open_authenticated_connection,
    )
    from services.repositories.postgres_family import PostgresFamilyRepository
    from services.repositories.postgres_marks import PostgresQuestionMarkRepository
    from services.repositories.postgres_submission import PostgresSubmissionRepository

    settings = get_settings()
    identity = Identity(user_id=user_id)
    conn = open_authenticated_connection(settings.db_dsn, identity)
    return (
        conn,
        PostgresFamilyRepository(conn),
        PostgresAssessmentRepository(conn),
        PostgresSubmissionRepository(conn),
        PostgresQuestionMarkRepository(conn),
    )


def _marks(submission_id: uuid.UUID, family_id: uuid.UUID) -> list[QuestionMark]:
    """Fully-reviewed marks matching ``maths_assessment()``'s question ids/totals."""
    return [
        QuestionMark(
            family_id=family_id,
            submission_id=submission_id,
            question_id=qid,
            marks_total=Decimal(total),
            suggested_marks=Decimal(final),
            final_marks=Decimal(final),
            grading_path=GradingPath.AUTO,
            needs_review=False,
        )
        for qid, total, final in [
            ("A.1", "1.0", "1.0"),
            ("A.2", "2.0", "2.0"),
            ("B.1", "3.0", "1.5"),
            ("B.2", "3.0", "1.5"),
        ]
    ]


def _cleanup_family(owner_conn: Any, family_id: uuid.UUID, user_id: uuid.UUID) -> None:
    cur = owner_conn.cursor()
    cur.execute("DELETE FROM question_marks WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM submissions WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM assessments WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM gap_reports WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM study_packs WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM cycle_round_approvals WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM cycles WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM subjects WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM children WHERE family_id = %s", (str(family_id),))
    cur.execute("DELETE FROM family_members WHERE user_id = %s", (str(user_id),))
    cur.execute("DELETE FROM families WHERE id = %s", (str(family_id),))
    owner_conn.commit()


def _build_round1_to_published(
    family_repo: Any,
    assessment_repo: Any,
    submission_repo: Any,
    marks_repo: Any,
    *,
    family_name: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Walk a fresh cycle through round 1 to a settled PUBLISHED phase
    (pack skipped — legal ``start_next_round`` predecessor per design §5),
    with a Variant-A assessment saved and fully-reviewed marks persisted.

    Returns (cycle_id, family_id, child_id).
    """
    family, child_id = family_repo.bootstrap_family(family_name, "Kid", "Grade 5")
    assert child_id is not None
    subject = family_repo.create_subject(family.id, child_id, "Mathematics", "en")
    cycle = family_repo.create_cycle(family.id, subject.id, "Grade 5 measurement & calculation")
    cycle_id = cycle.id

    advance_to_generating(family_repo, cycle_id)

    assessment = Assessment.model_validate(
        {
            **maths_assessment(),
            "assessment_id": str(uuid.uuid4()),
            "cycle_id": str(cycle_id),
        }
    )
    assessment_repo.save(assessment)

    advance_to_parent_reviews(family_repo, cycle_id)
    approve_draft(family_repo, cycle_id)
    advance_to_answers_entered(family_repo, cycle_id)

    submission = submission_repo.create_submission(
        family.id,
        assessment.assessment_id,
        SubmissionCreate(child_id=child_id, responses=[], proof_photo_paths=[]),
        cycle_id,
    )
    submission_id = uuid.UUID(str(submission.submission_id))
    marks_repo.bulk_upsert(family.id, submission_id, _marks(submission_id, family.id))

    advance_to_auto_marked(family_repo, cycle_id)
    advance_to_parent_review_marks(family_repo, cycle_id)
    publish_marks(family_repo, cycle_id, VisibilityDefaults())

    return cycle_id, family.id, child_id


@pytest.mark.skipif(not _try_connect_pg(), reason="Postgres not reachable")
class TestBugAStartNextRoundCarriesAssessments:
    """``start_next_round`` must return a cycle whose ``assessments`` list
    still contains round 1's Variant-A assessment (Bug A regression)."""

    def test_start_next_round_returned_cycle_has_variant_a_assessment(
        self, _pg_owner_conn: Any
    ) -> None:
        user_id = uuid.uuid4()
        conn, family_repo, assessment_repo, submission_repo, marks_repo = _pg_repos(user_id)
        family_id: uuid.UUID | None = None
        try:
            cycle_id, family_id, _child_id = _build_round1_to_published(
                family_repo,
                assessment_repo,
                submission_repo,
                marks_repo,
                family_name="BugARoundStart",
            )

            updated = start_next_round(family_repo, cycle_id)

            # This is the exact assertion that would have caught Bug A: before
            # the fix, `updated.assessments` was `[]` because
            # `update_cycle_state`'s RETURNING clause didn't join assessments.
            assert updated.assessments, (
                "start_next_round's returned cycle lost its assessments — "
                "Bug A regression (postgres_family.py RETURNING clause)"
            )
            variant_a = resolve_assessment(updated, "A")
            assert variant_a is not None, "Variant-A assessment must resolve from the round-2 cycle"

            # A fresh GET (get_cycle) must agree.
            refetched = family_repo.get_cycle(cycle_id)
            assert refetched is not None
            assert resolve_assessment(refetched, "A") is not None
        finally:
            conn.close()
            if family_id is not None:
                _cleanup_family(_pg_owner_conn, family_id, user_id)


@pytest.mark.skipif(not _try_connect_pg(), reason="Postgres not reachable")
class TestBugBVariantBAssessmentPersistsWithRound2:
    """``POST /cycles/{id}/variant-b`` from a settled round-1 PUBLISHED cycle
    must succeed: the round-2 assessment persists with ``round=2`` (no
    ``UNIQUE(cycle_id, round)`` collision — Bug B regression), and the cycle
    advances to round-2 DRAFT_REVIEW."""

    def test_variant_b_kickoff_end_to_end(self, _pg_owner_conn: Any) -> None:
        user_id = uuid.uuid4()
        conn, family_repo, assessment_repo, submission_repo, marks_repo = _pg_repos(user_id)
        family_id: uuid.UUID | None = None
        try:
            cycle_id, family_id, _child_id = _build_round1_to_published(
                family_repo,
                assessment_repo,
                submission_repo,
                marks_repo,
                family_name="BugBVariantBKickoff",
            )
        finally:
            conn.close()

        headers = {"x-user-id": str(user_id)}
        try:
            from main import app

            # No dependency overrides: dependencies.py's default providers are
            # already Postgres-backed (production wiring), so this exercises
            # the real POST /cycles/{id}/variant-b route end-to-end exactly as
            # the live browser re-test did.
            with TestClient(app) as client:
                resp = client.post(f"/cycles/{cycle_id}/variant-b", headers=headers)
                assert resp.status_code == 201, resp.text
                body = resp.json()
                assert body["variant"] == "B"
                assessment_b_id = body["assessment_id"]

                # Idempotent re-call: must still succeed (not a second
                # duplicate-key 500) and return the same assessment.
                resp2 = client.post(f"/cycles/{cycle_id}/variant-b", headers=headers)
                assert resp2.status_code == 201, resp2.text
                assert resp2.json()["assessment_id"] == assessment_b_id

            # Verify persistence directly: exactly one round-1 (A) row and one
            # round-2 (B) row, no collision.
            cur = _pg_owner_conn.cursor()
            cur.execute(
                "SELECT variant, round FROM assessments WHERE cycle_id = %s ORDER BY round",
                (str(cycle_id),),
            )
            rows = cur.fetchall()
            assert [(str(r["variant"]), int(r["round"])) for r in rows] == [
                ("A", 1),
                ("B", 2),
            ], f"Unexpected assessments rows: {rows}"

            # Cycle advanced to round 2, DRAFT_REVIEW (paper generated, awaiting
            # parent approval — golden rule 8, same as round 1).
            _conn2, family_repo2, _a, _s, _m = _pg_repos(user_id)
            try:
                refetched = family_repo2.get_cycle(cycle_id)
                assert refetched is not None
                assert refetched.round == 2
                assert refetched.phase is CyclePhase.DRAFT_REVIEW
                # Both variants still resolve off the round-2 cycle object.
                assert resolve_assessment(refetched, "A") is not None
                assert resolve_assessment(refetched, "B") is not None
            finally:
                _conn2.close()
        finally:
            if family_id is not None:
                _cleanup_family(_pg_owner_conn, family_id, user_id)
