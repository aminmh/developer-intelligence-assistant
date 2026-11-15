"""Runtime extraction import binds signed metadata to current reporting identity."""
from copy import deepcopy
import hashlib
import hmac
import json
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from analysis import schema_draft_routes
from analysis.analysis_store import AnalysisStore
from analysis.schema_drafts import SchemaDraftStore
from telemetry.registry import SnapshotRegistry

SECRET = 'runtime-test-secret-' * 3
ORIGIN = 'https://developer.example'
HOST = 'a' * 64
CONTAINER = 'b' * 64


def extraction():
    return {
        'schema': {'version': 1, 'tables': [{'id': 't', 'name': 'orders', 'namespace': 'public',
            'columns': [{'id': 'c', 'name': 'id', 'type': 'numeric(38, 12)', 'nullable': False,
                         'default_expression': "nextval('orders_id_seq'::regclass)", 'identity': 'by default'}],
            'inspection_metadata': ['CHECK (id > 0)']}], 'relationships': []},
        'provenance': {'project_id': 'shop', 'environment': 'development', 'host_id': HOST,
            'container_id': CONTAINER, 'image_id': 'sha256:' + 'c' * 64,
            'engine': 'postgresql', 'engine_version': '17.1', 'database': 'shop',
            'schemas': ['public'], 'extracted_at': '2026-11-14T10:00:00+00:00',
            'coverage': {'warnings': ['Check constraints require manual migration'], 'complete': False}},
        'sub': 'admin', 'origin': ORIGIN, 'expires_at': int(time.time()) + 120,
    }


def sign(payload):
    payload = deepcopy(payload)
    payload.pop('receipt', None)
    key = hmac.new(SECRET.encode(), b'devassistant-runtime-schema-result-v1', hashlib.sha256).digest()
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    payload['receipt'] = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return payload


@pytest.fixture
def runtime_client(tmp_path, monkeypatch):
    monkeypatch.setenv('RAG_CALLBACK_SECRET', SECRET)
    db = str(tmp_path / 'settings.db')
    AnalysisStore(db_path=db, analysis_dir=str(tmp_path / 'analyses'))
    store = SchemaDraftStore(db)
    registry = SnapshotRegistry(tmp_path / 'registry')
    registry.report_deployments(HOST, [{'container_id': CONTAINER, 'project_id': 'shop',
        'environment': 'development', 'service': 'database', 'commit_sha': 'd' * 40,
        'image_id': 'sha256:' + 'c' * 64, 'state': 'running'}])
    monkeypatch.setattr(schema_draft_routes, '_store', lambda: SchemaDraftStore(db))
    monkeypatch.setattr(schema_draft_routes, 'default_registry', lambda: registry)
    app = FastAPI()
    app.include_router(schema_draft_routes.router)
    headers = {'x-internal-secret': SECRET, 'x-user-id': 'admin', 'x-application-origin': ORIGIN}
    return TestClient(app), headers, registry, store


def test_sources_use_current_reporter_identity_and_scope(runtime_client):
    http, headers, registry, _ = runtime_client
    url = '/schema-drafts/runtime/sources?project_id=shop&environment=development'
    assert http.get(url).status_code == 401
    sources = http.get(url, headers=headers).json()['sources']
    assert len(sources) == 1
    assert sources[0]['host_id'] == HOST
    assert sources[0]['container_id'] == CONTAINER
    assert sources[0]['state'] == 'running'
    assert http.get(url.replace('development', 'production'), headers=headers).json() == {'sources': []}
    assert http.get(url.replace('shop', 'other'), headers=headers).json() == {'sources': []}
    with registry._connect() as db:
        db.execute("UPDATE deployments SET updated_at=datetime('now', '-181 seconds')")
    assert http.get(url, headers=headers).json() == {'sources': []}


def test_signed_runtime_draft_preserves_baseline_and_immutable_provenance(runtime_client):
    http, headers, _, store = runtime_client
    payload = sign(extraction())
    created = http.post('/schema-drafts/runtime', headers=headers, json=payload)
    assert created.status_code == 201, created.text
    original = created.json()
    assert original['source_kind'] == 'runtime'
    assert original['provenance'] == payload['provenance']
    assert original['baseline']['tables'][0]['columns'][0]['type'] == 'numeric(38, 12)'
    edited = deepcopy(original['schema'])
    edited['tables'][0]['name'] = 'purchases'
    saved = http.put('/schema-drafts/' + original['id'], headers=headers,
                     json={'schema': edited, 'layout': '<mxfile/>', 'revision': 1})
    assert saved.status_code == 200, saved.text
    restored = SchemaDraftStore(store.db_path).get(original['id'])
    assert restored['baseline'] == original['baseline']
    assert restored['provenance'] == original['provenance']
    assert restored['schema']['tables'][0]['inspection_metadata'] == ['CHECK (id > 0)']
    assert restored['schema']['tables'][0]['columns'][0]['identity'] == 'by default'
    assert http.get('/schema-drafts', headers=headers).json()['drafts'][0]['source_kind'] == 'runtime'
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('SELECT source_kind FROM schema_drafts').fetchone()[0] == 'import'
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE schema_draft_runtime_provenance SET provenance_json='{}'")


@pytest.mark.parametrize('field', ['schema', 'provenance', 'sub', 'origin', 'expires_at'])
def test_receipt_rejects_payload_tampering_without_persisting(runtime_client, field):
    http, headers, _, store = runtime_client
    payload = sign(extraction())
    if field == 'schema':
        payload[field]['tables'][0]['name'] = 'tampered'
    elif field == 'provenance':
        payload[field]['project_id'] = 'other-project'
    elif field == 'expires_at':
        payload[field] += 1
    else:
        payload[field] = 'other'
    assert http.post('/schema-drafts/runtime', headers=headers, json=payload).status_code == 403
    assert store.list() == []


@pytest.mark.parametrize('change', ['expired', 'future', 'different-user', 'different-origin', 'missing-origin'])
def test_signed_receipt_requires_fresh_user_and_origin_context(runtime_client, change):
    http, headers, _, store = runtime_client
    payload = extraction()
    if change == 'expired':
        payload['expires_at'] = int(time.time()) - 1
    elif change == 'future':
        payload['expires_at'] += 1000
    elif change == 'different-user':
        payload['sub'] = 'another-admin'
    elif change == 'different-origin':
        payload['origin'] = 'https://other.example'
    else:
        headers = {key: value for key, value in headers.items() if key != 'x-application-origin'}
    assert http.post('/schema-drafts/runtime', headers=headers, json=sign(payload)).status_code == 403
    assert store.list() == []


@pytest.mark.parametrize('field', ['project_id', 'environment', 'host_id', 'container_id', 'image_id'])
def test_valid_receipt_cannot_import_different_runtime_source(runtime_client, field):
    http, headers, _, store = runtime_client
    payload = extraction()
    payload['provenance'][field] = 'e' * 64 if field in ('host_id', 'container_id') else 'other'
    assert http.post('/schema-drafts/runtime', headers=headers, json=sign(payload)).status_code == 409
    assert store.list() == []


@pytest.mark.parametrize('state', ['stopped', 'stale', 'replaced-image'])
def test_result_requires_the_source_to_still_report_running(runtime_client, state):
    http, headers, registry, store = runtime_client
    payload = sign(extraction())
    with registry._connect() as db:
        if state == 'stopped':
            db.execute("UPDATE deployments SET state='stopped'")
        elif state == 'stale':
            db.execute("UPDATE deployments SET updated_at=datetime('now', '-181 seconds')")
        else:
            db.execute("UPDATE deployments SET image_id='replacement'")
    assert http.post('/schema-drafts/runtime', headers=headers, json=payload).status_code == 409
    assert store.list() == []


def test_runtime_metadata_cannot_be_supplied_as_unsigned_import(runtime_client):
    http, headers, _, store = runtime_client
    payload = sign(extraction())
    assert http.post('/schema-drafts/import', headers=headers, json=payload).status_code == 422
    payload['receipt'] = '0' * 64
    assert http.post('/schema-drafts/runtime', headers=headers, json=payload).status_code == 403
    assert store.list() == []


def test_runtime_signature_handles_unicode_catalog_names(runtime_client):
    http, headers, _, _ = runtime_client
    payload = extraction()
    payload['schema']['tables'][0]['name'] = 'سفارشات'
    result = http.post('/schema-drafts/runtime', headers=headers, json=sign(payload))
    assert result.status_code == 201, result.text
    assert result.json()['schema']['tables'][0]['name'] == 'سفارشات'


def test_invalid_signed_schema_does_not_persist_a_baseline(runtime_client):
    http, headers, _, store = runtime_client
    payload = extraction()
    payload['schema']['tables'][0]['columns'][0]['type'] = ''
    assert http.post('/schema-drafts/runtime', headers=headers, json=sign(payload)).status_code == 422
    assert store.list() == []
