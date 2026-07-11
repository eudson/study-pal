"""FastAPI dependency providers.

Kept simple and typed: a process-singleton in-memory repository stands in
for the eventual Postgres-backed one (ARCHITECTURE.md §4 exit strategy —
callers only ever depend on the ``AssessmentRepository`` protocol).
"""

from functools import lru_cache

from services.repositories.base import AssessmentRepository
from services.repositories.memory import InMemoryAssessmentRepository


@lru_cache
def get_assessment_repository() -> AssessmentRepository:
    return InMemoryAssessmentRepository()
