"""Pydantic v2 request/response models for family, child, subject, and cycle.

No bare dict crosses any service boundary (ARCHITECTURE.md §8).
family_id is NEVER accepted from the client — it is always derived
server-side from the authenticated Identity (invariant 3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from schemas.assessment_schema import Assessment

# ---------------------------------------------------------------------------
# Child visibility defaults
# ---------------------------------------------------------------------------


class VisibilityDefaults(BaseModel):
    """Per-child toggle defaults for the publish gate (Settings p13).

    Stored as JSONB in children.visibility_defaults.
    Not yet consumed by any downstream gate — modelled plainly, no coupling.

    Design defaults (p13): accuracy/effort/growing ON, ai_rationale OFF.
    """

    accuracy: bool = True
    effort: bool = True
    growing: bool = True
    ai_rationale: bool = False


# ---------------------------------------------------------------------------
# Cycle state enum (ARCHITECTURE.md §5 — exact match required)
# ---------------------------------------------------------------------------


class CycleState(StrEnum):
    SCOPE_UPLOADED = "SCOPE_UPLOADED"
    GENERATING_A = "GENERATING_A"
    PARENT_REVIEWS_DRAFT = "PARENT_REVIEWS_DRAFT"
    APPROVED_PRINTED = "APPROVED_PRINTED"
    ANSWERS_ENTERED = "ANSWERS_ENTERED"
    AUTO_MARKED = "AUTO_MARKED"
    PARENT_REVIEW_MARKS = "PARENT_REVIEW_MARKS"
    GAP_REPORT = "GAP_REPORT"
    GENERATING_STUDY_PACK = "GENERATING_STUDY_PACK"
    STUDY_PACK_DONE = "STUDY_PACK_DONE"
    GENERATING_B = "GENERATING_B"
    CYCLE_COMPLETE = "CYCLE_COMPLETE"


# ---------------------------------------------------------------------------
# Family
# ---------------------------------------------------------------------------


class FamilyCreate(BaseModel):
    """Bootstrap request — creates a family + the caller's membership row.

    Optionally creates the first child in the same transaction.
    child_name and grade_label must both be provided or both omitted.
    """

    family_name: str = Field(
        min_length=1, max_length=200, description="Display name for the family."
    )
    child_name: str | None = Field(default=None, min_length=1, max_length=200)
    grade_label: str | None = Field(default=None, min_length=1, max_length=50)


class FamilyResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class BootstrapResponse(BaseModel):
    """Response from POST /families (bootstrap)."""

    family: FamilyResponse
    child_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Child
# ---------------------------------------------------------------------------


class ChildCreate(BaseModel):
    """Create a child.  family_id is derived server-side; never from the client."""

    display_name: str = Field(min_length=1, max_length=200)
    grade_label: str = Field(min_length=1, max_length=50)
    visibility_defaults: VisibilityDefaults = Field(
        default_factory=VisibilityDefaults,
        description="Initial publish-gate toggle defaults; uses standard defaults when omitted.",
    )


class ChildUpdate(BaseModel):
    """PATCH payload for child profile (all fields optional — partial update semantics)."""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    grade_label: str | None = Field(default=None, min_length=1, max_length=50)
    visibility_defaults: VisibilityDefaults | None = Field(default=None)


class ChildResponse(BaseModel):
    id: uuid.UUID
    family_id: uuid.UUID
    display_name: str
    grade_label: str
    created_at: datetime
    archived_at: datetime | None = None
    visibility_defaults: VisibilityDefaults = Field(default_factory=VisibilityDefaults)


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------


class SubjectCreate(BaseModel):
    """Create a subject.  family_id is derived server-side.

    ``name`` is freeform (ARCHITECTURE golden rule 4: no subject == branches).
    ``content_language`` drives generation/grading language (ISO 639-1/2 lowercase).
    """

    child_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    content_language: str = Field(
        min_length=2,
        max_length=3,
        pattern=r"^[a-z]{2,3}$",
        description="ISO 639-1/2 lowercase, e.g. 'en', 'af'.",
    )


class SubjectResponse(BaseModel):
    id: uuid.UUID
    family_id: uuid.UUID
    child_id: uuid.UUID
    name: str
    content_language: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


class CycleCreate(BaseModel):
    """Create a cycle.  family_id is derived server-side.

    ``scope_text`` is the text-first scope intake (no Storage upload this slice).
    """

    subject_id: uuid.UUID
    scope_text: str = Field(
        min_length=1,
        description="Educational scope text.  Text-first intake (no file upload this slice).",
    )


class CycleApprove(BaseModel):
    """Parent approval payload for POST /cycles/{id}/approve."""

    note: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional parent note recorded with the approval timestamp.",
    )


class CycleResponse(BaseModel):
    id: uuid.UUID
    family_id: uuid.UUID
    subject_id: uuid.UUID
    state: CycleState
    scope_text: str | None = None
    parent_approval_at: datetime | None = None
    parent_approval_note: str | None = None
    # Phase 3 publish gate — distinct from the draft approval above.
    marks_published_at: datetime | None = None
    published_visibility: VisibilityDefaults | None = None
    created_at: datetime
    updated_at: datetime
    # Assessments belonging to this cycle (included on GET /cycles/{id}).
    assessments: list[Assessment] = Field(
        default_factory=list,
        description=(
            "Assessment document(s) for this cycle; "
            "populated on detail endpoint, empty on list views."
        ),
    )
