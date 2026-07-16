"""Repository protocols for all persistence boundaries.

Typed purely in terms of Pydantic models — no bare dicts crossing these
boundaries (ARCHITECTURE.md §8). Postgres-backed implementations can drop in
without changing callers, as long as they satisfy their Protocol.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class AssessmentRepository(Protocol):
    def save(self, assessment: Assessment) -> Assessment: ...

    def get(self, assessment_id: str) -> Assessment | None: ...

    def list(self) -> list[Assessment]: ...


@runtime_checkable
class FamilyRepository(Protocol):
    """CRUD for families, children, subjects, and cycles.

    family_id is NEVER accepted from the client.  It is always derived
    server-side from the authenticated user's membership row (invariant 3).
    """

    # -- Family --

    def bootstrap_family(
        self,
        family_name: str,
        child_name: str | None,
        grade_label: str | None,
    ) -> tuple[FamilyResponse, uuid.UUID | None]:
        """Atomically create family + membership (+ optional child).

        Returns (family, child_id | None).
        """
        ...

    def list_families(self) -> list[FamilyResponse]: ...

    # -- Child --

    def create_child(
        self,
        family_id: uuid.UUID,
        display_name: str,
        grade_label: str,
        visibility_defaults: VisibilityDefaults | None = None,
    ) -> ChildResponse: ...

    def list_children(self, family_id: uuid.UUID) -> list[ChildResponse]: ...

    def update_child(self, child_id: uuid.UUID, payload: ChildUpdate) -> ChildResponse:
        """Partial update of a child's profile fields.

        Raises ValueError if the child is not found or not accessible.
        """
        ...

    def archive_child(self, child_id: uuid.UUID) -> ChildResponse:
        """Set archived_at = now() on the child.

        Raises ValueError if the child is not found or not accessible.
        The child drops out of list_children() (active-only) after this call.
        """
        ...

    # -- Subject --

    def create_subject(
        self,
        family_id: uuid.UUID,
        child_id: uuid.UUID,
        name: str,
        content_language: str,
    ) -> SubjectResponse: ...

    def list_subjects(self, family_id: uuid.UUID) -> list[SubjectResponse]: ...

    # -- Cycle --

    def create_cycle(
        self,
        family_id: uuid.UUID,
        subject_id: uuid.UUID,
        scope_text: str,
    ) -> CycleResponse: ...

    def get_cycle(self, cycle_id: uuid.UUID) -> CycleResponse | None: ...

    def list_cycles(self, family_id: uuid.UUID) -> list[CycleResponse]: ...

    def update_cycle_state(
        self,
        cycle_id: uuid.UUID,
        new_state: CycleState,
        parent_approval_at: datetime | None = None,
        parent_approval_note: str | None = None,
    ) -> CycleResponse: ...

    def publish_marks(
        self,
        cycle_id: uuid.UUID,
        new_state: CycleState,
        marks_published_at: datetime,
        published_visibility: VisibilityDefaults,
    ) -> CycleResponse:
        """Record marks publish: set state, marks_published_at, published_visibility."""
        ...


@runtime_checkable
class SubmissionRepository(Protocol):
    """Persistence for child answer submissions.

    family_id is NEVER accepted from the client — resolved server-side via RLS.
    """

    def create_submission(
        self,
        family_id: uuid.UUID,
        assessment_id: str,
        payload: SubmissionCreate,
        cycle_id: uuid.UUID,
    ) -> SubmissionResponse:
        """Persist a new submission and return the response model.

        ``proof_photo_paths`` are stored as-is; they are NEVER fed to grading.
        """
        ...

    def get_submission(self, submission_id: uuid.UUID) -> SubmissionResponse | None: ...


@runtime_checkable
class QuestionMarkRepository(Protocol):
    """Persistence for graded question marks.

    family_id is NEVER accepted from the client — resolved server-side.
    """

    def bulk_upsert(
        self,
        family_id: uuid.UUID,
        submission_id: uuid.UUID,
        marks: list[QuestionMark],
    ) -> list[QuestionMark]:
        """Upsert all marks for a submission.

        Re-grading replaces stale marks via the (submission_id, question_id) unique key.
        """
        ...

    def list_for_submission(self, submission_id: uuid.UUID) -> list[QuestionMark]: ...

    def list_for_cycle(self, cycle_id: uuid.UUID, variant: str) -> list[QuestionMark]:
        """Return all marks for the cycle's submission of the given variant ("A" or "B").

        ``variant`` is always explicit — never inferred by recency — so Variant A
        and Variant B marks can never bleed into each other (Week 6 guardrail).
        """
        ...

    def get_submission_id_for_cycle(self, cycle_id: uuid.UUID, variant: str) -> uuid.UUID | None:
        """Find the submission_id for a cycle's given variant ("A" or "B").

        ``variant`` is always explicit — never inferred by recency.
        """
        ...

    def get_mark(
        self,
        submission_id: uuid.UUID,
        question_id: str,
    ) -> QuestionMark | None:
        """Fetch a single mark by (submission_id, question_id)."""
        ...

    def update_mark(
        self,
        submission_id: uuid.UUID,
        question_id: str,
        patch: MarkPatchRequest,
        now: datetime,
    ) -> QuestionMark:
        """Apply a parent review patch to a single mark.

        Sets final_marks and reviewed_at=now on every call.
        Sets overridden_at=now when final_marks != suggested_marks.
        Sets error_category when provided in the patch.

        Raises ValueError if the mark is not found or not accessible.
        """
        ...


@runtime_checkable
class GapReportRepository(Protocol):
    """Persistence for derived gap reports.

    family_id is NEVER accepted from the client — resolved server-side via RLS.
    One gap report per cycle (UNIQUE(cycle_id)); upsert on regenerate.
    """

    def upsert(
        self,
        family_id: uuid.UUID,
        cycle_id: uuid.UUID,
        submission_id: uuid.UUID,
        report: GapReport,
    ) -> GapReportRow:
        """Upsert the gap report for a cycle.

        Idempotent: re-running derive + upsert overwrites the previous row.
        Returns the persisted GapReportRow.
        """
        ...

    def get_for_cycle(self, cycle_id: uuid.UUID) -> GapReportRow | None:
        """Return the gap report row for a cycle, or None if not yet generated."""
        ...


@runtime_checkable
class StudyPackRepository(Protocol):
    """Persistence for generated study packs.

    family_id is NEVER accepted from the client — resolved server-side via RLS.
    One study pack per cycle (UNIQUE(cycle_id)); upsert on regenerate.
    """

    def upsert(
        self,
        family_id: uuid.UUID,
        cycle_id: uuid.UUID,
        pack: StudyPack,
    ) -> StudyPackRow:
        """Upsert the study pack for a cycle.

        Idempotent: re-running generate + upsert overwrites the previous row.
        Returns the persisted StudyPackRow.
        """
        ...

    def get_for_cycle(self, cycle_id: uuid.UUID) -> StudyPackRow | None:
        """Return the study pack row for a cycle, or None if not yet generated."""
        ...

    def set_approved_at(
        self,
        cycle_id: uuid.UUID,
        approved_at: datetime,
    ) -> StudyPackRow:
        """Record parent approval: set approved_at on the study pack row.

        Raises ValueError if no study pack row exists for this cycle.
        """
        ...
