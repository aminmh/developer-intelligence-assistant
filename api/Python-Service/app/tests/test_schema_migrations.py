import shutil
import json
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import default_registry
from analysis import schema_draft_routes
from analysis.schema_drafts import SchemaDraftStore
from analysis.schema_migrations import preview as generate_preview, schema_diff, validate_migration_timestamp
from analysis.schema_model import Schema
from analysis.analysis_store import AnalysisStore


def model_response(prompt):
    data = json.loads(prompt)
    content = default_registry().migration_adapter(data['framework']).generate(data['operations'], data['database'])[0]['content']
    if data['class_name']:
        content = content.replace('Version20000101000000', data['class_name'])
    return content


@pytest.fixture(autouse=True)
def mock_model(monkeypatch):
    from runtime import llm
    monkeypatch.setattr(llm, 'complete_with_model',
                        lambda prompt, **kwargs: (model_response(prompt), 'test', 'local'))


def preview(draft, framework, database, registry=None, migration_timestamp='2026_11_02_120000'):
    return generate_preview(draft, framework, database, migration_timestamp, registry)


def column(id, name, **kwargs):
    return {'id': id, 'name': name, 'type': 'integer', 'nullable': True, 'auto_increment': False, **kwargs}


def schema(columns=None):
    return {'tables': [{'id': 't', 'name': 'users', 'columns': columns or []}]}


def draft(before, after):
    return {'id': 'draft', 'revision': 2, 'baseline': before, 'schema': after}


def _with_inspected_definition(metadata):
    before = schema([column('a', 'age')])
    if metadata == 'inspection_metadata':
        before['tables'][0][metadata] = ['CHECK (age > 0)']
    else:
        before['tables'][0]['columns'][0][metadata] = 'always' if metadata == 'identity' else 'CURRENT_TIMESTAMP'
    return before


@pytest.mark.parametrize('metadata', ['inspection_metadata', 'default_expression', 'identity'])
@pytest.mark.parametrize('framework', ['laravel', 'symfony'])
def test_inspected_definitions_block_changes_to_existing_columns_of_the_affected_table(metadata, framework):
    before = _with_inspected_definition(metadata)
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'][0]['nullable'] = False
    result = preview(draft(before, after), framework, 'postgresql')
    assert result['generation_blocked']
    assert 'manual migration' in result['generation_error']
    assert result['files'] == []


@pytest.mark.parametrize('metadata', ['inspection_metadata', 'default_expression', 'identity'])
@pytest.mark.parametrize('framework', ['laravel', 'symfony'])
def test_a_plain_new_column_is_allowed_on_a_table_with_inspected_definitions(metadata, framework):
    before = _with_inspected_definition(metadata)
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'].append(column('b', 'other'))
    result = preview(draft(before, after), framework, 'postgresql')
    assert not result['generation_blocked'], result['generation_error']
    assert len(result['files']) == 1


@pytest.mark.parametrize('attribute', ['default_expression', 'identity'])
def test_a_new_column_with_its_own_expression_default_or_identity_still_needs_a_manual_migration(attribute):
    before = _with_inspected_definition('inspection_metadata')
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'].append(column('b', 'stamp', **{attribute: 'CURRENT_TIMESTAMP' if attribute == 'default_expression' else 'always'}))
    result = preview(draft(before, after), 'laravel', 'postgresql')
    assert result['generation_blocked'] and 'manual migration' in result['generation_error']


def test_a_new_column_together_with_an_index_on_an_inspected_table_is_still_blocked():
    before = _with_inspected_definition('inspection_metadata')
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'].append(column('b', 'other'))
    after['tables'][0]['indexes'] = [{'id': 'i', 'name': 'other_idx', 'column_ids': ['b'], 'unique': False}]
    result = preview(draft(before, after), 'laravel', 'postgresql')
    assert result['generation_blocked'] and 'inspected definitions' in result['generation_error']


def test_removing_inspection_metadata_cannot_bypass_migration_protection():
    before = schema([column('a', 'age')])
    before['tables'][0]['inspection_metadata'] = ['PARTITION BY RANGE (age)']
    after = schema([column('a', 'age', nullable=False)])
    result = preview(draft(before, after), 'laravel', 'postgresql')
    assert result['generation_blocked']
    assert 'inspected definitions' in result['generation_error']


def test_exact_catalog_column_type_survives_model_and_does_not_become_a_supported_alias():
    raw_type = "enum(" + ','.join("'value_%s'" % i for i in range(100)) + ")"
    parsed = Schema.model_validate(schema([column('a', 'state', type=raw_type)]))
    assert parsed.tables[0].columns[0].type == raw_type
    result = preview(draft(schema(), parsed.model_dump()), 'laravel', 'mysql')
    assert result['generation_blocked']


def test_inspection_metadata_on_an_unaffected_table_does_not_block_changes():
    before = schema([column('a', 'age')])
    before['tables'].append({'id': 'other', 'name': 'other', 'columns': [],
                             'inspection_metadata': ['CHECK (flag = true)']})
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'].append(column('b', 'nickname'))
    result = preview(draft(before, after), 'laravel', 'postgresql')
    assert not result['generation_blocked'], result['generation_error']


def test_relationship_changes_check_inspection_metadata_on_both_endpoint_tables():
    before = schema([column('a', 'age')])
    before['tables'].append({'id': 'other', 'name': 'other',
        'columns': [column('o', 'age')], 'inspection_metadata': ['Custom table definition']})
    after = json.loads(json.dumps(before))
    after['relationships'] = [{'id': 'fk', 'name': 'other_age_fk',
        'source_table_id': 't', 'source_column_ids': ['a'],
        'target_table_id': 'other', 'target_column_ids': ['o']}]
    result = preview(draft(before, after), 'laravel', 'postgresql')
    assert result['generation_blocked']
    assert 'inspected definitions' in result['generation_error']


def test_unknown_auto_increment_is_preserved_and_cannot_be_guessed():
    missing = {'id': 'a', 'name': 'age', 'type': 'integer', 'nullable': True}
    assert Schema.model_validate(schema([missing])).tables[0].columns[0].auto_increment is None
    unknown = column('a', 'age', auto_increment=None)
    confirmed = column('a', 'age', auto_increment=False)
    result = preview(draft(schema([unknown]), schema([confirmed])), 'laravel', 'mysql')
    assert result['generation_blocked'] and 'unknown' in result['generation_error']
    result = preview(draft(schema(), schema([unknown])), 'laravel', 'mysql')
    assert result['generation_blocked']


def test_unknown_auto_increment_is_safe_only_when_postgresql_preserves_it():
    before = column('a', 'age', auto_increment=None)
    after = column('a', 'age', nullable=False, auto_increment=None)
    pg = preview(draft(schema([before]), schema([after])), 'laravel', 'postgresql')
    assert not pg['generation_blocked']
    mysql = preview(draft(schema([before]), schema([after])), 'laravel', 'mysql')
    assert mysql['generation_blocked']


def test_diff_preserves_identity_and_marks_possible_rename():
    old = Schema.model_validate(schema([column('a', 'old')]))
    new = Schema.model_validate(schema([column('b', 'new')]))
    changes = schema_diff(old, new)
    assert [(o['action'], o['id']) for o in changes] == [('drop', 'a'), ('add', 'b')]
    assert all(o['ambiguous'] for o in changes)
    result = preview(draft(old.model_dump(), new.model_dump()), 'laravel', 'mysql')
    assert result['generation_blocked'] and 'rename' in result['generation_error']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('symfony', 'postgresql'), ('doctrine', 'mysql')])
def test_supported_candidate_is_valid_php(framework, database):
    result = preview(draft(schema(), schema([column('a', 'age')])), framework, database)
    assert result['operations'][0]['id'] == 'a'
    assert not result['requires_confirmation']
    assert result['baseline_coverage'] == 'unverified'
    php = shutil.which('php')
    if php:
        checked = subprocess.run([php, '-l'], input=result['files'][0]['content'], text=True, capture_output=True, timeout=5)
        assert checked.returncode == 0, checked.stderr


def test_destructive_and_unsupported_changes():
    result = preview(draft(schema([column('a', 'age')]), schema()), 'laravel', 'mysql')
    assert result['requires_confirmation'] and result['operations'][0]['destructive']
    cases = [
        (draft(schema(), schema([column('a', "x'); evil(); //")])), 'laravel', 'mysql'),
        (draft(schema(), schema([column('a', 'age', nullable=False)])), 'symfony', 'postgresql'),
        (draft(schema([column('a', 'age')]), schema([column('a', 'age', type='custom')])), 'symfony', 'postgresql'),
        (draft(schema(), schema([column('a', 'age')])), 'unknown', 'mysql'),
        (draft(schema(), schema([column('a', 'age')])), 'laravel', 'sqlite'),
    ]
    for case, framework, database in cases:
        result = preview(case, framework, database)
        assert result['generation_blocked'] and result['files'] == [] and result['operations']


def test_registry_optional_extension():
    registry = default_registry()
    assert registry.migration_adapter('laravel') is not None
    assert registry.framework('laravel') is not None
    with pytest.raises(TypeError):
        registry.register_migration_adapter('bad', object())


def test_preview_does_not_discover_disabled_external_plugins(monkeypatch):
    registry = default_registry()
    def fail_discovery():
        raise RuntimeError('External plugin constructor was invoked')
    monkeypatch.setattr(registry, 'discover_entry_points', fail_discovery)
    result = preview(draft(schema(), schema([column('a', 'age')])), 'laravel', 'mysql', registry)
    assert not result['generation_blocked'] and result['files']
    result = preview(draft(schema(), schema([column('a', 'age')])), 'external', 'mysql', registry)
    assert result['generation_blocked'] and not result['files']


def test_preview_route_uses_saved_revision(tmp_path, monkeypatch):
    monkeypatch.setenv('RAG_CALLBACK_SECRET', 's' * 40)
    AnalysisStore(db_path=str(tmp_path / 'db.sqlite'), analysis_dir=str(tmp_path / 'analyses'))
    store = SchemaDraftStore(str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(schema_draft_routes, '_store', lambda: store)
    app = FastAPI()
    app.include_router(schema_draft_routes.router)
    client = TestClient(app)
    headers = {'x-internal-secret': 's' * 40, 'x-user-id': 'admin'}
    created = store.create(schema())
    store.save(created['id'], schema([column('a', 'age')]), '', 1)
    url = '/schema-drafts/' + created['id'] + '/migration-preview'
    payload = {'framework': 'laravel', 'database': 'mysql', 'revision': 2,
               'migration_timestamp': '2026_11_02_120000'}
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json={**payload, 'revision': 1}, headers=headers).status_code == 409
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['operations'][0]['after']['name'] == 'age'
    assert client.post(url, json={**payload, 'schema': schema()}, headers=headers).status_code == 422
    # Use a baseline with an existing column to exercise destructive confirmation.
    destructive = store.create(schema([column('a', 'age')]))
    store.save(destructive['id'], schema(), '', 1)
    download = '/schema-drafts/' + destructive['id'] + '/migration-download'
    reviewed = client.post('/schema-drafts/' + destructive['id'] + '/migration-preview', json=payload, headers=headers).json()
    token = reviewed.pop('preview_token')
    payload = {**payload, 'reviewed_preview': reviewed}
    assert client.post(download, json={**payload, 'preview_token': token, 'confirmed': False}, headers=headers).status_code == 409
    response = client.post(download, json={**payload, 'preview_token': token, 'confirmed': True}, headers=headers)
    assert response.status_code == 200 and 'attachment' in response.headers['content-disposition']
    assert '2026_11_02_120000' in response.headers['content-disposition']
    assert reviewed['files'][0]['content'] == response.text
    assert reviewed['files'][0]['name'] in response.headers['content-disposition']
    assert 'schema_' in response.headers['content-disposition']
    assert client.post(download, json={**payload, 'preview_token': token, 'revision': 1, 'confirmed': True}, headers=headers).status_code == 409
    assert client.post(download, json={**payload, 'preview_token': token, 'migration_timestamp': '2026_11_03_120000', 'confirmed': True}, headers=headers).status_code == 409
    store.save(destructive['id'], schema([column('a', 'age', type='text')]), '', 2)
    blocked_payload = {**payload, 'revision': 3}
    blocked = client.post('/schema-drafts/' + destructive['id'] + '/migration-preview', json={k: v for k, v in blocked_payload.items() if k != 'reviewed_preview'}, headers=headers)
    assert blocked.status_code == 200 and blocked.json()['generation_blocked']
    assert blocked.json()['operations'][0]['before']['type'] == 'integer'
    assert blocked.json()['operations'][0]['after']['type'] == 'text'
    assert client.post(download, json={**blocked_payload, 'preview_token': token, 'confirmed': True}, headers=headers).status_code == 409
    change = store.create(schema([column('a', 'age')]))
    store.save(change['id'], schema([column('a', 'age', type='bigint')]), '', 1)
    change_payload = {'framework': 'laravel', 'database': 'postgresql', 'revision': 2, 'migration_timestamp': '2026_11_02_120000'}
    change_url = '/schema-drafts/' + change['id']
    change_preview = client.post(change_url + '/migration-preview', json=change_payload, headers=headers).json()
    assert not change_preview['generation_blocked'] and change_preview['requires_confirmation']
    change_token = change_preview.pop('preview_token')
    change_payload.update(preview_token=change_token, reviewed_preview=change_preview)
    assert client.post(change_url + '/migration-download', json={**change_payload, 'confirmed': False}, headers=headers).status_code == 409
    changed_download = client.post(change_url + '/migration-download', json={**change_payload, 'confirmed': True}, headers=headers)
    assert changed_download.status_code == 200 and changed_download.text == change_preview['files'][0]['content']
    assert client.post(url, json={**payload, 'migration_timestamp': '2026_02_30_120000'}, headers=headers).status_code == 422
    assert client.post(download, json={**payload, 'preview_token': token, 'confirmed': True, 'migration_timestamp': 'bad'}, headers=headers).status_code == 422


def test_untrusted_adapter_cannot_mutate_confirmation_or_return_files():
    class HostileAdapter:
        def generate(self, operations, database):
            operations[0]['destructive'] = False
            return [{'name': 'evil.php', 'content': '<?php evil();'}]

    registry = default_registry()
    registry.register_migration_adapter('laravel', HostileAdapter())
    result = preview(draft(schema([column('a', 'age')]), schema()), 'laravel', 'mysql', registry)
    assert result['requires_confirmation'] and result['operations'][0]['destructive']
    assert result['generation_blocked'] and not result['files']


def test_template_output_is_bounded_and_independently_reproduced(monkeypatch):
    registry = default_registry()
    adapter = registry.migration_adapter('laravel')
    monkeypatch.setattr(adapter, 'generate', lambda operations, database: [{'name': 'migration.php', 'content': 'x' * (1024 * 1024 + 1)}])
    result = preview(draft(schema(), schema([column('a', 'age')])), 'laravel', 'mysql', registry)
    assert result['generation_blocked'] and not result['files']


def test_trusted_instance_mutation_cannot_change_review_flags(monkeypatch):
    registry = default_registry()
    adapter = registry.migration_adapter('laravel')
    def corrupt(operations, database):
        operations[0]['destructive'] = False
        return [{'name': 'migration.php', 'content': '<?php malicious();'}]
    monkeypatch.setattr(adapter, 'generate', corrupt)
    result = preview(draft(schema([column('a', 'age')]), schema()), 'laravel', 'mysql', registry)
    assert result['requires_confirmation'] and result['operations'][0]['destructive']
    assert result['generation_blocked'] and not result['files']


def test_candidate_identity_tracks_draft_revision_and_target():
    value = draft(schema(), schema([column('a', 'age')]))
    result = preview(value, 'symfony', 'postgresql')
    file = result['files'][0]
    assert file['name'][:-4] in file['content']
    assert file['name'].startswith('Version20261102120000')
    assert preview(value, 'symfony', 'postgresql')['files'][0] == file
    assert preview({**value, 'revision': 3}, 'symfony', 'postgresql')['files'][0]['name'] != file['name']
    assert preview(value, 'symfony', 'mysql')['files'][0]['name'] != file['name']
    assert preview({**value, 'id': 'another'}, 'symfony', 'postgresql')['files'][0]['name'] != file['name']
    assert preview(value, 'symfony', 'postgresql', migration_timestamp='2026_11_03_120000')['files'][0]['name'] > file['name']


@pytest.mark.parametrize('timestamp', ['2026_02_30_120000', '2026_11_02_256000', '2026-11-02', '', None])
def test_invalid_migration_timestamp_is_rejected(timestamp):
    with pytest.raises(ValueError):
        validate_migration_timestamp(timestamp)


def test_moved_column_is_ambiguous_and_blocked():
    old = {'tables': [{'id': 'a', 'name': 'first', 'columns': [column('c', 'age')]},
                      {'id': 'b', 'name': 'second', 'columns': []}]}
    new = {'tables': [{'id': 'a', 'name': 'first', 'columns': []},
                      {'id': 'b', 'name': 'second', 'columns': [column('c', 'age')]}]}
    result = preview(draft(old, new), 'laravel', 'mysql')
    assert result['generation_blocked'] and result['requires_confirmation']
    assert len(result['operations']) == 2 and all(op['ambiguous'] for op in result['operations'])


def test_model_receives_only_structured_diff_and_rejects_injection(monkeypatch):
    from runtime import llm
    seen = []
    def malicious(prompt, **kwargs):
        data = json.loads(prompt)
        seen.append(data)
        return (model_response(prompt) + "\nevil();", 'test', 'local')
    monkeypatch.setattr(llm, 'complete_with_model', malicious)
    result = preview(draft(schema(), schema([column('a', 'age')])), 'laravel', 'mysql')
    assert result['generation_blocked'] and not result['files']
    assert seen[0]['operations'][0]['after']['name'] == 'age'
    assert 'required_php_reference' not in seen[0]
    assert 'layout' not in seen[0] and 'xml' not in json.dumps(seen[0]).lower()


def test_provider_and_php_fail_closed(monkeypatch):
    from runtime import llm
    from analysis import schema_migrations
    value = draft(schema(), schema([column('a', 'age')]))
    def fail(*args, **kwargs):
        raise RuntimeError('secret provider detail')
    monkeypatch.setattr(llm, 'complete_with_model', fail)
    result = preview(value, 'laravel', 'mysql')
    assert result['generation_blocked'] and 'provider' in result['generation_error']
    assert 'secret' not in result['generation_error']
    monkeypatch.setattr(llm, 'complete_with_model', lambda prompt, **kw: (model_response(prompt),))
    monkeypatch.setattr(schema_migrations.shutil, 'which', lambda name: None)
    assert 'unavailable' in preview(value, 'laravel', 'mysql')['generation_error']
    monkeypatch.setattr(schema_migrations.shutil, 'which', lambda name: '/php')
    monkeypatch.setattr(schema_migrations.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1))
    assert 'syntax' in preview(value, 'laravel', 'mysql')['generation_error']


def test_download_binds_user_and_reviewed_bytes(tmp_path, monkeypatch):
    from runtime import llm
    monkeypatch.setenv('RAG_CALLBACK_SECRET', 's' * 40)
    AnalysisStore(db_path=str(tmp_path / 'db'), analysis_dir=str(tmp_path / 'analyses'))
    store = SchemaDraftStore(str(tmp_path / 'db'))
    monkeypatch.setattr(schema_draft_routes, '_store', lambda: store)
    app = FastAPI(); app.include_router(schema_draft_routes.router)
    client = TestClient(app)
    headers = {'x-internal-secret': 's' * 40, 'x-user-id': 'admin'}
    item = store.create(schema()); store.save(item['id'], schema([column('a', 'age')]), '', 1)
    base = '/schema-drafts/' + item['id']
    req = {'framework': 'laravel', 'database': 'mysql', 'revision': 2, 'migration_timestamp': '2026_11_02_120000'}
    reviewed = client.post(base + '/migration-preview', headers=headers, json=req).json()
    token = reviewed.pop('preview_token')
    body = {**req, 'preview_token': token, 'reviewed_preview': reviewed, 'confirmed': True}
    monkeypatch.setattr(llm, 'complete_with_model', lambda *a, **kw: pytest.fail('download called model'))
    assert client.post(base + '/migration-download', headers=headers, json=body).text == reviewed['files'][0]['content']
    assert client.post(base + '/migration-download', headers={**headers, 'x-user-id': 'other'}, json=body).status_code == 409
    tampered = json.loads(json.dumps(body)); tampered['reviewed_preview']['files'][0]['content'] += 'evil();'
    assert client.post(base + '/migration-download', headers=headers, json=tampered).status_code == 409
    tampered = json.loads(json.dumps(body)); tampered['reviewed_preview']['requires_confirmation'] = True
    assert client.post(base + '/migration-download', headers=headers, json=tampered).status_code == 409


@pytest.mark.parametrize('remote,expected', [(True, 'remote'), (False, 'local')])
def test_provider_selection_and_failure_are_explicit(monkeypatch, remote, expected):
    from runtime import llm
    calls = []
    monkeypatch.setattr(llm, 'remote_configured', lambda: remote)
    def unavailable(prompt, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('provider failure')
    monkeypatch.setattr(llm, 'complete_with_model', unavailable)
    result = preview(draft(schema(), schema([column('a', 'age')])), 'laravel', 'mysql')
    assert result['generation_blocked'] and not result['files']
    assert len(calls) == 1 and calls[0]['provider'] == expected
    assert calls[0]['fallback_to_local'] is False
    assert calls[0]['max_retries'] == 0


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('doctrine', 'postgresql')])
def test_model_can_reorder_independent_additions_and_choose_identifier_quotes(monkeypatch, framework, database):
    from runtime import llm
    before = {'tables': [{'id': 'a', 'name': 'users', 'columns': []},
                         {'id': 'b', 'name': 'teams', 'columns': []}]}
    after = {'tables': [{'id': 'a', 'name': 'users', 'columns': [column('c', 'age')]},
                        {'id': 'b', 'name': 'teams', 'columns': [column('d', 'size')]}]}
    def authored(prompt, **kwargs):
        data = json.loads(prompt)
        data['operations'].reverse()
        source = model_response(json.dumps(data)).replace("'users'", '"users"')
        return (source, 'test', 'local')
    monkeypatch.setattr(llm, 'complete_with_model', authored)
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    assert result['files'][0]['content'].index('teams') < result['files'][0]['content'].index('users')


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'inject', 'wrong_column', 'bad_open_tag'])
def test_missing_duplicated_or_injected_operation_is_rejected(monkeypatch, change):
    from runtime import llm
    value = draft(schema(), schema([column('a', 'age')]))
    statement = "$table->integer('age')->nullable();"
    def altered(prompt, **kwargs):
        source = model_response(prompt)
        replacements = {'missing': '', 'duplicate': statement + statement,
                        'inject': statement + 'evil();',
                        'wrong_column': "$table->integer('other')->nullable();"}
        return ((source.replace('<?php', '<? php', 1) if change == 'bad_open_tag'
                 else source.replace(statement, replacements[change])),)
    monkeypatch.setattr(llm, 'complete_with_model', altered)
    assert preview(value, 'laravel', 'mysql')['generation_blocked']


def test_doctrine_platform_guard_cannot_be_removed(monkeypatch):
    from runtime import llm
    def altered(prompt, **kwargs):
        source = model_response(prompt)
        line = next(line for line in source.splitlines() if 'abortIf(' in line)
        return (source.replace(line, ''),)
    monkeypatch.setattr(llm, 'complete_with_model', altered)
    assert preview(draft(schema(), schema([column('a', 'age')])), 'doctrine', 'mysql')['generation_blocked']


def constrained_schema():
    return {'tables': [{'id': 'users', 'name': 'users', 'columns': [column('uid', 'id', nullable=False)],
                        'keys': [{'id': 'pk', 'name': 'users_pk', 'kind': 'primary', 'column_ids': ['uid']}]},
                       {'id': 'posts', 'name': 'posts', 'columns': [column('pid', 'user_id')]}]}


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'),
                                                 ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
def test_new_table_with_named_composite_keys(framework, database):
    before = {'tables': []}
    after = {'tables': [{'id': 'orders', 'name': 'orders',
        'columns': [column('a', 'customer_id', nullable=False), column('b', 'order_id', nullable=False)],
        'keys': [{'id': 'pk', 'name': 'PRIMARY' if database == 'mysql' else 'orders_pk', 'kind': 'primary', 'column_ids': ['a', 'b']},
                 {'id': 'uk', 'name': 'orders_uk', 'kind': 'unique', 'column_ids': ['b', 'a']}]}]}
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    assert not result['requires_confirmation']
    content = result['files'][0]['content']
    assert 'customer_id' in content and 'order_id' in content and 'orders_uk' in content
    assert ('PRIMARY KEY' in content and 'UNIQUE' in content) if framework == 'doctrine' else ('->primary(' in content and '->unique(' in content)
    assert preview(draft(after, before), framework, database)['generation_blocked']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('doctrine', 'postgresql')])
def test_new_table_rejects_unverified_or_unrepresentable_keys(framework, database):
    base = {'tables': [{'id': 'orders', 'name': 'orders', 'columns': [column('a', 'id', nullable=False)],
                        'keys': [{'id': 'pk', 'name': 'PRIMARY' if database == 'mysql' else 'orders_pk', 'kind': 'primary', 'column_ids': ['a']}]}]}
    for change in ('nullable', 'unnamed', 'auto_increment', 'bad_index_name'):
        after = json.loads(json.dumps(base))
        table = after['tables'][0]
        if change == 'nullable': table['columns'][0]['nullable'] = True
        elif change == 'unnamed': table['keys'][0]['name'] = None
        elif change == 'auto_increment': table['columns'][0]['auto_increment'] = None
        else: table['indexes'] = [{'id': 'idx', 'name': 'idx; DROP TABLE orders', 'column_ids': ['a']}]
        assert preview(draft({'tables': []}, after), framework, database)['generation_blocked']


def test_new_mysql_table_rejects_unrepresentable_primary_key_name():
    after = {'tables': [{'id': 't', 'name': 'items', 'columns': [column('c', 'id', nullable=False)],
                         'keys': [{'id': 'pk', 'name': 'items_pk', 'kind': 'primary', 'column_ids': ['c']}]}]}
    result = preview(draft({'tables': []}, after), 'laravel', 'mysql')
    assert result['generation_blocked'] and 'PRIMARY' in result['generation_error']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('doctrine', 'postgresql')])
@pytest.mark.parametrize('attribute', ['collation', 'mysql_definition'])
@pytest.mark.parametrize('new_table', [False, True])
def test_new_columns_with_explicit_metadata_are_not_silently_discarded(framework, database, attribute, new_table):
    value = 'pg_catalog.C' if attribute == 'collation' else {
        'unsigned': True, 'charset': None, 'collation': None, 'comment': None,
        'default_kind': 'none', 'other_attributes_absent': True}
    added = column('c', 'age', **{attribute: value})
    before = {'tables': []} if new_table else schema()
    after = schema([added])
    result = preview(draft(before, after), framework, database)
    assert result['generation_blocked'] and not result['files']
    assert 'metadata' in result['generation_error']


def test_new_table_rejects_model_omitting_key(monkeypatch):
    from runtime import llm
    after = {'tables': [{'id': 't', 'name': 'items', 'columns': [column('c', 'id', nullable=False)],
                         'keys': [{'id': 'pk', 'name': 'items_pk', 'kind': 'primary', 'column_ids': ['c']}]}]}
    def omit_key(prompt, **kwargs):
        source = model_response(prompt)
        return (source.replace("$table->primary(['id'], 'items_pk');", ''), 'test', 'local')
    monkeypatch.setattr(llm, 'complete_with_model', omit_key)
    result = preview(draft({'tables': []}, after), 'laravel', 'postgresql')
    assert result['generation_blocked'] and not result['files']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'),
                                                 ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
def test_new_table_indexes_follow_table_creation(framework, database):
    before = {'tables': []}
    after = {'tables': [{'id': 't', 'name': 'items', 'columns': [column('a', 'first'), column('b', 'second')],
        'indexes': [{'id': 'i1', 'name': 'items_idx', 'column_ids': ['a', 'b'], 'unique': False},
                    {'id': 'i2', 'name': 'items_uidx', 'column_ids': ['b', 'a'], 'unique': True}]}]}
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    content = result['files'][0]['content']
    assert content.index('items_idx') < content.index('items_uidx')
    assert content.index('CREATE TABLE' if framework == 'doctrine' else 'Schema::create') < content.index('items_idx')
    assert 'CREATE UNIQUE INDEX' in content and 'CREATE INDEX' in content
    assert not result['requires_confirmation']
    assert preview(draft(after, before), framework, database)['generation_blocked']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('doctrine', 'postgresql')])
@pytest.mark.parametrize('mutation', ['reorder', 'omit'])
def test_new_table_rejects_reordered_or_omitted_index(framework, database, mutation, monkeypatch):
    from runtime import llm
    after = {'tables': [{'id': 't', 'name': 'items', 'columns': [column('a', 'value')],
                         'indexes': [{'id': 'idx', 'name': 'items_idx', 'column_ids': ['a']}]}]}
    def altered(prompt, **kwargs):
        lines = model_response(prompt).splitlines()
        create = next(i for i, line in enumerate(lines) if 'CREATE TABLE' in line or 'Schema::create' in line)
        index = next(i for i, line in enumerate(lines) if 'CREATE INDEX' in line)
        if mutation == 'reorder': lines[create], lines[index] = lines[index], lines[create]
        else: lines.pop(index)
        return ('\n'.join(lines), 'test', 'local')
    monkeypatch.setattr(llm, 'complete_with_model', altered)
    result = preview(draft({'tables': []}, after), framework, database)
    assert result['generation_blocked'] and not result['files']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('doctrine', 'postgresql')])
def test_new_table_rejects_duplicate_key_and_index_names(framework, database):
    after = {'tables': [{'id': 't', 'name': 'items', 'columns': [column('c', 'id')],
                         'keys': [{'id': 'key', 'name': 'items_key', 'kind': 'unique', 'column_ids': ['c']}],
                         'indexes': [{'id': 'idx', 'name': 'ITEMS_KEY', 'column_ids': ['c']}]}]}
    result = preview(draft({'tables': []}, after), framework, database)
    assert result['generation_blocked'] and 'distinct' in result['generation_error']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'),
                                                 ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
@pytest.mark.parametrize('kind', ['index', 'key', 'relationship'])
def test_extended_operations_are_generated_and_confirmed_when_required(framework, database, kind):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    posts = after['tables'][1]
    if kind == 'index':
        posts['indexes'] = [{'id': 'idx', 'name': 'posts_user_idx', 'column_ids': ['pid']}]
    elif kind == 'key':
        posts['keys'] = [{'id': 'uk', 'name': 'posts_user_uk', 'kind': 'unique', 'column_ids': ['pid']}]
    elif kind == 'relationship':
        after['relationships'] = [{'id': 'rel', 'name': 'posts_user_fk', 'source_table_id': 'posts',
            'source_column_ids': ['pid'], 'target_table_id': 'users', 'target_column_ids': ['uid'],
            'on_delete': 'set null'}]
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    assert not result['requires_confirmation']
    assert len(result['files']) == 1
    content = result['files'][0]['content']
    if kind == 'relationship':
        assert 'posts_user_fk' in content and 'users' in content
        assert 'set null' in content.lower()
    else:
        assert ('posts_user_idx' if kind == 'index' else 'posts_user_uk') in content
    reverse = preview(draft(after, before), framework, database)
    assert not reverse['generation_blocked'], reverse['generation_error']
    assert reverse['requires_confirmation']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('doctrine', 'postgresql')])
def test_extended_operations_block_unsafe_cases(framework, database):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['keys'] = [{'id': 'uk', 'name': None, 'kind': 'unique', 'column_ids': ['pid']}]
    assert preview(draft(before, after), framework, database)['generation_blocked']
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'][0]['type'] = 'custom'
    assert preview(draft(before, after), framework, database)['generation_blocked']
    after = json.loads(json.dumps(before))
    after['tables'][1]['indexes'] = [{'id': 'idx', 'name': 'posts_idx', 'column_ids': ['pid']}]
    after['tables'][1]['columns'].append(column('other', 'other', nullable=False))
    assert preview(draft(before, after), framework, database)['generation_blocked']
    after = json.loads(json.dumps(before))
    after['relationships'] = [{'id': 'rel', 'name': 'fk', 'source_table_id': 'users',
        'source_column_ids': ['uid'], 'target_table_id': 'posts', 'target_column_ids': ['pid']}]
    assert preview(draft(before, after), framework, database)['generation_blocked']


@pytest.mark.parametrize('database,create,drop', [
    ('mysql', 'CREATE UNIQUE INDEX `idx` ON `posts` (`user_id`)', 'DROP INDEX `idx` ON `posts`'),
    ('postgresql', 'CREATE UNIQUE INDEX "idx" ON "posts" ("user_id")', 'DROP INDEX "idx"'),
])
def test_unique_index_add_and_drop_use_index_ddl(database, create, drop):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['indexes'] = [{'id': 'idx', 'name': 'idx', 'column_ids': ['pid'], 'unique': True}]
    for framework in ('laravel', 'doctrine'):
        added = preview(draft(before, after), framework, database)
        removed = preview(draft(after, before), framework, database)
        assert not added['generation_blocked'], added['generation_error']
        assert not removed['generation_blocked'], removed['generation_error']
        assert create in added['files'][0]['content']
        assert drop in removed['files'][0]['content']
        assert removed['requires_confirmation']
        if framework == 'laravel':
            assert 'use Illuminate\\Support\\Facades\\DB;' in added['files'][0]['content']
            assert 'DB::statement(' in removed['files'][0]['content']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'),
                                                 ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
def test_multiple_index_additions_support_independent_order(framework, database, monkeypatch):
    from runtime import llm
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['indexes'] = [
        {'id': 'idx_a', 'name': 'posts_idx_a', 'column_ids': ['pid'], 'unique': False},
        {'id': 'idx_b', 'name': 'posts_idx_b', 'column_ids': ['pid'], 'unique': True},
    ]
    def reversed_model(prompt, **kwargs):
        data = json.loads(prompt)
        data['operations'].reverse()
        return (model_response(json.dumps(data)), 'test', 'local')
    monkeypatch.setattr(llm, 'complete_with_model', reversed_model)
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    assert not result['requires_confirmation']
    assert result['files'][0]['content'].index('posts_idx_b') < result['files'][0]['content'].index('posts_idx_a')
    assert preview(draft(after, before), framework, database)['generation_blocked']


def test_multiple_index_additions_reject_missing_model_statement(monkeypatch):
    from runtime import llm
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['indexes'] = [
        {'id': 'idx_a', 'name': 'posts_idx_a', 'column_ids': ['pid'], 'unique': True},
        {'id': 'idx_b', 'name': 'posts_idx_b', 'column_ids': ['pid'], 'unique': True},
    ]
    def omitted(prompt, **kwargs):
        source = model_response(prompt)
        line = next(line for line in source.splitlines() if 'posts_idx_a' in line)
        return (source.replace(line, ''), 'test', 'local')
    monkeypatch.setattr(llm, 'complete_with_model', omitted)
    result = preview(draft(before, after), 'laravel', 'postgresql')
    assert result['generation_blocked'] and not result['files']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('doctrine', 'postgresql')])
def test_multiple_index_additions_reject_duplicate_names(framework, database):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['indexes'] = [
        {'id': 'idx_a', 'name': 'same_name', 'column_ids': ['pid']},
        {'id': 'idx_b', 'name': 'SAME_NAME', 'column_ids': ['pid']},
    ]
    result = preview(draft(before, after), framework, database)
    assert result['generation_blocked'] and 'distinct' in result['generation_error']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'),
                                                 ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
@pytest.mark.parametrize('unique', [False, True])
def test_add_nullable_column_before_its_index(framework, database, unique):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'].append(column('new', 'email'))
    after['tables'][1]['indexes'] = [{'id': 'idx', 'name': 'posts_email_idx', 'column_ids': ['new'], 'unique': unique}]
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    assert not result['requires_confirmation']
    content = result['files'][0]['content']
    assert content.index('email') < content.index('posts_email_idx')
    assert preview(draft(after, before), framework, database)['generation_blocked']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('doctrine', 'postgresql')])
def test_column_index_addition_rejects_reordered_model_statement(framework, database, monkeypatch):
    from runtime import llm
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'].append(column('new', 'email'))
    after['tables'][1]['indexes'] = [{'id': 'idx', 'name': 'posts_email_idx', 'column_ids': ['new'], 'unique': True}]
    def reordered(prompt, **kwargs):
        source = model_response(prompt)
        lines = source.splitlines()
        column_line = next(i for i, line in enumerate(lines) if 'email' in line and 'posts_email_idx' not in line)
        index_line = next(i for i, line in enumerate(lines) if 'posts_email_idx' in line)
        lines[column_line], lines[index_line] = lines[index_line], lines[column_line]
        return ('\n'.join(lines), 'test', 'local')
    monkeypatch.setattr(llm, 'complete_with_model', reordered)
    result = preview(draft(before, after), framework, database)
    assert result['generation_blocked'] and not result['files']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'),
                                                 ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
def test_primary_key_add_drop_requires_nonnullable_columns(framework, database):
    before = constrained_schema()
    before['tables'][1]['columns'][0]['nullable'] = False
    after = json.loads(json.dumps(before))
    after['tables'][1]['keys'] = [{'id': 'key', 'name': 'posts_pk', 'kind': 'primary', 'column_ids': ['pid']}]
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    assert 'primary' in result['files'][0]['content'].lower()
    result = preview(draft(after, before), framework, database)
    assert not result['generation_blocked'] and result['requires_confirmation']
    before['tables'][1]['columns'][0]['nullable'] = True
    after['tables'][1]['columns'][0]['nullable'] = True
    assert preview(draft(before, after), framework, database)['generation_blocked']


@pytest.mark.parametrize('database,expected_add,expected_drop', [
    ('mysql', 'ALTER TABLE `posts` ADD CONSTRAINT `posts_user_uk` UNIQUE (`user_id`)',
     'DROP INDEX `posts_user_uk` ON `posts`'),
    ('postgresql', 'ALTER TABLE "posts" ADD CONSTRAINT "posts_user_uk" UNIQUE ("user_id")',
     'ALTER TABLE "posts" DROP CONSTRAINT "posts_user_uk"'),
])
def test_doctrine_unique_constraint_ddl(database, expected_add, expected_drop):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['keys'] = [{'id': 'uk', 'name': 'posts_user_uk', 'kind': 'unique', 'column_ids': ['pid']}]
    assert expected_add in preview(draft(before, after), 'doctrine', database)['files'][0]['content']
    assert expected_drop in preview(draft(after, before), 'doctrine', database)['files'][0]['content']


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'),
                                                 ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
@pytest.mark.parametrize('field,value', [('default', '42'), ('auto_increment', True)])
def test_column_changes_blocked_without_verified_metadata(framework, database, field, value):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'][0][field] = value
    result = preview(draft(before, after), framework, database)
    assert result['generation_blocked'] and result['requires_confirmation']
    assert result['files'] == [] and ('metadata' in result['generation_error'] or 'definition' in result['generation_error'] or 'literal' in result['generation_error'])


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'),
                                                 ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
def test_column_rename_preserves_attributes_and_requires_confirmation(framework, database):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'][0]['name'] = 'owner_id'
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation']
    quote = '`' if database == 'mysql' else '"'
    assert f'RENAME COLUMN {quote}user_id{quote} TO {quote}owner_id{quote}' in result['files'][0]['content']
    if database == 'mysql':
        assert any('MySQL 8.0' in warning for warning in result['warnings'])


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_postgresql_column_changes_use_ordered_attribute_preserving_statements(framework):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'][0].update(name='owner_id', type='bigint', nullable=False)
    result = preview(draft(before, after), framework, 'postgresql')
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation']
    content = result['files'][0]['content']
    assert content.index('RENAME COLUMN "user_id" TO "owner_id"') < content.index('ALTER COLUMN "owner_id" TYPE BIGINT') < content.index('ALTER COLUMN "owner_id" SET NOT NULL')
    assert 'DROP COLUMN' not in content


def test_column_change_candidate_with_wrong_type_is_rejected(monkeypatch):
    from runtime import llm
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'][0]['type'] = 'bigint'
    def altered(prompt, **kwargs):
        return (model_response(prompt).replace('TYPE BIGINT', 'TYPE TEXT'),)
    monkeypatch.setattr(llm, 'complete_with_model', altered)
    result = preview(draft(before, after), 'laravel', 'postgresql')
    assert result['generation_blocked'] and not result['files']


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
@pytest.mark.parametrize('field,value', [('type', 'bigint'), ('nullable', False)])
def test_mysql_type_and_nullability_changes_remain_blocked(framework, field, value):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'][0][field] = value
    result = preview(draft(before, after), framework, 'mysql')
    assert result['generation_blocked'] and result['requires_confirmation']
    assert 'definition' in result['generation_error']


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_mysql_change_uses_confirmed_definition(framework):
    before = schema([column('a', 'age')])
    definition = {'unsigned': True, 'charset': None, 'collation': None, 'comment': 'Age in years',
                  'default_kind': 'literal', 'other_attributes_absent': True}
    after = schema([column('a', 'age', type='bigint', nullable=False, default=1, mysql_definition=definition)])
    result = preview(draft(before, after), framework, 'mysql')
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation']
    assert "MODIFY COLUMN `age` BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT \\'Age in years\\'" in result['files'][0]['content']


@pytest.mark.parametrize('key,value', [('other_attributes_absent', False), ('charset', 'utf8mb4'), ('comment', "unsafe'comment")])
def test_mysql_unconfirmed_or_unsupported_definition_is_blocked(key, value):
    before = schema([column('a', 'age')])
    definition = {'unsigned': False, 'charset': None, 'collation': None, 'comment': None,
                  'default_kind': 'none', 'other_attributes_absent': True}
    definition[key] = value
    after = schema([column('a', 'age', type='bigint', mysql_definition=definition)])
    assert preview(draft(before, after), 'laravel', 'mysql')['generation_blocked']


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_mysql_change_preserves_confirmed_charset_and_collation(framework):
    definition = {'unsigned': False, 'charset': 'utf8mb4', 'collation': 'utf8mb4_bin',
                  'comment': None, 'default_kind': 'none', 'other_attributes_absent': True}
    before = schema([column('a', 'label', type='text')])
    after = schema([column('a', 'label', type='text', nullable=False, mysql_definition=definition)])
    result = preview(draft(before, after), framework, 'mysql')
    assert not result['generation_blocked'], result['generation_error']
    assert 'MODIFY COLUMN `label` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL' in result['files'][0]['content']


@pytest.mark.parametrize('charset,collation,kind', [
    ('utf8mb4', None, 'text'), (None, 'utf8mb4_bin', 'text'),
    ('utf8mb4', 'utf8mb4_bin', 'integer'),
    ('utf8mb4;DROP', 'utf8mb4_bin', 'text'),
])
def test_mysql_change_blocks_incomplete_or_invalid_character_metadata(charset, collation, kind):
    definition = {'unsigned': False, 'charset': charset, 'collation': collation,
                  'comment': None, 'default_kind': 'none', 'other_attributes_absent': True}
    before = schema([column('a', 'value', type=kind)])
    after = schema([column('a', 'value', type=kind, nullable=False, mysql_definition=definition)])
    assert preview(draft(before, after), 'laravel', 'mysql')['generation_blocked']


def test_mysql_definition_confirmation_alone_is_not_a_schema_change():
    definition = {'unsigned': False, 'charset': None, 'collation': None, 'comment': None,
                  'default_kind': 'none', 'other_attributes_absent': True}
    before = Schema.model_validate(schema([column('a', 'age')]))
    after = Schema.model_validate(schema([column('a', 'age', mysql_definition=definition)]))
    assert schema_diff(before, after) == []


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_mysql_confirmed_collation_change_generates_migration(framework):
    definition = {'unsigned': False, 'charset': 'utf8mb4', 'collation': 'utf8mb4_general_ci',
                  'comment': None, 'default_kind': 'none', 'other_attributes_absent': True}
    before = schema([column('a', 'label', type='text', mysql_definition=definition)])
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'][0]['mysql_definition']['collation'] = 'utf8mb4_bin'
    result = preview(draft(before, after), framework, 'mysql')
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation']
    assert 'TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL' in result['files'][0]['content']
    assert preview(draft(before, after), framework, 'postgresql')['generation_blocked']


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_postgresql_confirmed_collation_change_generates_migration(framework):
    before = schema([column('a', 'label', type='text', collation='pg_catalog.default')])
    after = schema([column('a', 'label', type='text', collation='pg_catalog.C')])
    result = preview(draft(before, after), framework, 'postgresql')
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation']
    assert 'ALTER COLUMN "label" TYPE TEXT COLLATE "pg_catalog"."C"' in result['files'][0]['content']
    assert preview(draft(before, after), framework, 'mysql')['generation_blocked']


def test_mysql_confirmation_flag_alone_is_not_a_schema_change():
    definition = {'unsigned': False, 'charset': None, 'collation': None, 'comment': None,
                  'default_kind': 'none', 'other_attributes_absent': False}
    before = schema([column('a', 'label', mysql_definition=definition)])
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'][0]['mysql_definition']['other_attributes_absent'] = True
    assert schema_diff(Schema.model_validate(before), Schema.model_validate(after)) == []


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_mysql_auto_increment_requires_index_and_preserves_it(framework):
    definition = {'unsigned': False, 'charset': None, 'collation': None, 'comment': None,
                  'default_kind': 'none', 'other_attributes_absent': True}
    before = schema([column('a', 'age', nullable=False)])
    after = schema([column('a', 'age', nullable=False, auto_increment=True, mysql_definition=definition)])
    assert preview(draft(before, after), framework, 'mysql')['generation_blocked']
    for item in (before, after):
        item['tables'][0]['keys'] = [{'id': 'key', 'name': 'users_pk', 'kind': 'primary', 'column_ids': ['a']}]
    result = preview(draft(before, after), framework, 'mysql')
    assert not result['generation_blocked'], result['generation_error']
    assert 'NOT NULL AUTO_INCREMENT' in result['files'][0]['content']


def test_mysql_literal_default_is_php_escaped():
    definition = {'unsigned': False, 'charset': None, 'collation': None, 'comment': None,
                  'default_kind': 'literal', 'other_attributes_absent': True}
    before = schema([column('a', 'label', type='text')])
    after = schema([column('a', 'label', type='text', default='ready', mysql_definition=definition)])
    result = preview(draft(before, after), 'laravel', 'mysql')
    assert not result['generation_blocked'], result['generation_error']
    assert "DEFAULT \\'ready\\'" in result['files'][0]['content']


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
@pytest.mark.parametrize('old,new,expected', [(None, 7, 'SET DEFAULT 7'), (None, True, 'SET DEFAULT TRUE'), ('old', None, 'DROP DEFAULT')])
def test_postgresql_simple_default_changes(framework, old, new, expected):
    before = schema([column('a', 'value', default=old)])
    after = schema([column('a', 'value', default=new)])
    result = preview(draft(before, after), framework, 'postgresql')
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation'] and expected in result['files'][0]['content']


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_postgresql_independent_column_changes_on_different_tables(framework):
    before = {'tables': [{'id': 'a', 'name': 'users', 'columns': [column('c', 'age')]},
                         {'id': 'b', 'name': 'teams', 'columns': [column('d', 'size')]}]}
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'][0]['nullable'] = False
    after['tables'][1]['columns'][0]['default'] = 7
    result = preview(draft(before, after), framework, 'postgresql')
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation']
    content = result['files'][0]['content']
    assert 'ALTER TABLE "users" ALTER COLUMN "age" SET NOT NULL' in content
    assert 'ALTER TABLE "teams" ALTER COLUMN "size" SET DEFAULT 7' in content


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_postgresql_independent_column_changes_on_same_table(framework):
    before = schema([column('a', 'age'), column('b', 'size')])
    after = schema([column('a', 'age', nullable=False), column('b', 'size', default=7)])
    result = preview(draft(before, after), framework, 'postgresql')
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation']
    assert 'ALTER COLUMN "age" SET NOT NULL' in result['files'][0]['content']
    assert 'ALTER COLUMN "size" SET DEFAULT 7' in result['files'][0]['content']


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
@pytest.mark.parametrize('old_type,new_type', [('integer', 'text'), ('text', 'integer')])
def test_postgresql_text_type_change_requires_collation_metadata(framework, old_type, new_type):
    before = schema([column('a', 'value', type=old_type)])
    after = schema([column('a', 'value', type=new_type)])
    result = preview(draft(before, after), framework, 'postgresql')
    assert result['generation_blocked'] and result['requires_confirmation']
    assert 'collation' in result['generation_error'] and result['files'] == []


@pytest.mark.parametrize('framework', ['laravel', 'doctrine'])
def test_postgresql_text_type_change_uses_explicit_collation(framework):
    before = schema([column('a', 'value')])
    after = schema([column('a', 'value', type='text', collation='pg_catalog.C')])
    result = preview(draft(before, after), framework, 'postgresql')
    assert not result['generation_blocked'], result['generation_error']
    assert result['requires_confirmation']
    assert 'TYPE TEXT COLLATE "pg_catalog"."C"' in result['files'][0]['content']


def test_collation_confirmation_alone_is_not_a_schema_change():
    before = Schema.model_validate(schema([column('a', 'value')]))
    after = Schema.model_validate(schema([column('a', 'value', collation='pg_catalog.C')]))
    assert schema_diff(before, after) == []


def test_invalid_collation_is_not_embedded_in_sql():
    before = schema([column('a', 'value')])
    after = schema([column('a', 'value', type='text', collation='C; DROP TABLE users')])
    assert preview(draft(before, after), 'laravel', 'postgresql')['generation_blocked']


@pytest.mark.parametrize('kind', ['key', 'index', 'relationship'])
def test_changed_objects_with_new_references_are_blocked_without_error(kind):
    before = constrained_schema()
    if kind == 'relationship':
        before['relationships'] = [{'id': 'rel', 'name': 'fk', 'source_table_id': 'posts',
            'source_column_ids': ['pid'], 'target_table_id': 'users', 'target_column_ids': ['uid']}]
    else:
        item = {'id': 'object', 'name': 'object_name', 'column_ids': ['pid']}
        if kind == 'key':
            item['kind'] = 'unique'
        before['tables'][1]['keys' if kind == 'key' else 'indexes'] = [item]
    after = json.loads(json.dumps(before))
    after['tables'][1]['columns'].append(column('new', 'new_column'))
    if kind == 'relationship':
        after['relationships'][0]['source_column_ids'] = ['new']
    else:
        after['tables'][1]['keys' if kind == 'key' else 'indexes'][0]['column_ids'] = ['new']
    result = preview(draft(before, after), 'laravel', 'mysql')
    assert result['generation_blocked'] and result['files'] == []


@pytest.mark.parametrize('framework,database', [('laravel', 'mysql'), ('laravel', 'postgresql'), ('doctrine', 'mysql'), ('doctrine', 'postgresql')])
def test_add_nullable_column_with_unique_key_and_index(framework, database):
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    posts = after['tables'][1]
    posts['columns'].append(column('email', 'email'))
    posts['keys'] = [{'id': 'email_key', 'name': 'posts_email_key', 'kind': 'unique', 'column_ids': ['email']}]
    posts['indexes'] = [{'id': 'email_index', 'name': 'posts_email_idx', 'column_ids': ['email']}]
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    content = result['files'][0]['content']
    assert content.index('email') < content.index('posts_email_key') < content.index('posts_email_idx')
    assert not result['requires_confirmation']


def test_add_unique_key_and_index_reject_duplicate_names():
    before = constrained_schema()
    after = json.loads(json.dumps(before))
    posts = after['tables'][1]
    posts['keys'] = [{'id': 'key', 'name': 'same', 'kind': 'unique', 'column_ids': ['pid']}]
    posts['indexes'] = [{'id': 'index', 'name': 'SAME', 'column_ids': ['pid']}]
    assert preview(draft(before, after), 'laravel', 'mysql')['generation_blocked']


@pytest.mark.parametrize('framework', ['laravel', 'symfony'])
@pytest.mark.parametrize('database', ['mysql', 'postgresql'])
@pytest.mark.parametrize('type_name, laravel, sql', [
    ('timestamp', "$table->timestamp('added')->nullable();", 'TIMESTAMP'),
    ('varchar(255)', "$table->string('added', 255)->nullable();", 'VARCHAR(255)'),
    ('char(26)', "$table->char('added', 26)->nullable();", 'CHAR(26)'),
])
def test_timestamp_varchar_and_char_are_supported_for_new_columns(framework, database, type_name, laravel, sql):
    before = schema([column('a', 'age')])
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'].append(column('b', 'added', type=type_name))
    result = preview(draft(before, after), framework, database)
    assert not result['generation_blocked'], result['generation_error']
    content = result['files'][0]['content']
    assert (laravel if framework == 'laravel' else sql) in content


@pytest.mark.parametrize('type_name', ['varchar(0)', 'varchar(70000)', 'varchar(255); DROP TABLE users', 'varchar()', 'timestamp(6)', 'char(-1)'])
def test_unsafe_or_unknown_sized_types_stay_unsupported(type_name):
    before = schema([column('a', 'age')])
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'].append(column('b', 'added', type=type_name))
    result = preview(draft(before, after), 'laravel', 'mysql')
    assert result['generation_blocked'] and 'Unsupported column type' in result['generation_error']


def test_changing_an_existing_column_to_a_new_type_still_uses_the_narrow_type_list():
    before = schema([column('a', 'age')])
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'][0]['type'] = 'varchar(255)'
    result = preview(draft(before, after), 'laravel', 'postgresql')
    assert result['generation_blocked']


def _namespaced(columns, namespace='shop_db'):
    return {'tables': [{'id': 't', 'name': 'users', 'namespace': namespace, 'columns': columns}]}


def test_a_new_column_on_a_namespaced_mysql_table_is_allowed_with_a_database_warning():
    before = _namespaced([column('a', 'age')])
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'].append(column('b', 'deleted_at', type='timestamp'))
    result = preview(draft(before, after), 'laravel', 'mysql')
    assert not result['generation_blocked'], result['generation_error']
    assert any('MySQL database shop_db' in warning for warning in result['warnings'])


def test_namespaced_tables_stay_manual_for_postgresql_and_for_anything_but_new_columns():
    before = _namespaced([column('a', 'age')])
    added = json.loads(json.dumps(before))
    added['tables'][0]['columns'].append(column('b', 'extra'))
    postgres = preview(draft(before, added), 'laravel', 'postgresql')
    assert postgres['generation_blocked'] and 'Namespaced tables' in postgres['generation_error']
    dropped = json.loads(json.dumps(before)); dropped['tables'][0]['columns'] = []
    assert 'Namespaced tables' in preview(draft(before, dropped), 'laravel', 'mysql')['generation_error']
    required = json.loads(json.dumps(before)); required['tables'][0]['columns'][0]['nullable'] = False
    assert preview(draft(before, required), 'laravel', 'mysql')['generation_blocked']


@pytest.mark.parametrize('fence', ['```php\n{code}\n```', '```\n{code}\n```', '\n```php\r\n{code}\r\n```\n'])
def test_a_markdown_code_fence_around_the_candidate_is_removed_but_nothing_else_is_forgiven(monkeypatch, fence):
    from runtime import llm
    before = schema([column('a', 'age')])
    after = json.loads(json.dumps(before))
    after['tables'][0]['columns'].append(column('b', 'deleted_at', type='timestamp'))
    monkeypatch.setattr(llm, 'complete_with_model', lambda prompt, **kw: (fence.format(code=model_response(prompt)), 'test', 'local'))
    result = preview(draft(before, after), 'laravel', 'mysql')
    assert not result['generation_blocked'], result['generation_error']
    assert not result['files'][0]['content'].lstrip().startswith('```')

    monkeypatch.setattr(llm, 'complete_with_model', lambda prompt, **kw: (fence.format(code=model_response(prompt).replace("Manual rollback required'", "Manual rollback required.'")), 'test', 'local'))
    altered = preview(draft(before, after), 'laravel', 'mysql')
    assert altered['generation_blocked'] and 'does not match' in altered['generation_error']
