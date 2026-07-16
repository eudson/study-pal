"""Pydantic v2 models for Phase 5 — study pack generation.

``StudyPack`` is derived from the gap report's growing items (subject-agnostic;
keys only on gap_tags and question_type — never on subject name).  It is
produced by ``FakeStudyPack`` (deterministic fake, same pattern as FakeGrader)
and stored as JSONB in the study_packs table.

Child visibility gate (golden rule 8): the pack is only exposed to the child
after the parent calls POST /cycles/{id}/study-pack/approve, which sets
``approved_at`` on the study_packs row.

Shape:
- StudyPackItem: one practice item targeting one or more gap_tags.
- StudyPack: list of items + summary, derived from a GapReport.
- StudyPackRow: DB row shape (pack JSONB + approved_at nullable).
- StudyPackResponse: API response wrapper.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Practice item
# ---------------------------------------------------------------------------


class StudyPackItem(BaseModel):
    """One structured practice item derived from a gap.

    ``gap_tags`` identifies which growing gap(s) from the report this item
    targets — passed through verbatim from the GapReportItem(s) that seeded it.

    ``question_type`` is the type of practice question (mirrors the
    assessment schema's question_type values: mcq, short_answer, etc.).
    Grading and on-screen rendering key off this, not the subject.

    ``prompt`` is the full practice question text shown to the child on-screen
    and rendered in the PDF.

    ``worked_example`` is an optional detailed worked-through solution for the
    parent or tutor to reference — intentionally separate from the answer so
    the child's copy can omit it.  Stored as plain text (may include simple
    notation); subject-agnostic.

    ``answer`` is the expected answer for parent reference (plain text).
    Not shown to the child before they attempt the question.

    ``hint`` is an optional short scaffold hint shown to the child if they
    are stuck.  May be null when no hint is appropriate.
    """

    item_id: str = Field(description="Stable unique identifier for this practice item.")
    gap_tags: list[str] = Field(
        description=(
            "Gap tags from the gap report items this practice item targets. "
            "At least one tag must be present (items are only generated for growing gaps)."
        ),
        min_length=1,
    )
    question_type: str = Field(
        description=(
            "Question type driving on-screen rendering and PDF layout "
            "(e.g. 'short_answer', 'calculation', 'mcq'). "
            "Never branched on subject — subject intelligence lives in prompts."
        )
    )
    prompt: str = Field(
        description="Full practice question/task text for the child.",
        min_length=1,
    )
    worked_example: str | None = Field(
        default=None,
        description=(
            "Detailed worked-through solution (parent/tutor reference only). "
            "Not exposed to the child before they attempt."
        ),
    )
    answer: str = Field(
        description="Expected answer (parent reference). Not shown to child before attempt.",
        min_length=1,
    )
    hint: str | None = Field(
        default=None,
        description=(
            "Optional short scaffold hint for the child. "
            "Null when no hint is appropriate for this question type."
        ),
    )


# ---------------------------------------------------------------------------
# Top-level pack
# ---------------------------------------------------------------------------


class StudyPack(BaseModel):
    """Complete study pack for a single assessment cycle.

    Produced by ``FakeStudyPack.generate`` from a GapReport; stored as JSONB
    in the study_packs table.

    ``derived_from_gap_tags`` is the distinct sorted union of all gap_tags
    across items — the set fed to Variant B retargeting (mirrors
    GapReportSummary.growing_gap_tags).

    ``summary`` is a short human-readable intro/framing paragraph shown at
    the top of the pack (on-screen and in PDF).  Language follows
    ``content_language`` of the originating assessment.
    """

    cycle_id: str
    assessment_id: str
    items: list[StudyPackItem] = Field(default_factory=list)
    summary: str = Field(
        description=(
            "Short intro / framing paragraph for the pack (on-screen header and PDF cover text)."
        ),
        min_length=1,
    )
    derived_from_gap_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Distinct sorted union of all gap_tags across items — "
            "mirrors GapReportSummary.growing_gap_tags."
        ),
    )
    generated_at: datetime = Field(description="UTC timestamp when the pack was generated.")


# ---------------------------------------------------------------------------
# Storage / API response wrappers
# ---------------------------------------------------------------------------


class StudyPackRow(BaseModel):
    """Database row shape for the study_packs table.

    ``pack`` is the full StudyPack document stored as JSONB.
    ``approved_at`` is null until the parent calls the approve endpoint
    (golden rule 8 — child-visible gate).
    """

    id: uuid.UUID
    family_id: uuid.UUID
    cycle_id: uuid.UUID
    pack: StudyPack
    approved_at: datetime | None = Field(
        default=None,
        description=(
            "Timestamp when the parent approved this pack for child visibility. "
            "Null until POST /cycles/{id}/study-pack/approve is called."
        ),
    )
    created_at: datetime
    round: int = Field(
        default=1,
        description=(
            "Which round this pack belongs to (P4 of the round/phase redesign, "
            "docs/design/round-phase-architecture.md §4/§7). Defaults to 1 so "
            "pre-P4 callers/tests that never threaded a round are unaffected."
        ),
    )


class StudyPackResponse(BaseModel):
    """API response for study-pack endpoints."""

    cycle_id: uuid.UUID
    pack: StudyPack
    approved_at: datetime | None = Field(
        default=None,
        description="Null until the parent approves; set by POST .../study-pack/approve.",
    )
    round: int = 1
