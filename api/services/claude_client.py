"""``ClaudeClient`` interface + ``FakeClaude`` deterministic implementation.

All Claude calls are made through this interface (ARCHITECTURE.md §8).
``FakeClaude`` returns the in-tree Maths/Afrikaans sample assessments in
round-robin — fully deterministic, zero network, zero tokens.

Invariant 7: scope text is length-capped by ``GenerationService`` before
reaching the client; the client itself does not enforce the cap (single
responsibility).
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from schemas.generation import CallLog


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
        else:
            # Alternate between the two samples.
            samples = [maths_assessment(), afrikaans_assessment()]
            raw = json.dumps(samples[(self._call_count - 1) % len(samples)])

        latency = (time.monotonic() - t0) * 1000
        log = CallLog(
            model="fake-claude",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=round(latency, 2),
            attempt=attempt,
        )
        return raw, log
