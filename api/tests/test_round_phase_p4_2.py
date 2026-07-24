"""Tests for P4-2 of the generic (round, phase) redesign.

docs/design/round-phase-architecture.md §5, §7 (P4): gap-report, study-pack,
and child-results are now parameterized by round (via ``?variant=A|B``,
mirroring the sibling capture/grading/review/retest endpoints), and
child-results reads its per-round ``published_visibility`` snapshot from
``cycle_round_approvals`` (not the single-valued, round-ambiguous
``cycles.published_visibility`` compat column).

Round-1 behaviour is unchanged (see test_gap_report.py / test_study_pack.py /
test_child_results.py — the regression net). This file adds round-2
coverage:

- A round-2 gap report persists distinctly from round 1's
  (``gap_reports`` keyed on (cycle_id, round)).
- A round-2 study pack persists distinctly from round 1's
  (``study_packs`` keyed on (cycle_id, round)).
- Child-results for round 2 is blocked (404) — round 2 results are
  parent-only in v1 (``round_config(2).results_child_visible is False``),
  even though round 2's marks ARE published.
- Round 1's child-results remain fully available after round 2 publishes
  (proves the per-round ``cycle_round_approvals`` snapshot is NOT clobbered
  by round 2's publish — the bug this design closes).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from schemas.assessment_schema import Assessment, GradingPath
from schemas.family import CyclePhase, VisibilityDefaults
from schemas.grading import QuestionMark
from services.cycle import (
    advance_phase,
    advance_to_answers_entered,
    advance_to_auto_marked,
    advance_to_generating,
    advance_to_parent_review_marks,
    advance_to_parent_reviews,
    approve_draft,
    publish_marks,
    start_next_round,
)
from services.gap_report import derive_gap_report
from services.repositories.memory import (
    InMemoryFamilyRepository,
    InMemoryGapReportRepository,
    InMemoryQuestionMarkRepository,
    InMemoryStudyPackRepository,
    InMemorySubmissionRepository,
)
from tests.samples.maths_sample import maths_assessment

_FAMILY_ID = uuid.uuid4()


def _assessment(raw: dict[str, Any] | None = None, *, variant: str = "A") -> Assessment:
    return Assessment.model_validate({**(raw or maths_assessment()), "variant": variant})


class _VariantAwareMarksRepo(InMemoryQuestionMarkRepository):
    """Real cycle+variant-aware bookkeeping (mirrors test_variant_b.py's double)."""

    def __init__(self) -> None:
        super().__init__()
        self._cycle_variant: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}

    def register(self, submission_id: uuid.UUID, cycle_id: uuid.UUID, variant: str) -> None:
        self._cycle_variant[submission_id] = (cycle_id, variant)

    def get_submission_id_for_cycle(self, cycle_id: uuid.UUID, variant: str) -> uuid.UUID | None:
        for sid, (cid, v) in self._cycle_variant.items():
            if cid == cycle_id and v == variant:
                return sid
        return None

    def list_for_cycle(self, cycle_id: uuid.UUID, variant: str) -> list[QuestionMark]:
        sid = self.get_submission_id_for_cycle(cycle_id, variant)
        if sid is None:
            return []
        return self.list_for_submission(sid)


def _marks(submission_id: uuid.UUID, family_id: uuid.UUID) -> list[QuestionMark]:
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
            ("B.2", "2.0", "0.0"),
        ]
    ]


def _setup_cycle_round1_and_round2_published(
    family_repo: InMemoryFamilyRepository,
    marks_repo: _VariantAwareMarksRepo,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Walk a cycle through round 1 (published) and round 2 (published).

    Returns (cycle_id, family_id). Attaches Variant-A and Variant-B
    assessments to the cycle and seeds distinct marks for each round via the
    variant-aware marks repo.
    """
    family, _ = family_repo.bootstrap_family("Test Family", None, None)
    subject = family_repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
    cycle = family_repo.create_cycle(family.id, subject.id, "scope")

    # Round 1: SCOPE_UPLOADED -> ... -> PUBLISHED.
    advance_to_generating(family_repo, cycle.id)
    advance_to_parent_reviews(family_repo, cycle.id)
    approve_draft(family_repo, cycle.id)
    advance_to_answers_entered(family_repo, cycle.id)
    advance_to_auto_marked(family_repo, cycle.id)
    advance_to_parent_review_marks(family_repo, cycle.id)
    publish_marks(family_repo, cycle.id, VisibilityDefaults())

    submission_a = uuid.uuid4()
    marks_repo.register(submission_a, cycle.id, "A")
    marks_repo.bulk_upsert(family.id, submission_a, _marks(submission_a, family.id))

    variant_a = _assessment(variant="A")
    raw_cycle = family_repo.get_cycle(cycle.id)
    assert raw_cycle is not None
    family_repo._cycles[cycle.id] = raw_cycle.model_copy(
        update={"assessments": [*raw_cycle.assessments, variant_a]}
    )

    # Round 2: start_next_round (legal from PUBLISHED — pack skipped) then
    # walk the real phase sequence to PUBLISHED (mirrors test_variant_b.py).
    start_next_round(family_repo, cycle.id)
    advance_phase(family_repo, cycle.id, CyclePhase.DRAFT_REVIEW)
    advance_phase(family_repo, cycle.id, CyclePhase.PRINTED)
    advance_phase(family_repo, cycle.id, CyclePhase.ANSWERS_ENTERED)
    advance_phase(family_repo, cycle.id, CyclePhase.MARKED)
    advance_phase(family_repo, cycle.id, CyclePhase.REVIEW_MARKS)
    publish_marks(family_repo, cycle.id, VisibilityDefaults())

    submission_b = uuid.uuid4()
    marks_repo.register(submission_b, cycle.id, "B")
    # Distinct marks from round 1 so the two gap reports are distinguishable.
    marks_b = [
        QuestionMark(
            family_id=family.id,
            submission_id=submission_b,
            question_id=qid,
            marks_total=Decimal(total),
            suggested_marks=Decimal(final),
            final_marks=Decimal(final),
            grading_path=GradingPath.AUTO,
            needs_review=False,
        )
        for qid, total, final in [
            ("A.1", "1.0", "0.0"),
            ("A.2", "2.0", "0.0"),
            ("B.1", "3.0", "3.0"),
            ("B.2", "2.0", "2.0"),
        ]
    ]
    marks_repo.bulk_upsert(family.id, submission_b, marks_b)

    variant_b = _assessment(variant="B")
    raw_cycle = family_repo.get_cycle(cycle.id)
    assert raw_cycle is not None
    family_repo._cycles[cycle.id] = raw_cycle.model_copy(
        update={"assessments": [*raw_cycle.assessments, variant_b]}
    )

    return cycle.id, family.id


def _overrides(
    family_repo: InMemoryFamilyRepository,
    marks_repo: _VariantAwareMarksRepo,
    gap_repo: InMemoryGapReportRepository,
    pack_repo: InMemoryStudyPackRepository,
) -> None:
    from dependencies import (
        get_family_repository,
        get_family_repository_for_caller,
        get_gap_report_repository,
        get_gap_report_repository_for_caller,
        get_question_mark_repository,
        get_question_mark_repository_for_caller,
        get_study_pack_repository,
        get_submission_repository,
        get_submission_repository_for_caller,
    )
    from main import app

    submission_repo = InMemorySubmissionRepository()

    app.dependency_overrides[get_family_repository] = lambda: family_repo
    app.dependency_overrides[get_question_mark_repository] = lambda: marks_repo
    app.dependency_overrides[get_gap_report_repository] = lambda: gap_repo
    app.dependency_overrides[get_study_pack_repository] = lambda: pack_repo
    app.dependency_overrides[get_submission_repository] = lambda: submission_repo

    # Kiosk-capable variants (routers/capture.py, routers/child_results.py)
    # back onto the SAME repo instances so parent-Identity and kiosk-token
    # requests in the same test session see consistent state.
    app.dependency_overrides[get_family_repository_for_caller] = lambda: family_repo
    app.dependency_overrides[get_question_mark_repository_for_caller] = lambda: marks_repo
    app.dependency_overrides[get_gap_report_repository_for_caller] = lambda: gap_repo
    app.dependency_overrides[get_submission_repository_for_caller] = lambda: submission_repo


def _clear_overrides() -> None:
    from dependencies import (
        get_family_repository,
        get_family_repository_for_caller,
        get_gap_report_repository,
        get_gap_report_repository_for_caller,
        get_question_mark_repository,
        get_question_mark_repository_for_caller,
        get_study_pack_repository,
        get_submission_repository,
        get_submission_repository_for_caller,
    )
    from main import app

    app.dependency_overrides.pop(get_family_repository, None)
    app.dependency_overrides.pop(get_question_mark_repository, None)
    app.dependency_overrides.pop(get_gap_report_repository, None)
    app.dependency_overrides.pop(get_study_pack_repository, None)
    app.dependency_overrides.pop(get_submission_repository, None)
    app.dependency_overrides.pop(get_family_repository_for_caller, None)
    app.dependency_overrides.pop(get_question_mark_repository_for_caller, None)
    app.dependency_overrides.pop(get_gap_report_repository_for_caller, None)
    app.dependency_overrides.pop(get_submission_repository_for_caller, None)


class TestGapReportRoundParameterization:
    def test_round_2_gap_report_persists_distinct_from_round_1(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = _VariantAwareMarksRepo()
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id, _family_id = _setup_cycle_round1_and_round2_published(family_repo, marks_repo)

        _overrides(family_repo, marks_repo, gap_repo, pack_repo)
        headers = {"x-user-id": str(user_id)}
        try:
            with TestClient(app_module()) as client:
                resp_a = client.post(
                    f"/cycles/{cycle_id}/gap-report", params={"variant": "A"}, headers=headers
                )
                resp_b = client.post(
                    f"/cycles/{cycle_id}/gap-report", params={"variant": "B"}, headers=headers
                )
        finally:
            _clear_overrides()

        assert resp_a.status_code == 200, resp_a.text
        assert resp_b.status_code == 200, resp_b.text
        body_a = resp_a.json()
        body_b = resp_b.json()

        assert body_a["round"] == 1
        assert body_b["round"] == 2
        # Distinct content: round 1 vs round 2 marks are deliberately built so
        # DIFFERENT questions are mastered in each round (A.1/A.2 mastered in
        # round 1 vs B.1/B.2 mastered in round 2) — proves the two reports
        # are independently derived/persisted, not aliased to one row.
        mastered_a = {
            item["question_id"]
            for item in body_a["report"]["items"]
            if item["status"] == "mastered"
        }
        mastered_b = {
            item["question_id"]
            for item in body_b["report"]["items"]
            if item["status"] == "mastered"
        }
        assert mastered_a == {"A.1", "A.2"}
        assert mastered_b == {"B.1", "B.2"}

        # Both rows persisted independently in storage.
        row_1 = gap_repo.get_for_cycle(cycle_id, round=1)
        row_2 = gap_repo.get_for_cycle(cycle_id, round=2)
        assert row_1 is not None
        assert row_2 is not None
        assert row_1.id != row_2.id
        assert row_1.round == 1
        assert row_2.round == 2

    def test_get_gap_report_defaults_to_round_1(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = _VariantAwareMarksRepo()
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id, _family_id = _setup_cycle_round1_and_round2_published(family_repo, marks_repo)

        _overrides(family_repo, marks_repo, gap_repo, pack_repo)
        headers = {"x-user-id": str(user_id)}
        try:
            with TestClient(app_module()) as client:
                client.post(
                    f"/cycles/{cycle_id}/gap-report", params={"variant": "A"}, headers=headers
                )
                client.post(
                    f"/cycles/{cycle_id}/gap-report", params={"variant": "B"}, headers=headers
                )
                resp_default = client.get(f"/cycles/{cycle_id}/gap-report", headers=headers)
                resp_b = client.get(
                    f"/cycles/{cycle_id}/gap-report", params={"variant": "B"}, headers=headers
                )
        finally:
            _clear_overrides()

        assert resp_default.status_code == 200
        assert resp_b.status_code == 200
        assert resp_default.json()["round"] == 1
        assert resp_b.json()["round"] == 2


class TestStudyPackRoundParameterization:
    def test_round_2_study_pack_persists_distinct_from_round_1(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = _VariantAwareMarksRepo()
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id, _family_id = _setup_cycle_round1_and_round2_published(family_repo, marks_repo)

        _overrides(family_repo, marks_repo, gap_repo, pack_repo)
        headers = {"x-user-id": str(user_id)}
        try:
            with TestClient(app_module()) as client:
                # Gap reports must exist first (study pack reads the stored row).
                client.post(
                    f"/cycles/{cycle_id}/gap-report", params={"variant": "A"}, headers=headers
                )
                client.post(
                    f"/cycles/{cycle_id}/gap-report", params={"variant": "B"}, headers=headers
                )

                resp_a = client.post(
                    f"/cycles/{cycle_id}/study-pack", params={"variant": "A"}, headers=headers
                )
                resp_b = client.post(
                    f"/cycles/{cycle_id}/study-pack", params={"variant": "B"}, headers=headers
                )
        finally:
            _clear_overrides()

        assert resp_a.status_code == 200, resp_a.text
        assert resp_b.status_code == 200, resp_b.text
        assert resp_a.json()["round"] == 1
        assert resp_b.json()["round"] == 2

        row_1 = pack_repo.get_for_cycle(cycle_id, round=1)
        row_2 = pack_repo.get_for_cycle(cycle_id, round=2)
        assert row_1 is not None
        assert row_2 is not None
        assert row_1.id != row_2.id
        assert row_1.round == 1
        assert row_2.round == 2

    def test_round_1_study_pack_still_settles_to_study_pack_done(self) -> None:
        """Round-1 study pack generation still advances the cycle's own phase
        machinery when round 1 IS the cycle's current round (unaffected by
        this test file's round-2-focused setup elsewhere)."""
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = _VariantAwareMarksRepo()
        gap_repo = InMemoryGapReportRepository()

        family, _ = family_repo.bootstrap_family("Test", None, None)
        subject = family_repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = family_repo.create_cycle(family.id, subject.id, "scope")

        advance_to_generating(family_repo, cycle.id)
        advance_to_parent_reviews(family_repo, cycle.id)
        approve_draft(family_repo, cycle.id)
        advance_to_answers_entered(family_repo, cycle.id)
        advance_to_auto_marked(family_repo, cycle.id)
        advance_to_parent_review_marks(family_repo, cycle.id)
        publish_marks(family_repo, cycle.id, VisibilityDefaults())

        submission_a = uuid.uuid4()
        marks_repo.register(submission_a, cycle.id, "A")
        marks_repo.bulk_upsert(family.id, submission_a, _marks(submission_a, family.id))
        variant_a = _assessment(variant="A")
        raw_cycle = family_repo.get_cycle(cycle.id)
        assert raw_cycle is not None
        family_repo._cycles[cycle.id] = raw_cycle.model_copy(update={"assessments": [variant_a]})

        report = derive_gap_report(variant_a, marks_repo.list_for_cycle(cycle.id, "A"))
        gap_repo.upsert(family.id, cycle.id, submission_a, report, round=1)

        pack_repo = InMemoryStudyPackRepository()
        _overrides(family_repo, marks_repo, gap_repo, pack_repo)
        headers = {"x-user-id": str(user_id)}
        try:
            with TestClient(app_module()) as client:
                resp = client.post(f"/cycles/{cycle.id}/study-pack", headers=headers)
        finally:
            _clear_overrides()

        assert resp.status_code == 200, resp.text
        updated = family_repo.get_cycle(cycle.id)
        assert updated is not None
        assert updated.phase == CyclePhase.STUDY_PACK


class TestChildResultsRoundVisibilityGate:
    def test_round_2_results_not_child_visible_returns_404(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = _VariantAwareMarksRepo()
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id, _family_id = _setup_cycle_round1_and_round2_published(family_repo, marks_repo)

        _overrides(family_repo, marks_repo, gap_repo, pack_repo)
        headers = {"x-user-id": str(user_id)}
        try:
            with TestClient(app_module()) as client:
                resp_b = client.get(
                    f"/cycles/{cycle_id}/child-results", params={"variant": "B"}, headers=headers
                )
        finally:
            _clear_overrides()

        assert resp_b.status_code == 404

    def test_round_1_results_still_available_after_round_2_publishes(self) -> None:
        """Round 1's child-results must remain intact after round 2 publishes
        — proves the per-round cycle_round_approvals snapshot for round 1 is
        NOT clobbered by round 2's publish (the bug the round table closes)."""
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = _VariantAwareMarksRepo()
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id, _family_id = _setup_cycle_round1_and_round2_published(family_repo, marks_repo)

        # Round 2 was published with default VisibilityDefaults() — round 1
        # was ALSO published with default VisibilityDefaults() earlier in
        # _setup_cycle_round1_and_round2_published, so round 1's snapshot
        # must still read as that original round-1 approval row, not round 2's.
        round_1_approval = family_repo.get_round_approval(cycle_id, 1)
        round_2_approval = family_repo.get_round_approval(cycle_id, 2)
        assert round_1_approval is not None
        assert round_2_approval is not None
        assert round_1_approval.marks_published_at != round_2_approval.marks_published_at

        _overrides(family_repo, marks_repo, gap_repo, pack_repo)
        headers = {"x-user-id": str(user_id)}
        try:
            with TestClient(app_module()) as client:
                resp_a = client.get(
                    f"/cycles/{cycle_id}/child-results", params={"variant": "A"}, headers=headers
                )
                resp_default = client.get(f"/cycles/{cycle_id}/child-results", headers=headers)
        finally:
            _clear_overrides()

        assert resp_a.status_code == 200, resp_a.text
        assert resp_default.status_code == 200, resp_default.text
        assert resp_a.json()["summary"]["total_questions"] == 4


def app_module() -> Any:
    from main import app

    return app
