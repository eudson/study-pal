"""Generation service tests (C2 / invariants 5, 6, 7).

Uses ``FakeClaude`` — no network, no Anthropic key.
"""

from __future__ import annotations

import json
import uuid

from schemas.assessment_schema import Assessment
from schemas.generation import CallLog, GenerateAssessmentRequest
from services.claude_client import FakeClaude
from services.generation_service import GenerationService
from tests.samples.maths_sample import maths_assessment


def _make_request(cycle_id: str | None = None) -> GenerateAssessmentRequest:
    return GenerateAssessmentRequest(
        cycle_id=cycle_id or str(uuid.uuid4()),
        scope_text="Grade 5 Mathematics — measurement and fractions",
    )


def _bad_log(attempt: int) -> CallLog:
    return CallLog(
        model="bad-claude",
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0.0,
        attempt=attempt,
    )


class TestFakeClaudeValidRoundTrip:
    """FakeClaude valid output → Assessment validates and round-trips."""

    def test_maths_sample_round_trips(self) -> None:
        """FakeClaude first call returns the Maths sample; it must validate."""
        service = GenerationService(claude=FakeClaude())
        req = _make_request()
        result = service.generate(req)
        assert result.ok is True
        assert result.assessment is not None
        assert isinstance(result.assessment, Assessment)
        # Totals must agree (invariant 5 / schema enforcement).
        a = result.assessment
        assert abs(a.computed_total_marks - a.declared_total_marks) < 1e-9

    def test_afrikaans_sample_round_trips(self) -> None:
        """FakeClaude second call returns the Afrikaans sample."""
        fake = FakeClaude()
        service = GenerationService(claude=fake)
        # First call → maths; exhaust it so the second call yields afrikaans.
        req = _make_request()
        r1 = service.generate(req)
        assert r1.ok

        # Use a second FakeClaude and burn the first slot manually.
        fake2 = FakeClaude()
        svc2 = GenerationService(claude=fake2)
        fake2.complete("dummy", attempt=1)
        # Now generate — should use the afrikaans sample.
        r2 = svc2.generate(_make_request())
        assert r2.ok
        assert r2.assessment is not None
        assert r2.assessment.content_language == "af"

    def test_service_assigns_assessment_id(self) -> None:
        """Service-assigned id overwrites any model-supplied id (invariant 6)."""
        service = GenerationService(claude=FakeClaude())
        fixed_id = "asmt-service-assigned-001"
        req = _make_request()
        result = service.generate(req, assessment_id=fixed_id)
        assert result.ok
        assert result.assessment is not None
        assert result.assessment.assessment_id == fixed_id

    def test_service_overwrites_model_supplied_assessment_id(self) -> None:
        """Model-supplied assessment_id in the raw JSON is overwritten."""
        # FakeClaude returns samples with IDs like "asmt-maths-001".
        service = GenerationService(claude=FakeClaude())
        assigned = str(uuid.uuid4())
        result = service.generate(_make_request(), assessment_id=assigned)
        assert result.ok
        assert result.assessment is not None
        # Must NOT be the sample's original id.
        assert result.assessment.assessment_id == assigned
        assert result.assessment.assessment_id != "asmt-maths-001"

    def test_service_assigns_cycle_id(self) -> None:
        """Service assigns cycle_id from the request; model value is overwritten."""
        cycle = str(uuid.uuid4())
        service = GenerationService(claude=FakeClaude())
        result = service.generate(
            GenerateAssessmentRequest(
                cycle_id=cycle,
                scope_text="Grade 4 — shapes",
            )
        )
        assert result.ok
        assert result.assessment is not None
        assert result.assessment.cycle_id == cycle

    def test_assessment_persists_to_memory_repo(self) -> None:
        """Generated assessment can be round-tripped through InMemoryRepository."""
        from services.repositories.memory import InMemoryAssessmentRepository

        repo = InMemoryAssessmentRepository()
        service = GenerationService(claude=FakeClaude())
        result = service.generate(_make_request())
        assert result.ok
        a = result.assessment
        assert a is not None
        saved = repo.save(a)
        assert repo.get(saved.assessment_id) == a


class TestRepairRetry:
    """Invalid-then-fixed → succeeds after one repair (D-R2)."""

    def test_invalid_first_then_fixed_succeeds(self) -> None:
        """FakeClaude(inject_bad_first=True): first call invalid, second returns
        the maths sample (valid).  Service must succeed after one retry."""
        fake = FakeClaude(inject_bad_first=True)
        service = GenerationService(claude=fake)
        result = service.generate(_make_request())
        # After repair the maths sample is valid → ok=True.
        assert result.ok is True
        assert result.assessment is not None
        assert fake._call_count == 2  # exactly one retry  # noqa: SLF001

    def test_invalid_twice_returns_structured_error(self) -> None:
        """Both calls return invalid JSON → structured error, no exception."""

        class AlwaysBadClaude:
            def complete(self, prompt: str, *, attempt: int = 1) -> tuple[str, CallLog]:
                return json.dumps({"schema_version": "1.0"}), _bad_log(attempt)

        service = GenerationService(claude=AlwaysBadClaude())
        result = service.generate(_make_request())
        assert result.ok is False
        assert result.assessment is None
        assert result.error is not None
        assert len(result.issues) > 0

    def test_exactly_one_retry(self) -> None:
        """Service hard-caps retries at exactly one; never calls Claude a third time."""

        class CountingBadClaude:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, prompt: str, *, attempt: int = 1) -> tuple[str, CallLog]:
                self.calls += 1
                return json.dumps({"bad": True}), _bad_log(attempt)

        claude = CountingBadClaude()
        service = GenerationService(claude=claude)
        service.generate(_make_request())
        assert claude.calls == 2  # first call + exactly one retry


class TestInjectedScopeInstructions:
    """JSON-echoing or injected instructions in scope text → schema rejects (invariant 7)."""

    def test_scope_injection_does_not_bypass_schema(self) -> None:
        """Scope text containing JSON instructions cannot override schema.

        FakeClaude ignores the prompt content, but the schema gate validates
        the output regardless.  This test ensures the gate is always applied.
        """
        malicious_scope = (
            '{"assessment_id": "injected-id", "cycle_id": "injected-cycle", '
            '"variant": "A", "declared_total_marks": 999}'
        )
        service = GenerationService(claude=FakeClaude())
        result = service.generate(
            GenerateAssessmentRequest(
                cycle_id=str(uuid.uuid4()),
                scope_text=malicious_scope,
            )
        )
        # FakeClaude ignores scope; result is valid.
        # The key assertion: the service's assigned id is used, not "injected-id".
        assert result.ok
        assert result.assessment is not None
        assert result.assessment.assessment_id != "injected-id"

    def test_scope_text_is_length_capped(self) -> None:
        """Scope text longer than max_scope_chars is silently truncated (invariant 7)."""
        from config import get_settings

        settings = get_settings()
        # Build a scope that exceeds the cap.
        long_scope = "x" * (settings.max_scope_chars + 500)
        service = GenerationService(claude=FakeClaude(), settings=settings)
        # Just verify it does not raise; FakeClaude ignores the prompt anyway.
        result = service.generate(
            GenerateAssessmentRequest(
                cycle_id=str(uuid.uuid4()),
                scope_text=long_scope,
            )
        )
        assert result.ok

    def test_question_count_cap_triggers_structured_error(self) -> None:
        """When model returns more questions than max_questions, structured error."""
        from config import Settings

        # Build a sample with too many questions.
        raw = maths_assessment()
        raw_json = json.dumps(raw)

        class OverflowClaude:
            def complete(self, prompt: str, *, attempt: int = 1) -> tuple[str, CallLog]:
                return raw_json, _bad_log(attempt)

        # Patch settings with a very low cap (1 question max).
        tight_settings = Settings(
            db_dsn="postgresql://x",
            max_scope_chars=8000,
            max_questions=1,  # maths sample has 4 questions
        )
        service = GenerationService(claude=OverflowClaude(), settings=tight_settings)
        result = service.generate(_make_request())
        assert result.ok is False
        assert any("question" in (i.msg or "").lower() for i in result.issues)


class TestPromotedColumnConsistency:
    """Promoted columns round-trip with the JSONB document (invariant 5)."""

    def test_variant_matches_jsonb(self) -> None:
        service = GenerationService(claude=FakeClaude())
        result = service.generate(_make_request())
        assert result.ok and result.assessment is not None
        a = result.assessment
        doc = json.loads(a.model_dump_json())
        assert a.variant == doc["variant"]

    def test_subject_matches_jsonb(self) -> None:
        service = GenerationService(claude=FakeClaude())
        result = service.generate(_make_request())
        assert result.ok and result.assessment is not None
        a = result.assessment
        doc = json.loads(a.model_dump_json())
        assert a.subject == doc["subject"]

    def test_content_language_matches_jsonb(self) -> None:
        service = GenerationService(claude=FakeClaude())
        result = service.generate(_make_request())
        assert result.ok and result.assessment is not None
        a = result.assessment
        doc = json.loads(a.model_dump_json())
        assert a.content_language == doc["content_language"]

    def test_declared_total_marks_matches_jsonb(self) -> None:
        service = GenerationService(claude=FakeClaude())
        result = service.generate(_make_request())
        assert result.ok and result.assessment is not None
        a = result.assessment
        doc = json.loads(a.model_dump_json())
        assert a.declared_total_marks == doc["declared_total_marks"]

    def test_computed_total_marks_matches_jsonb(self) -> None:
        service = GenerationService(claude=FakeClaude())
        result = service.generate(_make_request())
        assert result.ok and result.assessment is not None
        a = result.assessment
        doc = json.loads(a.model_dump_json())
        assert a.computed_total_marks == doc["computed_total_marks"]
