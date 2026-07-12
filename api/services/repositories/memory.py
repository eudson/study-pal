"""In-memory repository implementations.

Used for unit tests that do not spin up Postgres (no DB needed).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from schemas.assessment_schema import Assessment
from schemas.family import (
    ChildResponse,
    ChildUpdate,
    CycleResponse,
    CycleState,
    FamilyResponse,
    SubjectResponse,
    VisibilityDefaults,
)


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
