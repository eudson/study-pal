"""Pydantic v2 request/response models for family, child, subject, and cycle.

No bare dict crosses any service boundary (ARCHITECTURE.md §8).
family_id is NEVER accepted from the client — it is always derived
server-side from the authenticated Identity (invariant 3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

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
# CyclePhase — generic (round, phase) axis
# (docs/design/round-phase-architecture.md §2, §4).
#
# `CycleState` remains the DRIVER of all state-machine logic in P1 (this
# phase is schema/migration foundation only — see the design doc's phased
# rollout §7).  `round` + `phase` are additive and derived from `state`;
# they must never disagree with `state`.  Logic migrates to (round, phase)
# in P2.
# ---------------------------------------------------------------------------


class CyclePhase(StrEnum):
    SCOPE_UPLOADED = "SCOPE_UPLOADED"
    GENERATING = "GENERATING"
    DRAFT_REVIEW = "DRAFT_REVIEW"
    PRINTED = "PRINTED"
    ANSWERS_ENTERED = "ANSWERS_ENTERED"
    MARKED = "MARKED"
    REVIEW_MARKS = "REVIEW_MARKS"
    PUBLISHED = "PUBLISHED"
    STUDY_PACK = "STUDY_PACK"
    COMPLETE = "COMPLETE"


# state -> (round, phase); total over all CycleState members (design §4 backfill
# table).  GENERATING_B is lossy by design (no production data — design §4 note).
_STATE_TO_ROUND_PHASE: dict[CycleState, tuple[int, CyclePhase]] = {
    CycleState.SCOPE_UPLOADED: (1, CyclePhase.SCOPE_UPLOADED),
    CycleState.GENERATING_A: (1, CyclePhase.GENERATING),
    CycleState.PARENT_REVIEWS_DRAFT: (1, CyclePhase.DRAFT_REVIEW),
    CycleState.APPROVED_PRINTED: (1, CyclePhase.PRINTED),
    CycleState.ANSWERS_ENTERED: (1, CyclePhase.ANSWERS_ENTERED),
    CycleState.AUTO_MARKED: (1, CyclePhase.MARKED),
    CycleState.PARENT_REVIEW_MARKS: (1, CyclePhase.REVIEW_MARKS),
    CycleState.GAP_REPORT: (1, CyclePhase.PUBLISHED),
    CycleState.GENERATING_STUDY_PACK: (1, CyclePhase.STUDY_PACK),
    CycleState.STUDY_PACK_DONE: (1, CyclePhase.STUDY_PACK),
    CycleState.GENERATING_B: (2, CyclePhase.GENERATING),
    CycleState.CYCLE_COMPLETE: (2, CyclePhase.COMPLETE),
}

# (round, phase) -> state — NOT 1:1 (design §6.4): STUDY_PACK collapses two old
# states (GENERATING_STUDY_PACK / STUDY_PACK_DONE).  Canonical choice for the
# reverse map: STUDY_PACK_DONE (the "settled" state — safe default for any
# reader still keying off the shadowed `state` column).  Only round 1 and 2
# are populated (P1 has no round >= 3 concept yet); round 1 phases map back to
# their round-1 states, round 2 phases to their round-2 (GENERATING_B/
# CYCLE_COMPLETE) states — this mirrors the only two rounds the backfill can
# produce (design §4 backfill table endpoints: (1, SCOPE_UPLOADED) and
# (2, COMPLETE) confirmed correct below).
_ROUND_PHASE_TO_STATE: dict[tuple[int, CyclePhase], CycleState] = {
    (1, CyclePhase.SCOPE_UPLOADED): CycleState.SCOPE_UPLOADED,
    (1, CyclePhase.GENERATING): CycleState.GENERATING_A,
    (1, CyclePhase.DRAFT_REVIEW): CycleState.PARENT_REVIEWS_DRAFT,
    (1, CyclePhase.PRINTED): CycleState.APPROVED_PRINTED,
    (1, CyclePhase.ANSWERS_ENTERED): CycleState.ANSWERS_ENTERED,
    (1, CyclePhase.MARKED): CycleState.AUTO_MARKED,
    (1, CyclePhase.REVIEW_MARKS): CycleState.PARENT_REVIEW_MARKS,
    (1, CyclePhase.PUBLISHED): CycleState.GAP_REPORT,
    (1, CyclePhase.STUDY_PACK): CycleState.STUDY_PACK_DONE,
    (2, CyclePhase.GENERATING): CycleState.GENERATING_B,
    (2, CyclePhase.COMPLETE): CycleState.CYCLE_COMPLETE,
}


def state_to_round_phase(state: CycleState) -> tuple[int, CyclePhase]:
    """Map a (legacy) flat ``CycleState`` to its ``(round, phase)`` pair.

    Total over all 12 ``CycleState`` members (design §4 backfill table).
    """
    return _STATE_TO_ROUND_PHASE[state]


def round_phase_to_state(round: int, phase: CyclePhase) -> CycleState:  # noqa: A002
    """Map ``(round, phase)`` back to the (legacy, shadowed) ``CycleState``.

    NOT 1:1 with ``state_to_round_phase`` — ``(1, STUDY_PACK)`` collapses two
    old states; the canonical choice is ``STUDY_PACK_DONE`` (design §6.4).
    Raises ``KeyError`` for any ``(round, phase)`` pair with no P1 backfill
    origin (e.g. round >= 3 — not yet representable by the legacy enum).
    """
    return _ROUND_PHASE_TO_STATE[(round, phase)]


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
    # Generic (round, phase) axis (docs/design/round-phase-architecture.md).
    # `state` remains the DRIVER of logic through P1-P4; round/phase are
    # derived from it (see the ``_fill_round_phase`` validator below) so
    # they can never disagree with `state`. Repos do not need to compute
    # these themselves — passing just `state` is enough; round/phase are
    # filled in automatically before validation if omitted.
    # Field defaults below are never actually relied on at runtime — the
    # ``_fill_round_phase`` before-validator always derives real values from
    # `state` (a required field) when the caller omits round/phase. They
    # exist only so callers are not forced to pass round/phase explicitly
    # (matching how repos construct CycleResponse today).
    round: int = Field(
        default=1,
        description="Generic round axis (1 = diagnostic, 2 = retest, ...). Derived from state.",
    )
    phase: CyclePhase = Field(
        default=CyclePhase.SCOPE_UPLOADED,
        description="Generic phase axis, uniform across rounds. Derived from state.",
    )
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

    @model_validator(mode="before")
    @classmethod
    def _fill_round_phase(cls, data: object) -> object:
        """Fill round/phase from state when not explicitly supplied.

        state and (round, phase) must never disagree (design §5). If a
        caller supplies round/phase explicitly, they are trusted as-is
        (repos may do this to avoid recomputing); otherwise they are
        derived here from ``state`` so every construction path — including
        ``model_copy(update=...)`` which re-validates — stays consistent.
        """
        if not isinstance(data, dict):
            return data
        if data.get("round") is not None and data.get("phase") is not None:
            return data
        state_raw = data.get("state")
        if state_raw is None:
            return data
        state = state_raw if isinstance(state_raw, CycleState) else CycleState(state_raw)
        derived_round, derived_phase = state_to_round_phase(state)
        filled = dict(data)
        filled.setdefault("round", derived_round)
        filled.setdefault("phase", derived_phase)
        return filled
