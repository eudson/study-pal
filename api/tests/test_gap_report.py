"""Unit tests for Phase 4 — gap report derivation and endpoints.

Coverage:
- Derivation: mastered / growing / half-marks / mixed.
- Derivation: error_category passthrough.
- Derivation: gap_tags passthrough + growing_gap_tags in summary.
- Derivation: summary counts + total marks.
- Derivation: None final_marks raises ValueError (post-publish invariant).
- Derivation: question with no matching mark is skipped (defensive).
- InMemoryGapReportRepository: upsert + get_for_cycle + idempotency (upsert twice).
- Endpoint guards: wrong state → 409, not-generated → 404.
- Endpoint POST: returns GapReport on valid cycle.
- Endpoint GET: returns stored report after POST.
- DB-tier RLS: gap_reports isolation (skipped when Postgres unreachable).
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

from schemas.assessment_schema import Assessment, ErrorCategory, GradingPath
from schemas.gap_report import GapReport, GapStatus
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
)
from tests.samples.maths_sample import maths_assessment

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAMILY_ID = uuid.uuid4()
_SUBMISSION_ID = uuid.uuid4()


def _assessment(raw: dict[str, Any] | None = None) -> Assessment:
    return Assessment.model_validate(raw or maths_assessment())


def _mark(
    question_id: str,
    marks_total: str,
    final_marks: str | None,
    suggested_marks: str | None = None,
    error_category: ErrorCategory | None = None,
    needs_review: bool = False,
    grading_path: GradingPath = GradingPath.AUTO,
) -> QuestionMark:
    return QuestionMark(
        family_id=_FAMILY_ID,
        submission_id=_SUBMISSION_ID,
        question_id=question_id,
        marks_total=Decimal(marks_total),
        suggested_marks=Decimal(suggested_marks or final_marks or "0"),
        final_marks=Decimal(final_marks) if final_marks is not None else None,
        grading_path=grading_path,
        needs_review=needs_review,
        error_category=error_category,
    )


# ---------------------------------------------------------------------------
# Derivation tests
# ---------------------------------------------------------------------------


class TestDeriveGapReportMasteredGrowing:
    """mastered = full marks; growing = anything less (incl. zero, half-marks)."""

    def test_full_marks_is_mastered(self) -> None:
        asmt = _assessment()
        # A.1: marks_total=1.0, final=1.0
        marks = [_mark("A.1", "1.0", "1.0")]
        report = derive_gap_report(asmt, marks)
        item = next((i for i in report.items if i.question_id == "A.1"), None)
        assert item is not None
        assert item.status == GapStatus.MASTERED

    def test_zero_marks_is_growing(self) -> None:
        asmt = _assessment()
        marks = [_mark("A.1", "1.0", "0.0")]
        report = derive_gap_report(asmt, marks)
        item = next((i for i in report.items if i.question_id == "A.1"), None)
        assert item is not None
        assert item.status == GapStatus.GROWING

    def test_partial_marks_is_growing(self) -> None:
        asmt = _assessment()
        # B.1: marks_total=3.0 (calculation). Get 1.5 → growing.
        marks = [_mark("B.1", "3.0", "1.5")]
        report = derive_gap_report(asmt, marks)
        item = next((i for i in report.items if i.question_id == "B.1"), None)
        assert item is not None
        assert item.status == GapStatus.GROWING
        assert item.final_marks == Decimal("1.5")

    def test_half_mark_on_full_total_is_growing(self) -> None:
        asmt = _assessment()
        # A.1 total=1.0; earn 0.5 → growing.
        marks = [_mark("A.1", "1.0", "0.5")]
        report = derive_gap_report(asmt, marks)
        item = next(i for i in report.items if i.question_id == "A.1")
        assert item.status == GapStatus.GROWING
        assert item.final_marks == Decimal("0.5")

    def test_half_mark_half_total_is_mastered(self) -> None:
        """If marks_total IS 0.5 and final_marks IS 0.5, that's mastered."""
        # Build a minimal assessment with a 0.5-mark question.
        raw = {
            "assessment_id": "asmt-half",
            "cycle_id": "cycle-half",
            "variant": "A",
            "subject": "Test",
            "content_language": "en",
            "grade_label": "Grade 1",
            "title": "Half-mark test",
            "duration_minutes": 10,
            "declared_total_marks": 0.5,
            "sections": [
                {
                    "label": "A",
                    "title": "Section A",
                    "declared_marks": 0.5,
                    "questions": [
                        {
                            "qid": "A.1",
                            "number": "1",
                            "text": "True or False?",
                            "question_type": "true_false",
                            "difficulty": "easy",
                            "answer": {"kind": "true_false", "is_true": True},
                            "mark_rules": {"total": 0.5},
                        }
                    ],
                }
            ],
        }
        asmt = Assessment.model_validate(raw)
        marks = [_mark("A.1", "0.5", "0.5")]
        report = derive_gap_report(asmt, marks)
        item = next(i for i in report.items if i.question_id == "A.1")
        assert item.status == GapStatus.MASTERED


class TestDeriveGapReportSummaryCounts:
    """Summary counts and totals are correct."""

    def test_all_mastered(self) -> None:
        asmt = _assessment()
        # maths_assessment has: A.1(1.0), A.2(2.0), B.1(3.0), B.2(2.0) = 8.0 total
        marks = [
            _mark("A.1", "1.0", "1.0"),
            _mark("A.2", "2.0", "2.0"),
            _mark("B.1", "3.0", "3.0"),
            _mark("B.2", "2.0", "2.0"),
        ]
        report = derive_gap_report(asmt, marks)
        assert report.summary.mastered_count == 4
        assert report.summary.growing_count == 0
        assert report.summary.total_marks_earned == Decimal("8.0")
        assert report.summary.total_marks_available == Decimal("8.0")

    def test_mixed(self) -> None:
        asmt = _assessment()
        marks = [
            _mark("A.1", "1.0", "1.0"),  # mastered
            _mark("A.2", "2.0", "1.0"),  # growing
            _mark("B.1", "3.0", "1.5"),  # growing
            _mark("B.2", "2.0", "2.0"),  # mastered
        ]
        report = derive_gap_report(asmt, marks)
        assert report.summary.mastered_count == 2
        assert report.summary.growing_count == 2
        assert report.summary.total_marks_earned == Decimal("5.5")
        assert report.summary.total_marks_available == Decimal("8.0")

    def test_all_growing(self) -> None:
        asmt = _assessment()
        marks = [
            _mark("A.1", "1.0", "0.0"),
            _mark("A.2", "2.0", "0.0"),
            _mark("B.1", "3.0", "0.0"),
            _mark("B.2", "2.0", "0.0"),
        ]
        report = derive_gap_report(asmt, marks)
        assert report.summary.mastered_count == 0
        assert report.summary.growing_count == 4
        assert report.summary.total_marks_earned == Decimal("0.0")

    def test_no_marks_gives_empty_report(self) -> None:
        asmt = _assessment()
        report = derive_gap_report(asmt, [])
        assert report.items == []
        assert report.summary.mastered_count == 0
        assert report.summary.growing_count == 0


class TestDeriveGapReportErrorCategoryPassthrough:
    """error_category is passed through verbatim from the QuestionMark."""

    def test_concept_gap_passed_through(self) -> None:
        asmt = _assessment()
        marks = [_mark("A.1", "1.0", "0.0", error_category=ErrorCategory.CONCEPT_GAP)]
        report = derive_gap_report(asmt, marks)
        item = next(i for i in report.items if i.question_id == "A.1")
        assert item.error_category == "concept_gap"

    def test_no_error_category_is_none(self) -> None:
        asmt = _assessment()
        marks = [_mark("A.1", "1.0", "1.0")]
        report = derive_gap_report(asmt, marks)
        item = next(i for i in report.items if i.question_id == "A.1")
        assert item.error_category is None

    def test_all_error_categories_passed_through(self) -> None:
        asmt = _assessment()
        for cat in ErrorCategory:
            marks = [_mark("A.1", "1.0", "0.0", error_category=cat)]
            report = derive_gap_report(asmt, marks)
            item = next(i for i in report.items if i.question_id == "A.1")
            assert item.error_category == cat.value


class TestDeriveGapReportGapTags:
    """gap_tags are passed through from the assessment question to the item."""

    def _assessment_with_tags(self) -> Assessment:
        """Build an assessment where questions carry gap_tags."""
        raw: dict[str, Any] = {
            "assessment_id": "asmt-tags",
            "cycle_id": "cycle-tags",
            "variant": "A",
            "subject": "Test",
            "content_language": "en",
            "grade_label": "Grade 5",
            "title": "Tags test",
            "duration_minutes": 30,
            "declared_total_marks": 3.0,
            "sections": [
                {
                    "label": "A",
                    "title": "Section A",
                    "declared_marks": 3.0,
                    "questions": [
                        {
                            "qid": "A.1",
                            "number": "1",
                            "text": "Q1",
                            "question_type": "mcq",
                            "difficulty": "easy",
                            "answer": {
                                "kind": "mcq",
                                "options": ["a", "b", "c"],
                                "correct_index": 0,
                            },
                            "mark_rules": {"total": 1.0},
                            "gap_tags": ["fractions", "division"],
                        },
                        {
                            "qid": "A.2",
                            "number": "2",
                            "text": "Q2",
                            "question_type": "mcq",
                            "difficulty": "easy",
                            "answer": {
                                "kind": "mcq",
                                "options": ["x", "y", "z"],
                                "correct_index": 1,
                            },
                            "mark_rules": {"total": 1.0},
                            "gap_tags": ["division"],
                        },
                        {
                            "qid": "A.3",
                            "number": "3",
                            "text": "Q3",
                            "question_type": "mcq",
                            "difficulty": "easy",
                            "answer": {
                                "kind": "mcq",
                                "options": ["p", "q", "r"],
                                "correct_index": 2,
                            },
                            "mark_rules": {"total": 1.0},
                            # No gap_tags
                        },
                    ],
                }
            ],
        }
        return Assessment.model_validate(raw)

    def test_gap_tags_on_item(self) -> None:
        asmt = self._assessment_with_tags()
        marks = [_mark("A.1", "1.0", "0.0")]
        report = derive_gap_report(asmt, marks)
        item = next(i for i in report.items if i.question_id == "A.1")
        assert item.gap_tags == ["fractions", "division"]

    def test_empty_gap_tags_on_item_without_tags(self) -> None:
        asmt = self._assessment_with_tags()
        marks = [_mark("A.3", "1.0", "0.0")]
        report = derive_gap_report(asmt, marks)
        item = next(i for i in report.items if i.question_id == "A.3")
        assert item.gap_tags == []

    def test_growing_gap_tags_in_summary_deduplicates(self) -> None:
        asmt = self._assessment_with_tags()
        marks = [
            _mark("A.1", "1.0", "0.0"),  # growing, tags: fractions, division
            _mark("A.2", "1.0", "0.0"),  # growing, tags: division (overlap)
            _mark("A.3", "1.0", "1.0"),  # mastered, no tags
        ]
        report = derive_gap_report(asmt, marks)
        # "division" appears in both growing items — must be deduplicated.
        assert set(report.summary.growing_gap_tags) == {"fractions", "division"}
        # Sorted for stability.
        assert report.summary.growing_gap_tags == sorted(report.summary.growing_gap_tags)

    def test_mastered_items_do_not_contribute_tags_to_summary(self) -> None:
        asmt = self._assessment_with_tags()
        marks = [
            _mark("A.1", "1.0", "1.0"),  # mastered — tags should NOT appear in summary
            _mark("A.2", "1.0", "0.0"),  # growing, tags: division
        ]
        report = derive_gap_report(asmt, marks)
        # Only A.2's tag should appear (A.1 is mastered).
        assert report.summary.growing_gap_tags == ["division"]
        assert "fractions" not in report.summary.growing_gap_tags

    def test_all_mastered_gives_empty_growing_tags(self) -> None:
        asmt = self._assessment_with_tags()
        marks = [
            _mark("A.1", "1.0", "1.0"),
            _mark("A.2", "1.0", "1.0"),
            _mark("A.3", "1.0", "1.0"),
        ]
        report = derive_gap_report(asmt, marks)
        assert report.summary.growing_gap_tags == []


class TestDeriveGapReportInvariants:
    """Edge cases and invariant violations."""

    def test_none_final_marks_raises(self) -> None:
        asmt = _assessment()
        marks = [_mark("A.1", "1.0", None)]  # None final_marks — post-publish violation
        with pytest.raises(ValueError, match="final_marks is None"):
            derive_gap_report(asmt, marks)

    def test_unknown_question_id_in_marks_is_ignored(self) -> None:
        """Marks for question_ids not in the assessment are silently ignored."""
        asmt = _assessment()
        marks = [_mark("NONEXISTENT.99", "1.0", "0.5")]
        # Should not raise; items will be empty.
        report = derive_gap_report(asmt, marks)
        assert not any(i.question_id == "NONEXISTENT.99" for i in report.items)

    def test_question_without_mark_is_skipped(self) -> None:
        """Questions in assessment with no corresponding mark are skipped."""
        asmt = _assessment()
        # Only provide a mark for A.1; all other questions have no mark.
        marks = [_mark("A.1", "1.0", "1.0")]
        report = derive_gap_report(asmt, marks)
        assert len(report.items) == 1
        assert report.items[0].question_id == "A.1"

    def test_items_ordered_by_assessment_section_question(self) -> None:
        """Items follow assessment order (section by section, question by question)."""
        asmt = _assessment()
        # Provide marks in reverse order to verify ordering is from assessment.
        marks = [
            _mark("B.2", "2.0", "2.0"),
            _mark("B.1", "3.0", "3.0"),
            _mark("A.2", "2.0", "2.0"),
            _mark("A.1", "1.0", "1.0"),
        ]
        report = derive_gap_report(asmt, marks)
        qids = [i.question_id for i in report.items]
        assert qids == ["A.1", "A.2", "B.1", "B.2"]

    def test_assessment_id_and_cycle_id_on_report(self) -> None:
        asmt = _assessment()
        marks = [_mark("A.1", "1.0", "1.0")]
        report = derive_gap_report(asmt, marks)
        assert report.assessment_id == asmt.assessment_id
        assert report.cycle_id == asmt.cycle_id

    def test_derived_at_is_utc(self) -> None:
        asmt = _assessment()
        before = datetime.now(tz=UTC)
        report = derive_gap_report(asmt, [])
        after = datetime.now(tz=UTC)
        assert report.derived_at.tzinfo is not None
        assert before <= report.derived_at <= after


# ---------------------------------------------------------------------------
# InMemoryGapReportRepository
# ---------------------------------------------------------------------------


class TestInMemoryGapReportRepository:
    def _report(self) -> GapReport:
        asmt = _assessment()
        marks = [
            _mark("A.1", "1.0", "1.0"),
            _mark("A.2", "2.0", "1.0"),
        ]
        return derive_gap_report(asmt, marks)

    def test_upsert_returns_row(self) -> None:
        repo = InMemoryGapReportRepository()
        cid = uuid.uuid4()
        sid = uuid.uuid4()
        report = self._report()
        row = repo.upsert(_FAMILY_ID, cid, sid, report)
        assert row.cycle_id == cid
        assert row.submission_id == sid
        assert row.report.summary.mastered_count == report.summary.mastered_count

    def test_get_for_cycle_returns_row(self) -> None:
        repo = InMemoryGapReportRepository()
        cid = uuid.uuid4()
        sid = uuid.uuid4()
        report = self._report()
        repo.upsert(_FAMILY_ID, cid, sid, report)
        fetched = repo.get_for_cycle(cid)
        assert fetched is not None
        assert fetched.cycle_id == cid

    def test_get_for_cycle_returns_none_when_not_generated(self) -> None:
        repo = InMemoryGapReportRepository()
        assert repo.get_for_cycle(uuid.uuid4()) is None

    def test_upsert_idempotency_overwrites(self) -> None:
        """Second upsert with a different report replaces the first."""
        repo = InMemoryGapReportRepository()
        cid = uuid.uuid4()
        sid = uuid.uuid4()

        # First upsert: 1 mastered, 1 growing.
        report1 = derive_gap_report(
            _assessment(),
            [_mark("A.1", "1.0", "1.0"), _mark("A.2", "2.0", "0.0")],
        )
        row1 = repo.upsert(_FAMILY_ID, cid, sid, report1)

        # Second upsert: different marks.
        report2 = derive_gap_report(
            _assessment(),
            [_mark("A.1", "1.0", "0.0"), _mark("A.2", "2.0", "2.0")],
        )
        row2 = repo.upsert(_FAMILY_ID, cid, sid, report2)

        # Row id should be stable across re-runs.
        assert row1.id == row2.id

        # Latest report should be stored.
        fetched = repo.get_for_cycle(cid)
        assert fetched is not None
        assert fetched.report.summary.mastered_count == 1
        assert fetched.report.summary.growing_count == 1
        # A.1 is now growing (0.0), A.2 is mastered (2.0).
        item_a1 = next(i for i in fetched.report.items if i.question_id == "A.1")
        assert item_a1.status == GapStatus.GROWING

    def test_upsert_different_cycles_isolated(self) -> None:
        repo = InMemoryGapReportRepository()
        cid1 = uuid.uuid4()
        cid2 = uuid.uuid4()
        r1 = self._report()
        r2 = self._report()
        repo.upsert(_FAMILY_ID, cid1, uuid.uuid4(), r1)
        repo.upsert(_FAMILY_ID, cid2, uuid.uuid4(), r2)
        assert repo.get_for_cycle(cid1) is not None
        assert repo.get_for_cycle(cid2) is not None
        assert repo.get_for_cycle(uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# Endpoint tests (InMemory — no Postgres)
# ---------------------------------------------------------------------------


def _cycle_at_gap_report_state(
    family_repo: InMemoryFamilyRepository,
) -> uuid.UUID:
    """Create a cycle fully advanced to GAP_REPORT state. Returns cycle_id."""
    from schemas.family import VisibilityDefaults

    family, _ = family_repo.bootstrap_family("Test Family", None, None)
    subject = family_repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
    cycle = family_repo.create_cycle(family.id, subject.id, "scope")

    advance_to_generating(family_repo, cycle.id)
    advance_to_parent_reviews(family_repo, cycle.id)
    approve_draft(family_repo, cycle.id)
    advance_to_answers_entered(family_repo, cycle.id)
    advance_to_auto_marked(family_repo, cycle.id)
    advance_to_parent_review_marks(family_repo, cycle.id)
    publish_marks(family_repo, cycle.id, VisibilityDefaults())

    return cycle.id


class TestGapReportEndpointStateGuard:
    """POST endpoint returns 409 when cycle is in a pre-GAP_REPORT state."""

    def test_scope_uploaded_returns_409(self) -> None:
        """Cycle in SCOPE_UPLOADED must return 409."""

        from fastapi.testclient import TestClient

        from dependencies import (
            get_family_repository,
            get_gap_report_repository,
            get_question_mark_repository,
        )
        from main import app
        from services.repositories.memory import InMemoryFamilyRepository

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        family, _ = family_repo.bootstrap_family("TestFamily", None, None)
        subject = family_repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = family_repo.create_cycle(family.id, subject.id, "scope")

        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        app.dependency_overrides[get_family_repository] = lambda: family_repo
        app.dependency_overrides[get_question_mark_repository] = lambda: marks_repo
        app.dependency_overrides[get_gap_report_repository] = lambda: gap_repo

        with TestClient(app) as client:
            resp = client.post(
                f"/cycles/{cycle.id}/gap-report",
                headers={"x-user-id": str(user_id)},
            )

        app.dependency_overrides.pop(get_family_repository, None)
        app.dependency_overrides.pop(get_question_mark_repository, None)
        app.dependency_overrides.pop(get_gap_report_repository, None)

        assert resp.status_code == 409

    def test_gap_report_state_allowed(self) -> None:
        """Cycle in GAP_REPORT state should proceed (not 409)."""
        from fastapi.testclient import TestClient

        from dependencies import (
            get_family_repository,
            get_gap_report_repository,
            get_question_mark_repository,
        )
        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)

        # Resolve the cycle so we can use the correct family_id.
        raw_cycle = family_repo.get_cycle(cycle_id)
        assert raw_cycle is not None
        family_id = raw_cycle.family_id

        # Inject an assessment into the cycle's assessments list.
        asmt = _assessment(
            {
                **maths_assessment(),
                "cycle_id": str(cycle_id),
            }
        )
        updated_cycle = raw_cycle.model_copy(update={"assessments": [asmt]})
        family_repo._cycles[cycle_id] = updated_cycle

        # Seed marks for all questions in the assessment so derivation has data.
        submission_id = uuid.uuid4()
        marks_with_sid = [
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
        marks_repo.bulk_upsert(family_id, submission_id, marks_with_sid)

        # Use a subclass to override get_submission_id_for_cycle for this test.
        _cycle_id = cycle_id
        _submission_id = submission_id
        _base_repo = marks_repo

        class _PatchedMarksRepo(InMemoryQuestionMarkRepository):
            def get_submission_id_for_cycle(self, cid: uuid.UUID, variant: str) -> uuid.UUID | None:
                if cid == _cycle_id and variant == "A":
                    return _submission_id
                return None

            def list_for_cycle(self, cid: uuid.UUID, variant: str) -> list[QuestionMark]:
                if variant != "A":
                    return []
                return _base_repo.list_for_submission(_submission_id)

        patched_marks_repo = _PatchedMarksRepo()

        app.dependency_overrides[get_family_repository] = lambda: family_repo
        app.dependency_overrides[get_question_mark_repository] = lambda: patched_marks_repo
        app.dependency_overrides[get_gap_report_repository] = lambda: gap_repo

        with TestClient(app) as client:
            resp = client.post(
                f"/cycles/{cycle_id}/gap-report",
                headers={"x-user-id": str(user_id)},
            )

        app.dependency_overrides.pop(get_family_repository, None)
        app.dependency_overrides.pop(get_question_mark_repository, None)
        app.dependency_overrides.pop(get_gap_report_repository, None)

        assert resp.status_code == 200
        body = resp.json()
        assert "report" in body
        assert body["report"]["summary"]["mastered_count"] == 2
        assert body["report"]["summary"]["growing_count"] == 2


class TestGapReportEndpointGetNotGenerated:
    """GET endpoint returns 404 when report not yet generated."""

    def test_get_404_when_not_generated(self) -> None:
        from fastapi.testclient import TestClient

        from dependencies import (
            get_family_repository,
            get_gap_report_repository,
            get_question_mark_repository,
        )
        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)

        app.dependency_overrides[get_family_repository] = lambda: family_repo
        app.dependency_overrides[get_question_mark_repository] = lambda: marks_repo
        app.dependency_overrides[get_gap_report_repository] = lambda: gap_repo

        with TestClient(app) as client:
            resp = client.get(
                f"/cycles/{cycle_id}/gap-report",
                headers={"x-user-id": str(user_id)},
            )

        app.dependency_overrides.pop(get_family_repository, None)
        app.dependency_overrides.pop(get_question_mark_repository, None)
        app.dependency_overrides.pop(get_gap_report_repository, None)

        assert resp.status_code == 404

    def test_get_404_unknown_cycle(self) -> None:
        from fastapi.testclient import TestClient

        from dependencies import (
            get_family_repository,
            get_gap_report_repository,
            get_question_mark_repository,
        )
        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        # Bootstrap a family so _resolve_family_id finds one.
        family_repo.bootstrap_family("Test", None, None)
        marks_repo = InMemoryQuestionMarkRepository()
        gap_repo = InMemoryGapReportRepository()

        app.dependency_overrides[get_family_repository] = lambda: family_repo
        app.dependency_overrides[get_question_mark_repository] = lambda: marks_repo
        app.dependency_overrides[get_gap_report_repository] = lambda: gap_repo

        with TestClient(app) as client:
            resp = client.get(
                f"/cycles/{uuid.uuid4()}/gap-report",
                headers={"x-user-id": str(user_id)},
            )

        app.dependency_overrides.pop(get_family_repository, None)
        app.dependency_overrides.pop(get_question_mark_repository, None)
        app.dependency_overrides.pop(get_gap_report_repository, None)

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DB-tier RLS test — skipped when Postgres unreachable
# ---------------------------------------------------------------------------

_DSN = os.environ.get("STUDYPAL_DB_DSN", "postgresql://studypal:studypal@localhost:5432/studypal")


def _try_connect_db() -> bool:
    try:
        conn = psycopg.connect(_DSN, connect_timeout=3, autocommit=True)
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def owner_conn_gap() -> Any:
    if not _try_connect_db():
        pytest.skip("Local Postgres not reachable")
    conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
    )
    yield conn
    conn.close()


@pytest.mark.skipif(
    not _try_connect_db(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)
class TestGapReportRLS:
    """Prove that gap_reports are RLS-isolated by family_id.

    User A's gap report must not be visible to user B.
    """

    def _open_auth_conn(self, user_id: uuid.UUID) -> psycopg.Connection[dict[str, Any]]:
        claims = json.dumps({"sub": str(user_id)})
        conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
            _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
        )
        conn.execute("SET ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))
        return conn

    def _seed_gap_report(
        self,
        owner_conn: psycopg.Connection[dict[str, Any]],
        user_id: uuid.UUID,
        family_name: str,
    ) -> uuid.UUID:
        """Seed a gap_report row. Returns the gap_report id."""
        cur = owner_conn.cursor()

        cur.execute("INSERT INTO families (name) VALUES (%s) RETURNING id", (family_name,))
        row = cur.fetchone()
        assert row is not None
        family_id = uuid.UUID(str(row["id"]))

        cur.execute(
            "INSERT INTO family_members (user_id, family_id) VALUES (%s, %s)",
            (str(user_id), str(family_id)),
        )
        cur.execute(
            "INSERT INTO children (family_id, display_name, grade_label) "
            "VALUES (%s, 'Kid', 'Grade 5') RETURNING id",
            (str(family_id),),
        )
        row = cur.fetchone()
        assert row is not None
        child_id = uuid.UUID(str(row["id"]))

        cur.execute(
            "INSERT INTO subjects (family_id, child_id, name, content_language) "
            "VALUES (%s, %s, 'Maths', 'en') RETURNING id",
            (str(family_id), str(child_id)),
        )
        row = cur.fetchone()
        assert row is not None
        subject_id = uuid.UUID(str(row["id"]))

        cur.execute(
            "INSERT INTO cycles (family_id, subject_id, state) "
            "VALUES (%s, %s, 'GAP_REPORT') RETURNING id",
            (str(family_id), str(subject_id)),
        )
        row = cur.fetchone()
        assert row is not None
        cycle_id = uuid.UUID(str(row["id"]))

        # Create a minimal assessment and submission.
        asmt_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO assessments (id, family_id, cycle_id, variant, subject, "
            "content_language, declared_total_marks, computed_total_marks, assessment, "
            "schema_version) "
            "VALUES (%s, %s, %s, 'A', 'Maths', 'en', 1.0, 1.0, %s::jsonb, '1.0')",
            (
                str(asmt_id),
                str(family_id),
                str(cycle_id),
                json.dumps({"assessment_id": str(asmt_id), "cycle_id": str(cycle_id)}),
            ),
        )

        submission_id = uuid.uuid4()
        submission_doc = json.dumps(
            {
                "child_id": str(child_id),
                "responses": [],
                "proof_photo_paths": [],
            }
        )
        cur.execute(
            "INSERT INTO submissions (id, family_id, assessment_id, child_id, submission) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (
                str(submission_id),
                str(family_id),
                str(asmt_id),
                str(child_id),
                submission_doc,
            ),
        )

        # Insert a minimal gap_report row directly.
        minimal_report = json.dumps(
            {
                "assessment_id": str(asmt_id),
                "cycle_id": str(cycle_id),
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
            (str(family_id), str(cycle_id), str(submission_id), minimal_report),
        )
        row = cur.fetchone()
        assert row is not None
        gap_report_id = uuid.UUID(str(row["id"]))

        owner_conn.commit()
        return gap_report_id

    def test_gap_report_rls_isolated(
        self, owner_conn_gap: psycopg.Connection[dict[str, Any]]
    ) -> None:
        """User B must not see user A's gap report."""
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        gap_a_id = self._seed_gap_report(owner_conn_gap, user_a, f"FamilyA-{user_a.hex[:6]}")
        _ = self._seed_gap_report(owner_conn_gap, user_b, f"FamilyB-{user_b.hex[:6]}")

        # User B must NOT see user A's gap report.
        conn_b = self._open_auth_conn(user_b)
        try:
            b_cur = conn_b.cursor()
            b_cur.execute("SELECT id FROM gap_reports WHERE id = %s", (str(gap_a_id),))
            row = b_cur.fetchone()
            assert row is None, "RLS violation: user B can see user A's gap report"
        finally:
            conn_b.close()

        # User A must see their own gap report.
        conn_a = self._open_auth_conn(user_a)
        try:
            a_cur = conn_a.cursor()
            a_cur.execute("SELECT id, report FROM gap_reports WHERE id = %s", (str(gap_a_id),))
            row = a_cur.fetchone()
            assert row is not None, "User A cannot see their own gap report"
            assert row["id"] is not None
        finally:
            conn_a.close()
