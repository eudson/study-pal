"""Phase 4 — gap report derivation service.

``derive_gap_report`` is a pure deterministic function: no Claude calls, no I/O.
It takes a validated Assessment and a list of QuestionMark objects and returns a
GapReport Pydantic model.

Rules (ARCHITECTURE.md §3, §6, §10 design):
- Subject-agnostic: no ``if subject ==`` branches.  Logic keys only on mark values.
- mastered = final_marks == marks_total (full marks earned).
- growing  = final_marks < marks_total (partial OR zero — including not_attempted).
  "growing" is the only status used for below-full-marks (design rule: wrong answers
  are diagnostic data, not punishment — plum semantic, never red or "failed").
- final_marks is guaranteed set post-publish (the publish gate enforces this);
  this function asserts the invariant and raises ValueError on violation rather
  than silently skipping a question.
- Half-marks (0.5) are legal everywhere (ARCHITECTURE.md §8).
- gap_tags are passed through from the Assessment question to the GapReportItem;
  the summary collects the distinct union of growing items' tags.
- Items are ordered to match question order in the assessment (section order, then
  question order within section).  Questions not found in the marks are skipped
  (defensive); marks for unknown question_ids are also skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from schemas.assessment_schema import Assessment
from schemas.gap_report import (
    GapReport,
    GapReportItem,
    GapReportSummary,
    GapStatus,
)
from schemas.grading import QuestionMark


def derive_gap_report(
    assessment: Assessment,
    marks: list[QuestionMark],
) -> GapReport:
    """Derive a GapReport from an Assessment and its reviewed marks.

    Args:
        assessment: The fully-validated Assessment (Variant A).
        marks: All QuestionMark records for this cycle's submission.
                Every mark's ``final_marks`` MUST be set (not None) — this is
                enforced by the publish gate before this function is called.

    Returns:
        A GapReport containing one GapReportItem per question that has a
        corresponding reviewed mark, plus an aggregate summary.

    Raises:
        ValueError: if any mark's ``final_marks`` is None (post-publish invariant
                    violation — should never occur in production flow).
    """
    # Build a lookup from question_id → QuestionMark for O(1) access.
    mark_by_qid: dict[str, QuestionMark] = {m.question_id: m for m in marks}

    items: list[GapReportItem] = []

    # Walk assessment in section/question order to produce a stable ordering.
    for section in assessment.sections:
        for question in section.questions:
            mark = mark_by_qid.get(question.qid)
            if mark is None:
                # No mark found for this question — skip (defensive; should not
                # occur in a fully graded + reviewed cycle).
                continue

            if mark.final_marks is None:
                raise ValueError(
                    f"Question {question.qid}: final_marks is None — "
                    "derive_gap_report must only be called after all marks are published "
                    "(post-publish invariant: every final_marks must be set)."
                )

            final = mark.final_marks
            total = mark.marks_total

            # mastered = full marks; growing = anything below full marks.
            status = GapStatus.MASTERED if final == total else GapStatus.GROWING

            items.append(
                GapReportItem(
                    question_id=question.qid,
                    number=question.number,
                    text=question.text,
                    status=status,
                    final_marks=final,
                    marks_total=total,
                    error_category=(
                        mark.error_category.value if mark.error_category is not None else None
                    ),
                    gap_tags=list(question.gap_tags),
                )
            )

    # Build summary.
    mastered_items = [it for it in items if it.status == GapStatus.MASTERED]
    growing_items = [it for it in items if it.status == GapStatus.GROWING]

    total_earned = sum((it.final_marks for it in items), Decimal("0"))
    total_available = sum((it.marks_total for it in items), Decimal("0"))

    # Collect distinct gap_tags from growing items; sort for determinism.
    growing_tags_set: set[str] = set()
    for it in growing_items:
        growing_tags_set.update(it.gap_tags)
    growing_gap_tags = sorted(growing_tags_set)

    summary = GapReportSummary(
        mastered_count=len(mastered_items),
        growing_count=len(growing_items),
        total_marks_earned=total_earned,
        total_marks_available=total_available,
        growing_gap_tags=growing_gap_tags,
    )

    return GapReport(
        assessment_id=assessment.assessment_id,
        cycle_id=assessment.cycle_id,
        items=items,
        summary=summary,
        derived_at=datetime.now(tz=UTC),
    )
