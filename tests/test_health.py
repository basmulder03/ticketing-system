"""Smoke test for the Milestone 0 skeleton: the app boots and /healthz responds.

Real unit/integration/e2e coverage per the brief's Testing section is added
by ``test-writer`` alongside each feature starting Milestone 1.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
