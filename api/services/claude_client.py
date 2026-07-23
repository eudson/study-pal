"""``ClaudeClient`` interface + ``FakeClaude`` deterministic implementation.

All Claude calls are made through this interface (ARCHITECTURE.md §8).
``FakeClaude`` returns the in-tree Maths/Afrikaans sample assessments in
round-robin — fully deterministic, zero network, zero tokens. Round-1
questions are seeded with deterministic ``gap_tags`` (see
``_seed_deterministic_gap_tags``) so the gap report / A-vs-B comparison
pipeline has real tags to partition on when exercised end-to-end on
FakeClaude.

Invariant 7: scope text is length-capped by ``GenerationService`` before
reaching the client; the client itself does not enforce the cap (single
responsibility).
"""

from __future__ import annotations

import copy
import json
import time
from typing import Protocol, runtime_checkable

from schemas.generation import CallLog

_VARIANT_B_SOURCE_MARKER = "---BEGIN SOURCE ASSESSMENT JSON---"
_VARIANT_B_SOURCE_END = "---END SOURCE ASSESSMENT JSON---"
_VARIANT_B_GAPS_MARKER = "---BEGIN GAPS JSON---"
_VARIANT_B_GAPS_END = "---END GAPS JSON---"


@runtime_checkable
class ClaudeClient(Protocol):
    """Interface for one Claude completion call.

    ``complete`` returns the raw response string (JSON) + a ``CallLog``.
    The caller is responsible for parsing and validating the JSON.
    """

    def complete(self, prompt: str, *, attempt: int = 1) -> tuple[str, CallLog]: ...


class FakeClaude:
    """Deterministic stand-in for the real Anthropic Claude API.

    Returns the Maths sample on the first call, the Afrikaans sample on the
    second, then cycles back.  The ``CallLog`` records 0 tokens (no API call).

    ``FakeClaude`` satisfies the ``ClaudeClient`` Protocol.
    """

    def __init__(self, *, inject_bad_first: bool = False) -> None:
        """
        Args:
            inject_bad_first: if True, the very first call returns JSON that
                will fail schema validation (used in repair-retry tests).
        """
        self._call_count = 0
        self._inject_bad_first = inject_bad_first

    def complete(self, prompt: str, *, attempt: int = 1) -> tuple[str, CallLog]:
        import json

        from tests.samples.afrikaans_sample import afrikaans_assessment
        from tests.samples.maths_sample import maths_assessment

        t0 = time.monotonic()
        self._call_count += 1

        if self._inject_bad_first and self._call_count == 1:
            # Return deliberately invalid JSON (missing required fields).
            raw = json.dumps({"schema_version": "1.0", "variant": "A"})
        elif _VARIANT_B_SOURCE_MARKER in prompt:
            # Variant B retest prompt: derive deterministically from the
            # embedded source assessment + flagged gaps (see
            # generate_variant_b_v1.md).  Never touches the network.
            raw = self._complete_variant_b(prompt)
        else:
            # Alternate between the two samples.
            samples = [maths_assessment(), afrikaans_assessment()]
            doc = _seed_deterministic_gap_tags(samples[(self._call_count - 1) % len(samples)])
            raw = json.dumps(doc)

        latency = (time.monotonic() - t0) * 1000
        log = CallLog(
            model="fake-claude",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=round(latency, 2),
            attempt=attempt,
        )
        return raw, log

    @staticmethod
    def _complete_variant_b(prompt: str) -> str:
        """Deterministically derive a schema-valid Variant B doc from the prompt.

        Extracts the embedded source assessment + gaps JSON blocks (placed by
        ``_build_variant_b_prompt``) and applies a pure, deterministic
        transform: same structure (so totals/index invariants stay valid),
        surface text/values changed, and ``gap_tags`` distributed round-robin
        across questions from the flagged gaps.
        """
        source_raw = _extract_between(prompt, _VARIANT_B_SOURCE_MARKER, _VARIANT_B_SOURCE_END)
        gaps_raw = _extract_between(prompt, _VARIANT_B_GAPS_MARKER, _VARIANT_B_GAPS_END)
        source: dict[str, object] = json.loads(source_raw)
        gaps: list[dict[str, object]] = json.loads(gaps_raw) if gaps_raw.strip() else []
        doc = _derive_variant_b_doc(source, gaps)
        return json.dumps(doc)


def _seed_deterministic_gap_tags(doc: dict[str, object]) -> dict[str, object]:
    """Seed each question's ``gap_tags`` with a stable, deterministic id.

    Round-1 (Variant A / scope) generation must give every question a
    non-empty ``gap_tags`` entry so the downstream gap report and A-vs-B
    comparison (``services/gap_report.py``, ``services/comparison.py``) have
    something to partition on. The real Claude prompt asks the model to
    tag questions by the underlying concept/skill they test; ``FakeClaude``
    has no such judgement, so it derives a stand-in tag purely from
    structural metadata already present on the question — its
    ``question_type`` plus its position (section label + index within the
    section). This is:

    - Deterministic: same input dict -> same tags, every call.
    - Subject-agnostic: never reads ``subject`` / ``content_language`` /
      question text; only structural fields every question type has
      (ARCHITECTURE.md golden rule 4 — no ``if subject == ...`` logic).
    - Stable across rounds: round-2 retargeting (``_derive_variant_b_doc``)
      re-uses these exact tag strings (as ``GapRetarget.gap_id``), so a gap
      closed/persisting/newly-surfaced in round 2 is identified correctly by
      ``derive_ab_comparison``.

    Only fills in tags that are missing (empty/absent) — never overwrites a
    tag the caller already set (defensive; keeps this idempotent and safe to
    apply to any doc, including ones a future FakeClaude variant already
    tags itself).
    """
    doc = copy.deepcopy(doc)
    raw_sections = doc.get("sections")
    sections: list[object] = raw_sections if isinstance(raw_sections, list) else []
    for section_obj in sections:
        if not isinstance(section_obj, dict):
            continue
        label = str(section_obj.get("label") or "s").strip().lower() or "s"
        raw_questions = section_obj.get("questions")
        questions: list[object] = raw_questions if isinstance(raw_questions, list) else []
        for index, question_obj in enumerate(questions, start=1):
            if not isinstance(question_obj, dict):
                continue
            existing = question_obj.get("gap_tags")
            if isinstance(existing, list) and existing:
                continue
            question_type = str(question_obj.get("question_type") or "question")
            question_obj["gap_tags"] = [f"{question_type}-{label}{index}"]
    return doc


def _extract_between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start + len(start_marker) : end].strip()


def _derive_variant_b_doc(
    source: dict[str, object], gaps: list[dict[str, object]]
) -> dict[str, object]:
    """Pure, deterministic Variant-B transform of a source assessment dict.

    Preserves every structural invariant the schema checks (section/question
    counts, mark totals, index ranges) by never touching count- or
    index-sensitive fields — only surface text/values change. This guarantees
    schema validity without needing real LLM judgement, which is the point of
    a deterministic fake.
    """
    doc = copy.deepcopy(source)
    doc["variant"] = "B"
    doc["assessment_id"] = ""  # service overwrites regardless
    doc["title"] = f"{doc.get('title', 'Assessment')} — Variant B (Retest)"
    raw_instructions = doc.get("instructions")
    instructions: list[object] = (
        list(raw_instructions) if isinstance(raw_instructions, list) else []
    )
    instructions.append("This is Variant B: a retest covering the same topics with new values.")
    doc["instructions"] = instructions

    gap_ids = [str(g["gap_id"]) for g in gaps if g.get("gap_id")]

    qi = 0
    raw_sections = doc.get("sections")
    sections: list[object] = raw_sections if isinstance(raw_sections, list) else []
    for section_obj in sections:
        if not isinstance(section_obj, dict):
            continue
        raw_questions = section_obj.get("questions")
        questions: list[object] = raw_questions if isinstance(raw_questions, list) else []
        for question_obj in questions:
            if not isinstance(question_obj, dict):
                continue
            question = question_obj
            question["text"] = f"{question.get('text', '')} (Variant B)"
            question["gap_tags"] = [gap_ids[qi % len(gap_ids)]] if gap_ids else []
            qi += 1

            answer = question.get("answer")
            if not isinstance(answer, dict):
                continue
            kind = answer.get("kind")
            if kind == "mcq":
                options = answer.get("options")
                if isinstance(options, list):
                    answer["options"] = [f"{opt} (B)" for opt in options]
            elif kind == "calculation":
                number_sentence = answer.get("number_sentence")
                if number_sentence:
                    answer["number_sentence"] = f"{number_sentence} (Variant B)"
            elif kind == "matching":
                left = answer.get("left")
                if isinstance(left, list):
                    answer["left"] = [f"{item} (B)" for item in left]
            elif kind == "ordering":
                items = answer.get("items")
                if isinstance(items, list):
                    answer["items"] = [f"{item} (B)" for item in items]

    return doc
