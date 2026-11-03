import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from analysis.analysis_store import AnalysisStore
from analysis.schema_drafts import SchemaDraftStore
from analysis import schema_draft_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    secret = "s" * 40
    monkeypatch.setenv("RAG_CALLBACK_SECRET", secret)
    db = str(tmp_path / "settings.db")
    analyses = AnalysisStore(db_path=db, analysis_dir=str(tmp_path / "analyses"))
    profile = {"schema": [{"table": "users", "columns": [
        {"name": "id", "type": "bigint", "nullable": False, "primary": True},
    ]}]}
    analyses.save("analysis-1", "job-1", "a.zip", profile=profile)
    monkeypatch.setattr(schema_draft_routes, "analysis_store", analyses)
    monkeypatch.setattr(schema_draft_routes, "_store", lambda: SchemaDraftStore(db))
    app = FastAPI()
    app.include_router(schema_draft_routes.router)
    return TestClient(app), {"x-internal-secret": secret, "x-user-id": "admin"}, analyses


def test_saved_analysis_lifecycle_and_conflict(client):
    http, headers, analyses = client
    assert http.get('/schema-drafts').status_code == 401
    assert http.post('/schema-drafts/analysis/missing', headers=headers).status_code == 404
    created = http.post('/schema-drafts/analysis/analysis-1', headers=headers)
    assert created.status_code == 201
    draft = created.json()
    assert draft['baseline_coverage'] == 'unverified'
    assert draft['baseline']['tables'][0]['name'] == 'users'
    assert http.post('/schema-drafts/analysis/analysis-1', headers=headers).status_code == 409
    changed = draft['schema']
    changed['tables'][0]['name'] = 'accounts'
    payload = {'schema': changed, 'layout': '<mxfile/>', 'revision': 1}
    saved = http.put('/schema-drafts/' + draft['id'], json=payload, headers=headers)
    assert saved.status_code == 200
    assert saved.json()['baseline']['tables'][0]['name'] == 'users'
    assert saved.json()['schema']['tables'][0]['name'] == 'accounts'
    assert http.put('/schema-drafts/' + draft['id'], json=payload, headers=headers).status_code == 409
    assert http.get('/schema-drafts/' + draft['id'], headers=headers).json()['revision'] == 2
    assert analyses.get('analysis-1')['profile']['schema'][0]['table'] == 'users'


def test_import_validation_and_bounded_request(client):
    http, headers, _ = client
    assert http.post('/schema-drafts/import', json={'schema': {'tables': []}}, headers=headers).status_code == 201
    assert http.post('/schema-drafts/import', json={'schema': {'tables': [], 'unknown': True}}, headers=headers).status_code == 422
    assert http.post('/schema-drafts/import', content=b'x' * (schema_draft_routes.MAX_REQUEST_BYTES + 1), headers=headers).status_code == 413
    assert http.get('/schema-drafts', headers={'x-internal-secret': headers['x-internal-secret']}).status_code == 401
