"""Phase 2 grading engine.

``grade_submission`` is the public entry point.  It takes a full ``Assessment``
and a list of ``ChildResponseItem`` responses, applies per-type grading logic,
and returns a list of ``QuestionMark`` — one per question.

Grading attaches to **question type, never subject** (ARCHITECTURE.md §6,
golden rule 4).  There are NO ``if subject == ...`` branches here.

Grading paths (ARCHITECTURE.md §6, GRADING_PATHS in assessment_schema.py):
  AUTO          — deterministic; final_marks = suggested_marks immediately.
  AUTO_FUZZY    — normalized-exact match against accepted_alternatives.
                  Match → full marks, needs_review=False.
                  No match → suggested_marks=0, needs_review=True, final_marks=NULL.
                  NEVER auto-finalize a zero (parent decides).
  CLAUDE_ASSIST — FakeGrader stub (one batched call per submission).
                  Always needs_review=True, final_marks=NULL.

Skipped / not_attempted → suggested_marks=0, error_category=not_attempted,
    needs_review=False, final_marks=0 (no ambiguity).

Proof photos are NEVER read here (ARCHITECTURE.md §10).

FakeGrader:
  - Deterministic stand-in for the real Claude grader.
  - ONE batched call per submission for all CLAUDE_ASSIST questions.
  - Swapping in live Claude changes zero call topology (same interface).
  - Logs a CallLog-style token record (0 tokens) per §8.
"""

from __future__ import annotations

import logging
import time
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from schemas.assessment_schema import (
    Assessment,
    CalculationAnswer,
    ErrorCategory,
    FillBlankAnswer,
    GradingPath,
    MatchingAnswer,
    McqAnswer,
    OrderingAnswer,
    Question,
    ShortAnswerSpec,
    TrueFalseAnswer,
)
from schemas.capture import ChildResponseItem
from schemas.generation import CallLog
from schemas.grading import QuestionMark

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------


def _d(v: float | str | int) -> Decimal:
    """Convert a numeric value to Decimal."""
    return Decimal(str(v))


def _half(v: float | str | int) -> Decimal:
    """Round a Decimal to the nearest 0.5 step (floor-safe)."""
    d = Decimal(str(v))
    # Quantize to 0.5: multiply by 2, round to int, divide by 2.
    return Decimal(int(d * 2)) / Decimal("2")


# ---------------------------------------------------------------------------
# Text normalisation for AUTO_FUZZY matching
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Normalise a string for fuzzy comparison.

    Steps (language-agnostic):
    1. Unicode NFC normalisation.
    2. str.casefold() — more aggressive than .lower(), handles German ß etc.
    3. Strip leading/trailing whitespace.
    4. Collapse internal whitespace runs to a single space.
    5. Strip terminal punctuation (. , ; : ! ?).
    """
    s = unicodedata.normalize("NFC", text)
    s = s.casefold()
    s = s.strip()
    # Collapse internal whitespace.
    s = " ".join(s.split())
    # Strip terminal punctuation (one or more chars at the very end).
    s = s.rstrip(".,;:!?")
    return s


# ---------------------------------------------------------------------------
# Numeric normalisation for AUTO number fill_blank
# ---------------------------------------------------------------------------


def _parse_numeric(raw: str) -> Decimal | None:
    """Parse a numeric string that may use comma OR dot as decimal separator.

    Also strips spaces and thousand-separators (both comma and space variants).
    Returns None on parse failure.
    """
    # Remove any whitespace.
    s = raw.strip().replace(" ", "")
    # Strip thousand-separator patterns: if comma appears as thousands separator
    # it will be a digit-comma-digit-digit-digit pattern.  We attempt both
    # interpretations: comma-as-decimal and comma-as-thousands.
    # Strategy: try both substitutions and return whichever parses.
    candidates: list[str] = []
    # Attempt 1: comma is decimal separator → replace with dot.
    candidates.append(s.replace(",", "."))
    # Attempt 2: comma is thousands separator → remove it.
    candidates.append(s.replace(",", ""))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    for candidate in unique:
        try:
            return Decimal(candidate)
        except InvalidOperation:
            pass
    return None


def _numeric_match(child_raw: str, accepted: list[str], tolerance: Decimal | None) -> bool:
    """Return True if child's numeric answer matches any accepted value within tolerance."""
    child_val = _parse_numeric(child_raw)
    if child_val is None:
        return False
    tol = tolerance if tolerance is not None else Decimal("0")
    for acc in accepted:
        acc_val = _parse_numeric(acc)
        if acc_val is None:
            continue
        if abs(child_val - acc_val) <= tol:
            return True
    return False


# ---------------------------------------------------------------------------
# FakeGrader — deterministic Claude-assist stub
# ---------------------------------------------------------------------------


class FakeGraderSuggestion:
    """Suggestion from FakeGrader for one CLAUDE_ASSIST question."""

    def __init__(
        self,
        question_id: str,
        suggested_marks: Decimal,
        marks_total: Decimal,
        ai_rationale: str,
    ) -> None:
        self.question_id = question_id
        self.suggested_marks = suggested_marks
        self.marks_total = marks_total
        self.ai_rationale = ai_rationale


class FakeGrader:
    """Deterministic stand-in for a live Claude grading call.

    Interface: ONE batched call per submission — ``grade_batch`` receives all
    CLAUDE_ASSIST questions and their responses in one shot, returning a list
    of ``FakeGraderSuggestion``.  Swapping in real Claude changes zero call
    topology because the router/service only calls ``grade_batch`` once.

    Determinism rule:
    - calculation: if the child's ``answer`` field (after numeric normalisation)
      matches the assessment's ``final_answer``, suggest answer_marks (or total);
      otherwise suggest 0.
    - table_completion / labelling: suggest 0 (safe default; parent reviews).
    - extended_response: suggest 0 (safe default; parent reviews).

    Logs a ``CallLog`` with 0 tokens per §8 (grading = one batched call per
    submission).
    """

    def grade_batch(
        self,
        questions_and_responses: list[tuple[Question, ChildResponseItem]],
        submission_id: uuid.UUID,
    ) -> tuple[list[FakeGraderSuggestion], CallLog]:
        """Grade all CLAUDE_ASSIST questions in one deterministic batch.

        Returns (suggestions, call_log).
        """
        t0 = time.monotonic()
        suggestions: list[FakeGraderSuggestion] = []

        for question, response in questions_and_responses:
            total = _d(question.mark_rules.total)
            suggestion = self._grade_one(question, response, total)
            suggestions.append(suggestion)

        latency = (time.monotonic() - t0) * 1000
        call_log = CallLog(
            model="fake-grader",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=round(latency, 2),
            attempt=1,
        )
        log.info(
            "FakeGrader.grade_batch: submission=%s questions=%d latency_ms=%.2f",
            submission_id,
            len(questions_and_responses),
            latency,
        )
        return suggestions, call_log

    def _grade_one(
        self,
        question: Question,
        response: ChildResponseItem,
        total: Decimal,
    ) -> FakeGraderSuggestion:
        """Deterministic suggestion for one CLAUDE_ASSIST question."""
        answer = question.answer
        payload = response.payload

        # calculation: grade final answer AUTO-style; suggest answer_marks on match.
        if isinstance(answer, CalculationAnswer):
            child_raw = str(payload.get("answer", "")).strip()
            answer_marks = (
                _d(question.mark_rules.answer_marks)
                if question.mark_rules.answer_marks is not None
                else total
            )
            tolerance = _d(answer.tolerance) if answer.tolerance is not None else Decimal("0")
            accepted_numerics = [answer.final_answer]
            matched = _numeric_match(child_raw, accepted_numerics, tolerance)
            if not matched:
                # Also try normalised-exact for non-numeric final answers.
                matched = _normalize(child_raw) == _normalize(answer.final_answer)
            suggested = answer_marks if matched else Decimal("0")
            rationale = (
                f"FakeGrader: final answer {'matches' if matched else 'does not match'} "
                f"expected '{answer.final_answer}'. "
                "Method marks require parent review."
            )
            return FakeGraderSuggestion(
                question_id=question.qid,
                suggested_marks=suggested,
                marks_total=total,
                ai_rationale=rationale,
            )

        # table_completion / labelling / extended_response: safe default = 0.
        rationale = (
            f"FakeGrader: {question.question_type.value} requires parent review. "
            "Suggested 0 as safe default."
        )
        return FakeGraderSuggestion(
            question_id=question.qid,
            suggested_marks=Decimal("0"),
            marks_total=total,
            ai_rationale=rationale,
        )


# ---------------------------------------------------------------------------
# Per-type AUTO graders
# ---------------------------------------------------------------------------


def _grade_mcq(
    question: Question,
    answer: McqAnswer,
    payload: dict[str, Any],
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> QuestionMark:
    total = _d(question.mark_rules.total)
    selected = payload.get("selected_index")
    correct = selected == answer.correct_index
    awarded = total if correct else Decimal("0")
    return QuestionMark(
        family_id=family_id,
        submission_id=submission_id,
        question_id=question.qid,
        marks_total=total,
        suggested_marks=awarded,
        final_marks=awarded,
        grading_path=GradingPath.AUTO,
        confidence=Decimal("1"),
        needs_review=False,
        error_category=None if correct else ErrorCategory.CARELESS,
    )


def _grade_true_false(
    question: Question,
    answer: TrueFalseAnswer,
    payload: dict[str, Any],
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> QuestionMark:
    total = _d(question.mark_rules.total)
    child_value = payload.get("value")
    # Simple boolean match — the full correction mark goes to parent review
    # only if this is a true_false that requires_correction.  In AUTO we award
    # full marks only when the child's value matches the expected bool exactly.
    if isinstance(child_value, (bool, int)):
        child_bool = bool(child_value)
        correct = child_bool == answer.is_true
    else:
        correct = False
    awarded = total if correct else Decimal("0")
    return QuestionMark(
        family_id=family_id,
        submission_id=submission_id,
        question_id=question.qid,
        marks_total=total,
        suggested_marks=awarded,
        final_marks=awarded,
        grading_path=GradingPath.AUTO,
        confidence=Decimal("1"),
        needs_review=False,
        error_category=None if correct else ErrorCategory.CARELESS,
    )


def _grade_matching(
    question: Question,
    answer: MatchingAnswer,
    payload: dict[str, Any],
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> QuestionMark:
    """Per-pair partial credit unless mark_rules has tick_allocation='all_or_nothing'."""
    total = _d(question.mark_rules.total)
    n_pairs = len(answer.correct_pairs)
    if n_pairs == 0:
        return QuestionMark(
            family_id=family_id,
            submission_id=submission_id,
            question_id=question.qid,
            marks_total=total,
            suggested_marks=Decimal("0"),
            final_marks=Decimal("0"),
            grading_path=GradingPath.AUTO,
            confidence=Decimal("1"),
            needs_review=False,
        )

    # mark_rules.tick_allocation == 'all_or_nothing' disables partial credit.
    all_or_nothing = (
        question.mark_rules.tick_allocation is not None
        and "all_or_nothing" in question.mark_rules.tick_allocation.lower()
    )

    # Child submits pairs: [{"left": int, "right": int}, ...]
    child_pairs_raw = payload.get("pairs", [])
    child_pairs: dict[int, int] = {}
    if isinstance(child_pairs_raw, list):
        for p in child_pairs_raw:
            if isinstance(p, dict):
                left_idx = p.get("left")
                right_idx = p.get("right")
                if isinstance(left_idx, int) and isinstance(right_idx, int):
                    child_pairs[left_idx] = right_idx

    correct_count = sum(1 for li, ri in answer.correct_pairs.items() if child_pairs.get(li) == ri)

    if all_or_nothing:
        awarded = total if correct_count == n_pairs else Decimal("0")
    else:
        # Per-pair credit: marks_per_pair = total / n_pairs, quantised to 0.5.
        marks_per_pair = _half(float(total) / n_pairs)
        awarded = _half(float(marks_per_pair) * correct_count)
        # Clamp to total (rounding can push over).
        if awarded > total:
            awarded = total

    is_correct = awarded == total
    return QuestionMark(
        family_id=family_id,
        submission_id=submission_id,
        question_id=question.qid,
        marks_total=total,
        suggested_marks=awarded,
        final_marks=awarded,
        grading_path=GradingPath.AUTO,
        confidence=Decimal("1"),
        needs_review=False,
        error_category=None if is_correct else ErrorCategory.CARELESS,
    )


def _grade_ordering(
    question: Question,
    answer: OrderingAnswer,
    payload: dict[str, Any],
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> QuestionMark:
    """Exact-position-per-slot scoring (default).

    mark_rules.tick_allocation == 'all_or_nothing' awards total only for a
    fully correct sequence.

    Child submits: {"order": [orig_index, ...]} — the original indices in the
    child's chosen order.  correct_order is the expected sequence of original
    indices.
    """
    total = _d(question.mark_rules.total)
    n_slots = len(answer.correct_order)
    if n_slots == 0:
        return QuestionMark(
            family_id=family_id,
            submission_id=submission_id,
            question_id=question.qid,
            marks_total=total,
            suggested_marks=Decimal("0"),
            final_marks=Decimal("0"),
            grading_path=GradingPath.AUTO,
            confidence=Decimal("1"),
            needs_review=False,
        )

    all_or_nothing = (
        question.mark_rules.tick_allocation is not None
        and "all_or_nothing" in question.mark_rules.tick_allocation.lower()
    )

    child_order_raw = payload.get("order", [])
    child_order: list[int] = []
    if isinstance(child_order_raw, list):
        child_order = [int(x) for x in child_order_raw if isinstance(x, (int, float))]

    correct_positions = sum(
        1
        for i, expected_idx in enumerate(answer.correct_order)
        if i < len(child_order) and child_order[i] == expected_idx
    )

    if all_or_nothing:
        awarded = total if correct_positions == n_slots else Decimal("0")
    else:
        marks_per_slot = _half(float(total) / n_slots)
        awarded = _half(float(marks_per_slot) * correct_positions)
        if awarded > total:
            awarded = total

    is_correct = awarded == total
    return QuestionMark(
        family_id=family_id,
        submission_id=submission_id,
        question_id=question.qid,
        marks_total=total,
        suggested_marks=awarded,
        final_marks=awarded,
        grading_path=GradingPath.AUTO,
        confidence=Decimal("1"),
        needs_review=False,
        error_category=None if is_correct else ErrorCategory.CARELESS,
    )


def _grade_fill_blank(
    question: Question,
    answer: FillBlankAnswer,
    payload: dict[str, Any],
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> QuestionMark:
    """Grade fill_blank: AUTO for number-only, AUTO_FUZZY for word blanks.

    Per-blank credit: each blank is worth total / n_blanks (quantised to 0.5).
    Tolerance comes from MarkRules — no per-blank tolerance in the schema.
    """
    total = _d(question.mark_rules.total)
    n_blanks = len(answer.blanks)
    if n_blanks == 0:
        return QuestionMark(
            family_id=family_id,
            submission_id=submission_id,
            question_id=question.qid,
            marks_total=total,
            suggested_marks=total,
            final_marks=total,
            grading_path=question.grading_path,
            confidence=Decimal("1"),
            needs_review=False,
        )

    effective_path = answer.effective_grading_path
    child_values_raw = payload.get("values", [])
    child_values: list[str] = []
    if isinstance(child_values_raw, list):
        child_values = [str(v) for v in child_values_raw]

    marks_per_blank = _half(float(total) / n_blanks)
    awarded = Decimal("0")
    all_matched = True
    matched_alternatives: list[str | None] = []
    any_unmatched = False

    for i, blank in enumerate(answer.blanks):
        child_raw = child_values[i].strip() if i < len(child_values) else ""
        matched_alt: str | None = None

        if blank.value_type == "number":
            # AUTO path: numeric compare.
            matched = _numeric_match(child_raw, blank.accepted, None)
            if matched:
                awarded += marks_per_blank
                matched_alt = child_raw
            else:
                all_matched = False
        else:
            # AUTO_FUZZY path: normalised-exact match.
            child_norm = _normalize(child_raw)
            match_found = False
            for acc in blank.accepted:
                if _normalize(acc) == child_norm:
                    awarded += marks_per_blank
                    matched_alt = acc
                    match_found = True
                    break
            if not match_found:
                all_matched = False
                any_unmatched = True

        matched_alternatives.append(matched_alt)

    # Clamp.
    if awarded > total:
        awarded = total

    if effective_path == GradingPath.AUTO:
        # Pure-AUTO: finalise immediately.
        return QuestionMark(
            family_id=family_id,
            submission_id=submission_id,
            question_id=question.qid,
            marks_total=total,
            suggested_marks=awarded,
            final_marks=awarded,
            grading_path=GradingPath.AUTO,
            confidence=Decimal("1"),
            needs_review=False,
            matched_alternative=(", ".join(a for a in matched_alternatives if a) or None),
            error_category=None if all_matched else ErrorCategory.CARELESS,
        )
    else:
        # AUTO_FUZZY: NEVER auto-finalize a zero — parent decides.
        if any_unmatched:
            return QuestionMark(
                family_id=family_id,
                submission_id=submission_id,
                question_id=question.qid,
                marks_total=total,
                suggested_marks=Decimal("0"),
                final_marks=None,
                grading_path=GradingPath.AUTO_FUZZY,
                confidence=Decimal("0.5"),
                needs_review=True,
                matched_alternative=None,
                error_category=None,
            )
        else:
            # All word blanks matched — full or partial marks, no review needed.
            return QuestionMark(
                family_id=family_id,
                submission_id=submission_id,
                question_id=question.qid,
                marks_total=total,
                suggested_marks=awarded,
                final_marks=awarded,
                grading_path=GradingPath.AUTO_FUZZY,
                confidence=Decimal("1"),
                needs_review=False,
                matched_alternative=(", ".join(a for a in matched_alternatives if a) or None),
            )


def _grade_short_answer(
    question: Question,
    answer: ShortAnswerSpec,
    payload: dict[str, Any],
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> QuestionMark:
    """AUTO_FUZZY: normalised-exact match against accepted alternatives.

    Match → full marks, needs_review=False.
    No match → suggested_marks=0, needs_review=True, final_marks=NULL.
    NEVER auto-finalize a zero.
    """
    total = _d(question.mark_rules.total)
    child_raw = str(payload.get("text", "")).strip()
    child_norm = _normalize(child_raw)

    matched_alt: str | None = None
    for acc in answer.accepted:
        if _normalize(acc) == child_norm:
            matched_alt = acc
            break

    if matched_alt is not None:
        return QuestionMark(
            family_id=family_id,
            submission_id=submission_id,
            question_id=question.qid,
            marks_total=total,
            suggested_marks=total,
            final_marks=total,
            grading_path=GradingPath.AUTO_FUZZY,
            confidence=Decimal("1"),
            needs_review=False,
            matched_alternative=matched_alt,
        )
    else:
        return QuestionMark(
            family_id=family_id,
            submission_id=submission_id,
            question_id=question.qid,
            marks_total=total,
            suggested_marks=Decimal("0"),
            final_marks=None,
            grading_path=GradingPath.AUTO_FUZZY,
            confidence=Decimal("0.5"),
            needs_review=True,
        )


def _make_claude_assist_mark(
    question: Question,
    suggestion: FakeGraderSuggestion,
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> QuestionMark:
    """Build a QuestionMark from a FakeGrader suggestion."""
    return QuestionMark(
        family_id=family_id,
        submission_id=submission_id,
        question_id=question.qid,
        marks_total=_d(question.mark_rules.total),
        suggested_marks=suggestion.suggested_marks,
        final_marks=None,  # always NULL until parent reviews
        grading_path=GradingPath.CLAUDE_ASSIST,
        confidence=None,
        needs_review=True,
        ai_rationale=suggestion.ai_rationale,
    )


def _make_not_attempted(
    question: Question,
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
    grading_path: GradingPath,
) -> QuestionMark:
    """A question that was skipped / not attempted."""
    return QuestionMark(
        family_id=family_id,
        submission_id=submission_id,
        question_id=question.qid,
        marks_total=_d(question.mark_rules.total),
        suggested_marks=Decimal("0"),
        final_marks=Decimal("0"),
        grading_path=grading_path,
        confidence=Decimal("1"),
        needs_review=False,
        error_category=ErrorCategory.NOT_ATTEMPTED,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def grade_submission(
    assessment: Assessment,
    responses: list[ChildResponseItem],
    *,
    family_id: uuid.UUID,
    submission_id: uuid.UUID,
    grader: FakeGrader | None = None,
) -> list[QuestionMark]:
    """Grade every question in *assessment* against *responses*.

    Returns a list of ``QuestionMark``, one per question in assessment order.
    Proof photos are NEVER accessed (ARCHITECTURE.md §10).

    Args:
        assessment: the full validated assessment (includes answer keys).
        responses: the child's per-question responses (from ChildResponseItem list).
        family_id: the RLS tenant id (from server-side identity, never client).
        submission_id: the persisted submission id.
        grader: optional FakeGrader instance; a new one is created if not supplied.
    """
    if grader is None:
        grader = FakeGrader()

    # Build a fast lookup: qid → response.
    response_map: dict[str, ChildResponseItem] = {r.qid: r for r in responses}

    # Collect all CLAUDE_ASSIST questions for the batched call.
    claude_questions: list[tuple[Question, ChildResponseItem]] = []
    all_questions: list[Question] = [
        q for section in assessment.sections for q in section.questions
    ]

    # We need to identify CLAUDE_ASSIST questions before grading them,
    # to batch them.  We'll do two passes.
    claude_assist_qids: set[str] = set()
    for question in all_questions:
        response = response_map.get(question.qid)
        attempted = response is not None and response.attempted and bool(response.payload)
        if not attempted:
            continue
        if question.grading_path == GradingPath.CLAUDE_ASSIST:
            assert response is not None  # noqa: S101 — guaranteed above
            claude_questions.append((question, response))
            claude_assist_qids.add(question.qid)

    # Batched FakeGrader call (one per submission).
    suggestions: dict[str, FakeGraderSuggestion] = {}
    if claude_questions:
        suggestion_list, call_log = grader.grade_batch(claude_questions, submission_id)
        log.info(
            "Grading Claude-assist batch: submission=%s model=%s tokens_in=%d "
            "tokens_out=%d latency_ms=%.2f",
            submission_id,
            call_log.model,
            call_log.prompt_tokens,
            call_log.completion_tokens,
            call_log.latency_ms,
        )
        for s in suggestion_list:
            suggestions[s.question_id] = s

    # Now grade every question.
    marks: list[QuestionMark] = []
    for question in all_questions:
        response = response_map.get(question.qid)
        attempted = response is not None and response.attempted and bool(response.payload)
        grading_path = question.grading_path
        answer = question.answer

        # --- Not attempted ---
        if not attempted:
            marks.append(_make_not_attempted(question, family_id, submission_id, grading_path))
            continue

        assert response is not None  # noqa: S101 — guaranteed by attempted check
        payload = response.payload

        # --- CLAUDE_ASSIST ---
        if grading_path == GradingPath.CLAUDE_ASSIST:
            suggestion = suggestions.get(question.qid)
            if suggestion is None:
                # Fallback: should not happen if batch was built correctly.
                log.warning(
                    "Missing FakeGrader suggestion for qid=%s submission=%s",
                    question.qid,
                    submission_id,
                )
                suggestion = FakeGraderSuggestion(
                    question_id=question.qid,
                    suggested_marks=Decimal("0"),
                    marks_total=_d(question.mark_rules.total),
                    ai_rationale="FakeGrader: no suggestion available (fallback).",
                )
            marks.append(_make_claude_assist_mark(question, suggestion, family_id, submission_id))
            continue

        # --- AUTO and AUTO_FUZZY ---
        if isinstance(answer, McqAnswer):
            marks.append(_grade_mcq(question, answer, payload, family_id, submission_id))
        elif isinstance(answer, TrueFalseAnswer):
            marks.append(_grade_true_false(question, answer, payload, family_id, submission_id))
        elif isinstance(answer, MatchingAnswer):
            marks.append(_grade_matching(question, answer, payload, family_id, submission_id))
        elif isinstance(answer, OrderingAnswer):
            marks.append(_grade_ordering(question, answer, payload, family_id, submission_id))
        elif isinstance(answer, FillBlankAnswer):
            marks.append(_grade_fill_blank(question, answer, payload, family_id, submission_id))
        elif isinstance(answer, ShortAnswerSpec):
            marks.append(_grade_short_answer(question, answer, payload, family_id, submission_id))
        else:
            # Safety net: unknown answer type with a non-CLAUDE_ASSIST path.
            # Should be unreachable given exhaustive grading_path mapping.
            log.error(  # noqa: LOG015
                "Unhandled answer type %s for qid=%s — defaulting to 0 marks",
                type(answer).__name__,
                question.qid,
            )
            marks.append(_make_not_attempted(question, family_id, submission_id, grading_path))

    return marks
