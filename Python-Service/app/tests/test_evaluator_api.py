import time
import os

os.environ.setdefault("SETTINGS_DB_PATH", "/tmp/evaluator-test-settings.db")
os.environ.setdefault("ANALYSIS_DIR", "/tmp/evaluator-test-analyses")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.evaluator import router
from evaluator.service import evaluator_service
import routers.sandbox as sandbox_router


def _wait(run_id: str):
    for _ in range(50):
        state = evaluator_service.status(run_id)
        if state and state["status"] in {"completed", "error"}:
            return state
        time.sleep(0.01)
    return evaluator_service.status(run_id)


def test_evaluator_lifecycle_and_review_only_recommendation(monkeypatch, tmp_path):
    monkeypatch.setattr("evaluator.service.analysis_store.save_evaluator_reports", lambda *args, **kwargs: {})
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.post("/evaluator/runs", json={
        "filename": "Orders.zip",
        "slow_log": "# Query_time: 2.0 Lock_time: 0 Rows_sent: 1 Rows_examined: 20000\nSELECT * FROM orders WHERE id = 1;",
        "database": "mysql",
    })
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    state = _wait(run_id)
    assert state["status"] == "completed"
    reports = client.get(f"/evaluator/runs/{run_id}/reports").json()["reports"]
    assert reports and "metrics" in reports[0]
    recommendation = client.post(f"/evaluator/runs/{run_id}/recommendation", json={}).json()["recommendation"]
    assert recommendation["review_required"] is True
    assert recommendation["auto_apply"] is False


def test_unknown_evaluator_run_is_404():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/evaluator/runs/missing").status_code == 404


def test_sandbox_route_request_can_opt_into_evaluation(monkeypatch):
    class FakeExecutor:
        def inject_evaluator_hook(self, project_id, source):
            return {"status": "done"}

        def remove_evaluator_hook(self, project_id):
            return {"status": "done"}

        def run_api_request(self, project_id, method, path, **kwargs):
            return {"status": "done", "exit_code": 0, "stdout": "HTTP/1.1 200 OK\\n\\n{}", "request": {"method": method, "path": path, "headers": []}}

    monkeypatch.setattr(sandbox_router, "default_executor", lambda: FakeExecutor())
    app = FastAPI()
    app.include_router(sandbox_router.router)
    response = TestClient(app).post("/sandbox/request", json={
        "filename": "Orders.zip", "method": "GET", "path": "/api/orders",
        "evaluate_database": True,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["database_evaluation"]["enabled"] is True
    assert body["database_evaluation"]["run_id"]
