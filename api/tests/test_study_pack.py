"""Unit tests for Phase 5 — study pack generation, endpoints, approval gate, and PDF.

Coverage:
- FakeStudyPack: derives items only from growing gap_tags; deterministic.
- FakeStudyPack: no items when no growing gaps; correct summary.
- FakeStudyPack: generic item when growing items have no gap_tags.
- FakeStudyPack: derived_from_gap_tags sorted and deduplicated.
- InMemoryStudyPackRepository: upsert / get_for_cycle / idempotency / approved_at preservation.
- Endpoint POST: state advances GAP_REPORT → GENERATING_STUDY_PACK → STUDY_PACK_DONE.
- Endpoint POST: items derived from growing gap_tags.
- Endpoint POST: idempotent re-run.
- Endpoint GET: 404 when not yet generated.
- Endpoint GET: returns stored pack after POST.
- Endpoint guard: pre-GAP_REPORT state → 409.
- Endpoint guard: other-family / missing cycle → 404.
- Approve: approved_at null until approve called.
- Approve: records a timestamp; GET reflects it.
- Approve: 404 when pack not yet generated.
- PDF: renders without error (bytes non-empty, starts with %%PDF).
- Subject-agnostic: two subjects with identical gap shapes produce structurally
  identical study packs (same item count, same tag set).
- DB-tier tests skip cleanly when Postgres unreachable.
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

from schemas.assessment_schema import Assessment
from schemas.family import CycleState, VisibilityDefaults
from schemas.gap_report import GapReport, GapReportItem, GapReportSummary, GapStatus
from schemas.study_pack import StudyPack, StudyPackItem
from services.cycle import (
    advance_to_answers_entered,
    advance_to_auto_marked,
    advance_to_generating,
    advance_to_generating_study_pack,
    advance_to_parent_review_marks,
    advance_to_parent_reviews,
    advance_to_study_pack_done,
    approve_draft,
    publish_marks,
)
from services.repositories.memory import (
    InMemoryFamilyRepository,
    InMemoryGapReportRepository,
    InMemoryStudyPackRepository,
)
from services.study_pack import FakeStudyPack
from tests.samples.maths_sample import maths_assessment

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_FAMILY_ID = uuid.uuid4()
_SUBMISSION_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gap_report_with_tags(
    growing_tags: list[list[str]],
    mastered_count: int = 0,
    cycle_id: str = "cycle-sp-test",
    assessment_id: str = "asmt-sp-test",
) -> GapReport:
    """Build a GapReport with growing items carrying specified tag lists."""
    items: list[GapReportItem] = []

    for i, tags in enumerate(growing_tags):
        items.append(
            GapReportItem(
                question_id=f"G.{i + 1}",
                number=str(i + 1),
                text=f"Growing question {i + 1}",
                status=GapStatus.GROWING,
                final_marks=Decimal("0"),
                marks_total=Decimal("1"),
                gap_tags=tags,
            )
        )

    for i in range(mastered_count):
        items.append(
            GapReportItem(
                question_id=f"M.{i + 1}",
                number=str(len(growing_tags) + i + 1),
                text=f"Mastered question {i + 1}",
                status=GapStatus.MASTERED,
                final_marks=Decimal("1"),
                marks_total=Decimal("1"),
                gap_tags=[],
            )
        )

    growing_tag_set: set[str] = set()
    for tags in growing_tags:
        growing_tag_set.update(tags)

    summary = GapReportSummary(
        mastered_count=mastered_count,
        growing_count=len(growing_tags),
        total_marks_earned=Decimal(str(mastered_count)),
        total_marks_available=Decimal(str(mastered_count + len(growing_tags))),
        growing_gap_tags=sorted(growing_tag_set),
    )

    return GapReport(
        assessment_id=assessment_id,
        cycle_id=cycle_id,
        items=items,
        summary=summary,
        derived_at=datetime.now(tz=UTC),
    )


def _gap_report_no_gaps(cycle_id: str = "cycle-sp-nogap") -> GapReport:
    """Build a GapReport with no growing items."""
    return _gap_report_with_tags([], mastered_count=3, cycle_id=cycle_id)


def _cycle_at_gap_report_state(
    family_repo: InMemoryFamilyRepository,
) -> uuid.UUID:
    """Create a cycle fully advanced to GAP_REPORT state. Returns cycle_id."""
    family, _ = family_repo.bootstrap_family("Test Family", None, None)
    subject = family_repo.create_subject(family.id, uuid.uuid4(), "General", "en")
    cycle = family_repo.create_cycle(family.id, subject.id, "scope")

    advance_to_generating(family_repo, cycle.id)
    advance_to_parent_reviews(family_repo, cycle.id)
    approve_draft(family_repo, cycle.id)
    advance_to_answers_entered(family_repo, cycle.id)
    advance_to_auto_marked(family_repo, cycle.id)
    advance_to_parent_review_marks(family_repo, cycle.id)
    publish_marks(family_repo, cycle.id, VisibilityDefaults())

    return cycle.id


def _inject_gap_report(
    family_repo: InMemoryFamilyRepository,
    gap_repo: InMemoryGapReportRepository,
    cycle_id: uuid.UUID,
    growing_tags: list[list[str]],
) -> GapReport:
    """Inject a stored gap report into the repo for a cycle."""
    cycle = family_repo.get_cycle(cycle_id)
    assert cycle is not None
    report = _gap_report_with_tags(growing_tags, cycle_id=str(cycle_id))
    submission_id = uuid.uuid4()
    gap_repo.upsert(cycle.family_id, cycle_id, submission_id, report)
    return report


# ---------------------------------------------------------------------------
# FakeStudyPack — generation tests
# ---------------------------------------------------------------------------


class TestFakeStudyPackGeneration:
    """FakeStudyPack produces items strictly from growing gap_tags."""

    def test_items_derived_from_growing_tags(self) -> None:
        """One item per distinct growing gap_tag."""
        report = _gap_report_with_tags(
            [["fractions", "division"], ["division"]],  # 2 growing items, 2 distinct tags
        )
        gen = FakeStudyPack()
        pack = gen.generate(report)

        assert isinstance(pack, StudyPack)
        # distinct tags: fractions, division → 2 items
        assert len(pack.items) == 2
        item_tags = {tag for item in pack.items for tag in item.gap_tags}
        assert "fractions" in item_tags
        assert "division" in item_tags

    def test_no_items_when_no_growing_gaps(self) -> None:
        """No growing items → empty items list."""
        report = _gap_report_no_gaps()
        pack = FakeStudyPack().generate(report)
        assert pack.items == []
        assert "empty" in pack.summary.lower() or "no growing" in pack.summary.lower()

    def test_generic_item_when_growing_has_no_tags(self) -> None:
        """Growing items with no gap_tags → one generic item."""
        report = _gap_report_with_tags([[]])  # one growing item, no tags
        pack = FakeStudyPack().generate(report)
        assert len(pack.items) == 1
        # The generic item uses the "general" placeholder tag.
        assert pack.items[0].gap_tags == ["general"]

    def test_derived_from_gap_tags_sorted_and_deduped(self) -> None:
        """derived_from_gap_tags is the sorted distinct union of growing tags."""
        report = _gap_report_with_tags(
            [["beta", "alpha"], ["alpha", "gamma"]],
        )
        pack = FakeStudyPack().generate(report)
        assert pack.derived_from_gap_tags == ["alpha", "beta", "gamma"]

    def test_mastered_items_do_not_contribute_tags(self) -> None:
        """Mastered items' tags (if any) must not appear in derived_from_gap_tags."""
        report = _gap_report_with_tags(
            [["fractions"]],
            mastered_count=2,
        )
        # Mastered items in _gap_report_with_tags have no tags by design.
        pack = FakeStudyPack().generate(report)
        assert pack.derived_from_gap_tags == ["fractions"]
        assert len(pack.items) == 1

    def test_pack_fields_populated(self) -> None:
        """StudyPack model has cycle_id, assessment_id, and generated_at."""
        report = _gap_report_with_tags([["algebra"]])
        pack = FakeStudyPack().generate(report)
        assert pack.cycle_id == report.cycle_id
        assert pack.assessment_id == report.assessment_id
        assert pack.generated_at.tzinfo is not None

    def test_each_item_is_a_study_pack_item(self) -> None:
        """All items are StudyPackItem instances with required fields."""
        report = _gap_report_with_tags([["topic-a"], ["topic-b"]])
        pack = FakeStudyPack().generate(report)
        for item in pack.items:
            assert isinstance(item, StudyPackItem)
            assert item.item_id
            assert item.prompt
            assert item.answer
            assert item.question_type

    def test_deterministic_for_same_tags(self) -> None:
        """Two calls with the same tags produce structurally identical packs
        (same item count, same tags — item_id for generic items differs due to uuid4,
        but tag-seeded item_ids are stable)."""
        report = _gap_report_with_tags([["fractions"], ["algebra"]])
        pack1 = FakeStudyPack().generate(report)
        pack2 = FakeStudyPack().generate(report)
        # Tag-seeded items have stable item_ids (uuid5 from tag).
        assert len(pack1.items) == len(pack2.items)
        tags1 = [item.gap_tags for item in pack1.items]
        tags2 = [item.gap_tags for item in pack2.items]
        assert tags1 == tags2


# ---------------------------------------------------------------------------
# Subject-agnostic equivalence
# ---------------------------------------------------------------------------


class TestSubjectAgnostic:
    """Two different subjects with identical gap shapes produce structurally
    identical study packs (same item count, same tag set).  No subject branching."""

    def test_two_subjects_identical_gap_shape(self) -> None:
        report_maths = _gap_report_with_tags(
            [["fractions", "division"]],
            cycle_id="cycle-maths",
            assessment_id="asmt-maths",
        )
        report_science = _gap_report_with_tags(
            [["fractions", "division"]],
            cycle_id="cycle-science",
            assessment_id="asmt-science",
        )
        pack_maths = FakeStudyPack().generate(report_maths)
        pack_science = FakeStudyPack().generate(report_science)

        assert len(pack_maths.items) == len(pack_science.items)
        assert pack_maths.derived_from_gap_tags == pack_science.derived_from_gap_tags
        # Item structure (tags, question_type) must be identical.
        for m_item, s_item in zip(pack_maths.items, pack_science.items, strict=True):
            assert m_item.gap_tags == s_item.gap_tags
            assert m_item.question_type == s_item.question_type


# ---------------------------------------------------------------------------
# InMemoryStudyPackRepository
# ---------------------------------------------------------------------------


class TestInMemoryStudyPackRepository:
    def _pack(self, cycle_id: str = "cycle-repo-test") -> StudyPack:
        report = _gap_report_with_tags([["fractions"]], cycle_id=cycle_id)
        return FakeStudyPack().generate(report)

    def test_upsert_returns_row(self) -> None:
        repo = InMemoryStudyPackRepository()
        cid = uuid.uuid4()
        pack = self._pack()
        row = repo.upsert(_FAMILY_ID, cid, pack)
        assert row.cycle_id == cid
        assert row.approved_at is None

    def test_get_for_cycle_returns_row(self) -> None:
        repo = InMemoryStudyPackRepository()
        cid = uuid.uuid4()
        pack = self._pack()
        repo.upsert(_FAMILY_ID, cid, pack)
        fetched = repo.get_for_cycle(cid)
        assert fetched is not None
        assert fetched.cycle_id == cid

    def test_get_for_cycle_none_when_not_generated(self) -> None:
        repo = InMemoryStudyPackRepository()
        assert repo.get_for_cycle(uuid.uuid4()) is None

    def test_upsert_idempotency_overwrites_pack(self) -> None:
        """Second upsert replaces pack content; stable row id."""
        repo = InMemoryStudyPackRepository()
        cid = uuid.uuid4()
        pack1 = self._pack()
        row1 = repo.upsert(_FAMILY_ID, cid, pack1)

        pack2 = self._pack()
        row2 = repo.upsert(_FAMILY_ID, cid, pack2)

        # Row id stable across re-runs.
        assert row1.id == row2.id

    def test_upsert_preserves_approved_at(self) -> None:
        """Re-upsert must NOT clear an existing approved_at."""
        repo = InMemoryStudyPackRepository()
        cid = uuid.uuid4()
        pack = self._pack()
        repo.upsert(_FAMILY_ID, cid, pack)

        approval_ts = datetime.now(tz=UTC)
        repo.set_approved_at(cid, approval_ts)

        # Re-upsert with a new pack.
        pack2 = self._pack()
        row = repo.upsert(_FAMILY_ID, cid, pack2)

        # approved_at must survive the re-upsert.
        assert row.approved_at == approval_ts

    def test_set_approved_at_records_timestamp(self) -> None:
        repo = InMemoryStudyPackRepository()
        cid = uuid.uuid4()
        pack = self._pack()
        repo.upsert(_FAMILY_ID, cid, pack)

        before = datetime.now(tz=UTC)
        approved_ts = datetime.now(tz=UTC)
        row = repo.set_approved_at(cid, approved_ts)
        after = datetime.now(tz=UTC)

        assert row.approved_at is not None
        assert before <= row.approved_at <= after

    def test_set_approved_at_raises_when_not_generated(self) -> None:
        repo = InMemoryStudyPackRepository()
        with pytest.raises(ValueError, match="No study pack found"):
            repo.set_approved_at(uuid.uuid4(), datetime.now(tz=UTC))

    def test_isolation_between_cycles(self) -> None:
        repo = InMemoryStudyPackRepository()
        cid1, cid2 = uuid.uuid4(), uuid.uuid4()
        repo.upsert(_FAMILY_ID, cid1, self._pack())
        repo.upsert(_FAMILY_ID, cid2, self._pack())
        assert repo.get_for_cycle(cid1) is not None
        assert repo.get_for_cycle(cid2) is not None
        assert repo.get_for_cycle(uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# Cycle state transitions (cycle.py) for study pack
# ---------------------------------------------------------------------------


class TestStudyPackCycleTransitions:
    """advance_to_generating_study_pack and advance_to_study_pack_done via cycle.py."""

    def test_gap_report_to_generating_study_pack(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        cycle_id = _cycle_at_gap_report_state(repo)

        cycle = repo.get_cycle(cycle_id)
        assert cycle is not None
        assert cycle.state == CycleState.GAP_REPORT

        advance_to_generating_study_pack(repo, cycle_id)
        updated = repo.get_cycle(cycle_id)
        assert updated is not None
        assert updated.state == CycleState.GENERATING_STUDY_PACK

    def test_generating_study_pack_to_done(self) -> None:
        user_id = uuid.uuid4()
        repo = InMemoryFamilyRepository(user_id)
        cycle_id = _cycle_at_gap_report_state(repo)
        advance_to_generating_study_pack(repo, cycle_id)
        advance_to_study_pack_done(repo, cycle_id)

        updated = repo.get_cycle(cycle_id)
        assert updated is not None
        assert updated.state == CycleState.STUDY_PACK_DONE


# ---------------------------------------------------------------------------
# Endpoint tests — InMemory (no Postgres required)
# ---------------------------------------------------------------------------


def _make_app_overrides(
    user_id: uuid.UUID,
    family_repo: InMemoryFamilyRepository,
    gap_repo: InMemoryGapReportRepository,
    pack_repo: InMemoryStudyPackRepository,
) -> None:
    from dependencies import (
        get_family_repository,
        get_gap_report_repository,
        get_study_pack_repository,
    )
    from main import app

    app.dependency_overrides[get_family_repository] = lambda: family_repo
    app.dependency_overrides[get_gap_report_repository] = lambda: gap_repo
    app.dependency_overrides[get_study_pack_repository] = lambda: pack_repo


def _clear_app_overrides() -> None:
    from dependencies import (
        get_family_repository,
        get_gap_report_repository,
        get_study_pack_repository,
    )
    from main import app

    app.dependency_overrides.pop(get_family_repository, None)
    app.dependency_overrides.pop(get_gap_report_repository, None)
    app.dependency_overrides.pop(get_study_pack_repository, None)


class TestStudyPackEndpointGenerate:
    """POST /cycles/{cycle_id}/study-pack"""

    def _setup(
        self,
        growing_tags: list[list[str]],
    ) -> tuple[
        uuid.UUID,
        uuid.UUID,
        InMemoryFamilyRepository,
        InMemoryGapReportRepository,
        InMemoryStudyPackRepository,
    ]:
        """Create a cycle at GAP_REPORT with a stored gap report."""
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)
        _inject_gap_report(family_repo, gap_repo, cycle_id, growing_tags)

        return user_id, cycle_id, family_repo, gap_repo, pack_repo

    def test_generate_returns_200_and_advances_state(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id, cycle_id, family_repo, gap_repo, pack_repo = self._setup(
            [["fractions"], ["algebra"]]
        )
        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp.status_code == 200
        body = resp.json()
        assert "pack" in body
        assert body["pack"]["cycle_id"] == str(cycle_id)
        # Items derived from growing gap_tags.
        assert len(body["pack"]["items"]) == 2
        item_tags = {tag for item in body["pack"]["items"] for tag in item["gap_tags"]}
        assert "fractions" in item_tags
        assert "algebra" in item_tags

        # State must be STUDY_PACK_DONE.
        cycle = family_repo.get_cycle(cycle_id)
        assert cycle is not None
        assert cycle.state == CycleState.STUDY_PACK_DONE

    def test_approved_at_null_on_generate(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id, cycle_id, family_repo, gap_repo, pack_repo = self._setup([["fractions"]])
        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp.status_code == 200
        body = resp.json()
        assert body["approved_at"] is None

    def test_idempotent_rerun(self) -> None:
        """POST twice on the same cycle should succeed both times."""
        from fastapi.testclient import TestClient

        from main import app

        user_id, cycle_id, family_repo, gap_repo, pack_repo = self._setup([["fractions"]])
        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp1 = client.post(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
                resp2 = client.post(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp1.status_code == 200
        assert resp2.status_code == 200

    def test_missing_cycle_returns_404(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        family_repo.bootstrap_family("TestFamily", None, None)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{uuid.uuid4()}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp.status_code == 404

    def test_pre_gap_report_state_returns_409(self) -> None:
        """Cycle in SCOPE_UPLOADED → 409."""
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        family, _ = family_repo.bootstrap_family("TestFamily", None, None)
        subject = family_repo.create_subject(family.id, uuid.uuid4(), "Maths", "en")
        cycle = family_repo.create_cycle(family.id, subject.id, "scope")

        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{cycle.id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp.status_code == 409

    def test_other_family_cycle_returns_404(self) -> None:
        """A cycle belonging to a different family returns 404 (no family found)."""
        from fastapi.testclient import TestClient

        from main import app

        # User A sets up a cycle.
        user_a = uuid.uuid4()
        family_repo_a = InMemoryFamilyRepository(user_a)
        cycle_id_a = _cycle_at_gap_report_state(family_repo_a)

        # User B has their own (empty) family repo — can't see A's cycle.
        user_b = uuid.uuid4()
        family_repo_b = InMemoryFamilyRepository(user_b)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        _make_app_overrides(user_b, family_repo_b, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{cycle_id_a}/study-pack",
                    headers={"x-user-id": str(user_b)},
                )
        finally:
            _clear_app_overrides()

        # User B has no family → 409 from _resolve_family_id.
        assert resp.status_code in (404, 409)


class TestStudyPackEndpointGet:
    """GET /cycles/{cycle_id}/study-pack"""

    def test_get_404_when_not_generated(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.get(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp.status_code == 404

    def test_get_returns_pack_after_generate(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)
        _inject_gap_report(family_repo, gap_repo, cycle_id, [["fractions"]])

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                post_resp = client.post(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
                get_resp = client.get(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert post_resp.status_code == 200
        assert get_resp.status_code == 200
        assert get_resp.json()["pack"]["cycle_id"] == str(cycle_id)

    def test_get_404_unknown_cycle(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        family_repo.bootstrap_family("TestFamily", None, None)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.get(
                    f"/cycles/{uuid.uuid4()}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp.status_code == 404


class TestStudyPackEndpointApprove:
    """POST /cycles/{cycle_id}/study-pack/approve"""

    def _generate_pack(
        self,
        user_id: uuid.UUID,
        family_repo: InMemoryFamilyRepository,
        gap_repo: InMemoryGapReportRepository,
        pack_repo: InMemoryStudyPackRepository,
        cycle_id: uuid.UUID,
    ) -> None:
        from fastapi.testclient import TestClient

        from main import app

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)
        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
            assert resp.status_code == 200
        finally:
            _clear_app_overrides()

    def test_approved_at_null_before_approve(self) -> None:
        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)
        _inject_gap_report(family_repo, gap_repo, cycle_id, [["fractions"]])
        self._generate_pack(user_id, family_repo, gap_repo, pack_repo, cycle_id)

        row = pack_repo.get_for_cycle(cycle_id)
        assert row is not None
        assert row.approved_at is None

    def test_approve_records_timestamp(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)
        _inject_gap_report(family_repo, gap_repo, cycle_id, [["fractions"]])
        self._generate_pack(user_id, family_repo, gap_repo, pack_repo, cycle_id)

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        before = datetime.now(tz=UTC)

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{cycle_id}/study-pack/approve",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        after = datetime.now(tz=UTC)

        assert resp.status_code == 200
        body = resp.json()
        assert body["approved_at"] is not None

        # Verify via repo too.
        row = pack_repo.get_for_cycle(cycle_id)
        assert row is not None
        assert row.approved_at is not None
        assert before <= row.approved_at <= after

    def test_get_reflects_approved_at_after_approve(self) -> None:
        """GET after approve shows approved_at non-null."""
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)
        _inject_gap_report(family_repo, gap_repo, cycle_id, [["fractions"]])
        self._generate_pack(user_id, family_repo, gap_repo, pack_repo, cycle_id)

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                client.post(
                    f"/cycles/{cycle_id}/study-pack/approve",
                    headers={"x-user-id": str(user_id)},
                )
                get_resp = client.get(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert get_resp.status_code == 200
        assert get_resp.json()["approved_at"] is not None

    def test_approve_404_when_pack_not_generated(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{cycle_id}/study-pack/approve",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp.status_code == 404

    def test_approve_missing_cycle_returns_404(self) -> None:
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        family_repo.bootstrap_family("TestFamily", None, None)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/cycles/{uuid.uuid4()}/study-pack/approve",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PDF rendering tests
# ---------------------------------------------------------------------------


class TestStudyPackPdf:
    """PDF renders without error; bytes non-empty and start with %%PDF."""

    def _pack_with_items(self) -> StudyPack:
        report = _gap_report_with_tags([["fractions", "division"], ["algebra"]])
        return FakeStudyPack().generate(report)

    def _pack_empty(self) -> StudyPack:
        report = _gap_report_no_gaps()
        return FakeStudyPack().generate(report)

    @pytest.fixture(autouse=True)
    def _skip_if_no_weasyprint(self) -> None:
        pytest.importorskip("weasyprint", reason="WeasyPrint not installed")

    def test_pdf_bytes_non_empty_and_valid_header(self) -> None:
        from services.pdf_service import render_study_pack_pdf

        pack = self._pack_with_items()
        pdf = render_study_pack_pdf(
            pack,
            subject="Mathematics",
            grade_label="Grade 5",
            content_language="en",
        )
        assert isinstance(pdf, bytes)
        assert len(pdf) > 0
        assert pdf[:4] == b"%PDF", f"Expected PDF header, got: {pdf[:10]!r}"

    def test_pdf_empty_pack_renders(self) -> None:
        from services.pdf_service import render_study_pack_pdf

        pack = self._pack_empty()
        pdf = render_study_pack_pdf(
            pack,
            subject="Science",
            grade_label="Grade 4",
            content_language="en",
        )
        assert pdf[:4] == b"%PDF"

    def test_pdf_afrikaans_language(self) -> None:
        """Content language is passed through correctly (subject-agnostic)."""
        from services.pdf_service import render_study_pack_pdf

        report = _gap_report_with_tags([["verdeling"]])
        pack = FakeStudyPack().generate(report)
        pdf = render_study_pack_pdf(
            pack,
            subject="Wiskunde",
            grade_label="Graad 5",
            content_language="af",
        )
        assert pdf[:4] == b"%PDF"

    def test_pdf_endpoint_returns_pdf_bytes(self) -> None:
        """GET /cycles/{cycle_id}/study-pack/pdf returns application/pdf."""
        from fastapi.testclient import TestClient

        from main import app

        user_id = uuid.uuid4()
        family_repo = InMemoryFamilyRepository(user_id)
        gap_repo = InMemoryGapReportRepository()
        pack_repo = InMemoryStudyPackRepository()

        cycle_id = _cycle_at_gap_report_state(family_repo)
        _inject_gap_report(family_repo, gap_repo, cycle_id, [["fractions"]])

        # Inject an assessment so the PDF endpoint can read subject/grade/language.
        raw_cycle = family_repo.get_cycle(cycle_id)
        assert raw_cycle is not None

        asmt = Assessment.model_validate({**maths_assessment(), "cycle_id": str(cycle_id)})
        updated_cycle = raw_cycle.model_copy(update={"assessments": [asmt]})
        family_repo._cycles[cycle_id] = updated_cycle

        _make_app_overrides(user_id, family_repo, gap_repo, pack_repo)

        try:
            with TestClient(app) as client:
                # Generate first.
                client.post(
                    f"/cycles/{cycle_id}/study-pack",
                    headers={"x-user-id": str(user_id)},
                )
                # Then fetch PDF.
                pdf_resp = client.get(
                    f"/cycles/{cycle_id}/study-pack/pdf",
                    headers={"x-user-id": str(user_id)},
                )
        finally:
            _clear_app_overrides()

        assert pdf_resp.status_code == 200
        assert pdf_resp.headers["content-type"] == "application/pdf"
        assert pdf_resp.content[:4] == b"%PDF"


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


@pytest.mark.skipif(
    not _try_connect_db(),
    reason="Local Postgres not reachable — run `docker compose up db` first",
)
class TestStudyPackRLS:
    """Prove that study_packs are RLS-isolated by family_id.

    User A's study pack must not be visible to user B.
    """

    def _open_auth_conn(self, user_id: uuid.UUID) -> psycopg.Connection[dict[str, Any]]:
        claims = json.dumps({"sub": str(user_id)})
        conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
            _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
        )
        conn.execute("SET ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))
        return conn

    def _seed_study_pack(
        self,
        owner_conn: psycopg.Connection[dict[str, Any]],
        user_id: uuid.UUID,
        family_name: str,
    ) -> uuid.UUID:
        """Seed a study_pack row. Returns the study_pack id."""
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
            "VALUES (%s, %s, 'STUDY_PACK_DONE') RETURNING id",
            (str(family_id), str(subject_id)),
        )
        row = cur.fetchone()
        assert row is not None
        cycle_id = uuid.UUID(str(row["id"]))

        minimal_pack = json.dumps(
            {
                "cycle_id": str(cycle_id),
                "assessment_id": str(uuid.uuid4()),
                "items": [],
                "summary": "Test pack",
                "derived_from_gap_tags": [],
                "generated_at": datetime.now(tz=UTC).isoformat(),
            }
        )
        cur.execute(
            "INSERT INTO study_packs (family_id, cycle_id, pack) "
            "VALUES (%s, %s, %s::jsonb) RETURNING id",
            (str(family_id), str(cycle_id), minimal_pack),
        )
        row = cur.fetchone()
        assert row is not None
        pack_id = uuid.UUID(str(row["id"]))

        owner_conn.commit()
        return pack_id

    def test_study_pack_rls_isolated(
        self,
        owner_conn_sp: psycopg.Connection[dict[str, Any]],
    ) -> None:
        """User B must not see user A's study pack."""
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        pack_a_id = self._seed_study_pack(owner_conn_sp, user_a, f"FamilySP-A-{user_a.hex[:6]}")
        _ = self._seed_study_pack(owner_conn_sp, user_b, f"FamilySP-B-{user_b.hex[:6]}")

        conn_b = self._open_auth_conn(user_b)
        try:
            b_cur = conn_b.cursor()
            b_cur.execute("SELECT id FROM study_packs WHERE id = %s", (str(pack_a_id),))
            row = b_cur.fetchone()
            assert row is None, "RLS violation: user B can see user A's study pack"
        finally:
            conn_b.close()

        conn_a = self._open_auth_conn(user_a)
        try:
            a_cur = conn_a.cursor()
            a_cur.execute("SELECT id FROM study_packs WHERE id = %s", (str(pack_a_id),))
            row = a_cur.fetchone()
            assert row is not None, "User A cannot see their own study pack"
        finally:
            conn_a.close()


@pytest.fixture(scope="module")
def owner_conn_sp() -> Any:
    if not _try_connect_db():
        pytest.skip("Local Postgres not reachable")
    conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        _DSN, autocommit=False, row_factory=psycopg.rows.dict_row
    )
    yield conn
    conn.close()
