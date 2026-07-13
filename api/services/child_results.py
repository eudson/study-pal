"""Child results projection service (Phase 4+).

``project_results_for_child`` is the ONLY authoritative function that
builds a ``ChildResultsView``.  All answer-key and memo fields are excluded
by construction — never by omission of a guard — mirroring the
``capture_service.project_for_child`` boundary discipline.

Security invariants:
- MUST NOT import ``render_correct_answer`` or touch ``AnswerPayload``
  or the raw Assessment answer keys.  Structural exclusion, not runtime hiding.
- Visibility gates are applied by leaving fields ``None``; the schema never
  carries a fake zero in place of a gated field.
- Called only from ``routers/child_results.py``; no other code path may
  produce a ``ChildResultsView``.
"""

from __future__ import annotations

from decimal import Decimal

from schemas.capture import ChildResponseItem
from schemas.child_results import ChildResultItem, ChildResultsSummary, ChildResultsView
from schemas.family import CycleResponse, VisibilityDefaults
from schemas.gap_report import GapReport, GapStatus
from schemas.grading import QuestionMark, render_child_answer


def project_results_for_child(
    cycle: CycleResponse,
    report: GapReport,
    marks: list[QuestionMark],
    responses: list[ChildResponseItem],
    snapshot: VisibilityDefaults,
) -> ChildResultsView:
    """Build the child-visible results view from published data.

    Args:
        cycle:     The cycle row; used for cycle_id, title (from the assessment
                   on the cycle), and marks_published_at.
        report:    The GapReport (stored or in-memory derived) — provides
                   question_id, number, text, status, final_marks, marks_total
                   per item in assessment order.
        marks:     All QuestionMark records for this submission — used only to
                   retrieve ``ai_rationale`` per question_id.
        responses: The child's submitted ChildResponseItem list — used to call
                   ``render_child_answer`` (memo-free).
        snapshot:  The FROZEN ``published_visibility`` from the cycle's publish
                   row.  Gating from the child's current ``visibility_defaults``
                   MUST NOT be used here — the snapshot is the contract.

    Returns:
        A ``ChildResultsView`` containing no memo, correct-answer, or
        answer-key fields.

    Security contract:
        This function MUST NOT import or call ``render_correct_answer``.
        The import is deliberately absent from this module.
    """
    # Build O(1) lookups.
    mark_by_qid: dict[str, QuestionMark] = {m.question_id: m for m in marks}
    payload_by_qid: dict[str, dict[str, object]] = {r.qid: r.payload for r in responses}

    items: list[ChildResultItem] = []

    for gap_item in report.items:
        qid = gap_item.question_id
        mark = mark_by_qid.get(qid)
        payload = payload_by_qid.get(qid, {})

        # render_child_answer is memo-free by definition — see schemas/grading.py.
        # The question_type is embedded inside the GapReportItem text; however, we
        # need the question_type string.  The gap report doesn't carry question_type
        # directly — but render_child_answer's fallback path ("raw: ...") is safe
        # even for unknown types.  For known types, the payload shape itself is
        # sufficient.  We derive question_type from the mark's grading_path context
        # where possible; for the child-results view, the rendered string is the
        # only consumer so the fallback is acceptable.
        #
        # The preferred approach: we don't have question_type on GapReportItem.
        # Walk the assessment sections would require the Assessment object.  Since
        # the spec passes ``report`` and ``marks`` (not the Assessment), we derive
        # question_type from the mark's context if available, otherwise use the
        # payload keys to infer the type for render_child_answer's dispatch.
        #
        # Implementation: render_child_answer already handles unknown types safely
        # via its final fallback branch.  We pass an empty string as question_type
        # when there is no mark, which triggers the safe fallback.
        question_type: str = ""
        if mark is not None:
            # QuestionMark doesn't carry question_type directly; it carries grading_path.
            # We need the question_type for render_child_answer dispatch.
            # The payload keys reveal the type implicitly and render_child_answer
            # dispatches on qt — but we need the string.
            # Best-effort inference from payload keys when grading_path alone is insufficient:
            # We'll use the payload key heuristic only as a last resort.  The correct
            # approach is to use the question_type from the gap item; but GapReportItem
            # doesn't carry it.  Therefore we rely on payload inspection.
            question_type = _infer_question_type_from_payload(payload)

        child_answer_rendered = render_child_answer(question_type, dict(payload))

        # Apply visibility gates: leave fields None when the toggle is off.
        marks_earned: Decimal | None = None
        marks_total: Decimal | None = None
        if snapshot.accuracy:
            marks_earned = gap_item.final_marks
            marks_total = gap_item.marks_total

        item_status: GapStatus | None = None
        if snapshot.growing:
            item_status = gap_item.status

        ai_rationale: str | None = None
        if snapshot.ai_rationale and mark is not None:
            ai_rationale = mark.ai_rationale

        items.append(
            ChildResultItem(
                question_id=qid,
                number=gap_item.number,
                text=gap_item.text,
                child_answer_rendered=child_answer_rendered,
                marks_earned=marks_earned,
                marks_total=marks_total,
                status=item_status,
                ai_rationale=ai_rationale,
            )
        )

    # Build summary with visibility gates.
    total_questions = len(report.items)

    attempted_count: int | None = None
    if snapshot.effort:
        # A question is "attempted" when the child supplied a non-empty payload.
        attempted_count = sum(
            1 for gap_item in report.items if bool(payload_by_qid.get(gap_item.question_id))
        )

    mastered_count: int | None = None
    growing_count: int | None = None
    if snapshot.growing:
        mastered_count = report.summary.mastered_count
        growing_count = report.summary.growing_count

    summary_marks_earned: Decimal | None = None
    summary_marks_available: Decimal | None = None
    if snapshot.accuracy:
        summary_marks_earned = report.summary.total_marks_earned
        summary_marks_available = report.summary.total_marks_available

    summary = ChildResultsSummary(
        total_questions=total_questions,
        attempted_count=attempted_count,
        mastered_count=mastered_count,
        growing_count=growing_count,
        marks_earned=summary_marks_earned,
        marks_available=summary_marks_available,
    )

    # published_at comes from the cycle's marks_published_at.
    # The router guarantees this is not None for valid states (GAP_REPORT+).
    assert cycle.marks_published_at is not None, (
        "project_results_for_child called with cycle.marks_published_at=None; "
        "router must validate this before calling."
    )

    # title: use the Variant A assessment title if available, else fall back to cycle id.
    title: str = str(cycle.id)
    if cycle.assessments:
        variant_a = next((a for a in cycle.assessments if a.variant == "A"), None)
        if variant_a is not None:
            title = variant_a.title

    return ChildResultsView(
        cycle_id=cycle.id,
        title=title,
        published_at=cycle.marks_published_at,
        summary=summary,
        items=items,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _infer_question_type_from_payload(payload: dict[str, object]) -> str:
    """Best-effort question_type inference from payload keys.

    ``render_child_answer`` dispatches on ``question_type`` (str).  The child
    results path does not carry ``question_type`` on ``GapReportItem``; this
    helper infers it from the payload shape so the rendered string is accurate.

    Falls back to empty string, which causes ``render_child_answer`` to emit
    its safe "(raw: ...)" fallback — never crashes, never leaks answer info.

    Keyed on payload structure, never on subject (golden rule 4).
    """
    if not payload:
        return ""
    keys = set(payload.keys())
    if "selected_index" in keys:
        return "mcq"
    if "value" in keys and len(keys) <= 2:
        # true_false: {"value": bool} or {"value": bool, ...}
        return "true_false"
    if "pairs" in keys:
        return "matching"
    if "order" in keys:
        return "ordering"
    if "values" in keys:
        return "fill_blank"
    if "text" in keys and len(keys) <= 2:
        # short_answer or extended_response both use "text"
        return "short_answer"
    if "answer" in keys or "working" in keys:
        return "calculation"
    if "cells" in keys:
        return "table_completion"
    if "labels" in keys:
        return "labelling"
    return ""
