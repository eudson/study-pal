"""Assessment generation service (C2).

Design contract:
- Calls Claude exactly once per generation; if schema validation fails, exactly
  one repair retry (hard cap); then structured error.  Invariant D-R2.
- Routes ALL output through ``validation_service.validate_assessment`` — the
  single validation gate.  Never bypasses it.  Invariant 6.
- Service assigns ``assessment_id`` and ``cycle_id``; overwrites anything the
  model supplies.  Invariant 6.
- Scope text is length-capped before reaching the prompt.  Invariant 7.
- Output question count is capped after parsing.  Invariant 7.
- Logs model, tokens, latency for every call (§8).
- Subject/language-agnostic — no ``if subject == ...`` branches.  Golden rule 4.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from config import Settings, get_settings
from schemas.assessment_schema import Assessment, VariantBRequest
from schemas.generation import CallLog, GenerateAssessmentRequest, GenerateAssessmentResponse
from schemas.validation import ValidationResult
from services.claude_client import ClaudeClient
from services.validation_service import validate_assessment

logger = logging.getLogger(__name__)

_PROMPT_PATH = (
    __file__[: __file__.rfind("services/generation_service.py")]
    + "services/prompts/generate_assessment_v1.md"
)

_VARIANT_B_PROMPT_PATH = (
    __file__[: __file__.rfind("services/generation_service.py")]
    + "services/prompts/generate_variant_b_v1.md"
)


def _load_prompt_template() -> str:
    with open(_PROMPT_PATH, encoding="utf-8") as fh:
        return fh.read()


def _load_variant_b_prompt_template() -> str:
    with open(_VARIANT_B_PROMPT_PATH, encoding="utf-8") as fh:
        return fh.read()


def _build_prompt(scope_text: str, max_questions: int) -> str:
    template = _load_prompt_template()
    prompt = template.replace("{{SCOPE_TEXT}}", scope_text)
    prompt = prompt.replace("{{MAX_QUESTIONS}}", str(max_questions))
    return prompt


def _build_variant_b_prompt(request: VariantBRequest) -> str:
    template = _load_variant_b_prompt_template()
    prompt = template.replace(
        "{{SOURCE_ASSESSMENT_JSON}}", request.source_assessment.model_dump_json(indent=2)
    )
    prompt = prompt.replace(
        "{{GAPS_JSON}}",
        json.dumps([g.model_dump(mode="json") for g in request.gaps], indent=2),
    )
    prompt = prompt.replace("{{NOTE}}", request.note)
    return prompt


def _extract_json(raw: str) -> dict[str, object]:
    """Extract JSON object from the model response.

    The model is instructed to return bare JSON, but may wrap it in fences.
    """
    # Strip leading/trailing whitespace and optional markdown fences.
    stripped = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()
    return json.loads(stripped)  # type: ignore[no-any-return]


def _count_questions(raw: dict[str, object]) -> int:
    total = 0
    sections = raw.get("sections", [])
    if isinstance(sections, list):
        for section in sections:
            if isinstance(section, dict):
                qs = section.get("questions", [])
                if isinstance(qs, list):
                    total += len(qs)
    return total


def _log_call(log: CallLog) -> None:
    logger.info(
        "claude_call attempt=%d model=%s prompt_tokens=%d completion_tokens=%d latency_ms=%.1f",
        log.attempt,
        log.model,
        log.prompt_tokens,
        log.completion_tokens,
        log.latency_ms,
    )


class GenerationService:
    """Turns a ``GenerateAssessmentRequest`` into a ``GenerateAssessmentResponse``.

    Exactly one ``ClaudeClient`` instance is injected; all retries use the same
    instance (so ``FakeClaude`` counter increments correctly in tests).
    """

    def __init__(
        self,
        claude: ClaudeClient,
        settings: Settings | None = None,
    ) -> None:
        self._claude = claude
        self._settings = settings or get_settings()

    def generate(
        self,
        request: GenerateAssessmentRequest,
        *,
        assessment_id: str | None = None,
    ) -> GenerateAssessmentResponse:
        """Generate and validate an assessment; return structured result.

        ``assessment_id`` is always assigned by this service; any value the
        model returns is overwritten (invariant 6).
        """
        settings = self._settings
        assigned_id = assessment_id or str(uuid.uuid4())

        # Invariant 7: cap scope text length.
        scope = request.scope_text[: settings.max_scope_chars]

        prompt = _build_prompt(scope, settings.max_questions)

        # --- First call ---
        raw_str, log1 = self._claude.complete(prompt, attempt=1)
        _log_call(log1)

        result, raw_doc = self._try_validate(raw_str, assigned_id, request.cycle_id, settings)

        if result.valid and raw_doc is not None:
            assessment = Assessment.model_validate(raw_doc)
            return GenerateAssessmentResponse(ok=True, assessment=assessment)

        # --- Repair retry (hard cap: exactly one) ---
        repair_prompt = self._build_repair_prompt(prompt, raw_str, result)
        raw_str2, log2 = self._claude.complete(repair_prompt, attempt=2)
        _log_call(log2)

        result2, raw_doc2 = self._try_validate(raw_str2, assigned_id, request.cycle_id, settings)

        if result2.valid and raw_doc2 is not None:
            assessment = Assessment.model_validate(raw_doc2)
            return GenerateAssessmentResponse(ok=True, assessment=assessment)

        # --- Structured error after two failures ---
        return GenerateAssessmentResponse(
            ok=False,
            issues=result2.issues,
            error=(
                "Assessment generation failed schema validation after "
                "one repair attempt. See issues for details."
            ),
        )

    def generate_variant_b(
        self,
        request: VariantBRequest,
        *,
        assessment_id: str | None = None,
    ) -> GenerateAssessmentResponse:
        """Generate and validate a Variant-B retest; return structured result.

        Mirrors ``generate()`` exactly (one Claude call, validate, one repair
        retry, structured error) but targets the Variant-B prompt and hard-sets
        ``variant="B"`` on the output (invariant 6 extension — the model must
        not decide the variant either).  ``cycle_id`` is taken from the source
        assessment (Variant B is same-cycle, ARCHITECTURE.md §5).
        """
        settings = self._settings
        assigned_id = assessment_id or str(uuid.uuid4())
        cycle_id = request.source_assessment.cycle_id

        prompt = _build_variant_b_prompt(request)

        # --- First call ---
        raw_str, log1 = self._claude.complete(prompt, attempt=1)
        _log_call(log1)

        result, raw_doc = self._try_validate(raw_str, assigned_id, cycle_id, settings, variant="B")

        if result.valid and raw_doc is not None:
            assessment = Assessment.model_validate(raw_doc)
            return GenerateAssessmentResponse(ok=True, assessment=assessment)

        # --- Repair retry (hard cap: exactly one) ---
        repair_prompt = self._build_repair_prompt(prompt, raw_str, result)
        raw_str2, log2 = self._claude.complete(repair_prompt, attempt=2)
        _log_call(log2)

        result2, raw_doc2 = self._try_validate(
            raw_str2, assigned_id, cycle_id, settings, variant="B"
        )

        if result2.valid and raw_doc2 is not None:
            assessment = Assessment.model_validate(raw_doc2)
            return GenerateAssessmentResponse(ok=True, assessment=assessment)

        # --- Structured error after two failures ---
        return GenerateAssessmentResponse(
            ok=False,
            issues=result2.issues,
            error=(
                "Variant B generation failed schema validation after "
                "one repair attempt. See issues for details."
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_validate(
        self,
        raw_str: str,
        assessment_id: str,
        cycle_id: str,
        settings: Settings,
        *,
        variant: str = "A",
    ) -> tuple[ValidationResult, dict[str, object] | None]:
        """Parse JSON, enforce caps, inject service-assigned ids, validate."""
        try:
            raw_doc = _extract_json(raw_str)
        except (json.JSONDecodeError, ValueError) as exc:
            from schemas.validation import ValidationIssue
            from schemas.validation import ValidationResult as VR

            return (
                VR(
                    valid=False,
                    schema_version="1.0",
                    issues=[
                        ValidationIssue(
                            loc=["__root__"],
                            msg=f"JSON parse error: {exc}",
                            type="json_parse_error",
                        )
                    ],
                ),
                None,
            )

        # Invariant 7: cap question count.
        count = _count_questions(raw_doc)
        if count > settings.max_questions:
            from schemas.validation import ValidationIssue
            from schemas.validation import ValidationResult as VR

            return (
                VR(
                    valid=False,
                    schema_version="1.0",
                    issues=[
                        ValidationIssue(
                            loc=["sections"],
                            msg=(
                                f"Output has {count} questions; "
                                f"max allowed is {settings.max_questions}."
                            ),
                            type="question_count_exceeded",
                        )
                    ],
                ),
                None,
            )

        # Invariant 6: service assigns ids — overwrite anything the model supplied.
        raw_doc["assessment_id"] = assessment_id
        raw_doc["cycle_id"] = cycle_id
        raw_doc["variant"] = variant

        result = validate_assessment(raw_doc)
        if result.valid:
            return result, raw_doc
        return result, None

    @staticmethod
    def _build_repair_prompt(
        original_prompt: str,
        bad_output: str,
        result: ValidationResult,
    ) -> str:
        issue_lines = "\n".join(f"- loc={issue.loc} msg={issue.msg}" for issue in result.issues)
        return (
            f"{original_prompt}\n\n"
            "## Your previous output failed schema validation. Issues found:\n\n"
            f"{issue_lines}\n\n"
            "## Your previous (invalid) output was:\n\n"
            f"{bad_output}\n\n"
            "Please produce a corrected JSON object that fixes all issues above. "
            "Return only valid JSON."
        )
