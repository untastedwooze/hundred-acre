from fastapi.testclient import TestClient

from hundred_acre.main import app


def test_health_endpoint_reports_ok():
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
