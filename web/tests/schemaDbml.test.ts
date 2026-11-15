import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { StructuredSchemaDraft } from '../src/types';
import { structuredSchemaToDbml } from '../src/utils/schemaDbml';
import { parseSchemaInput } from '../src/utils/erdTransformer';
import { importDraftSchema } from '../src/utils/schemaDraft';

test('DBML export preserves supported columns, composite constraints and relationships on import', async () => {
  const column = (id: string, name: string) => ({ id, name, type: 'integer', nullable: false, default: null, auto_increment: null });
  const draft: StructuredSchemaDraft = { version: 1, tables: [
    { id: 'p', name: 'parents', namespace: 'app', columns: [column('p1', 'a'), column('p2', 'b')], keys: [{ id: 'pk', name: 'parent_pk', kind: 'primary', column_ids: ['p1', 'p2'] }], indexes: [] },
    { id: 'c', name: 'children', namespace: 'app', columns: [column('c1', 'a'), column('c2', 'b')], keys: [], indexes: [{ id: 'ix', name: 'child_ix', column_ids: ['c1', 'c2'], unique: true }] },
  ], relationships: [{ id: 'fk', name: 'child_fk', source_table_id: 'c', source_column_ids: ['c1', 'c2'], target_table_id: 'p', target_column_ids: ['p1', 'p2'], on_delete: 'cascade', on_update: 'restrict' }] };
  const restored = importDraftSchema(await parseSchemaInput(structuredSchemaToDbml(draft), { format: 'dbml' }));
  assert.equal(restored.tables[0].keys[0].name, 'parent_pk');
  assert.equal(restored.tables[0].keys[0].column_ids.length, 2);
  assert.equal(restored.tables[1].indexes[0].name, 'child_ix');
  assert.equal(restored.tables[1].indexes[0].unique, true);
  assert.equal(restored.relationships[0].name, 'child_fk');
  assert.equal(restored.relationships[0].source_column_ids.length, 2);
  assert.equal(restored.relationships[0].on_delete, 'cascade');
  assert.equal(restored.relationships[0].on_update, 'restrict');
});

test('DBML export blocks unsafe identifiers and unsupported column types', () => {
  const draft: StructuredSchemaDraft = { version: 1, tables: [{ id: 't', name: 'items', namespace: null, columns: [{ id: 'c', name: 'value', type: 'integer', nullable: true, default: null, auto_increment: null }], keys: [], indexes: [] }], relationships: [] };
  draft.tables[0].name = 'items } Ref:';
  assert.throws(() => structuredSchemaToDbml(draft), /identifier/);
  draft.tables[0].name = 'items';
  draft.tables[0].columns[0].type = 'int ] }';
  assert.throws(() => structuredSchemaToDbml(draft), /type/);
});

test('DBML export carries literal defaults and an unnamed single-column unique key', async () => {
  const draft: StructuredSchemaDraft = { version: 1, tables: [{ id: 't', name: 'items', namespace: null, columns: [
    { id: 'c', name: 'label', type: 'varchar(100)', nullable: false, default: "it's ready", auto_increment: null },
    { id: 'n', name: 'count', type: 'integer', nullable: true, default: 42, auto_increment: null },
  ], keys: [{ id: 'unique', name: null, kind: 'unique', column_ids: ['c'] }], indexes: [] }], relationships: [] };
  const restored = importDraftSchema(await parseSchemaInput(structuredSchemaToDbml(draft), { format: 'dbml' }));
  assert.equal(restored.tables[0].columns[0].default, "it's ready");
  assert.equal(restored.tables[0].columns[1].default, 42);
  assert.equal(restored.tables[0].keys[0].kind, 'unique');
});
