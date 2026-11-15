import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from analysis import schema_draft_routes
from analysis.schema_inspection import InspectionError, compare_column_names, inspect_columns


class Cursor:
    def __init__(self): self.queries = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, *args): self.queries.append(args)
    def fetchall(self): return [('users', 'id', 'integer', 'NO', None)]


class Connection:
    def __init__(self): self.current = Cursor(); self.rolled_back = False; self.closed = False
    def cursor(self): return self.current
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


@pytest.mark.parametrize('engine,module', [('postgresql', 'psycopg'), ('mysql', 'pymysql')])
def test_inspection_uses_read_only_transaction_and_verified_tls(monkeypatch, engine, module):
    connection = Connection()
    arguments = {}
    def connect(**kwargs):
        arguments.update(kwargs)
        return connection
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(connect=connect))
    monkeypatch.setenv('SCHEMA_INSPECTION_TARGETS', json.dumps({'production': {'engine': engine, 'host': 'db.example', 'database': 'app', 'username': 'reader', 'password': 'secret', 'ca_file': '/ca.pem'}}))
    report = inspect_columns('production')
    assert 'READ ONLY' in connection.current.queries[0][0]
    assert 'information_schema.columns' in connection.current.queries[-1][0]
    assert connection.rolled_back and connection.closed
    assert report['columns'][0]['name'] == 'id'
    assert 'secret' not in str(report)
    if engine == 'postgresql': assert arguments['sslmode'] == 'verify-full'
    else: assert arguments['ssl_verify_cert'] and arguments['ssl_verify_identity']


def test_inspection_disallows_unconfigured_targets_and_redacts_errors(monkeypatch):
    monkeypatch.setenv('SCHEMA_INSPECTION_TARGETS', '{}')
    with pytest.raises(InspectionError): inspect_columns('unconfigured')
    monkeypatch.setenv('SCHEMA_INSPECTION_TARGETS', json.dumps({'db': {'engine': 'postgresql', 'host': 'h', 'database': 'd', 'username': 'u', 'password': 'secret', 'ca_file': '/ca'}}))
    def fail(**kwargs): raise RuntimeError('secret')
    monkeypatch.setitem(sys.modules, 'psycopg', SimpleNamespace(connect=fail))
    with pytest.raises(InspectionError) as error: inspect_columns('db')
    assert 'secret' not in str(error.value)


def test_truncated_report_does_not_claim_missing_columns():
    baseline = {'tables': [{'name': 'users', 'columns': [{'name': 'id'}]}]}
    report = {'namespace': 'public', 'columns': [], 'truncated': True}
    assert compare_column_names(baseline, report)['missing_from_database'] is None


def test_inspection_requires_explicit_consent_and_internal_auth(monkeypatch):
    app = FastAPI(); app.include_router(schema_draft_routes.router)
    monkeypatch.setenv('RAG_CALLBACK_SECRET', 'x' * 32)
    client = TestClient(app)
    headers = {'x-internal-secret': 'x' * 32, 'x-user-id': 'admin'}
    assert client.post('/schema-drafts/a/inspect', json={'target': 'db', 'consent': True}).status_code == 401
    assert client.post('/schema-drafts/a/inspect', headers=headers, json={'target': 'db', 'consent': False}).status_code == 422


def test_inspection_compares_original_without_changing_draft(monkeypatch):
    app = FastAPI(); app.include_router(schema_draft_routes.router)
    monkeypatch.setenv('RAG_CALLBACK_SECRET', 'x' * 32)
    baseline = {'tables': [{'name': 'users', 'columns': [{'name': 'id'}, {'name': 'email'}]}]}
    draft = {'baseline': baseline, 'schema': {'tables': []}}
    monkeypatch.setattr(schema_draft_routes, '_store', lambda: SimpleNamespace(get=lambda _: draft))
    monkeypatch.setattr(schema_draft_routes, 'inspect_columns', lambda _: {'engine': 'postgresql', 'namespace': 'public', 'truncated': False, 'columns': [{'table': 'users', 'name': 'id'}]})
    result = TestClient(app).post('/schema-drafts/a/inspect', headers={'x-internal-secret': 'x' * 32, 'x-user-id': 'admin'}, json={'target': 'reader', 'consent': True})
    assert result.status_code == 200
    assert result.json()['name_comparison']['missing_from_database'] == [{'table': 'users', 'column': 'email'}]
    assert draft['baseline'] == baseline and draft['schema'] == {'tables': []}
