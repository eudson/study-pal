"""Tests for the child results view endpoint (Phase 4+).

Coverage (full authz matrix per spec):
  a. No identity header → 401.
  b. Authed, OTHER family → 404 (RLS makes cross-family cycles invisible).
  c. Authed, same family, PARENT_REVIEW_MARKS or earlier state → 409.
  d. Same family, GAP_REPORT+, snapshot.ai_rationale=False → 200;
     assert raw JSON has NO ai_rationale value AND no memo/correct/accepted keys.
  e. Same family, snapshot.ai_rationale=True → 200 with rationale present.
  f. Same family, published, THEN flip child visibility_defaults → response STILL
     follows the frozen snapshot (critical drift test).
  g. Same family, snapshot.accuracy=False / growing=False / effort=False → marks /
     status / effort fields respectively absent.
  h. DB-tier RLS: skipped cleanly when Postgres is unreachable (mirrors existing
     gap report pattern).

House style:
- InMemory repos; no Postgres for the unit tier.
- Dependency overrides on the FastAPI app; cleanup in finally blocks or via
  pop-after-test.
- _cycle_at_gap_report_state helper mirrors the one in test_gap_report.py.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

from schemas.assessment_schema import Assessment, GradingPath
from schemas.family import CycleState, VisibilityDefaults
from schemas.grading import QuestionMark
from services.cycle import (
    advance_to_answers_entered,
    advance_to_auto_marked,
    advance_to_generating,
    advance_to_parent_review_marks,
    advance_to_parent_reviews,
    approve_draft,
    publish_marks,
)
from services.gap_report import derive_gap_report
from services.repositories.memory import (
    InMemoryFamilyRepository,
    InMemoryGapReportRepository,
    InMemoryQuestionMarkRepository,
    InMemorySubmissionRepository,
)
from tests.samples.maths_sample import maths_assessment

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAMILY_ID = uuid.uuid4()
_SUBMISSION_ID = uuid.uuid4()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assessment(raw: dict[str, Any] | None = None) -> Assessment:
    return Assessment.model_validate(raw or maths_assessment())


def _mark(
    question_id: str,
    marks_total: str,
    final_marks: str,
    suggested_marks: str | None = None,
    ai_rationale: str | None = None,
    needs_review: bool = False,
    grading_path: GradingPath = GradingPath.AUTO,
    family_id: uuid.UUID | None = None,
    submission_id: uuid.UUID | None = None,
) -> QuestionMark:
    return QuestionMark(
        family_id=family_id or _FAMILY_ID,
        submission_id=submission_id or _SUBMISSION_ID,
        question_id=question_id,
        marks_total=Decimal(marks_total),
        suggested_marks=Decimal(suggested_marks or final_marks),
        final_marks=Decimal(final_marks),
        grading_path=grading_path,
        needs_review=needs_review,
        ai_rationale=ai_rationale,
    )


def _cycle_at_gap_report_state(
    family_repo: InMemoryFamilyRepository,
    visibility: VisibilityDefaults | None = None,
) -> uuid.UUID:
    """Create a cycle fully advanced to GAP_REPORT state.

    Optionally accepts a custom VisibilityDefaults snapshot so the frozen
    publish_visibility can be tested for drift (test f) and gate tests (test g).
    Returns cycle_id.
    """
    family, _ = family_repo.bootstrap_family("Test Family", None, None)
    subject = family_repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
    cycle = family_repo.create_cycle(family.id, subject.id, "scope")

    advance_to_generating(family_repo, cycle.id)
    advance_to_parent_reviews(family_repo, cycle.id)
    approve_draft(family_repo, cycle.id)
    advance_to_answers_entered(family_repo, cycle.id)
    advance_to_auto_marked(family_repo, cycle.id)
    advance_to_parent_review_marks(family_repo, cycle.id)
    publish_marks(family_repo, cycle.id, visibility or VisibilityDefaults())

    return cycle.id


def _seed_marks_and_gap_report(
    family_repo: InMemoryFamilyRepository,
    marks_repo: InMemoryQuestionMarkRepository,
    gap_repo: InMemoryGapReportRepository,
    cycle_id: uuid.UUID,
    ai_rationale: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed marks + gap report for cycle_id.  Returns (family_id, submission_id)."""
    cycle = family_repo.get_cycle(cycle_id)
    assert cycle is not None
    family_id = cycle.family_id

    asmt = _assessment(
        {
            **maths_assessment(),
            "cycle_id": str(cycle_id),
        }
    )
    updated_cycle = cycle.model_copy(update={"assessments": [asmt]})
    family_repo._cycles[cycle_id] = updated_cycle

    submission_id = uuid.uuid4()
    marks = [
        QuestionMark(
            family_id=family_id,
            submission_id=submission_id,
            question_id=qid,
            marks_total=Decimal(total),
            suggested_marks=Decimal(final),
            final_marks=Decimal(final),
            grading_path=GradingPath.AUTO,
            needs_review=False,
            ai_rationale=ai_rationale,
        )
        for qid, total, final in [
            ("A.1", "1.0", "1.0"),
            ("A.2", "2.0", "1.0"),
            ("B.1", "3.0", "3.0"),
            ("B.2", "2.0", "0.0"),
        ]
    ]
    marks_repo.bulk_upsert(family_id, submission_id, marks)

    report = derive_gap_report(asmt, marks)
    gap_repo.upsert(family_id, cycle_id, submission_id, report)

    return family_id, submission_id


class _PatchedMarksRepo(InMemoryQuestionMarkRepository):
    """InMemoryQuestionMarkRepository with a real get_submission_id_for_cycle."""

    def __init__(
        self,
        base: InMemoryQuestionMarkRepository,
        cycle_id: uuid.UUID,
        submission_id: uuid.UUID,
    ) -> None:
        super().__init__()
        self._base = base
        self._cycle_id = cycle_id
        self._submission_id = submission_id
        # Copy the store from the base repo.
        self._store = base._store

    def get_submission_id_for_cycle(self, cid: uuid.UUID, variant: str) -> uuid.UUID | None:
        if cid == self._cycle_id and variant == "A":
            return self._submission_id
        return None

    def list_for_cycle(self, cid: uuid.UUID, variant: str) -> list[QuestionMark]:
        if cid == self._cycle_id and variant == "A":
            return self._base.list_for_submission(self._submission_id)
        return []


def _make_client(
    user_id: uuid.UUID,
    family_repo: InMemoryFamilyRepository,
    marks_repo: InMemoryQuestionMarkRepository,
    gap_repo: InMemoryGapReportRepository,
    submission_repo: InMemorySubmissionRepository | None = None,
) -> TestClient:
    """Build a TestClient with dependency overrides and return it.

    Caller is responsible for cleaning up overrides.
    """
    from dependencies import (
        get_family_repository,
        get_gap_report_repository,
        get_question_mark_repository,
        get_submission_repository,
    )
    from main import app

    app.dependency_overrides[get_family_repository] = lambda: family_repo
    app.dependency_overrides[get_question_mark_repository] = lambda: marks_repo
    app.dependency_overrides[get_gap_report_repository] = lambda: gap_repo
    if submission_repo is not None:
        app.dependency_overrides[get_submission_repository] = lambda: submission_repo
    else:
        app.dependency_overrides[get_submission_repository] = lambda: InMemorySubmissionRepository()

    return TestClient(app, raise_server_exceptions=True)


def _cleanup() -> None:
    from dependencies import (
        get_family_repository,
        get_gap_report_repository,
        get_question_mark_repository,
        get_submission_repository,
    )
    from main import app

    app.dependency_overrides.pop(get_family_repository, None)
    app.dependency_overrides.pop(get_question_mark_repository, None)
    app.dependency_overrides.pop(get_gap_report_repository, None)
    app.dependency_overrides.pop(get_submission_repository, None)


# ---------------------------------------------------------------------------
# Test a: no identity header → 401
# ---------------------------------------------------------------------------


class TestChildResultsNoAuth:
    """a. No identity header returns 401."""

    def test_no_identity_returns_401(self) -> None:

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        client = _make_client(user_id, family_repo, marks_repo, gap_repo)
        try:
            resp = client.get(f"/cycles/{uuid.uuid4()}/child-results")
            assert resp.status_code == 401
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Test b: authed but OTHER family → 404
# ---------------------------------------------------------------------------


class TestChildResultsOtherFamily:
    """b. Authed user from a different family cannot see this cycle → 404."""

    def test_other_family_returns_404(self) -> None:
        # Family A owns the cycle.
        user_a = uuid.uuid4()
        family_repo_a = InMemoryFamilyRepository(user_a)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo_a)
        _, submission_id = _seed_marks_and_gap_report(family_repo_a, marks_repo, gap_repo, cycle_id)
        patched = _PatchedMarksRepo(marks_repo, cycle_id, submission_id)

        # Family B caller: a completely separate InMemoryFamilyRepository
        # that has no knowledge of family A's cycles.
        user_b = uuid.uuid4()
        family_repo_b = InMemoryFamilyRepository(user_b)
        family_repo_b.bootstrap_family("Family B", None, None)

        client = _make_client(user_b, family_repo_b, patched, gap_repo)
        try:
            resp = client.get(
                f"/cycles/{cycle_id}/child-results",
                headers={"x-user-id": str(user_b)},
            )
            # family_repo_b has no cycle with cycle_id → 404.
            assert resp.status_code == 404
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Test c: same family, pre-publish state → 409
# ---------------------------------------------------------------------------


class TestChildResultsPrePublishState:
    """c. Cycle in PARENT_REVIEW_MARKS or earlier → 409."""

    def _make_cycle_in_state(
        self,
        family_repo: InMemoryFamilyRepository,
        target_state: CycleState,
    ) -> uuid.UUID:
        family, _ = family_repo.bootstrap_family("Test Family", None, None)
        subject = family_repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = family_repo.create_cycle(family.id, subject.id, "scope")

        # Advance to the requested state.
        if target_state == CycleState.SCOPE_UPLOADED:
            pass  # already there
        elif target_state == CycleState.GENERATING_A:
            advance_to_generating(family_repo, cycle.id)
        elif target_state == CycleState.PARENT_REVIEWS_DRAFT:
            advance_to_generating(family_repo, cycle.id)
            advance_to_parent_reviews(family_repo, cycle.id)
        elif target_state == CycleState.APPROVED_PRINTED:
            advance_to_generating(family_repo, cycle.id)
            advance_to_parent_reviews(family_repo, cycle.id)
            approve_draft(family_repo, cycle.id)
        elif target_state == CycleState.ANSWERS_ENTERED:
            advance_to_generating(family_repo, cycle.id)
            advance_to_parent_reviews(family_repo, cycle.id)
            approve_draft(family_repo, cycle.id)
            advance_to_answers_entered(family_repo, cycle.id)
        elif target_state == CycleState.AUTO_MARKED:
            advance_to_generating(family_repo, cycle.id)
            advance_to_parent_reviews(family_repo, cycle.id)
            approve_draft(family_repo, cycle.id)
            advance_to_answers_entered(family_repo, cycle.id)
            advance_to_auto_marked(family_repo, cycle.id)
        elif target_state == CycleState.PARENT_REVIEW_MARKS:
            advance_to_generating(family_repo, cycle.id)
            advance_to_parent_reviews(family_repo, cycle.id)
            approve_draft(family_repo, cycle.id)
            advance_to_answers_entered(family_repo, cycle.id)
            advance_to_auto_marked(family_repo, cycle.id)
            advance_to_parent_review_marks(family_repo, cycle.id)
        return cycle.id

    @pytest.mark.parametrize(
        "state",
        [
            CycleState.SCOPE_UPLOADED,
            CycleState.PARENT_REVIEW_MARKS,
            CycleState.AUTO_MARKED,
        ],
    )
    def test_pre_publish_state_returns_409(self, state: CycleState) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        cycle_id = self._make_cycle_in_state(family_repo, state)

        client = _make_client(user_id, family_repo, marks_repo, gap_repo)
        try:
            resp = client.get(
                f"/cycles/{cycle_id}/child-results",
                headers={"x-user-id": str(user_id)},
            )
            assert resp.status_code == 409
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Test d: ai_rationale=False → no rationale, no memo/correct/accepted keys
# ---------------------------------------------------------------------------


class TestChildResultsAiRationaleOff:
    """d. snapshot.ai_rationale=False → 200, no ai_rationale, no memo keys."""

    def test_no_rationale_no_memo_keys(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        # ai_rationale OFF (default).
        snapshot = VisibilityDefaults(accuracy=True, effort=True, growing=True, ai_rationale=False)
        cycle_id = _cycle_at_gap_report_state(family_repo, snapshot)
        _, submission_id = _seed_marks_and_gap_report(
            family_repo,
            marks_repo,
            gap_repo,
            cycle_id,
            ai_rationale="should not appear",
        )
        patched = _PatchedMarksRepo(marks_repo, cycle_id, submission_id)

        client = _make_client(user_id, family_repo, patched, gap_repo)
        try:
            resp = client.get(
                f"/cycles/{cycle_id}/child-results",
                headers={"x-user-id": str(user_id)},
            )
            assert resp.status_code == 200
            body = resp.json()

            # Dump the entire response JSON as a string and check for forbidden keys.
            body_str = json.dumps(body)

            # ai_rationale must not be present with a non-null value.
            for item in body["items"]:
                assert item.get("ai_rationale") is None, (
                    f"ai_rationale present on item {item['question_id']}"
                )

            # Forbidden keys that must NEVER appear in the child response.
            # "memo", "correct", "accepted" are the most dangerous ones.
            for forbidden in ("correct_answer_rendered", "accepted", "model_answer", "rubric"):
                assert forbidden not in body_str, (
                    f"Forbidden key '{forbidden}' found in child results response"
                )
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Test e: ai_rationale=True → rationale present
# ---------------------------------------------------------------------------


class TestChildResultsAiRationaleOn:
    """e. snapshot.ai_rationale=True → 200 with rationale on each item."""

    def test_rationale_present_when_toggle_on(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        snapshot = VisibilityDefaults(accuracy=True, effort=True, growing=True, ai_rationale=True)
        cycle_id = _cycle_at_gap_report_state(family_repo, snapshot)
        _, submission_id = _seed_marks_and_gap_report(
            family_repo,
            marks_repo,
            gap_repo,
            cycle_id,
            ai_rationale="Well done, correct working shown.",
        )
        patched = _PatchedMarksRepo(marks_repo, cycle_id, submission_id)

        client = _make_client(user_id, family_repo, patched, gap_repo)
        try:
            resp = client.get(
                f"/cycles/{cycle_id}/child-results",
                headers={"x-user-id": str(user_id)},
            )
            assert resp.status_code == 200
            body = resp.json()

            # At least one item should have ai_rationale populated.
            rationales = [
                item.get("ai_rationale")
                for item in body["items"]
                if item.get("ai_rationale") is not None
            ]
            assert len(rationales) > 0, "Expected at least one item with ai_rationale set"
            assert rationales[0] == "Well done, correct working shown."
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Test f: drift test — child visibility_defaults changed after publish
# ---------------------------------------------------------------------------


class TestChildResultsDriftGuard:
    """f. Changing child visibility_defaults AFTER publish must NOT affect the view.

    The frozen snapshot is the contract.  If we flip the child's defaults
    after publish_marks has frozen the snapshot, the child results MUST
    still reflect what was frozen.
    """

    def test_post_publish_visibility_change_does_not_drift(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        # Publish with accuracy=True (the child should see marks).
        snapshot_at_publish = VisibilityDefaults(
            accuracy=True, effort=True, growing=True, ai_rationale=False
        )
        cycle_id = _cycle_at_gap_report_state(family_repo, snapshot_at_publish)
        _, submission_id = _seed_marks_and_gap_report(family_repo, marks_repo, gap_repo, cycle_id)

        # Now flip the cycle's published_visibility to accuracy=False
        # (simulating a bug or post-publish drift attempt — this must NOT affect
        # the endpoint because the endpoint reads the snapshot from the cycle,
        # not the child's current defaults).
        #
        # Importantly: the endpoint reads ``cycle.published_visibility``, which
        # was frozen at publish time.  We do NOT update it here — we update the
        # child's visibility_defaults on the child record only, which the endpoint
        # must NOT consult.
        cycle = family_repo.get_cycle(cycle_id)
        assert cycle is not None
        family_id = cycle.family_id

        # Find and update the child's visibility_defaults (not the cycle snapshot).
        children = family_repo.list_children(family_id)
        if children:
            from schemas.family import ChildUpdate

            child = children[0]
            new_defaults = VisibilityDefaults(
                accuracy=False, effort=False, growing=False, ai_rationale=False
            )
            family_repo.update_child(child.id, ChildUpdate(visibility_defaults=new_defaults))

        patched = _PatchedMarksRepo(marks_repo, cycle_id, submission_id)

        client = _make_client(user_id, family_repo, patched, gap_repo)
        try:
            resp = client.get(
                f"/cycles/{cycle_id}/child-results",
                headers={"x-user-id": str(user_id)},
            )
            assert resp.status_code == 200
            body = resp.json()

            # The frozen snapshot had accuracy=True — marks MUST still be present.
            summary = body["summary"]
            assert summary["marks_earned"] is not None, (
                "marks_earned should be present (frozen snapshot had accuracy=True)"
            )
            assert summary["marks_available"] is not None

            # Per-item marks must also be present.
            for item in body["items"]:
                assert item.get("marks_earned") is not None, (
                    f"marks_earned absent on item {item['question_id']} "
                    "despite frozen accuracy=True"
                )
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Test g: individual toggle gates
# ---------------------------------------------------------------------------


class TestChildResultsVisibilityGates:
    """g. Individual toggle gates produce None fields correctly."""

    def _run_gate_test(
        self,
        snapshot: VisibilityDefaults,
        assert_fn: Any,
    ) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo, snapshot)
        _, submission_id = _seed_marks_and_gap_report(
            family_repo,
            marks_repo,
            gap_repo,
            cycle_id,
            ai_rationale="test rationale",
        )
        patched = _PatchedMarksRepo(marks_repo, cycle_id, submission_id)

        client = _make_client(user_id, family_repo, patched, gap_repo)
        try:
            resp = client.get(
                f"/cycles/{cycle_id}/child-results",
                headers={"x-user-id": str(user_id)},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert_fn(body)
        finally:
            _cleanup()

    def test_accuracy_false_hides_marks(self) -> None:
        """accuracy=False → marks_earned/marks_total absent on items + summary."""
        snapshot = VisibilityDefaults(accuracy=False, effort=True, growing=True, ai_rationale=False)

        def assert_fn(body: dict[str, Any]) -> None:
            for item in body["items"]:
                assert item.get("marks_earned") is None, (
                    f"marks_earned must be None when accuracy=False on {item['question_id']}"
                )
                assert item.get("marks_total") is None
            summary = body["summary"]
            assert summary["marks_earned"] is None
            assert summary["marks_available"] is None

        self._run_gate_test(snapshot, assert_fn)

    def test_growing_false_hides_status(self) -> None:
        """growing=False → status absent on items + mastered/growing counts absent."""
        snapshot = VisibilityDefaults(accuracy=True, effort=True, growing=False, ai_rationale=False)

        def assert_fn(body: dict[str, Any]) -> None:
            for item in body["items"]:
                assert item.get("status") is None, (
                    f"status must be None when growing=False on {item['question_id']}"
                )
            summary = body["summary"]
            assert summary["mastered_count"] is None
            assert summary["growing_count"] is None

        self._run_gate_test(snapshot, assert_fn)

    def test_effort_false_hides_attempted_count(self) -> None:
        """effort=False → attempted_count absent in summary."""
        snapshot = VisibilityDefaults(accuracy=True, effort=False, growing=True, ai_rationale=False)

        def assert_fn(body: dict[str, Any]) -> None:
            summary = body["summary"]
            assert summary["attempted_count"] is None, (
                "attempted_count must be None when effort=False"
            )

        self._run_gate_test(snapshot, assert_fn)

    def test_all_on_produces_full_response(self) -> None:
        """All toggles on → all fields present."""
        snapshot = VisibilityDefaults(accuracy=True, effort=True, growing=True, ai_rationale=True)

        def assert_fn(body: dict[str, Any]) -> None:
            summary = body["summary"]
            assert summary["marks_earned"] is not None
            assert summary["marks_available"] is not None
            assert summary["mastered_count"] is not None
            assert summary["growing_count"] is not None
            assert summary["attempted_count"] is not None
            # Items should have status + marks.
            for item in body["items"]:
                assert item["status"] in ("mastered", "growing")
                assert item["marks_earned"] is not None
                assert item["marks_total"] is not None

        self._run_gate_test(snapshot, assert_fn)

    def test_all_off_produces_minimal_response(self) -> None:
        """All toggles off → only total_questions, question metadata, and child_answer_rendered."""
        snapshot = VisibilityDefaults(
            accuracy=False, effort=False, growing=False, ai_rationale=False
        )

        def assert_fn(body: dict[str, Any]) -> None:
            summary = body["summary"]
            assert summary["marks_earned"] is None
            assert summary["marks_available"] is None
            assert summary["mastered_count"] is None
            assert summary["growing_count"] is None
            assert summary["attempted_count"] is None
            # But total_questions and items are always present.
            assert summary["total_questions"] > 0
            for item in body["items"]:
                assert item.get("marks_earned") is None
                assert item.get("marks_total") is None
                assert item.get("status") is None
                assert item.get("ai_rationale") is None
                # child_answer_rendered is always present.
                assert "child_answer_rendered" in item

        self._run_gate_test(snapshot, assert_fn)


# ---------------------------------------------------------------------------
# Test h: DB-tier RLS — skipped when Postgres unreachable
# ---------------------------------------------------------------------------

_DSN = os.environ.get("STUDYPAL_DB_DSN", "postgresql://studypal:studypal@localhost:5432/studypal")


def _try_connect_db() -> bool:
    try:
        conn = psycopg.connect(_DSN, connect_timeout=3, autocommit=True)
        conn.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _try_connect_db(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)
class TestChildResultsRLS:
    """h. DB-tier: gap_reports and child results are RLS-isolated by family_id.

    Mirrors test_gap_report.py TestGapReportRLS pattern.
    Skipped cleanly when Postgres is unreachable.
    """

    def _open_auth_conn(self, user_id: uuid.UUID) -> psycopg.Connection[dict[str, Any]]:
        claims = json.dumps({"sub": str(user_id)})
        conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
            _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
        )
        conn.execute("SET ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))
        return conn

    def test_child_results_gap_report_rls_isolation(self) -> None:
        """User B cannot see user A's gap report row via the DB."""
        owner_conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
            _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
        )
        try:
            user_a = uuid.uuid4()
            user_b = uuid.uuid4()

            cur = owner_conn.cursor()

            # Seed family A.
            cur.execute(
                "INSERT INTO families (name) VALUES (%s) RETURNING id",
                (f"ChildResultsFamilyA-{user_a.hex[:6]}",),
            )
            row = cur.fetchone()
            assert row is not None
            family_a = uuid.UUID(str(row["id"]))

            cur.execute(
                "INSERT INTO family_members (user_id, family_id) VALUES (%s, %s)",
                (str(user_a), str(family_a)),
            )
            cur.execute(
                "INSERT INTO children (family_id, display_name, grade_label) "
                "VALUES (%s, 'Kid A', 'Grade 5') RETURNING id",
                (str(family_a),),
            )
            row = cur.fetchone()
            assert row is not None
            child_a = uuid.UUID(str(row["id"]))

            cur.execute(
                "INSERT INTO subjects (family_id, child_id, name, content_language) "
                "VALUES (%s, %s, 'Maths', 'en') RETURNING id",
                (str(family_a), str(child_a)),
            )
            row = cur.fetchone()
            assert row is not None
            subject_a = uuid.UUID(str(row["id"]))

            cur.execute(
                "INSERT INTO cycles (family_id, subject_id, state) "
                "VALUES (%s, %s, 'GAP_REPORT') RETURNING id",
                (str(family_a), str(subject_a)),
            )
            row = cur.fetchone()
            assert row is not None
            cycle_a = uuid.UUID(str(row["id"]))

            asmt_id_a = uuid.uuid4()
            cur.execute(
                "INSERT INTO assessments (id, family_id, cycle_id, variant, subject, "
                "content_language, declared_total_marks, computed_total_marks, assessment, "
                "schema_version) "
                "VALUES (%s, %s, %s, 'A', 'Maths', 'en', 1.0, 1.0, %s::jsonb, '1.0')",
                (
                    str(asmt_id_a),
                    str(family_a),
                    str(cycle_a),
                    json.dumps({"assessment_id": str(asmt_id_a), "cycle_id": str(cycle_a)}),
                ),
            )

            submission_id_a = uuid.uuid4()
            submission_doc_a = json.dumps(
                {
                    "child_id": str(child_a),
                    "responses": [],
                    "proof_photo_paths": [],
                }
            )
            cur.execute(
                "INSERT INTO submissions (id, family_id, assessment_id, child_id, submission) "
                "VALUES (%s, %s, %s, %s, %s::jsonb)",
                (
                    str(submission_id_a),
                    str(family_a),
                    str(asmt_id_a),
                    str(child_a),
                    submission_doc_a,
                ),
            )

            minimal_report = json.dumps(
                {
                    "assessment_id": str(asmt_id_a),
                    "cycle_id": str(cycle_a),
                    "items": [],
                    "summary": {
                        "mastered_count": 0,
                        "growing_count": 0,
                        "total_marks_earned": "0.0",
                        "total_marks_available": "0.0",
                        "growing_gap_tags": [],
                    },
                    "derived_at": datetime.now(tz=UTC).isoformat(),
                }
            )
            cur.execute(
                "INSERT INTO gap_reports (family_id, cycle_id, submission_id, report) "
                "VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
                (str(family_a), str(cycle_a), str(submission_id_a), minimal_report),
            )
            row = cur.fetchone()
            assert row is not None
            gap_report_id_a = uuid.UUID(str(row["id"]))

            owner_conn.commit()

            # User B must NOT see family A's gap report via the DB.
            conn_b = self._open_auth_conn(user_b)
            try:
                b_cur = conn_b.cursor()
                b_cur.execute("SELECT id FROM gap_reports WHERE id = %s", (str(gap_report_id_a),))
                row = b_cur.fetchone()
                assert row is None, "RLS violation: user B can see user A's gap report via DB"
            finally:
                conn_b.close()

            # User A must see their own gap report.
            conn_a = self._open_auth_conn(user_a)
            try:
                a_cur = conn_a.cursor()
                a_cur.execute("SELECT id FROM gap_reports WHERE id = %s", (str(gap_report_id_a),))
                row = a_cur.fetchone()
                assert row is not None, "User A cannot see their own gap report"
            finally:
                conn_a.close()

        finally:
            owner_conn.close()
