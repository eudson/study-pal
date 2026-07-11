"""Repository protocol for persisting ``Assessment`` models.

Typed purely in terms of ``Assessment`` — no bare dicts crossing this
boundary (ARCHITECTURE.md §8). A Postgres-backed implementation can drop in
later with zero change to callers, as long as it satisfies this Protocol.
"""

from typing import Protocol, runtime_checkable

from schemas.assessment_schema import Assessment


@runtime_checkable
class AssessmentRepository(Protocol):
    def save(self, assessment: Assessment) -> Assessment: ...

    def get(self, assessment_id: str) -> Assessment | None: ...

    def list(self) -> list[Assessment]: ...
