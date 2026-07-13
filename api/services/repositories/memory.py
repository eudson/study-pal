"""In-memory repository implementations.

Used for unit tests that do not spin up Postgres (no DB needed).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from schemas.assessment_schema import Assessment
from schemas.capture import SubmissionCreate, SubmissionResponse
from schemas.family import (
    ChildResponse,
    ChildUpdate,
    CycleResponse,
    CycleState,
    FamilyResponse,
    SubjectResponse,
    VisibilityDefaults,
)
from schemas.gap_report import GapReport, GapReportRow
from schemas.grading import QuestionMark
from schemas.review import MarkPatchRequest
from schemas.study_pack import StudyPack, StudyPackRow


def _now() -> datetime:
    return datetime.now(tz=UTC)


class InMemoryAssessmentRepository:
    def __init__(self) -> None:
        self._store: dict[str, Assessment] = {}

    def save(self, assessment: Assessment) -> Assessment:
        self._store[assessment.assessment_id] = assessment
        return assessment

    def get(self, assessment_id: str) -> Assessment | None:
        return self._store.get(assessment_id)

    def list(self) -> list[Assessment]:
        return list(self._store.values())


class InMemoryFamilyRepository:
    """Process-local family/child/subject/cycle store for unit tests.

    All writes are isolated to the instance; thread safety is not guaranteed
    (single-threaded test use only).
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        # Simulate the RLS claim: this user owns the families they create.
        self._user_id = user_id
        self._families: dict[uuid.UUID, FamilyResponse] = {}
        # user_id -> set of family_ids (membership)
        self._memberships: dict[uuid.UUID, set[uuid.UUID]] = {}
        self._children: dict[uuid.UUID, ChildResponse] = {}
        self._subjects: dict[uuid.UUID, SubjectResponse] = {}
        self._cycles: dict[uuid.UUID, CycleResponse] = {}
        self._cycle_scope: dict[uuid.UUID, str] = {}

    # -- Family --

    def bootstrap_family(
        self,
        family_name: str,
        child_name: str | None,
        grade_label: str | None,
    ) -> tuple[FamilyResponse, uuid.UUID | None]:
        # Idempotent: return existing family if user already has one.
        existing = self._memberships.get(self._user_id)
        if existing:
            fid = next(iter(existing))
            return self._families[fid], None

        fid = uuid.uuid4()
        family = FamilyResponse(id=fid, name=family_name, created_at=_now())
        self._families[fid] = family
        self._memberships.setdefault(self._user_id, set()).add(fid)

        child_id: uuid.UUID | None = None
        if child_name is not None and grade_label is not None:
            child_id = uuid.uuid4()
            self._children[child_id] = ChildResponse(
                id=child_id,
                family_id=fid,
                display_name=child_name,
                grade_label=grade_label,
                created_at=_now(),
                visibility_defaults=VisibilityDefaults(),
            )
        return family, child_id

    def list_families(self) -> list[FamilyResponse]:
        my_families = self._memberships.get(self._user_id, set())
        return [self._families[fid] for fid in my_families if fid in self._families]

    # -- Child --

    def create_child(
        self,
        family_id: uuid.UUID,
        display_name: str,
        grade_label: str,
        visibility_defaults: VisibilityDefaults | None = None,
    ) -> ChildResponse:
        cid = uuid.uuid4()
        child = ChildResponse(
            id=cid,
            family_id=family_id,
            display_name=display_name,
            grade_label=grade_label,
            created_at=_now(),
            visibility_defaults=visibility_defaults or VisibilityDefaults(),
        )
        self._children[cid] = child
        return child

    def list_children(self, family_id: uuid.UUID) -> list[ChildResponse]:
        """Return active children only (archived_at is None)."""
        return [
            c for c in self._children.values() if c.family_id == family_id and c.archived_at is None
        ]

    def update_child(self, child_id: uuid.UUID, payload: ChildUpdate) -> ChildResponse:
        child = self._children.get(child_id)
        if child is None:
            raise ValueError(f"Child {child_id} not found or not accessible")
        update: dict[str, object] = {}
        if payload.display_name is not None:
            update["display_name"] = payload.display_name
        if payload.grade_label is not None:
            update["grade_label"] = payload.grade_label
        if payload.visibility_defaults is not None:
            update["visibility_defaults"] = payload.visibility_defaults
        updated = child.model_copy(update=update)
        self._children[child_id] = updated
        return updated

    def archive_child(self, child_id: uuid.UUID) -> ChildResponse:
        child = self._children.get(child_id)
        if child is None:
            raise ValueError(f"Child {child_id} not found or not accessible")
        if child.archived_at is not None:
            raise ValueError(f"Child {child_id} is already archived")
        archived = child.model_copy(update={"archived_at": _now()})
        self._children[child_id] = archived
        return archived

    # -- Subject --

    def create_subject(
        self,
        family_id: uuid.UUID,
        child_id: uuid.UUID,
        name: str,
        content_language: str,
    ) -> SubjectResponse:
        sid = uuid.uuid4()
        subject = SubjectResponse(
            id=sid,
            family_id=family_id,
            child_id=child_id,
            name=name,
            content_language=content_language,
            created_at=_now(),
        )
        self._subjects[sid] = subject
        return subject

    def list_subjects(self, family_id: uuid.UUID) -> list[SubjectResponse]:
        return [s for s in self._subjects.values() if s.family_id == family_id]

    # -- Cycle --

    def create_cycle(
        self,
        family_id: uuid.UUID,
        subject_id: uuid.UUID,
        scope_text: str,
    ) -> CycleResponse:
        cid = uuid.uuid4()
        now = _now()
        cycle = CycleResponse(
            id=cid,
            family_id=family_id,
            subject_id=subject_id,
            state=CycleState.SCOPE_UPLOADED,
            scope_text=scope_text,
            created_at=now,
            updated_at=now,
        )
        self._cycles[cid] = cycle
        self._cycle_scope[cid] = scope_text
        return cycle

    def get_cycle(self, cycle_id: uuid.UUID) -> CycleResponse | None:
        return self._cycles.get(cycle_id)

    def list_cycles(self, family_id: uuid.UUID) -> list[CycleResponse]:
        return [c for c in self._cycles.values() if c.family_id == family_id]

    def update_cycle_state(
        self,
        cycle_id: uuid.UUID,
        new_state: CycleState,
        parent_approval_at: datetime | None = None,
        parent_approval_note: str | None = None,
    ) -> CycleResponse:
        cycle = self._cycles.get(cycle_id)
        if cycle is None:
            raise ValueError(f"Cycle {cycle_id} not found")
        updated = cycle.model_copy(
            update={
                "state": new_state,
                "updated_at": _now(),
                "parent_approval_at": parent_approval_at or cycle.parent_approval_at,
                "parent_approval_note": parent_approval_note or cycle.parent_approval_note,
            }
        )
        self._cycles[cycle_id] = updated
        return updated

    def publish_marks(
        self,
        cycle_id: uuid.UUID,
        new_state: CycleState,
        marks_published_at: datetime,
        published_visibility: VisibilityDefaults,
    ) -> CycleResponse:
        """Record marks publish: freeze visibility snapshot + advance state."""
        cycle = self._cycles.get(cycle_id)
        if cycle is None:
            raise ValueError(f"Cycle {cycle_id} not found")
        updated = cycle.model_copy(
            update={
                "state": new_state,
                "updated_at": _now(),
                "marks_published_at": marks_published_at,
                "published_visibility": published_visibility,
            }
        )
        self._cycles[cycle_id] = updated
        return updated


class InMemorySubmissionRepository:
    """Process-local submission store for unit tests (no Postgres required)."""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, SubmissionResponse] = {}

    def create_submission(
        self,
        family_id: uuid.UUID,
        assessment_id: str,
        payload: SubmissionCreate,
        cycle_id: uuid.UUID,
    ) -> SubmissionResponse:
        submission_id = uuid.uuid4()
        response = SubmissionResponse(
            submission_id=submission_id,
            assessment_id=assessment_id,
            child_id=payload.child_id,
            cycle_id=cycle_id,
            responses_count=len(payload.responses),
            proof_photo_paths=list(payload.proof_photo_paths),
            created_at=_now().isoformat(),
        )
        self._store[submission_id] = response
        return response

    def get_submission(self, submission_id: uuid.UUID) -> SubmissionResponse | None:
        return self._store.get(submission_id)


class InMemoryQuestionMarkRepository:
    """Process-local question mark store for unit tests (no Postgres required)."""

    def __init__(self) -> None:
        # Keyed by (submission_id, question_id) for upsert semantics.
        self._store: dict[tuple[uuid.UUID, str], QuestionMark] = {}

    def bulk_upsert(
        self,
        family_id: uuid.UUID,
        submission_id: uuid.UUID,
        marks: list[QuestionMark],
    ) -> list[QuestionMark]:
        now = _now()
        persisted: list[QuestionMark] = []
        for mark in marks:
            updated = mark.model_copy(update={"created_at": mark.created_at or now})
            self._store[(submission_id, mark.question_id)] = updated
            persisted.append(updated)
        return persisted

    def list_for_submission(self, submission_id: uuid.UUID) -> list[QuestionMark]:
        return [m for (sid, _qid), m in self._store.items() if sid == submission_id]

    def list_for_cycle(self, cycle_id: uuid.UUID) -> list[QuestionMark]:
        # In-memory: we can't join through assessments, so return all marks.
        # Tests that need cycle-level isolation should use Postgres.
        return list(self._store.values())

    def get_submission_id_for_cycle(self, cycle_id: uuid.UUID) -> uuid.UUID | None:
        # Not meaningfully implementable without the relational join.
        # Tests that need this should use Postgres or pass submission_id directly.
        return None

    def get_mark(
        self,
        submission_id: uuid.UUID,
        question_id: str,
    ) -> QuestionMark | None:
        return self._store.get((submission_id, question_id))

    def update_mark(
        self,
        submission_id: uuid.UUID,
        question_id: str,
        patch: MarkPatchRequest,
        now: datetime,
    ) -> QuestionMark:
        """Apply parent review patch: final_marks, reviewed_at, overridden_at, error_category."""
        existing = self._store.get((submission_id, question_id))
        if existing is None:
            raise ValueError(
                f"Mark not found: submission_id={submission_id} question_id={question_id}"
            )
        update: dict[str, object] = {"reviewed_at": now}
        if patch.final_marks is not None:
            update["final_marks"] = patch.final_marks
            if patch.final_marks != existing.suggested_marks:
                update["overridden_at"] = now
        if patch.error_category is not None:
            update["error_category"] = patch.error_category
        updated = existing.model_copy(update=update)
        self._store[(submission_id, question_id)] = updated
        return updated


class InMemoryGapReportRepository:
    """Process-local gap report store for unit tests (no Postgres required).

    Satisfies the GapReportRepository Protocol.
    Keyed by cycle_id (UNIQUE constraint mirrors the DB table).
    """

    def __init__(self) -> None:
        # cycle_id → GapReportRow
        self._store: dict[uuid.UUID, GapReportRow] = {}

    def upsert(
        self,
        family_id: uuid.UUID,
        cycle_id: uuid.UUID,
        submission_id: uuid.UUID,
        report: GapReport,
    ) -> GapReportRow:
        """Upsert: insert or overwrite the gap report for this cycle."""
        # Preserve the existing row id on re-runs for stable audit trails.
        existing = self._store.get(cycle_id)
        row_id = existing.id if existing is not None else uuid.uuid4()
        now = _now()
        row = GapReportRow(
            id=row_id,
            family_id=family_id,
            cycle_id=cycle_id,
            submission_id=submission_id,
            report=report,
            created_at=now,
        )
        self._store[cycle_id] = row
        return row

    def get_for_cycle(self, cycle_id: uuid.UUID) -> GapReportRow | None:
        return self._store.get(cycle_id)


class InMemoryStudyPackRepository:
    """Process-local study pack store for unit tests (no Postgres required).

    Satisfies the StudyPackRepository Protocol.
    Keyed by cycle_id (UNIQUE constraint mirrors the DB table).
    """

    def __init__(self) -> None:
        # cycle_id → StudyPackRow
        self._store: dict[uuid.UUID, StudyPackRow] = {}

    def upsert(
        self,
        family_id: uuid.UUID,
        cycle_id: uuid.UUID,
        pack: StudyPack,
    ) -> StudyPackRow:
        """Upsert: insert or overwrite the study pack for this cycle."""
        existing = self._store.get(cycle_id)
        row_id = existing.id if existing is not None else uuid.uuid4()
        # Preserve approved_at on re-run so approval is not silently cleared.
        approved_at = existing.approved_at if existing is not None else None
        now = _now()
        row = StudyPackRow(
            id=row_id,
            family_id=family_id,
            cycle_id=cycle_id,
            pack=pack,
            approved_at=approved_at,
            created_at=now,
        )
        self._store[cycle_id] = row
        return row

    def get_for_cycle(self, cycle_id: uuid.UUID) -> StudyPackRow | None:
        return self._store.get(cycle_id)

    def set_approved_at(
        self,
        cycle_id: uuid.UUID,
        approved_at: datetime,
    ) -> StudyPackRow:
        """Record parent approval timestamp on the study pack row."""
        existing = self._store.get(cycle_id)
        if existing is None:
            raise ValueError(f"No study pack found for cycle {cycle_id} — generate one first.")
        updated = existing.model_copy(update={"approved_at": approved_at})
        self._store[cycle_id] = updated
        return updated
