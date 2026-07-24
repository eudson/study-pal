"""Unit tests for the retest (round 2 / "Variant B") flow + A/B comparison.

P4 of the generic (round, phase) redesign (docs/design/round-phase-
architecture.md §2, §3, §5, §7): round 2 now traverses the exact same real
phase sequence as round 1 — GENERATING -> DRAFT_REVIEW -> PRINTED ->
ANSWERS_ENTERED -> MARKED -> REVIEW_MARKS -> PUBLISHED -> COMPLETE — with its
own parent DRAFT_REVIEW approval and publish gate, recorded per round in
``cycle_round_approvals``. This supersedes the old flow where Variant B's
entire capture->grade->review sub-loop was crammed into a single
``GENERATING_B`` state.

Coverage:
- cycle.py: start_next_round / advance_to_cycle_complete (legal + illegal)
  for round 2.
- GenerationService.generate_variant_b: schema-valid variant="B", gap_tags
  propagated, deterministic.
- A+B COEXISTENCE (advisor guardrail #3): list_for_cycle(cycle_id, variant)
  never bleeds A and B marks together; grading B leaves A's marks unchanged.
- derive_ab_comparison: closed/persisting/new partitioning incl. half-marks.
- retest router (generateVariantB/comparison/complete) + SHARED
  capture/grade/review/approve/publish endpoints hit with ``?variant=B``:
  authz/phase guards (401/404/409) + the full happy path generate ->
  approve draft -> capture -> submit -> grade -> review -> publish ->
  comparison -> complete, asserting the round-2 approval rows are recorded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from schemas.assessment_schema import (
    Assessment,
    ErrorCategory,
    GapRetarget,
    GradingPath,
    VariantBRequest,
)
from schemas.family import CyclePhase, CycleState, VisibilityDefaults
from schemas.gap_report import GapReport, GapReportItem, GapReportSummary, GapStatus
from schemas.grading import QuestionMark
from services.claude_client import FakeClaude
from services.comparison import derive_ab_comparison
from services.cycle import (
    IllegalTransitionError,
    advance_phase,
    advance_to_answers_entered,
    advance_to_auto_marked,
    advance_to_cycle_complete,
    advance_to_generating,
    advance_to_generating_study_pack,
    advance_to_parent_review_marks,
    advance_to_parent_reviews,
    advance_to_study_pack_done,
    approve_draft,
    publish_marks,
    start_next_round,
)
from services.generation_service import GenerationService
from services.repositories.memory import (
    InMemoryFamilyRepository,
    InMemoryGapReportRepository,
    InMemoryQuestionMarkRepository,
)
from tests.samples.maths_sample import maths_assessment

_FAMILY_ID = uuid.uuid4()


def _assessment(raw: dict[str, Any] | None = None) -> Assessment:
    return Assessment.model_validate(raw or maths_assessment())


def _full_round_1(repo: InMemoryFamilyRepository, cycle_id: uuid.UUID) -> None:
    """Walk a fresh cycle through round 1 to a settled STUDY_PACK_DONE."""
    advance_to_generating(repo, cycle_id)
    advance_to_parent_reviews(repo, cycle_id)
    approve_draft(repo, cycle_id)
    advance_to_answers_entered(repo, cycle_id)
    advance_to_auto_marked(repo, cycle_id)
    advance_to_parent_review_marks(repo, cycle_id)
    publish_marks(repo, cycle_id, VisibilityDefaults())
    advance_to_generating_study_pack(repo, cycle_id)
    advance_to_study_pack_done(repo, cycle_id)


def _walk_round_2_to_published(repo: InMemoryFamilyRepository, cycle_id: uuid.UUID) -> None:
    """From (2, GENERATING) walk the real phase sequence to (2, PUBLISHED)."""
    advance_phase(repo, cycle_id, CyclePhase.DRAFT_REVIEW)
    advance_phase(repo, cycle_id, CyclePhase.PRINTED)
    advance_phase(repo, cycle_id, CyclePhase.ANSWERS_ENTERED)
    advance_phase(repo, cycle_id, CyclePhase.MARKED)
    advance_phase(repo, cycle_id, CyclePhase.REVIEW_MARKS)
    publish_marks(repo, cycle_id, VisibilityDefaults())


# ---------------------------------------------------------------------------
# cycle.py transitions: start_next_round / advance_to_cycle_complete
# ---------------------------------------------------------------------------


class TestStartNextRoundToRoundTwo:
    def test_legal_from_study_pack_done(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        family, _ = repo.bootstrap_family("Test", None, None)
        subject = repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = repo.create_cycle(family.id, subject.id, "scope")

        _full_round_1(repo, cycle.id)

        updated = start_next_round(repo, cycle.id)
        assert updated.round == 2
        assert updated.phase == CyclePhase.GENERATING

    def test_illegal_from_scope_uploaded(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        family, _ = repo.bootstrap_family("Test", None, None)
        subject = repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = repo.create_cycle(family.id, subject.id, "scope")

        with pytest.raises(IllegalTransitionError):
            start_next_round(repo, cycle.id)


class TestAdvanceToCycleComplete:
    def test_legal_from_round_2_published(self) -> None:
        """Round 2 must walk its own real phases to PUBLISHED before COMPLETE
        — the old (2, GENERATING) -> (2, COMPLETE) collapse is retired."""
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        family, _ = repo.bootstrap_family("Test", None, None)
        subject = repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = repo.create_cycle(family.id, subject.id, "scope")

        _full_round_1(repo, cycle.id)
        start_next_round(repo, cycle.id)
        _walk_round_2_to_published(repo, cycle.id)

        updated = advance_to_cycle_complete(repo, cycle.id)
        assert updated.state == CycleState.CYCLE_COMPLETE
        assert updated.round == 2
        assert updated.phase == CyclePhase.COMPLETE

    def test_illegal_from_round_2_generating(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        family, _ = repo.bootstrap_family("Test", None, None)
        subject = repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = repo.create_cycle(family.id, subject.id, "scope")

        _full_round_1(repo, cycle.id)
        start_next_round(repo, cycle.id)

        with pytest.raises(IllegalTransitionError):
            advance_to_cycle_complete(repo, cycle.id)


# ---------------------------------------------------------------------------
# GenerationService.generate_variant_b
# ---------------------------------------------------------------------------


class TestGenerateVariantB:
    def _request(self) -> VariantBRequest:
        source = _assessment()
        gaps = [
            GapRetarget(
                gap_id="measurement-conversion",
                category=ErrorCategory.CONCEPT_GAP,
                description="Struggles converting km to m.",
                source_question_ids=["A.2"],
            )
        ]
        return VariantBRequest(source_assessment=source, gaps=gaps)

    def test_returns_schema_valid_variant_b(self) -> None:
        service = GenerationService(claude=FakeClaude())
        result = service.generate_variant_b(self._request(), assessment_id="asmt-b-001")
        assert result.ok
        assert result.assessment is not None
        assert result.assessment.variant == "B"
        assert result.assessment.assessment_id == "asmt-b-001"

    def test_cycle_id_matches_source(self) -> None:
        request = self._request()
        service = GenerationService(claude=FakeClaude())
        result = service.generate_variant_b(request, assessment_id="asmt-b-002")
        assert result.assessment is not None
        assert result.assessment.cycle_id == request.source_assessment.cycle_id

    def test_gap_tags_propagated(self) -> None:
        service = GenerationService(claude=FakeClaude())
        result = service.generate_variant_b(self._request(), assessment_id="asmt-b-003")
        assert result.assessment is not None
        all_tags = {
            tag
            for section in result.assessment.sections
            for q in section.questions
            for tag in q.gap_tags
        }
        assert "measurement-conversion" in all_tags

    def test_same_structure_as_source(self) -> None:
        """Same section/question counts and per-question mark totals as source."""
        request = self._request()
        service = GenerationService(claude=FakeClaude())
        result = service.generate_variant_b(request, assessment_id="asmt-b-004")
        assert result.assessment is not None
        source = request.source_assessment
        assert len(result.assessment.sections) == len(source.sections)
        for b_section, a_section in zip(result.assessment.sections, source.sections, strict=True):
            assert len(b_section.questions) == len(a_section.questions)
            for b_q, a_q in zip(b_section.questions, a_section.questions, strict=True):
                assert b_q.question_type == a_q.question_type
                assert b_q.mark_rules.total == a_q.mark_rules.total

    def test_deterministic(self) -> None:
        """Two independent calls with the same request + assessment_id produce
        an identical dumped document (FakeClaude has no randomness)."""
        request = self._request()
        result1 = GenerationService(claude=FakeClaude()).generate_variant_b(
            request, assessment_id="asmt-b-fixed"
        )
        result2 = GenerationService(claude=FakeClaude()).generate_variant_b(
            request, assessment_id="asmt-b-fixed"
        )
        assert result1.assessment is not None
        assert result2.assessment is not None
        assert result1.assessment.model_dump() == result2.assessment.model_dump()

    def test_values_changed_from_source(self) -> None:
        """Surface text differs from the source at the same question position."""
        request = self._request()
        result = GenerationService(claude=FakeClaude()).generate_variant_b(
            request, assessment_id="asmt-b-005"
        )
        assert result.assessment is not None
        source = request.source_assessment
        b_q0 = result.assessment.sections[0].questions[0]
        a_q0 = source.sections[0].questions[0]
        assert b_q0.text != a_q0.text


# ---------------------------------------------------------------------------
# A+B coexistence (advisor guardrail #3 — REQUIRED)
# ---------------------------------------------------------------------------


class _VariantAwareMarksRepo(InMemoryQuestionMarkRepository):
    """Real cycle+variant-aware bookkeeping backed by ``self._store``.

    Mirrors the established test-double pattern in test_gap_report.py /
    test_child_results.py (subclass overriding the two cycle-scoped lookups),
    extended with an explicit ``register`` call so a single repo instance can
    serve BOTH Variant A and Variant B submissions in one test — proving they
    never bleed together.
    """

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


class TestABCoexistence:
    def _mark(
        self,
        submission_id: uuid.UUID,
        question_id: str,
        final_marks: str,
    ) -> QuestionMark:
        return QuestionMark(
            family_id=_FAMILY_ID,
            submission_id=submission_id,
            question_id=question_id,
            marks_total=Decimal("1.0"),
            suggested_marks=Decimal(final_marks),
            final_marks=Decimal(final_marks),
            grading_path=GradingPath.AUTO,
            needs_review=False,
        )

    def test_variant_isolation_and_a_unchanged_after_b_graded(self) -> None:
        repo = _VariantAwareMarksRepo()
        cycle_id = uuid.uuid4()
        submission_a = uuid.uuid4()
        submission_b = uuid.uuid4()

        repo.register(submission_a, cycle_id, "A")
        repo.register(submission_b, cycle_id, "B")

        marks_a = [self._mark(submission_a, "A.1", "1.0"), self._mark(submission_a, "A.2", "0.0")]
        repo.bulk_upsert(_FAMILY_ID, submission_a, marks_a)

        # Snapshot A's marks BEFORE grading B.
        a_before = {m.question_id: m.final_marks for m in repo.list_for_cycle(cycle_id, "A")}

        marks_b = [self._mark(submission_b, "A.1", "0.5"), self._mark(submission_b, "A.2", "1.0")]
        repo.bulk_upsert(_FAMILY_ID, submission_b, marks_b)

        # Isolation: each variant sees ONLY its own marks.
        listed_a = repo.list_for_cycle(cycle_id, "A")
        listed_b = repo.list_for_cycle(cycle_id, "B")
        assert {m.submission_id for m in listed_a} == {submission_a}
        assert {m.submission_id for m in listed_b} == {submission_b}
        assert len(listed_a) == 2
        assert len(listed_b) == 2

        # A's marks are unchanged after B was graded.
        a_after = {m.question_id: m.final_marks for m in repo.list_for_cycle(cycle_id, "A")}
        assert a_after == a_before
        assert a_after == {"A.1": Decimal("1.0"), "A.2": Decimal("0.0")}

        # get_submission_id_for_cycle also stays variant-scoped.
        assert repo.get_submission_id_for_cycle(cycle_id, "A") == submission_a
        assert repo.get_submission_id_for_cycle(cycle_id, "B") == submission_b


# ---------------------------------------------------------------------------
# derive_ab_comparison
# ---------------------------------------------------------------------------


def _gap_report(
    growing: list[tuple[str, list[str], str, str]],
    cycle_id: str = "cycle-ab-test",
    assessment_id: str = "asmt-ab-test",
) -> GapReport:
    """Build a GapReport. ``growing`` is a list of
    (question_id, gap_tags, final_marks, marks_total)."""
    items = [
        GapReportItem(
            question_id=qid,
            number=qid,
            text=f"Question {qid}",
            status=GapStatus.GROWING,
            final_marks=Decimal(final),
            marks_total=Decimal(total),
            gap_tags=tags,
        )
        for qid, tags, final, total in growing
    ]
    tag_set: set[str] = set()
    for _qid, tags, _f, _t in growing:
        tag_set.update(tags)
    summary = GapReportSummary(
        mastered_count=0,
        growing_count=len(items),
        total_marks_earned=sum((it.final_marks for it in items), Decimal("0")),
        total_marks_available=sum((it.marks_total for it in items), Decimal("0")),
        growing_gap_tags=sorted(tag_set),
    )
    return GapReport(
        assessment_id=assessment_id,
        cycle_id=cycle_id,
        items=items,
        summary=summary,
        derived_at=datetime.now(tz=UTC),
    )


class TestDeriveAbComparison:
    def test_closed_persisting_new_partitioning(self) -> None:
        gap_a = _gap_report(
            [
                ("A.1", ["fractions"], "0", "1"),
                ("A.2", ["division"], "0", "1"),
            ]
        )
        gap_b = _gap_report(
            [
                ("B.1", ["division"], "0", "1"),  # persisting
                ("B.2", ["decimals"], "0", "1"),  # new
            ]
        )
        comparison = derive_ab_comparison(gap_a, gap_b)

        assert [d.gap_tag for d in comparison.closed] == ["fractions"]
        assert [d.gap_tag for d in comparison.persisting] == ["division"]
        assert [d.gap_tag for d in comparison.new] == ["decimals"]
        assert comparison.summary.closed_count == 1
        assert comparison.summary.persisting_count == 1
        assert comparison.summary.new_count == 1

    def test_all_closed_when_b_has_no_growing(self) -> None:
        gap_a = _gap_report([("A.1", ["fractions"], "0", "1")])
        gap_b = _gap_report([])
        comparison = derive_ab_comparison(gap_a, gap_b)
        assert [d.gap_tag for d in comparison.closed] == ["fractions"]
        assert comparison.persisting == []
        assert comparison.new == []

    def test_half_marks_do_not_affect_matching(self) -> None:
        """A half-mark growing item still counts as growing for matching purposes."""
        gap_a = _gap_report([("A.1", ["measurement"], "0.5", "1.0")])
        gap_b = _gap_report([("B.1", ["measurement"], "1.5", "2.0")])
        comparison = derive_ab_comparison(gap_a, gap_b)
        assert [d.gap_tag for d in comparison.persisting] == ["measurement"]
        assert comparison.summary.score_a == Decimal("0.5")
        assert comparison.summary.score_b == Decimal("1.5")

    def test_error_category_carried_through(self) -> None:
        gap_a = _gap_report([("A.1", ["fractions"], "0", "1")])
        gap_a.items[0].error_category = ErrorCategory.CONCEPT_GAP.value
        gap_b = _gap_report([])
        comparison = derive_ab_comparison(gap_a, gap_b)
        assert comparison.closed[0].error_category == ErrorCategory.CONCEPT_GAP.value

    def test_deterministic_ordering(self) -> None:
        gap_a = _gap_report(
            [
                ("A.1", ["zeta"], "0", "1"),
                ("A.2", ["alpha"], "0", "1"),
            ]
        )
        gap_b = _gap_report([])
        comparison = derive_ab_comparison(gap_a, gap_b)
        assert [d.gap_tag for d in comparison.closed] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# Router: authz / phase guards + full happy path
# ---------------------------------------------------------------------------


def _make_overrides(
    family_repo: InMemoryFamilyRepository,
    gap_repo: InMemoryGapReportRepository,
    marks_repo: _VariantAwareMarksRepo,
) -> None:
    from dependencies import (
        get_assessment_repository,
        get_assessment_repository_for_caller,
        get_family_repository,
        get_family_repository_for_caller,
        get_gap_report_repository,
        get_gap_report_repository_for_caller,
        get_question_mark_repository,
        get_question_mark_repository_for_caller,
        get_submission_repository,
        get_submission_repository_for_caller,
    )
    from main import app
    from services.repositories.memory import (
        InMemoryAssessmentRepository,
        InMemorySubmissionRepository,
    )

    assessment_repo = InMemoryAssessmentRepository()
    submission_repo = InMemorySubmissionRepository()

    app.dependency_overrides[get_family_repository] = lambda: family_repo
    app.dependency_overrides[get_gap_report_repository] = lambda: gap_repo
    app.dependency_overrides[get_question_mark_repository] = lambda: marks_repo
    app.dependency_overrides[get_assessment_repository] = lambda: assessment_repo
    app.dependency_overrides[get_submission_repository] = lambda: submission_repo

    # Kiosk-capable variants (routers/capture.py, routers/child_results.py)
    # back onto the SAME repo instances.
    app.dependency_overrides[get_family_repository_for_caller] = lambda: family_repo
    app.dependency_overrides[get_gap_report_repository_for_caller] = lambda: gap_repo
    app.dependency_overrides[get_question_mark_repository_for_caller] = lambda: marks_repo
    app.dependency_overrides[get_assessment_repository_for_caller] = lambda: assessment_repo
    app.dependency_overrides[get_submission_repository_for_caller] = lambda: submission_repo


def _clear_overrides() -> None:
    from dependencies import (
        get_assessment_repository,
        get_assessment_repository_for_caller,
        get_family_repository,
        get_family_repository_for_caller,
        get_gap_report_repository,
        get_gap_report_repository_for_caller,
        get_question_mark_repository,
        get_question_mark_repository_for_caller,
        get_submission_repository,
        get_submission_repository_for_caller,
    )
    from main import app

    app.dependency_overrides.pop(get_family_repository, None)
    app.dependency_overrides.pop(get_gap_report_repository, None)
    app.dependency_overrides.pop(get_question_mark_repository, None)
    app.dependency_overrides.pop(get_assessment_repository, None)
    app.dependency_overrides.pop(get_submission_repository, None)
    app.dependency_overrides.pop(get_family_repository_for_caller, None)
    app.dependency_overrides.pop(get_gap_report_repository_for_caller, None)
    app.dependency_overrides.pop(get_question_mark_repository_for_caller, None)
    app.dependency_overrides.pop(get_assessment_repository_for_caller, None)
    app.dependency_overrides.pop(get_submission_repository_for_caller, None)


def _cycle_at_study_pack_done_with_gap_report(
    family_repo: InMemoryFamilyRepository,
    gap_repo: InMemoryGapReportRepository,
    *,
    child_id: uuid.UUID,
) -> uuid.UUID:
    """Create a cycle at (round 1, STUDY_PACK settled), with a Variant-A
    assessment attached and a stored gap report carrying a growing gap_tag
    (for retargeting)."""
    family, _ = family_repo.bootstrap_family("Test Family", None, None)
    subject = family_repo.create_subject(family.id, child_id, "Maths", "en")
    cycle = family_repo.create_cycle(family.id, subject.id, "scope")

    _full_round_1(family_repo, cycle.id)

    variant_a = Assessment.model_validate({**maths_assessment(), "cycle_id": str(cycle.id)})
    raw_cycle = family_repo.get_cycle(cycle.id)
    assert raw_cycle is not None
    family_repo._cycles[cycle.id] = raw_cycle.model_copy(update={"assessments": [variant_a]})

    report = _gap_report(
        [("A.2", ["measurement-conversion"], "0", "2")],
        cycle_id=str(cycle.id),
        assessment_id=variant_a.assessment_id,
    )
    gap_repo.upsert(family.id, cycle.id, uuid.uuid4(), report)

    return cycle.id


def app_module() -> Any:
    from main import app

    return app


def _answered_responses(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a non-empty, type-appropriate payload per question.

    ``grade_submission`` treats an empty ``payload`` as "not attempted"
    regardless of the ``attempted`` flag — a non-empty payload is required
    so the CLAUDE_ASSIST questions (calculation/table_completion) come back
    ``needs_review=True`` and drive the MARKED -> REVIEW_MARKS transition
    via the review PATCH loop below.
    """
    payload_by_type: dict[str, dict[str, Any]] = {
        "mcq": {"selected_index": 0},
        "true_false": {"value": True},
        "matching": {"pairs": []},
        "ordering": {"order": []},
        "fill_blank": {"values": ["x"]},
        "short_answer": {"text": "x"},
        "calculation": {"answer": "0", "working": "x"},
        "table_completion": {"cells": [{"row": 0, "col": 0, "value": "x"}]},
        "labelling": {"labels": {}},
        "extended_response": {"text": "x"},
    }
    return [
        {
            "qid": q["qid"],
            "attempted": True,
            "payload": payload_by_type.get(q["question_type"], {"value": "x"}),
        }
        for section in doc["sections"]
        for q in section["questions"]
    ]


class TestVariantBRouterGuards:
    def test_unauth_returns_401(self) -> None:
        with TestClient(app_module()) as client:
            resp = client.post(f"/cycles/{uuid.uuid4()}/variant-b")
        assert resp.status_code == 401

    def test_other_family_cycle_returns_404_or_409(self) -> None:
        user_a = uuid.uuid4()
        family_repo_a = InMemoryFamilyRepository(user_a)
        gap_repo_a = InMemoryGapReportRepository()
        family, child_id = family_repo_a.bootstrap_family("Fam A", "Kid", "Grade 5")
        assert child_id is not None
        cycle_id_a = _cycle_at_study_pack_done_with_gap_report(
            family_repo_a, gap_repo_a, child_id=child_id
        )

        user_b = uuid.uuid4()
        family_repo_b = InMemoryFamilyRepository(user_b)
        gap_repo_b = InMemoryGapReportRepository()
        marks_repo_b = _VariantAwareMarksRepo()

        _make_overrides(family_repo_b, gap_repo_b, marks_repo_b)
        try:
            with TestClient(app_module()) as client:
                resp = client.post(
                    f"/cycles/{cycle_id_a}/variant-b",
                    headers={"x-user-id": str(user_b)},
                )
        finally:
            _clear_overrides()
        assert resp.status_code in (404, 409)

    def test_wrong_phase_returns_409(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        marks_repo = _VariantAwareMarksRepo()

        family, _ = family_repo.bootstrap_family("Fam", None, None)
        subject = family_repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = family_repo.create_cycle(family.id, subject.id, "scope")  # SCOPE_UPLOADED

        _make_overrides(family_repo, gap_repo, marks_repo)
        try:
            with TestClient(app_module()) as client:
                resp = client.post(
                    f"/cycles/{cycle.id}/variant-b",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_overrides()
        assert resp.status_code == 409

    def test_b_subroutes_409_before_generation(self) -> None:
        """capture/grade/comparison/complete 409 before round 2 has been
        started (cycle still round 1, settled STUDY_PACK)."""
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        marks_repo = _VariantAwareMarksRepo()

        family, child_id = family_repo.bootstrap_family("Fam", "Kid", "Grade 5")
        assert child_id is not None
        cycle_id = _cycle_at_study_pack_done_with_gap_report(
            family_repo, gap_repo, child_id=child_id
        )

        _make_overrides(family_repo, gap_repo, marks_repo)
        try:
            with TestClient(app_module()) as client:
                headers = {"x-user-id": str(user_id)}
                assert (
                    client.get(
                        f"/cycles/{cycle_id}/capture", params={"variant": "B"}, headers=headers
                    ).status_code
                    == 409
                )
                assert (
                    client.post(
                        f"/cycles/{cycle_id}/grade", params={"variant": "B"}, headers=headers
                    ).status_code
                    == 409
                )
                assert (
                    client.get(f"/cycles/{cycle_id}/comparison", headers=headers).status_code == 409
                )
                assert (
                    client.post(f"/cycles/{cycle_id}/complete", headers=headers).status_code == 409
                )
        finally:
            _clear_overrides()


class TestVariantBFullHappyPath:
    def test_generate_approve_capture_submit_grade_review_publish_comparison_complete(
        self,
    ) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        marks_repo = _VariantAwareMarksRepo()

        family, child_id = family_repo.bootstrap_family("Fam", "Kid", "Grade 5")
        assert child_id is not None
        cycle_id = _cycle_at_study_pack_done_with_gap_report(
            family_repo, gap_repo, child_id=child_id
        )

        _make_overrides(family_repo, gap_repo, marks_repo)
        headers = {"x-user-id": str(user_id)}

        try:
            with TestClient(app_module()) as client:
                # 1. Start round 2 + generate its assessment.
                gen_resp = client.post(f"/cycles/{cycle_id}/variant-b", headers=headers)
                assert gen_resp.status_code == 201, gen_resp.text
                b_doc = gen_resp.json()
                assert b_doc["variant"] == "B"
                all_tags = {
                    tag
                    for section in b_doc["sections"]
                    for q in section["questions"]
                    for tag in q["gap_tags"]
                }
                assert "measurement-conversion" in all_tags

                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.round == 2
                assert cycle.phase == CyclePhase.DRAFT_REVIEW

                # On the in-memory tier the cycle's ``assessments`` list is not
                # auto-refreshed from the assessment repo after save() (unlike
                # the Postgres tier's join-on-read) — attach it manually here.
                b_assessment = Assessment.model_validate(b_doc)
                family_repo._cycles[cycle_id] = cycle.model_copy(
                    update={"assessments": [*cycle.assessments, b_assessment]}
                )

                # Idempotent re-call returns the same assessment, does not
                # regenerate, and does not re-advance the phase.
                gen_resp2 = client.post(f"/cycles/{cycle_id}/variant-b", headers=headers)
                assert gen_resp2.status_code == 201
                assert gen_resp2.json()["assessment_id"] == b_doc["assessment_id"]
                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.phase == CyclePhase.DRAFT_REVIEW

                # 2. Parent approves round 2's draft — the SAME endpoint round 1
                #    uses (golden rule 8, per-round approval row).
                approve_resp = client.post(
                    f"/cycles/{cycle_id}/approve", headers=headers, json={"note": "round 2 ok"}
                )
                assert approve_resp.status_code == 200, approve_resp.text
                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.phase == CyclePhase.PRINTED

                round_2_approval = family_repo.get_round_approval(cycle_id, 2)
                assert round_2_approval is not None
                assert round_2_approval.draft_approved_at is not None
                assert round_2_approval.draft_approval_note == "round 2 ok"
                # Round 1's approval row is untouched by round 2's approval.
                round_1_approval = family_repo.get_round_approval(cycle_id, 1)
                assert round_1_approval is not None
                assert round_1_approval.draft_approved_at is not None

                # 3. Capture view (memo-free) — shared endpoint, ?variant=B.
                capture_resp = client.get(
                    f"/cycles/{cycle_id}/capture", params={"variant": "B"}, headers=headers
                )
                assert capture_resp.status_code == 200
                capture_body = capture_resp.json()
                assert "sections" in capture_body
                for section in capture_body["sections"]:
                    for q in section["questions"]:
                        assert "answer" not in q  # no answer key leaked to child view

                # 4. Submit responses.
                responses = _answered_responses(b_doc)
                submit_resp = client.post(
                    f"/cycles/{cycle_id}/submissions",
                    params={"variant": "B"},
                    headers=headers,
                    json={"child_id": str(child_id), "responses": responses},
                )
                assert submit_resp.status_code == 201, submit_resp.text
                submission_id = uuid.UUID(submit_resp.json()["submission_id"])
                marks_repo.register(submission_id, cycle_id, "B")

                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.phase == CyclePhase.ANSWERS_ENTERED

                # 5. Grade — shared endpoint, ?variant=B. Advances to MARKED.
                grade_resp = client.post(
                    f"/cycles/{cycle_id}/grade", params={"variant": "B"}, headers=headers
                )
                assert grade_resp.status_code == 200, grade_resp.text
                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.phase == CyclePhase.MARKED

                # The InMemory submission repo doesn't retain full response
                # payloads for grading (services/grading.py's `_get_responses`
                # InMemory fallback returns []), so the real grader always
                # sees "not attempted" here — seed one mark as an unresolved
                # CLAUDE_ASSIST outcome directly (established pattern for this
                # InMemory-tier limitation) to exercise the review PATCH ->
                # REVIEW_MARKS transition realistically.
                existing_marks = marks_repo.list_for_submission(submission_id)
                assert existing_marks
                unresolved_mark = existing_marks[0].model_copy(
                    update={"final_marks": None, "needs_review": True}
                )
                marks_repo.bulk_upsert(family.id, submission_id, [unresolved_mark])

                # 6. List marks + review any unresolved (CLAUDE_ASSIST) ones —
                #    first PATCH advances MARKED -> REVIEW_MARKS.
                marks_resp = client.get(
                    f"/cycles/{cycle_id}/marks", params={"variant": "B"}, headers=headers
                )
                assert marks_resp.status_code == 200
                for item in marks_resp.json()["items"]:
                    mark = item["mark"]
                    if mark["final_marks"] is None:
                        patch_resp = client.patch(
                            f"/cycles/{cycle_id}/marks/{mark['question_id']}",
                            params={"variant": "B"},
                            headers=headers,
                            json={"final_marks": "0"},
                        )
                        assert patch_resp.status_code == 200

                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.phase == CyclePhase.REVIEW_MARKS

                # 7. Publish round 2's marks — the SAME endpoint round 1 uses,
                #    ?variant=B. Transitions REVIEW_MARKS -> PUBLISHED.
                publish_resp = client.post(
                    f"/cycles/{cycle_id}/publish",
                    params={"variant": "B"},
                    headers=headers,
                    json={},
                )
                assert publish_resp.status_code == 200, publish_resp.text
                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.phase == CyclePhase.PUBLISHED

                round_2_approval = family_repo.get_round_approval(cycle_id, 2)
                assert round_2_approval is not None
                assert round_2_approval.marks_published_at is not None

                # 8. Comparison.
                comparison_resp = client.get(f"/cycles/{cycle_id}/comparison", headers=headers)
                assert comparison_resp.status_code == 200, comparison_resp.text
                comparison_body = comparison_resp.json()
                assert comparison_body["cycle_id"] == str(cycle_id)
                assert "summary" in comparison_body

                # 9. Complete.
                complete_resp = client.post(f"/cycles/{cycle_id}/complete", headers=headers)
                assert complete_resp.status_code == 200, complete_resp.text
                assert complete_resp.json()["state"] == CycleState.CYCLE_COMPLETE.value

                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.state == CycleState.CYCLE_COMPLETE
                assert cycle.round == 2
                assert cycle.phase == CyclePhase.COMPLETE
        finally:
            _clear_overrides()


# ---------------------------------------------------------------------------
# Published-immutability write guard (universal, table-driven via PHASE_CONFIG)
# ---------------------------------------------------------------------------


class TestPublishedImmutabilityGuard:
    """Once round 1's marks are published, its writes are permanently
    blocked (409) — per-round, via ``cycle_round_approvals``. Round 2 (not
    yet published) must still accept writes once it reaches PRINTED."""

    def test_round_1_writes_blocked_after_publish_round_2_writes_ok(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        marks_repo = _VariantAwareMarksRepo()

        family, child_id = family_repo.bootstrap_family("Fam", "Kid", "Grade 5")
        assert child_id is not None
        # This helper's cycle already passes through publish_marks() on its
        # way to a settled STUDY_PACK, so round 1's marks_published_at is set.
        cycle_id = _cycle_at_study_pack_done_with_gap_report(
            family_repo, gap_repo, child_id=child_id
        )
        round_1_approval = family_repo.get_round_approval(cycle_id, 1)
        assert round_1_approval is not None
        assert round_1_approval.marks_published_at is not None

        _make_overrides(family_repo, gap_repo, marks_repo)
        headers = {"x-user-id": str(user_id)}
        try:
            with TestClient(app_module()) as client:
                # Round 1 (Variant A) create_submission is blocked — published + immutable.
                resp = client.post(
                    f"/cycles/{cycle_id}/submissions",
                    params={"variant": "A"},
                    headers=headers,
                    json={"child_id": str(child_id), "responses": []},
                )
                assert resp.status_code == 409
                assert "published" in resp.json()["detail"].lower()

                # Round 1 grade is blocked.
                resp = client.post(
                    f"/cycles/{cycle_id}/grade", params={"variant": "A"}, headers=headers
                )
                assert resp.status_code == 409
                assert "published" in resp.json()["detail"].lower()

                # Round 1 review PATCH is blocked.
                resp = client.patch(
                    f"/cycles/{cycle_id}/marks/A.1",
                    params={"variant": "A"},
                    headers=headers,
                    json={"final_marks": "1.0"},
                )
                assert resp.status_code == 409
                assert "published" in resp.json()["detail"].lower()

                # Meanwhile, round 2 (Variant B) writes still work once
                # started — round 2 is never published yet at this point.
                gen_resp = client.post(f"/cycles/{cycle_id}/variant-b", headers=headers)
                assert gen_resp.status_code == 201, gen_resp.text
                b_doc = gen_resp.json()

                cycle = family_repo.get_cycle(cycle_id)
                assert cycle is not None
                assert cycle.round == 2
                assert cycle.phase == CyclePhase.DRAFT_REVIEW

                # On the in-memory tier, cycle.assessments is not auto-refreshed
                # from the assessment repo after save() (unlike Postgres's
                # join-on-read) — attach the B assessment manually, mirroring
                # TestVariantBFullHappyPath.
                b_assessment = Assessment.model_validate(b_doc)
                family_repo._cycles[cycle_id] = cycle.model_copy(
                    update={"assessments": [*cycle.assessments, b_assessment]}
                )

                approve_resp = client.post(f"/cycles/{cycle_id}/approve", headers=headers, json={})
                assert approve_resp.status_code == 200, approve_resp.text

                responses = _answered_responses(b_doc)
                submit_resp = client.post(
                    f"/cycles/{cycle_id}/submissions",
                    params={"variant": "B"},
                    headers=headers,
                    json={"child_id": str(child_id), "responses": responses},
                )
                assert submit_resp.status_code == 201, submit_resp.text
                submission_id = uuid.UUID(submit_resp.json()["submission_id"])
                marks_repo.register(submission_id, cycle_id, "B")

                grade_resp = client.post(
                    f"/cycles/{cycle_id}/grade", params={"variant": "B"}, headers=headers
                )
                assert grade_resp.status_code == 200, grade_resp.text
        finally:
            _clear_overrides()
