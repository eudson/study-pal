"""In-memory ``AssessmentRepository`` implementation.

The only persistence implementation in this bootstrap milestone. No
database, no drivers — a process-local dict keyed by ``assessment_id``.
"""

from schemas.assessment_schema import Assessment


class InMemoryAssessmentRepository:
    def __init__(self) -> None:
        self._store: dict[str, Assessment] = {}

    def save(self, assessment: Assessment) -> Assessment:
        self._store[assessment.assessment_id] = assessment
        return assessment

    def get(self, assessment_id: str) -> Assessment | None:
        return self._store.get(assessment_id)

    def list(self) -> list[Assessment]:
        return list(self._store.values())
