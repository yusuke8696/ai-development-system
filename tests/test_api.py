import pytest
from fastapi.testclient import TestClient
from ai_system.server import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, bootstrap="test-code", polling=False)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/launch/test-code")
        csrf = c.get("/api/state").json()["csrf"]
        c.headers["x-csrf-token"] = csrf
        yield c


def test_auth_and_csrf(tmp_path):
    app = create_app(tmp_path, bootstrap="code", polling=False)
    with TestClient(app) as c:
        assert c.get("/api/state").status_code == 401
        assert c.get("/launch/wrong").status_code == 403
        c.get("/launch/code")
        assert c.get("/launch/code").status_code == 403
        assert c.get("/api/state").status_code == 200
        assert c.post("/api/projects", json={}).status_code == 403
        assert c.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.get("/api/state", headers={"Host": "evil.example"}).status_code == 403


def test_project_and_final_gate(client):
    p = client.post("/api/projects", json={"name": "demo", "intent": "demo app"}).json()
    assert len(p["tasks"]) == 7
    final = p["tasks"][-1]
    r = client.post(f"/api/tasks/{final['id']}/approve", json={"approve": True, "expected_version": final["version"]})
    assert r.status_code == 403
    assert client.post(f"/api/tasks/{final['id']}/prepare-final", json={}).status_code == 400


def test_template_registration_and_preview(client):
    r = client.post("/api/templates", json={"id": "company-format", "name": "社内書式", "document_type": "requirements",
        "body": "# {{document.title}}\n\n## 社内必須項目", "required_headings": ["社内必須項目"]})
    assert r.status_code == 200
    assert client.post("/api/templates/validate", json={"template_id": "company-format", "markdown": "# 不足"}).json()["errors"]
    html = client.post("/api/preview", json={"markdown": "<script>alert(1)</script>"}).json()["html"]
    assert "<script>" not in html


def test_multiple_models_and_role_assignment(client):
    r = client.post("/api/models", json={"name": "coding", "provider": "openai", "endpoint": "http://localhost:1234/v1", "model_id": "test", "roles": ["coding"]})
    assert r.status_code == 200
    data = client.get("/api/state").json()
    assert len(data["models"]) == 2
    assert sum("coding" in m["roles"] for m in data["models"]) == 1


def test_settings_version_conflict(client):
    old = client.get("/api/state").json()["settings"]
    assert client.post("/api/settings", json={"repo": "x/y", "expected_version": old["version"]}).status_code == 200
    assert client.post("/api/settings", json={"repo": "other/repo", "expected_version": old["version"]}).status_code == 409
