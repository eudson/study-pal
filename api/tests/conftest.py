"""Pytest configuration and shared fixtures.

The ``client`` fixture overrides ``get_assessment_repository`` with the
``InMemoryAssessmentRepository`` so unit tests never require a Postgres
connection.  The RLS tier (test_rls_isolation.py) manages its own
connections independently and is skipped when Postgres is unreachable.
"""

from __future__ import annotations

import copy
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dependencies import get_assessment_repository
from main import app
from services.repositories.base import AssessmentRepository
from services.repositories.memory import InMemoryAssessmentRepository


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """TestClient with Postgres dependency overridden by InMemory repo."""
    repo = InMemoryAssessmentRepository()

    def _override() -> AssessmentRepository:
        return repo

    app.dependency_overrides[get_assessment_repository] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_assessment_repository, None)


def minimal_assessment() -> dict[str, Any]:
    """A dict that validates against ``schemas.assessment_schema.Assessment``.

    One Section (label "A", declared_marks 1.0) with one MCQ Question
    (mark_rules.total 1.0). All totals agree: 1.0 == 1.0.
    """
    return copy.deepcopy(
        {
            "assessment_id": "asmt-001",
            "cycle_id": "cycle-001",
            "variant": "A",
            "subject": "general",
            "content_language": "en",
            "grade_label": "Grade 5",
            "title": "Minimal Assessment",
            "duration_minutes": 30,
            "instructions": [],
            "declared_total_marks": 1.0,
            "sections": [
                {
                    "label": "A",
                    "title": "Section A",
                    "declared_marks": 1.0,
                    "questions": [
                        {
                            "qid": "A.1",
                            "number": "1",
                            "text": "What is 1 + 1?",
                            "question_type": "mcq",
                            "difficulty": "easy",
                            "answer": {
                                "kind": "mcq",
                                "options": ["1", "2", "3"],
                                "correct_index": 1,
                            },
                            "mark_rules": {"total": 1.0},
                        }
                    ],
                }
            ],
        }
    )
