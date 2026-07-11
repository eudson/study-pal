from schemas.assessment_schema import Assessment
from services.repositories.base import AssessmentRepository
from services.repositories.memory import InMemoryAssessmentRepository
from tests.conftest import minimal_assessment


def test_minimal_assessment_validates() -> None:
    assessment = Assessment.model_validate(minimal_assessment())
    assert assessment.computed_total_marks == assessment.declared_total_marks


def test_save_get_list_roundtrip() -> None:
    repo = InMemoryAssessmentRepository()
    assert isinstance(repo, AssessmentRepository)

    assessment = Assessment.model_validate(minimal_assessment())
    saved = repo.save(assessment)
    assert saved == assessment

    fetched = repo.get(assessment.assessment_id)
    assert fetched == assessment

    assert assessment in repo.list()
    assert repo.get("missing") is None
