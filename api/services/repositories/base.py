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
    CyclePhase,
    CycleResponse,
    CycleRoundApproval,
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
        round: int,  # noqa: A002
        phase: CyclePhase,
        parent_approval_at: datetime | None = None,
        parent_approval_note: str | None = None,
    ) -> CycleResponse:
        """Advance the cycle's ``(round, phase)`` and (deprecated, compat) ``state``.

        ``round``/``phase`` are the authoritative axis (P4, design §5) and are
        always explicit — they can no longer be reliably re-derived from
        ``new_state`` alone, since ``round_phase_to_state`` is round-agnostic
        (round 2's real intermediate phases share round 1's legacy state
        values).  Repos persist ``round``/``phase`` verbatim.
        """
        ...

    def publish_marks(
        self,
        cycle_id: uuid.UUID,
        new_state: CycleState,
        round: int,  # noqa: A002
        phase: CyclePhase,
        marks_published_at: datetime,
        published_visibility: VisibilityDefaults,
    ) -> CycleResponse:
        """Record marks publish: set state/round/phase, marks_published_at, published_visibility."""
        ...

    # -- Per-round approvals (design §4.6 `cycle_round_approvals`) --
    #
    # Dual-written alongside the single-valued `cycles` approval columns
    # (parent_approval_at/note, marks_published_at/published_visibility)
    # through P2-P3; these are additive, not yet a read path (P4).

    def record_round_draft_approval(
        self,
        cycle_id: uuid.UUID,
        round: int,  # noqa: A002
        approved_at: datetime,
        note: str | None,
    ) -> CycleRoundApproval:
        """Upsert the draft-approval half of a round's approval row.

        Keyed on (cycle_id, round); does not disturb an existing publish
        half of the same row.
        """
        ...

    def record_round_publish(
        self,
        cycle_id: uuid.UUID,
        round: int,  # noqa: A002
        published_at: datetime,
        visibility: VisibilityDefaults,
    ) -> CycleRoundApproval:
        """Upsert the publish half of a round's approval row.

        Keyed on (cycle_id, round); does not disturb an existing draft
        half of the same row.
        """
        ...

    def get_round_approval(
        self,
        cycle_id: uuid.UUID,
        round: int,  # noqa: A002
    ) -> CycleRoundApproval | None:
        """Return the approval row for (cycle_id, round), or None if absent."""
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
