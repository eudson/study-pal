"""Phase 1 child capture backend tests.

Test hierarchy:
1. Memo-exclusion: the serialised ChildAssessmentView must contain NONE of the
   answer/memo fields — this is the most important test in the phase.
2. capture_service unit tests: projection correctness per question type.
3. GET /cycles/{cycle_id}/capture endpoint guards.
4. POST /cycles/{cycle_id}/submissions endpoint guards and state-machine wiring.
5. InMemorySubmissionRepository direct tests.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dependencies import (
    get_assessment_repository,
    get_family_repository,
    get_submission_repository,
)
from main import app
from schemas.assessment_schema import Assessment
from schemas.capture import ChildAnswerView, SubmissionCreate
from schemas.family import CycleState
from services.capture_service import project_for_child
from services.cycle import (
    advance_to_answers_entered,
    advance_to_generating,
    advance_to_parent_reviews,
    approve_draft,
)
from services.repositories.base import FamilyRepository, SubmissionRepository
from services.repositories.memory import (
    InMemoryAssessmentRepository,
    InMemoryFamilyRepository,
    InMemorySubmissionRepository,
)
from tests.samples.maths_sample import maths_assessment

# ---------------------------------------------------------------------------
# Answer-key fields that MUST NOT appear anywhere in the child view JSON.
# This set is derived directly from assessment_schema.py by reading every
# answer model and every memo/grading-aid field.
# ---------------------------------------------------------------------------

_ANSWER_KEY_FIELDS: frozenset[str] = frozenset(
    {
        # McqAnswer
        "correct_index",
        "distractor_notes",
        # TrueFalseAnswer
        "is_true",
        "requires_correction",
        "corrected_statement",
        # MatchingAnswer
        "correct_pairs",
        # OrderingAnswer
        "correct_order",
        # FillBlankAnswer / Blank
        "accepted",
        "case_sensitive",
        # ShortAnswerSpec
        # "accepted" covered above
        "required_keywords",
        "marker_guidance",
        # CalculationAnswer
        "final_answer",
        "unit",
        "tolerance",
        "number_sentence",
        "method_steps",
        # TableCompletionAnswer / TableCell
        # "accepted" covered above
        "half_mark",
        # LabellingAnswer
        "positions",  # dict of position -> correct term
        # ExtendedResponseAnswer
        "model_answer",
        "rubric",
        "required_structure",
        # MarkRules grading aids
        "answer_marks",
        "method_marks",
        "tick_allocation",
        # Memo
        "worked_solution",
        "marker_tip",
        # Question internals never shown to child
        "gap_tags",
        "difficulty",
        "grading_path",
        # Assessment / Section / Question internals
        "schema_version",
        "computed_total_marks",
        "computed_marks",
    }
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_STUB_HEADER = str(_USER_ID)


def _make_family_repo() -> InMemoryFamilyRepository:
    return InMemoryFamilyRepository(user_id=_USER_ID)


def _full_assessment_dict() -> dict[str, Any]:
    """A multi-question-type assessment covering all answer kinds."""
    return {
        "assessment_id": "asmt-capture-001",
        "cycle_id": "cycle-capture-001",
        "variant": "A",
        "subject": "Mixed Subjects",
        "content_language": "en",
        "grade_label": "Grade 4",
        "title": "Capture Test Assessment",
        "duration_minutes": 45,
        "instructions": ["Answer all questions."],
        "declared_total_marks": 11.0,
        "sections": [
            {
                "label": "A",
                "title": "Section A",
                "instructions": "Choose the correct answer.",
                "declared_marks": 11.0,
                "questions": [
                    {
                        "qid": "A.1",
                        "number": "1",
                        "text": "What is 2+2?",
                        "question_type": "mcq",
                        "difficulty": "easy",
                        "answer": {
                            "kind": "mcq",
                            "options": ["3", "4", "5"],
                            "correct_index": 1,
                            "distractor_notes": {0: "off by one", 2: "off by one other"},
                        },
                        "mark_rules": {"total": 1.0},
                        "memo": {"marker_tip": "No partial credit."},
                    },
                    {
                        "qid": "A.2",
                        "number": "2",
                        "text": "The sun rises in the east. True or False?",
                        "question_type": "true_false",
                        "difficulty": "easy",
                        "answer": {
                            "kind": "true_false",
                            "is_true": True,
                            "requires_correction": False,
                        },
                        "mark_rules": {"total": 1.0},
                    },
                    {
                        "qid": "A.3",
                        "number": "3",
                        "text": "Match the items.",
                        "question_type": "matching",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "matching",
                            "left": ["Cat", "Dog"],
                            "right": ["Meow", "Woof"],
                            "correct_pairs": {0: 0, 1: 1},
                        },
                        "mark_rules": {"total": 1.0},
                    },
                    {
                        "qid": "A.4",
                        "number": "4",
                        "text": "Order these steps.",
                        "question_type": "ordering",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "ordering",
                            "items": ["Step C", "Step A", "Step B"],
                            "correct_order": [1, 2, 0],
                        },
                        "mark_rules": {"total": 1.0},
                    },
                    {
                        "qid": "A.5",
                        "number": "5",
                        "text": "Fill in: 1 km = ___ m",
                        "question_type": "fill_blank",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "fill_blank",
                            "blanks": [
                                {
                                    "accepted": ["1000", "1 000"],
                                    "value_type": "number",
                                    "case_sensitive": False,
                                }
                            ],
                        },
                        "mark_rules": {"total": 1.0},
                    },
                    {
                        "qid": "A.6",
                        "number": "6",
                        "text": "Name the capital of France.",
                        "question_type": "short_answer",
                        "difficulty": "easy",
                        "answer": {
                            "kind": "short_answer",
                            "accepted": ["Paris"],
                            "required_keywords": ["Paris"],
                            "marker_guidance": "Accept 'paris' (case insensitive).",
                        },
                        "mark_rules": {"total": 1.0},
                    },
                    {
                        "qid": "A.7",
                        "number": "7",
                        "text": "Calculate: 5 × 6",
                        "question_type": "calculation",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "calculation",
                            "final_answer": "30",
                            "unit": None,
                            "method_steps": ["5 × 6 = 30"],
                        },
                        "mark_rules": {
                            "total": 2.0,
                            "answer_marks": 1.0,
                            "method_marks": 1.0,
                            "tick_allocation": "1 method + 1 answer",
                        },
                        "render_hints": {"working_lines": 3},
                        "memo": {"worked_solution": "5 × 6 = 30"},
                    },
                    {
                        "qid": "A.8",
                        "number": "8",
                        "text": "Complete the table.",
                        "question_type": "table_completion",
                        "difficulty": "challenging",
                        "answer": {
                            "kind": "table_completion",
                            "row_headers": ["Example", "Row 1"],
                            "col_headers": ["Col A", "Col B"],
                            "cells": [
                                {"row": 1, "col": 0, "accepted": ["X"], "half_mark": True},
                                {"row": 1, "col": 1, "accepted": ["Y"], "half_mark": True},
                            ],
                            "format_example_row": True,
                        },
                        "mark_rules": {"total": 1.0},
                        "memo": {"worked_solution": "X, Y."},
                    },
                    {
                        "qid": "A.9",
                        "number": "9",
                        "text": "Label the diagram.",
                        "question_type": "labelling",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "labelling",
                            "positions": {"1": "Heart", "2": "Lung"},
                            "term_bank": ["Heart", "Lung", "Liver"],
                            "diagram_asset": None,
                        },
                        "mark_rules": {"total": 1.0},
                    },
                    {
                        "qid": "A.10",
                        "number": "10",
                        "text": "Write a short paragraph about water.",
                        "question_type": "extended_response",
                        "difficulty": "challenging",
                        "answer": {
                            "kind": "extended_response",
                            "model_answer": "Water is essential for life.",
                            "rubric": [{"point": "Mentions water cycle", "marks": 1.0}],
                        },
                        "mark_rules": {"total": 1.0},
                    },
                ],
            }
        ],
    }


@pytest.fixture()
def full_assessment() -> Assessment:
    return Assessment.model_validate(_full_assessment_dict())


# ---------------------------------------------------------------------------
# 1. MEMO-EXCLUSION TEST — the most important test in the phase
# ---------------------------------------------------------------------------


class TestMemoExclusion:
    """Walk the JSON of ChildAssessmentView and assert no answer-key field leaks."""

    def _collect_all_keys(self, obj: object, path: str = "") -> set[str]:
        """Recursively collect every dict key in the serialised object."""
        keys: set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                keys.add(k)
                keys |= self._collect_all_keys(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                keys |= self._collect_all_keys(item, f"{path}[{i}]")
        return keys

    def test_child_view_contains_no_answer_key_fields(self, full_assessment: Assessment) -> None:
        """Serialised ChildAssessmentView must not contain ANY answer-key field name.

        This test walks the entire JSON tree and asserts that every key in
        _ANSWER_KEY_FIELDS is absent.  A child inspecting the network response
        must find no answer information.
        """
        view = project_for_child(full_assessment)
        serialised = json.loads(view.model_dump_json())
        all_keys = self._collect_all_keys(serialised)

        leaked = all_keys & _ANSWER_KEY_FIELDS
        assert leaked == set(), (
            f"Answer-key fields found in child view JSON: {sorted(leaked)}. "
            "These fields MUST NOT appear in the wire response."
        )

    def test_memo_free_view_also_covers_maths_sample(self) -> None:
        """Run memo-exclusion check on the maths sample (exercise calculation / table types)."""
        assessment = Assessment.model_validate(maths_assessment())
        view = project_for_child(assessment)
        serialised = json.loads(view.model_dump_json())
        keys = self._collect_all_keys(serialised)
        leaked = keys & _ANSWER_KEY_FIELDS
        assert leaked == set(), f"Answer-key fields leaked in maths sample view: {sorted(leaked)}"

    def test_child_view_preserves_safe_fields(self, full_assessment: Assessment) -> None:
        """Essential display fields must be present in the serialised view."""
        view = project_for_child(full_assessment)
        data = json.loads(view.model_dump_json())

        # Top-level assessment fields
        assert data["assessment_id"] == full_assessment.assessment_id
        assert data["cycle_id"] == full_assessment.cycle_id
        assert data["title"] == full_assessment.title
        assert data["content_language"] == full_assessment.content_language
        assert data["declared_total_marks"] == full_assessment.declared_total_marks

        # Sections preserved
        assert len(data["sections"]) == len(full_assessment.sections)
        section = data["sections"][0]
        assert "label" in section
        assert "title" in section
        assert "questions" in section

        # Questions preserve safe fields
        q = section["questions"][0]
        assert "qid" in q
        assert "number" in q
        assert "text" in q
        assert "question_type" in q
        assert "marks_total" in q
        assert "render_hints" in q
        assert "answer_view" in q


# ---------------------------------------------------------------------------
# 2. capture_service unit tests — one per question type
# ---------------------------------------------------------------------------


class TestProjectForChild:
    def _section_q(self, assessment: Assessment, qid: str) -> ChildAnswerView:
        view = project_for_child(assessment)
        for sv in view.sections:
            for qv in sv.questions:
                if qv.qid == qid:
                    return qv.answer_view
        raise ValueError(f"Question {qid} not found")

    def test_mcq_view_has_options_no_correct_index(self, full_assessment: Assessment) -> None:
        av = self._section_q(full_assessment, "A.1")
        d = av.model_dump()
        assert "options" in d
        assert "correct_index" not in d
        assert "distractor_notes" not in d

    def test_true_false_view_has_no_answer(self, full_assessment: Assessment) -> None:
        av = self._section_q(full_assessment, "A.2")
        d = av.model_dump()
        assert "is_true" not in d
        assert "requires_correction" not in d

    def test_matching_view_has_items_no_pairs(self, full_assessment: Assessment) -> None:
        av = self._section_q(full_assessment, "A.3")
        d = av.model_dump()
        assert "left" in d
        assert "right" in d
        assert "correct_pairs" not in d

    def test_ordering_view_has_items_no_order(self, full_assessment: Assessment) -> None:
        av = self._section_q(full_assessment, "A.4")
        d = av.model_dump()
        assert "items" in d
        assert "correct_order" not in d

    def test_fill_blank_view_has_count_no_accepted(self, full_assessment: Assessment) -> None:
        av = self._section_q(full_assessment, "A.5")
        d = av.model_dump()
        assert d["blank_count"] == 1
        assert "accepted" not in d
        assert "case_sensitive" not in d

    def test_short_answer_view_has_no_accepted(self, full_assessment: Assessment) -> None:
        av = self._section_q(full_assessment, "A.6")
        d = av.model_dump()
        assert "accepted" not in d
        assert "required_keywords" not in d
        assert "marker_guidance" not in d

    def test_calculation_view_has_no_answer(self, full_assessment: Assessment) -> None:
        av = self._section_q(full_assessment, "A.7")
        d = av.model_dump()
        assert "final_answer" not in d
        assert "method_steps" not in d
        assert "unit" not in d
        # working_lines_hint is safe (layout only)
        assert "working_lines_hint" in d

    def test_table_completion_view_has_positions_no_accepted(
        self, full_assessment: Assessment
    ) -> None:
        av = self._section_q(full_assessment, "A.8")
        d = av.model_dump()
        assert "row_headers" in d
        assert "col_headers" in d
        assert "blank_cell_positions" in d
        # Each position only has row/col — no accepted values
        for pos in d["blank_cell_positions"]:
            assert set(pos.keys()) == {"row", "col"}
        # answer cells' accepted values must not appear
        assert "accepted" not in d

    def test_labelling_view_has_positions_no_correct_labels(
        self, full_assessment: Assessment
    ) -> None:
        av = self._section_q(full_assessment, "A.9")
        d = av.model_dump()
        assert "position_ids" in d
        assert "term_bank" in d
        # positions dict (with correct labels) must not appear
        assert "positions" not in d

    def test_extended_response_view_has_no_model_answer(self, full_assessment: Assessment) -> None:
        av = self._section_q(full_assessment, "A.10")
        d = av.model_dump()
        assert "model_answer" not in d
        assert "rubric" not in d
        assert "required_structure" not in d


# ---------------------------------------------------------------------------
# 3. HTTP endpoint tests — shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_approved() -> Generator[tuple[TestClient, dict[str, Any]], None, None]:
    """TestClient with family + child + subject + cycle in APPROVED_PRINTED state
    with a generated assessment attached."""
    family_repo = _make_family_repo()
    asmt_repo = InMemoryAssessmentRepository()
    sub_repo = InMemorySubmissionRepository()

    family, child_id = family_repo.bootstrap_family("Smith", "Alice", "Grade 5")
    assert child_id is not None
    subject = family_repo.create_subject(family.id, child_id, "Mathematics", "en")
    cycle = family_repo.create_cycle(family.id, subject.id, "Grade 5 fractions")

    # Build and save a minimal Variant-A assessment linked to this cycle.
    asmt_dict = {
        "assessment_id": str(uuid.uuid4()),
        "cycle_id": str(cycle.id),
        "variant": "A",
        "subject": "Mathematics",
        "content_language": "en",
        "grade_label": "Grade 5",
        "title": "Fractions Diagnostic",
        "duration_minutes": 30,
        "instructions": ["Answer all questions."],
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
                        "text": "What is 1/2 + 1/2?",
                        "question_type": "mcq",
                        "difficulty": "easy",
                        "answer": {
                            "kind": "mcq",
                            "options": ["0", "1", "2"],
                            "correct_index": 1,
                        },
                        "mark_rules": {"total": 1.0},
                    }
                ],
            }
        ],
    }
    assessment = Assessment.model_validate(asmt_dict)
    asmt_repo.save(assessment)

    # Manually attach the assessment to the cycle (InMemory cycles store)
    # by advancing the cycle to APPROVED_PRINTED via the state machine.
    advance_to_generating(family_repo, cycle.id)
    advance_to_parent_reviews(family_repo, cycle.id)
    approve_draft(family_repo, cycle.id, note="approved")

    # Patch the in-memory cycle to include the assessment in its assessments list.
    # Access the internal store via the typed helper; this is a test-only seam.
    updated_cycle = family_repo.get_cycle(cycle.id)
    assert updated_cycle is not None
    cycle_with_assessment = updated_cycle.model_copy(update={"assessments": [assessment]})
    family_repo._cycles[cycle.id] = cycle_with_assessment

    ids: dict[str, Any] = {
        "family_id": str(family.id),
        "child_id": str(child_id),
        "subject_id": str(subject.id),
        "cycle_id": str(cycle.id),
        "assessment_id": assessment.assessment_id,
        "qid": "A.1",
    }

    def _family() -> FamilyRepository:
        return family_repo

    def _asmt() -> InMemoryAssessmentRepository:
        return asmt_repo

    def _sub() -> SubmissionRepository:
        return sub_repo

    app.dependency_overrides[get_family_repository] = _family
    app.dependency_overrides[get_assessment_repository] = _asmt
    app.dependency_overrides[get_submission_repository] = _sub
    with TestClient(app) as c:
        yield c, ids
    app.dependency_overrides.pop(get_family_repository, None)
    app.dependency_overrides.pop(get_assessment_repository, None)
    app.dependency_overrides.pop(get_submission_repository, None)


# ---------------------------------------------------------------------------
# 4a. GET /cycles/{cycle_id}/capture tests
# ---------------------------------------------------------------------------


class TestGetCaptureView:
    def test_returns_child_view_when_approved(
        self, client_approved: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_approved
        resp = client.get(
            f"/cycles/{ids['cycle_id']}/capture",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["assessment_id"] == ids["assessment_id"]
        assert body["variant"] == "A"
        assert "sections" in body

    def test_child_view_response_has_no_answer_keys(
        self, client_approved: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """Wire-level memo-exclusion: the HTTP response body must be clean."""
        client, ids = client_approved
        resp = client.get(
            f"/cycles/{ids['cycle_id']}/capture",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 200

        def _all_keys(obj: object) -> set[str]:
            keys: set[str] = set()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    keys.add(k)
                    keys |= _all_keys(v)
            elif isinstance(obj, list):
                for item in obj:
                    keys |= _all_keys(item)
            return keys

        all_keys = _all_keys(resp.json())
        leaked = all_keys & _ANSWER_KEY_FIELDS
        assert leaked == set(), f"Answer-key fields in HTTP response: {sorted(leaked)}"

    def test_returns_409_when_not_approved(self) -> None:
        """Capture view is not available before APPROVED_PRINTED."""
        family_repo = _make_family_repo()
        asmt_repo = InMemoryAssessmentRepository()
        sub_repo = InMemorySubmissionRepository()

        family, child_id = family_repo.bootstrap_family("Jones", "Bob", "Grade 3")
        assert child_id is not None
        subject = family_repo.create_subject(family.id, child_id, "Science", "en")
        cycle = family_repo.create_cycle(family.id, subject.id, "scope")
        # Cycle is still in SCOPE_UPLOADED.

        def _family() -> FamilyRepository:
            return family_repo

        def _asmt() -> InMemoryAssessmentRepository:
            return asmt_repo

        def _sub() -> SubmissionRepository:
            return sub_repo

        app.dependency_overrides[get_family_repository] = _family
        app.dependency_overrides[get_assessment_repository] = _asmt
        app.dependency_overrides[get_submission_repository] = _sub
        with TestClient(app) as c:
            resp = c.get(
                f"/cycles/{cycle.id}/capture",
                headers={"x-user-id": _STUB_HEADER},
            )
        app.dependency_overrides.pop(get_family_repository, None)
        app.dependency_overrides.pop(get_assessment_repository, None)
        app.dependency_overrides.pop(get_submission_repository, None)

        assert resp.status_code == 409

    def test_returns_404_for_nonexistent_cycle(
        self, client_approved: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, _ = client_approved
        resp = client.get(
            f"/cycles/{uuid.uuid4()}/capture",
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client_approved: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_approved
        resp = client.get(f"/cycles/{ids['cycle_id']}/capture")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4b. POST /cycles/{cycle_id}/submissions tests
# ---------------------------------------------------------------------------


class TestCreateSubmission:
    def test_submit_advances_state_to_answers_entered(
        self, client_approved: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_approved
        resp = client.post(
            f"/cycles/{ids['cycle_id']}/submissions",
            json={
                "child_id": ids["child_id"],
                "responses": [{"qid": ids["qid"], "attempted": True, "payload": {"answer": "1"}}],
                "proof_photo_paths": [],
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["child_id"] == ids["child_id"]
        assert body["responses_count"] == 1

        # Verify cycle state advanced.
        cycle_resp = client.get(f"/cycles/{ids['cycle_id']}", headers={"x-user-id": _STUB_HEADER})
        assert cycle_resp.json()["state"] == "ANSWERS_ENTERED"

    def test_submit_returns_409_when_cycle_not_approved(self) -> None:
        family_repo = _make_family_repo()
        asmt_repo = InMemoryAssessmentRepository()
        sub_repo = InMemorySubmissionRepository()

        family, child_id = family_repo.bootstrap_family("Test", "Kid", "Grade 1")
        assert child_id is not None
        subject = family_repo.create_subject(family.id, child_id, "Art", "en")
        cycle = family_repo.create_cycle(family.id, subject.id, "scope")
        # Still in SCOPE_UPLOADED.

        def _family() -> FamilyRepository:
            return family_repo

        def _asmt() -> InMemoryAssessmentRepository:
            return asmt_repo

        def _sub() -> SubmissionRepository:
            return sub_repo

        app.dependency_overrides[get_family_repository] = _family
        app.dependency_overrides[get_assessment_repository] = _asmt
        app.dependency_overrides[get_submission_repository] = _sub
        with TestClient(app) as c:
            resp = c.post(
                f"/cycles/{cycle.id}/submissions",
                json={
                    "child_id": str(child_id),
                    "responses": [],
                    "proof_photo_paths": [],
                },
                headers={"x-user-id": _STUB_HEADER},
            )
        app.dependency_overrides.pop(get_family_repository, None)
        app.dependency_overrides.pop(get_assessment_repository, None)
        app.dependency_overrides.pop(get_submission_repository, None)

        assert resp.status_code == 409

    def test_submit_returns_403_when_wrong_child(
        self, client_approved: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_approved
        wrong_child_id = str(uuid.uuid4())
        resp = client.post(
            f"/cycles/{ids['cycle_id']}/submissions",
            json={
                "child_id": wrong_child_id,
                "responses": [],
                "proof_photo_paths": [],
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 403

    def test_submit_returns_422_for_unknown_qids(
        self, client_approved: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, ids = client_approved
        resp = client.post(
            f"/cycles/{ids['cycle_id']}/submissions",
            json={
                "child_id": ids["child_id"],
                "responses": [{"qid": "GHOST.99", "attempted": True, "payload": {}}],
                "proof_photo_paths": [],
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 422

    def test_submit_with_empty_responses_is_valid(
        self, client_approved: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """A submission with no responses (child skipped all) is valid."""
        client, ids = client_approved
        resp = client.post(
            f"/cycles/{ids['cycle_id']}/submissions",
            json={
                "child_id": ids["child_id"],
                "responses": [],
                "proof_photo_paths": [],
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201
        assert resp.json()["responses_count"] == 0

    def test_proof_photo_paths_stored_not_validated(
        self, client_approved: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """Proof photo paths are accepted and stored as-is (audit only)."""
        client, ids = client_approved
        resp = client.post(
            f"/cycles/{ids['cycle_id']}/submissions",
            json={
                "child_id": ids["child_id"],
                "responses": [],
                "proof_photo_paths": ["storage/families/abc/proof1.jpg"],
            },
            headers={"x-user-id": _STUB_HEADER},
        )
        assert resp.status_code == 201
        assert resp.json()["proof_photo_paths"] == ["storage/families/abc/proof1.jpg"]

    def test_submit_requires_auth(self, client_approved: tuple[TestClient, dict[str, Any]]) -> None:
        client, ids = client_approved
        resp = client.post(
            f"/cycles/{ids['cycle_id']}/submissions",
            json={
                "child_id": ids["child_id"],
                "responses": [],
            },
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5. InMemorySubmissionRepository direct tests
# ---------------------------------------------------------------------------


class TestInMemorySubmissionRepository:
    def test_create_and_get_roundtrip(self) -> None:
        repo = InMemorySubmissionRepository()
        family_id = uuid.uuid4()
        cycle_id = uuid.uuid4()
        child_id = uuid.uuid4()
        payload = SubmissionCreate(
            child_id=child_id,
            responses=[],
            proof_photo_paths=["path/to/photo.jpg"],
        )
        created = repo.create_submission(
            family_id=family_id,
            assessment_id="asmt-001",
            payload=payload,
            cycle_id=cycle_id,
        )
        assert created.child_id == child_id
        assert created.assessment_id == "asmt-001"
        assert created.cycle_id == cycle_id
        assert created.responses_count == 0
        assert created.proof_photo_paths == ["path/to/photo.jpg"]

        fetched = repo.get_submission(created.submission_id)
        assert fetched is not None
        assert fetched.submission_id == created.submission_id

    def test_get_nonexistent_returns_none(self) -> None:
        repo = InMemorySubmissionRepository()
        assert repo.get_submission(uuid.uuid4()) is None

    def test_responses_count_correct(self) -> None:
        repo = InMemorySubmissionRepository()
        from schemas.capture import ChildResponseItem

        payload = SubmissionCreate(
            child_id=uuid.uuid4(),
            responses=[
                ChildResponseItem(qid="A.1", attempted=True, payload={"answer": "x"}),
                ChildResponseItem(qid="A.2", attempted=False, payload={}),
            ],
        )
        result = repo.create_submission(
            family_id=uuid.uuid4(),
            assessment_id="asmt-002",
            payload=payload,
            cycle_id=uuid.uuid4(),
        )
        assert result.responses_count == 2


# ---------------------------------------------------------------------------
# 6. advance_to_answers_entered unit test
# ---------------------------------------------------------------------------


class TestAdvanceToAnswersEntered:
    def _make_approved_cycle(self) -> tuple[InMemoryFamilyRepository, uuid.UUID]:
        repo = InMemoryFamilyRepository(user_id=uuid.uuid4())
        family, child_id = repo.bootstrap_family("F", "C", "G1")
        assert child_id is not None
        subj = repo.create_subject(family.id, child_id, "Math", "en")
        cycle = repo.create_cycle(family.id, subj.id, "scope")
        advance_to_generating(repo, cycle.id)
        advance_to_parent_reviews(repo, cycle.id)
        approve_draft(repo, cycle.id)
        return repo, cycle.id

    def test_advance_from_approved_printed(self) -> None:
        repo, cycle_id = self._make_approved_cycle()
        result = advance_to_answers_entered(repo, cycle_id)
        assert result.state == CycleState.ANSWERS_ENTERED

    def test_illegal_from_scope_uploaded_raises(self) -> None:
        from services.cycle import IllegalTransitionError

        repo = InMemoryFamilyRepository(user_id=uuid.uuid4())
        family, child_id = repo.bootstrap_family("F", "C", "G1")
        assert child_id is not None
        subj = repo.create_subject(family.id, child_id, "Math", "en")
        cycle = repo.create_cycle(family.id, subj.id, "scope")
        with pytest.raises(IllegalTransitionError):
            advance_to_answers_entered(repo, cycle.id)
