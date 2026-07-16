"""Week 6 — A/B comparison derivation.

``derive_ab_comparison`` is a pure, deterministic function: no Claude calls,
no I/O.  It takes a cycle's Variant-A and Variant-B ``GapReport``s and
returns an ``ABComparison`` Pydantic model.

Matching rule (locked spec — ARCHITECTURE.md §5): gap_tags, NEVER
question_id.  Variant B's questions carry entirely new ids (it is a
regeneration of the source assessment, not the same questions re-asked), so
question_id-based matching would silently produce an empty comparison.

Definitions:
- A gap_tag is "growing" in a report if it appears in any growing item's
  ``gap_tags`` (equivalently: in ``report.summary.growing_gap_tags``).
- closed      = growing in A AND NOT growing in B  (the gap has closed).
- persisting  = growing in A AND growing in B      (still a gap).
- new         = growing in B AND NOT growing in A  (newly surfaced gap).

Ordering is deterministic (sorted by gap_tag) for a stable API response.
"""

from __future__ import annotations

from schemas.comparison import ABComparison, ABComparisonSummary, GapDelta
from schemas.gap_report import GapReport, GapStatus


def _growing_tag_categories(report: GapReport) -> dict[str, str | None]:
    """Map each growing gap_tag to its first non-null error_category (if any).

    A tag may appear on multiple growing items; the first non-null
    error_category encountered (in question order) wins.
    """
    out: dict[str, str | None] = {}
    for item in report.items:
        if item.status != GapStatus.GROWING:
            continue
        for tag in item.gap_tags:
            if tag not in out or (out[tag] is None and item.error_category is not None):
                out[tag] = item.error_category
    return out


def _describe(tag: str) -> str:
    """Human-readable label derived from the tag itself (no external lookup)."""
    label = tag.replace("_", " ").replace("-", " ").strip()
    return label[:1].upper() + label[1:] if label else tag


def derive_ab_comparison(gap_a: GapReport, gap_b: GapReport) -> ABComparison:
    """Derive the Variant A vs B comparison. Pure — no I/O, no Claude call."""
    tags_a = _growing_tag_categories(gap_a)
    tags_b = _growing_tag_categories(gap_b)

    closed_tags = sorted(t for t in tags_a if t not in tags_b)
    persisting_tags = sorted(t for t in tags_a if t in tags_b)
    new_tags = sorted(t for t in tags_b if t not in tags_a)

    closed = [
        GapDelta(gap_tag=t, description=_describe(t), error_category=tags_a[t]) for t in closed_tags
    ]
    persisting = [
        GapDelta(
            gap_tag=t,
            description=_describe(t),
            error_category=tags_a[t] if tags_a[t] is not None else tags_b[t],
        )
        for t in persisting_tags
    ]
    new = [
        GapDelta(gap_tag=t, description=_describe(t), error_category=tags_b[t]) for t in new_tags
    ]

    summary = ABComparisonSummary(
        closed_count=len(closed),
        persisting_count=len(persisting),
        new_count=len(new),
        score_a=gap_a.summary.total_marks_earned,
        score_a_total=gap_a.summary.total_marks_available,
        score_b=gap_b.summary.total_marks_earned,
        score_b_total=gap_b.summary.total_marks_available,
    )

    return ABComparison(
        cycle_id=gap_a.cycle_id,
        closed=closed,
        persisting=persisting,
        new=new,
        summary=summary,
    )
