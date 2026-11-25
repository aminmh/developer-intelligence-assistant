import assert from 'node:assert/strict';
import { test } from 'node:test';
import { diffSchemas, relationshipKeys, sameStructuredSchema, tableChangeKey } from '../src/utils/schemaChanges';
import type { NormalizedSchema, StructuredSchemaDraft } from '../src/types';

const original = (): NormalizedSchema => ({
  tables: [
    { id: 't-users', name: 'users', schema: null, fields: [
      { id: 'c-uid', name: 'id', type: 'INTEGER', nullable: false, primaryKey: true },
      { id: 'c-email', name: 'email', type: 'TEXT', nullable: false, primaryKey: false, unique: true }] },
    { id: 't-posts', name: 'posts', schema: null, fields: [
      { id: 'c-pid', name: 'id', type: 'INTEGER', nullable: false, primaryKey: true },
      { id: 'c-puser', name: 'user_id', type: 'INTEGER', nullable: false, primaryKey: false, foreignKey: true },
      { id: 'c-title', name: 'title', type: 'TEXT', nullable: true, primaryKey: false }] },
  ],
  refs: [{ id: 'r1', sourceTable: 'table:posts', sourceColumn: 'user_id', targetTable: 'table:users', targetColumn: 'id', cardinality: '1:N' }],
  warnings: [], source: { kind: 'sql' },
} as unknown as NormalizedSchema);

const draft = (): StructuredSchemaDraft => ({
  version: 1,
  tables: [
    { id: 't-users', name: 'users', namespace: null, inspection_metadata: undefined, indexes: [],
      columns: [{ id: 'c-uid', name: 'id', type: 'INTEGER', nullable: false, default: null, auto_increment: null },
                { id: 'c-email', name: 'email', type: 'TEXT', nullable: false, default: null, auto_increment: null }],
      keys: [{ id: 'k1', name: null, kind: 'primary', column_ids: ['c-uid'] }, { id: 'k2', name: null, kind: 'unique', column_ids: ['c-email'] }] },
    { id: 't-posts', name: 'posts', namespace: null, indexes: [],
      columns: [{ id: 'c-pid', name: 'id', type: 'INTEGER', nullable: false, default: null, auto_increment: null },
                { id: 'c-puser', name: 'user_id', type: 'INTEGER', nullable: false, default: null, auto_increment: null },
                { id: 'c-title', name: 'title', type: 'TEXT', nullable: true, default: null, auto_increment: null }],
      keys: [{ id: 'k3', name: null, kind: 'primary', column_ids: ['c-pid'] }] },
  ],
  relationships: [{ id: 'r1', name: null, source_table_id: 't-posts', source_column_ids: ['c-puser'], target_table_id: 't-users', target_column_ids: ['c-uid'], on_delete: null, on_update: null }],
});

test('an unchanged diagram has no differences', () => {
  const changes = diffSchemas(original(), draft());
  assert.ok(changes);
  assert.equal(changes.total, 0);
  assert.equal(changes.signature, '');
});

test('added tables, added or changed columns, removed columns and relationships are reported', () => {
  const edited = draft();
  edited.tables.push({ id: 't-tags', name: 'tags', namespace: null, indexes: [],
    columns: [{ id: 'c-tid', name: 'id', type: 'INTEGER', nullable: false, default: null, auto_increment: null }],
    keys: [{ id: 'k9', name: null, kind: 'primary', column_ids: ['c-tid'] }] });
  edited.tables[0].columns.push({ id: 'c-name', name: 'name', type: 'VARCHAR(80)', nullable: true, default: null, auto_increment: null });
  edited.tables[1].columns[2].type = 'VARCHAR(200)';
  edited.tables[1].columns[2].nullable = false;
  edited.tables[1].columns.splice(1, 1);                          // user_id removed
  edited.relationships = [];                                       // so its relationship is removed too
  const changes = diffSchemas(original(), edited)!;

  const byName = Object.fromEntries(changes.tables.map((table) => [table.name, table]));
  assert.equal(byName.tags.status, 'added');
  assert.deepEqual(byName.tags.fields, [{ name: 'id', type: 'INTEGER', primaryKey: true, unique: false, nullable: false }]);
  assert.deepEqual(byName.users.columns, [{ name: 'name', status: 'added', detail: 'VARCHAR(80)' }]);
  assert.equal(byName.posts.status, 'changed');
  assert.deepEqual(byName.posts.columns.map((column) => [column.name, column.status]), [['title', 'changed'], ['user_id', 'removed']]);
  assert.match(byName.posts.columns[0].detail!, /type TEXT → VARCHAR\(200\).*now required/);
  assert.deepEqual(changes.relationships, [{ key: 'posts.user_id>users.id', status: 'removed', text: 'posts.user_id → users.id', id: 'r1', sourceTable: 'posts', sourceColumn: 'user_id', targetTable: 'users', targetColumn: 'id' }]);
  assert.equal(changes.total, 4);
});

test('a removed table and an added relationship are reported, and a rename keeps its identity', () => {
  const edited = draft();
  edited.tables.splice(1, 1);
  edited.relationships = [];
  assert.deepEqual(diffSchemas(original(), edited)!.tables.map((table) => [table.name, table.status]), [['posts', 'removed']]);

  const renamed = draft();
  renamed.tables[0].name = 'accounts';
  const [change] = diffSchemas(original(), renamed)!.tables;
  assert.deepEqual([change.name, change.status, change.detail, change.id], ['accounts', 'changed', 'renamed from users', 't-users']);

  const linked = draft();
  linked.relationships.push({ id: 'r2', name: null, source_table_id: 't-users', source_column_ids: ['c-uid'], target_table_id: 't-posts', target_column_ids: ['c-pid'], on_delete: null, on_update: null });
  assert.deepEqual(diffSchemas(original(), linked)!.relationships, [{ key: 'users.id>posts.id', status: 'added', text: 'users.id → posts.id', sourceTable: 'users', sourceColumn: 'id', targetTable: 'posts', targetColumn: 'id' }]);
});

test('the signature changes only when the differences change', () => {
  const a = draft(); a.tables[0].columns[1].type = 'VARCHAR(10)';
  const b = draft(); b.tables[0].columns[1].type = 'VARCHAR(10)';
  const c = draft(); c.tables[0].columns[1].type = 'VARCHAR(20)';
  assert.equal(diffSchemas(original(), a)!.signature, diffSchemas(original(), b)!.signature);
  assert.notEqual(diffSchemas(original(), a)!.signature, diffSchemas(original(), c)!.signature);
});

test('schemas the editor cannot describe are ignored', () => {
  assert.equal(diffSchemas(original(), null), null);
  assert.equal(diffSchemas(original(), { tables: 'x' }), null);
});

test('structured schemas compare by content, not key order', () => {
  const reordered = JSON.parse(JSON.stringify(draft(), (_key, value) => value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).reverse()) : value));
  assert.equal(sameStructuredSchema(draft(), reordered), true);
  const changed = draft(); changed.tables[0].name = 'people';
  assert.equal(sameStructuredSchema(draft(), changed), false);
});

test('changes are measured against the saved draft, so a differently shaped source does not add noise', () => {
  const saved = draft();
  const edited = draft();
  edited.tables[0].columns.push({ id: 'c-new', name: 'created_at', type: 'TIMESTAMP', nullable: true, default: null, auto_increment: null });
  edited.relationships = [];
  const changes = diffSchemas(saved, edited)!;
  assert.deepEqual(changes.tables.map((table) => [table.name, table.status]), [['users', 'changed']]);
  assert.deepEqual(changes.tables[0].columns, [{ name: 'created_at', status: 'added', detail: 'TIMESTAMP' }]);
  assert.deepEqual(changes.relationships.map((relation) => [relation.status, relation.id]), [['removed', 'r1']]);
  assert.equal(diffSchemas(saved, structuredClone(saved))!.total, 0);
});

test('displayed tables and relationships line up with changes by name', () => {
  const keys = relationshipKeys(original());
  assert.deepEqual([...keys], [['r1', 'posts.user_id>users.id']]);
  assert.equal(tableChangeKey(null, 'Users'), tableChangeKey('', 'users'));
  assert.equal(tableChangeKey('public', 'users'), 'public.users');
});
