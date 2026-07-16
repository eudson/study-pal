"""Pydantic v2 models for Phase 4 — gap report derivation.

GapReport is derived deterministically from reviewed question marks (no Claude call).
See ARCHITECTURE.md §3 (subject-agnostic, no bare dicts), §6 (error taxonomy),
§10 (design: wrong answers are "growing", plum semantic, never red).

Shape:
- GapReportItem: one per question; status is mastered|growing.
- GapReportSummary: aggregate counts + distinct growing gap_tags.
- GapReport: flat items list + summary.

The study-pack phase will consume growing items and their gap_tags.
Clustering note: gap_tags on each item are passed through verbatim — consumers
can group by tag; the report itself presents a flat list ordered by question number.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GapStatus(StrEnum):
    """Mastery status for a single question's result.

    "growing" is used instead of "wrong"/"failed" (ARCHITECTURE.md §10 design rule:
    wrong answers are diagnostic data, not punishment — plum semantic).
    """

    MASTERED = "mastered"
    GROWING = "growing"


# ---------------------------------------------------------------------------
# Per-item model
# ---------------------------------------------------------------------------


class GapReportItem(BaseModel):
    """One question's result in a gap report.

    ``status`` is "mastered" when full marks were earned, "growing" otherwise
    (partial or zero marks — half-marks are legal everywhere, §8).

    ``gap_tags`` is passed through from the originating Question.gap_tags; these
    seed Variant B retargeting (ARCHITECTURE.md §5, VariantBRequest.gaps).
    """

    question_id: str = Field(description="qid from the original assessment question.")
    number: str = Field(description='Question number as printed, e.g. "3", "3.1".')
    text: str = Field(description="Short question label (the full question text).")
    status: GapStatus
    final_marks: Decimal = Field(description="Parent-reviewed final mark (post-publish).")
    marks_total: Decimal = Field(description="Maximum marks available for this question.")
    error_category: str | None = Field(
        default=None,
        description=(
            "ErrorCategory value if the parent set one during review; "
            "null for mastered questions and untagged growing questions."
        ),
    )
    gap_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Gap tags from the original question; empty when none were declared. "
            "Growing items with tags are candidates for Variant B retargeting."
        ),
    )


# ---------------------------------------------------------------------------
# Summary model
# ---------------------------------------------------------------------------


class GapReportSummary(BaseModel):
    """Aggregate summary of a gap report.

    ``growing_gap_tags`` is the deduplicated union of all gap_tags across
    growing items — the set fed to Variant B generation to retarget gaps.
    """

    mastered_count: int = Field(ge=0)
    growing_count: int = Field(ge=0)
    total_marks_earned: Decimal = Field(description="Sum of final_marks across all questions.")
    total_marks_available: Decimal = Field(description="Sum of marks_total across all questions.")
    growing_gap_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Distinct gap_tags from all growing items, sorted for stability. "
            "Empty when no growing items carry tags."
        ),
    )


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


class GapReport(BaseModel):
    """Complete gap report for a single assessment cycle.

    Produced by ``derive_gap_report`` from the reviewed marks; stored as JSONB
    in the gap_reports table.  The study-pack phase reads ``items`` filtered to
    status="growing" to select retarget content.

    Clustering: ``items`` is a flat list (one per question).  There is no
    explicit topic field on questions — consumers wanting to cluster by tag should
    group ``items`` by ``gap_tags`` membership.  The report remains flat here to
    keep the Pydantic boundary simple and let the UI / study-pack phase decide
    the grouping strategy.
    """

    assessment_id: str
    cycle_id: str
    items: list[GapReportItem] = Field(default_factory=list)
    summary: GapReportSummary
    derived_at: datetime = Field(description="UTC timestamp when the report was derived.")


# ---------------------------------------------------------------------------
# Storage / API response wrappers
# ---------------------------------------------------------------------------


class GapReportRow(BaseModel):
    """Database row shape for the gap_reports table.

    ``report`` is the full GapReport document stored as JSONB.
    ``round`` identifies which round this report belongs to (P4 of the
    round/phase redesign, docs/design/round-phase-architecture.md §4/§7);
    defaults to 1 so pre-P4 callers/tests that never threaded a round
    continue to construct rows unchanged.
    """

    id: uuid.UUID
    family_id: uuid.UUID
    cycle_id: uuid.UUID
    submission_id: uuid.UUID
    report: GapReport
    created_at: datetime
    round: int = 1


class GapReportResponse(BaseModel):
    """API response for POST /cycles/{cycle_id}/gap-report and GET /.../gap-report."""

    cycle_id: uuid.UUID
    submission_id: uuid.UUID
    report: GapReport
    round: int = 1
