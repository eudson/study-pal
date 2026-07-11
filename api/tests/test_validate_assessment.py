from fastapi.testclient import TestClient

from tests.conftest import minimal_assessment


def test_validate_assessment_valid(client: TestClient) -> None:
    response = client.post("/assessments/validate", json={"assessment": minimal_assessment()})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["issues"] == []


def test_validate_assessment_invalid_total_mismatch(client: TestClient) -> None:
    broken = minimal_assessment()
    broken["declared_total_marks"] = 2.0
    response = client.post("/assessments/validate", json={"assessment": broken})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("declared_total_marks" in issue["msg"] for issue in body["issues"])
