"""Phase 5 — study pack generation service.

``FakeStudyPack`` is a deterministic fake that models ONE generation call
(ARCHITECTURE.md §8: one call per generation artefact).  It takes a GapReport,
derives practice items strictly from growing items' gap_tags, and returns a
validated StudyPack Pydantic model.

Design rules:
- Subject-agnostic: no ``if subject ==`` branches (golden rule 4).
  Logic keys only on gap_tags and question_type; subjects play no role.
- Exactly one call per artefact (§8): FakeStudyPack.generate() models the
  single future ClaudeClient call.  Swapping in real Claude changes zero
  call topology.
- Logs a token-usage line per §8 ("log tokens per call") with 0 actual tokens
  — this mirrors FakeGrader's convention and keeps the logging contract intact
  for when the real client is wired.
- No WeasyPrint import: generation ≠ rendering (kept strictly separate so the
  PDF renderer can be swapped without touching this module).
- Pydantic at every boundary: returns StudyPack (never a bare dict).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

from schemas.gap_report import GapReport, GapStatus
from schemas.study_pack import StudyPack, StudyPackItem

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FakeStudyPack
# ---------------------------------------------------------------------------


class FakeStudyPack:
    """Deterministic stand-in for a real Claude-powered study pack generator.

    Models ONE generation call per artefact (ARCHITECTURE.md §8).
    All items are derived strictly from growing GapReportItems and their
    gap_tags — no subject-specific logic.

    The generated items are question-type-driven:
    - Tags cluster into one item per distinct gap_tag.
    - Each item's ``question_type`` is set to ``"short_answer"`` (a safe
      subject-agnostic default; live Claude would select per-tag).
    - Prompt, answer, hint, and worked_example are deterministic strings
      seeded by the tag name so tests can assert on structure without
      depending on real LLM output.

    Swapping in real Claude: replace FakeStudyPack with a StudyPackService
    that calls ClaudeClient.complete() once, parses the JSON response, and
    validates through StudyPack.model_validate() (same retry-on-failure
    pattern as GenerationService).
    """

    def generate(
        self,
        gap_report: GapReport,
    ) -> StudyPack:
        """Derive a StudyPack from a GapReport.

        Args:
            gap_report: The validated GapReport for the cycle.

        Returns:
            A StudyPack containing one practice item per distinct growing
            gap_tag (or one generic item when there are growing items but
            no tags), plus a short summary.

        Note:
            §8 token logging: this method logs a zero-token CallLog-style
            record to honour the "log tokens per call" convention; the real
            client replaces this with actual token counts.
        """
        t0 = time.monotonic()

        growing_items = [it for it in gap_report.items if it.status == GapStatus.GROWING]

        # Collect distinct gap_tags from growing items (preserve sorted order
        # for determinism, matching GapReportSummary.growing_gap_tags).
        tags_seen: set[str] = set()
        ordered_tags: list[str] = []
        for gap_item in growing_items:
            for tag in gap_item.gap_tags:
                if tag not in tags_seen:
                    tags_seen.add(tag)
                    ordered_tags.append(tag)

        items: list[StudyPackItem] = []

        if ordered_tags:
            # One practice item per distinct growing gap_tag.
            for tag in ordered_tags:
                practice_item = _make_item_for_tag(tag)
                items.append(practice_item)
        elif growing_items:
            # Growing items exist but carry no gap_tags — produce one generic item.
            items.append(_make_generic_item())

        # Summary uses the count of growing items and tags — no subject references.
        if items:
            summary = (
                f"This practice pack targets {len(items)} area(s) where you are still growing. "
                "Work through each question carefully — mistakes are just the next step forward."
            )
        else:
            summary = (
                "Great work — no growing gaps were found in this cycle. "
                "This pack is empty; keep it up for Variant B!"
            )

        derived_tags = sorted(tags_seen)

        latency_ms = (time.monotonic() - t0) * 1000

        # §8: log model, tokens in/out, latency on every call (fake = 0 tokens).
        log.info(
            "FakeStudyPack.generate: cycle=%s model=fake-study-pack "
            "prompt_tokens=0 completion_tokens=0 latency_ms=%.2f items=%d tags=%d",
            gap_report.cycle_id,
            latency_ms,
            len(items),
            len(derived_tags),
        )

        return StudyPack(
            cycle_id=gap_report.cycle_id,
            assessment_id=gap_report.assessment_id,
            items=items,
            summary=summary,
            derived_from_gap_tags=derived_tags,
            generated_at=datetime.now(tz=UTC),
        )


# ---------------------------------------------------------------------------
# Item builder helpers (pure, deterministic, subject-agnostic)
# ---------------------------------------------------------------------------


def _make_item_for_tag(tag: str) -> StudyPackItem:
    """Build a deterministic practice item for a single gap_tag.

    The content is templated off the tag name so it is stable and testable
    without an LLM.  Live Claude would replace the prompt/answer/hint with
    pedagogically rich content for the specific concept.
    """
    item_id = f"sp-{uuid.uuid5(uuid.NAMESPACE_DNS, tag)}"
    return StudyPackItem(
        item_id=item_id,
        gap_tags=[tag],
        question_type="short_answer",
        prompt=(
            f"Practice question for: {tag}.\n\n"
            "Show all your working clearly and write your answer in the space below."
        ),
        worked_example=(
            f"Worked example for {tag}: "
            "Read the question carefully, identify the key information, "
            "apply the relevant concept step by step, then state your answer clearly."
        ),
        answer=f"[Model answer for {tag} — to be completed by parent/tutor before printing.]",
        hint=f"Hint: think about what you know about {tag} and how it applies here.",
    )


def _make_generic_item() -> StudyPackItem:
    """Build a single generic practice item for growing items with no gap_tags."""
    return StudyPackItem(
        item_id=f"sp-generic-{uuid.uuid4()}",
        gap_tags=["general"],
        question_type="short_answer",
        prompt=(
            "Practice question: review the questions you found challenging "
            "in your assessment and try a similar problem below. "
            "Show all your working."
        ),
        worked_example=(
            "Worked example: break the problem into steps, show each step clearly, "
            "and double-check your answer."
        ),
        answer="[Model answer — to be completed by parent/tutor before printing.]",
        hint="Hint: go back to the original question and look at what was being tested.",
    )
