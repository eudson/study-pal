"""Pydantic v2 models for the Variant A vs Variant B comparison (Week 6).

``ABComparison`` is derived deterministically (no Claude call, no I/O — see
``services/comparison.py``) from a cycle's Variant-A and Variant-B
``GapReport``s.  Matching is done on ``gap_tag`` — NEVER on ``question_id``,
because Variant B's questions carry entirely new ids (ARCHITECTURE.md §5:
Variant B is a regeneration, not the same questions re-asked).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class GapDelta(BaseModel):
    """One gap_tag's before/after status in the A-vs-B comparison.

    ``description`` is a human-readable label derived from the tag itself —
    there is no separate lookup table guaranteed to exist server-side.
    ``error_category`` is the ErrorCategory value (if any) associated with
    this tag in the contributing report(s); may be null when no growing item
    carrying this tag was ever categorised by the parent.
    """

    gap_tag: str
    description: str
    error_category: str | None = None


class ABComparisonSummary(BaseModel):
    """Aggregate counts + score comparison for the A-vs-B retest."""

    closed_count: int = Field(ge=0)
    persisting_count: int = Field(ge=0)
    new_count: int = Field(ge=0)
    score_a: Decimal = Field(description="Total marks earned on Variant A.")
    score_a_total: Decimal = Field(description="Total marks available on Variant A.")
    score_b: Decimal = Field(description="Total marks earned on Variant B.")
    score_b_total: Decimal = Field(description="Total marks available on Variant B.")


class ABComparison(BaseModel):
    """Full A-vs-B comparison for a cycle.

    - ``closed``: gap_tags that were growing in Variant A and are NOT growing
      in Variant B — the child has mastered this gap.
    - ``persisting``: gap_tags growing in BOTH variants — still a gap.
    - ``new``: gap_tags growing in Variant B that were NOT growing in Variant A
      — a newly surfaced gap (can happen since Variant B's questions differ).

    Deterministic ordering: each list is sorted by ``gap_tag`` for a stable
    API response (``services/comparison.py``).
    """

    cycle_id: str
    closed: list[GapDelta] = Field(default_factory=list)
    persisting: list[GapDelta] = Field(default_factory=list)
    new: list[GapDelta] = Field(default_factory=list)
    summary: ABComparisonSummary
