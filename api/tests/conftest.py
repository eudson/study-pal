import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


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
