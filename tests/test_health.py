from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint():
    """Test the /health endpoint returns OK."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
