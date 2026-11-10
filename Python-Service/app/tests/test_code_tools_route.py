"""Tests for the citation source preview endpoint."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_tools.runtime as runtime
from routers.code_tools import router


def _client(monkeypatch, workspace):
    monkeypatch.setattr(runtime, "latest_workspace", lambda filename: str(workspace) if filename == "p.zip" else None)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _project(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "User.php").write_text("\n".join(f"line {i}" for i in range(1, 61)))
    (tmp_path / ".env").write_text("SECRET=1")
    return tmp_path


def test_preview_returns_lines_around_the_cited_line(monkeypatch, tmp_path):
    client = _client(monkeypatch, _project(tmp_path))

    data = client.post("/code-tools/read", json={"filename": "p.zip", "path": "app/User.php", "line": 30}).json()

    assert data["highlight_line"] == 30 and data["start_line"] == 10 and data["end_line"] == 50
    assert "30: line 30" in data["content"] and data["total_lines"] == 60


def test_preview_refuses_secrets_traversal_and_missing_source(monkeypatch, tmp_path):
    client = _client(monkeypatch, _project(tmp_path))

    assert client.post("/code-tools/read", json={"filename": "p.zip", "path": ".env", "line": 1}).status_code == 403
    assert client.post("/code-tools/read", json={"filename": "p.zip", "path": "../x", "line": 1}).status_code == 400
    assert client.post("/code-tools/read", json={"filename": "p.zip", "path": "nope.php", "line": 1}).status_code == 404
    assert client.post("/code-tools/read", json={"filename": "other.zip", "path": "app/User.php", "line": 1}).status_code == 404
    assert client.post("/code-tools/read", json={"filename": "p.zip", "path": "app/User.php", "line": "x"}).status_code == 400
