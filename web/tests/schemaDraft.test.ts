import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseSchemaInput, schemaToJson } from '../src/utils/erdTransformer';
import { importDraftSchema } from '../src/utils/schemaDraft';

test('SQL import preserves table, column and relationship IDs in a draft', async () => {
  const input = 'CREATE TABLE users (id INTEGER PRIMARY KEY); CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id));';
  const schema = await parseSchemaInput(input, { format: 'sql' });
  const draft = importDraftSchema(schema);
  assert.deepEqual(draft.tables.map((table) => table.id), schema.tables.map((table) => table.id));
  assert.equal(draft.relationships[0].source_column_ids[0], schema.tables[1].fields[1].id);
  assert.equal(draft.relationships[0].target_column_ids[0], schema.tables[0].fields[0].id);
  assert.equal(draft.tables[1].columns[1].nullable, false);
});

test('SQL import preserves PostgreSQL serial auto-increment variants', async () => {
  const schema = await parseSchemaInput('CREATE TABLE items (a SMALLSERIAL, b SERIAL, c BIGSERIAL, d INTEGER);', { format: 'sql' });
  assert.deepEqual(importDraftSchema(schema).tables[0].columns.map((column) => column.auto_increment), [true, true, true, null]);
});

test('SQL import preserves named composite constraints and relationship actions', async () => {
  const sql = `CREATE TABLE app.parents (a INTEGER NOT NULL, b INTEGER NOT NULL, CONSTRAINT parent_pk PRIMARY KEY (a, b));
    CREATE TABLE app.children (a INTEGER, b INTEGER, CONSTRAINT child_unique UNIQUE (a, b), CONSTRAINT child_fk FOREIGN KEY (a, b) REFERENCES app.parents(a, b) ON DELETE CASCADE ON UPDATE RESTRICT);`;
  const parsed = await parseSchemaInput(sql, { format: 'sql' });
  const draft = importDraftSchema(parsed);
  const parent = draft.tables[0], child = draft.tables[1];
  assert.deepEqual(parent.keys.map(key => [key.name, key.kind, key.column_ids]), [['parent_pk', 'primary', parent.columns.map(column => column.id)]]);
  assert.deepEqual(child.keys.map(key => [key.name, key.kind, key.column_ids]), [['child_unique', 'unique', child.columns.map(column => column.id)]]);
  assert.deepEqual(draft.relationships.map(rel => [rel.name, rel.source_table_id, rel.source_column_ids, rel.target_table_id, rel.target_column_ids, rel.on_delete, rel.on_update]), [['child_fk', child.id, child.columns.map(column => column.id), parent.id, parent.columns.map(column => column.id), 'cascade', 'restrict']]);
  assert.match(parsed.warnings[0].message, /first column/);
  assert.deepEqual(importDraftSchema(await parseSchemaInput(schemaToJson(parsed), { format: 'json' })), draft);
});

test('SQL ALTER TABLE retains composite foreign key names and actions', async () => {
  const sql = 'CREATE TABLE parents (a INTEGER, b INTEGER); CREATE TABLE children (a INTEGER, b INTEGER); ALTER TABLE children ADD CONSTRAINT child_fk FOREIGN KEY (a,b) REFERENCES parents(a,b) ON DELETE SET NULL;';
  const draft = importDraftSchema(await parseSchemaInput(sql, { format: 'sql' }));
  assert.equal(draft.relationships.length, 1);
  assert.equal(draft.relationships[0].name, 'child_fk');
  assert.equal(draft.relationships[0].on_delete, 'set null');
  assert.equal(draft.relationships[0].source_column_ids.length, 2);
});

test('SQL qualified names distinguish tables and columns in different namespaces', async () => {
  const sql = 'CREATE TABLE "app"."users" (id INTEGER); CREATE TABLE "other"."users" (id INTEGER); CREATE TABLE "app"."posts" (user_id INTEGER REFERENCES "app"."users"(id));';
  const draft = importDraftSchema(await parseSchemaInput(sql, { format: 'sql' }));
  assert.equal(new Set(draft.tables.flatMap(table => table.columns.map(column => column.id))).size, 3);
  assert.equal(draft.relationships[0].target_table_id, draft.tables[0].id);
});

test('SQL imports named inline and standalone indexes with their columns and uniqueness', async () => {
  const sql = 'CREATE TABLE app.items (a INTEGER, b INTEGER, KEY item_a (a), UNIQUE INDEX item_ab (a,b)); CREATE UNIQUE INDEX item_ba ON app.items (b,a);';
  const parsed = await parseSchemaInput(sql, { format: 'sql' });
  const draft = importDraftSchema(parsed);
  const table = draft.tables[0], [a, b] = table.columns.map(column => column.id);
  assert.deepEqual(table.indexes.map(index => [index.name, index.unique, index.column_ids]), [
    ['item_a', false, [a]], ['item_ab', true, [a, b]], ['item_ba', true, [b, a]],
  ]);
  assert.equal(new Set(table.indexes.map(index => index.id)).size, 3);
  assert.deepEqual(importDraftSchema(await parseSchemaInput(schemaToJson(parsed), { format: 'json' })), draft);
});

test('SQL imports reject indexes that cannot be represented', async () => {
  for (const index of ['CREATE INDEX idx ON items (lower(a));', 'CREATE INDEX idx ON items (a DESC);', 'CREATE INDEX idx ON items (a) WHERE a > 0;', 'CREATE INDEX idx ON missing (a);']) {
    const parsed = await parseSchemaInput(`CREATE TABLE items (a INTEGER); ${index}`, { format: 'sql' });
    assert.ok(parsed.warnings.some(item => item.severity === 'error'));
    assert.throws(() => importDraftSchema(parsed), /valid schema/);
  }
});

test('SQL unsupported or incomplete constraints cannot silently disappear', async () => {
  for (const suffix of ['CONSTRAINT ck CHECK (a > 0)', 'INDEX child_idx (a(5))', 'FOREIGN KEY (a,b) REFERENCES parents(a)', 'FOREIGN KEY (a) REFERENCES parents(a) DEFERRABLE']) {
    const parsed = await parseSchemaInput(`CREATE TABLE parents (a INTEGER); CREATE TABLE children (a INTEGER, b INTEGER, ${suffix});`, { format: 'sql' });
    assert.ok(parsed.warnings.some(item => item.severity === 'error'));
    assert.throws(() => importDraftSchema(parsed), /valid schema/);
  }
});

test('JSON import distinguishes unknown and explicit auto-increment values', async () => {
  const fields = [true, false, null, undefined].map((value, index) => ({ id: `c${index}`, name: `c${index}`, type: 'int', auto_increment: value }));
  const schema = await parseSchemaInput({ tables: [{ id: 't', name: 'items', fields }], refs: [] }, { format: 'json' });
  assert.deepEqual(importDraftSchema(schema).tables[0].columns.map(column => column.auto_increment), [true, false, null, null]);
});

test('structured JSON retains named composite keys, indexes, relationships and IDs', async () => {
  const column = (id: string) => ({ id, name: id, type: 'integer', nullable: false, default: null, auto_increment: false });
  const original = { version: 1, tables: [
    { id: 'parent', name: 'parents', namespace: 'app', columns: [column('p1'), column('p2')], keys: [{ id: 'pk', name: 'parent_pk', kind: 'primary', column_ids: ['p1', 'p2'] }], indexes: [] },
    { id: 'child', name: 'children', namespace: 'app', columns: [column('c1'), column('c2')], keys: [], indexes: [{ id: 'ix', name: 'child_ix', column_ids: ['c1', 'c2'], unique: true }] },
  ], relationships: [{ id: 'fk', name: 'child_parent_fk', source_table_id: 'child', source_column_ids: ['c1', 'c2'], target_table_id: 'parent', target_column_ids: ['p1', 'p2'], on_delete: 'cascade', on_update: 'restrict' }] };
  const preview = await parseSchemaInput(JSON.stringify(original), { format: 'json' });
  assert.match(preview.warnings[0].message, /first column/);
  assert.deepEqual(importDraftSchema(preview), original);
  const reimport = await parseSchemaInput(schemaToJson(preview), { format: 'json' });
  assert.deepEqual(importDraftSchema(reimport), original);
  const copy = importDraftSchema(reimport);
  copy.tables[0].name = 'changed';
  assert.equal(importDraftSchema(reimport).tables[0].name, 'parents');
});

test('invalid structured JSON relationships are rejected before creating a draft', async () => {
  const invalid = { version: 1, tables: [], relationships: [{ id: 'bad', source_table_id: 'missing', source_column_ids: ['c'], target_table_id: 'missing', target_column_ids: ['c'] }] };
  await assert.rejects(parseSchemaInput(invalid, { format: 'json' }), /Invalid structured schema relationship/);
});

test('DBML and normalized JSON imports produce structured drafts', async () => {
  const dbml = await parseSchemaInput('Table users {\n id int [pk]\n email varchar [unique]\n}', { format: 'dbml' });
  const json = await parseSchemaInput(JSON.stringify({ tables: [{ id: 't', name: 'users', fields: [{ id: 'c', name: 'id', type: 'int', primaryKey: true }] }], refs: [] }), { format: 'json' });
  assert.equal(importDraftSchema(dbml).tables[0].keys.length, 2);
  assert.equal(importDraftSchema(json).tables[0].keys[0].column_ids[0], 'c');
});

test('DBML preserves composite indexes and directed relationship actions', async () => {
  const dbml = `Table app.parents {
  a int
  b int
  indexes {
    (a, b) [pk, name: "parent_pk"]
  }
}
Table app.children {
  a int
  b int
  indexes {
    (a, b) [unique, name: "child_ix"]
  }
}
Ref child_fk: app.children.(a, b) > app.parents.(a, b) [delete: cascade, update: restrict]`;
  const parsed = await parseSchemaInput(dbml, { format: 'dbml' });
  const draft = importDraftSchema(parsed);
  const parent = draft.tables.find(table => table.name === 'parents')!;
  const child = draft.tables.find(table => table.name === 'children')!;
  assert.deepEqual(parent.keys.map(key => [key.name, key.kind, key.column_ids]), [['parent_pk', 'primary', parent.columns.map(column => column.id)]]);
  assert.deepEqual(child.indexes.map(index => [index.name, index.unique, index.column_ids]), [['child_ix', true, child.columns.map(column => column.id)]]);
  assert.deepEqual(draft.relationships.map(rel => [rel.name, rel.source_table_id, rel.source_column_ids, rel.target_table_id, rel.target_column_ids, rel.on_delete, rel.on_update]), [['child_fk', child.id, child.columns.map(column => column.id), parent.id, parent.columns.map(column => column.id), 'cascade', 'restrict']]);
  assert.match(parsed.warnings[0].message, /first column/);
  assert.deepEqual(importDraftSchema(await parseSchemaInput(schemaToJson(parsed), { format: 'json' })), draft);
});

test('DBML inline references and namespaced table IDs retain their direction', async () => {
  const dbml = `Table app.posts {\n user_id int [ref: > app.users.id]\n}\nTable app.users {\n id int [pk]\n}\nTable other.users {\n id int\n}`;
  const draft = importDraftSchema(await parseSchemaInput(dbml, { format: 'dbml' }));
  const posts = draft.tables.find(table => table.name === 'posts')!;
  const users = draft.tables.find(table => table.name === 'users' && table.namespace === 'app')!;
  assert.equal(draft.relationships[0].source_table_id, posts.id);
  assert.equal(draft.relationships[0].target_table_id, users.id);
  assert.equal(new Set(draft.tables.flatMap(table => table.columns.map(column => column.id))).size, 3);
});

test('DBML unsupported indexes cannot silently disappear from drafts', async () => {
  for (const index of ['(id)', '(`lower(name)`) [name: "expr"]', '(id) [name: "hash_idx", type: hash]']) {
    const dbml = `Table users {\n id int\n name varchar\n indexes {\n ${index}\n }\n}`;
    const parsed = await parseSchemaInput(dbml, { format: 'dbml' });
    assert.ok(parsed.warnings.some(item => item.severity === 'error'));
    assert.throws(() => importDraftSchema(parsed), /valid schema/);
  }
});

test('DBML many-to-many references require a junction table', async () => {
  const dbml = 'Table a {\n id int\n}\nTable b {\n id int\n}\nRef: a.id <> b.id';
  const parsed = await parseSchemaInput(dbml, { format: 'dbml' });
  assert.ok(parsed.warnings.some(item => item.severity === 'error' && /junction table/.test(item.message)));
  assert.throws(() => importDraftSchema(parsed), /valid schema/);
});

test('normalized JSON retains explicitly supplied column definitions', async () => {
  const mysqlDefinition = { unsigned: true, charset: null, collation: null, comment: null, default_kind: 'none', other_attributes_absent: true };
  const schema = await parseSchemaInput({ tables: [{ id: 't', name: 'users', fields: [{ id: 'c', name: 'id', type: 'int', auto_increment: true, mysql_definition: mysqlDefinition, collation: 'pg_catalog.C' }] }], refs: [] }, { format: 'json' });
  const column = importDraftSchema(schema).tables[0].columns[0];
  assert.deepEqual(column.mysql_definition, mysqlDefinition);
  assert.equal(column.collation, 'pg_catalog.C');
  assert.equal(column.auto_increment, true);
  const roundTrip = await parseSchemaInput(JSON.stringify(schema), { format: 'json' });
  assert.deepEqual(importDraftSchema(roundTrip).tables[0].columns[0].mysql_definition, mysqlDefinition);
});

test('invalid schemas and unresolved or ambiguous references cannot become drafts', async () => {
  const invalid = await parseSchemaInput('not sql', { format: 'sql' });
  assert.throws(() => importDraftSchema(invalid), /valid schema/);
  const unresolved = await parseSchemaInput('CREATE TABLE posts (user_id INTEGER REFERENCES missing(id));', { format: 'sql' });
  assert.throws(() => importDraftSchema(unresolved), /unsupported references/);
  const ambiguous = await parseSchemaInput({ tables: [{ id: 'a', name: 'users', schema: 'a', fields: [{ id: 'a.id', name: 'id', type: 'int' }] }, { id: 'b', name: 'users', schema: 'b', fields: [{ id: 'b.id', name: 'id', type: 'int' }] }], refs: [{ id: 'r', sourceTable: 'users', sourceColumn: 'id', targetTable: 'users', targetColumn: 'id' }] }, { format: 'json' });
  assert.throws(() => importDraftSchema(ambiguous), /uniquely resolve/);
});


test('Runtime structured JSON retains expressions, identity and table inspection metadata', async () => {
  const original = { version: 1, tables: [{ id: 'runtime-table', name: 'items', namespace: 'app', columns: [{ id: 'runtime-column', name: 'id', type: 'numeric(30, 4)', nullable: false, default: null, default_expression: "nextval('app.items_id_seq'::regclass)", identity: 'by default', auto_increment: true }], keys: [], indexes: [], inspection_metadata: ['CHECK (id > 0)', 'Catalog trigger retained as metadata.'] }], relationships: [] };
  const normalized = await parseSchemaInput(original, { format: 'json' });
  assert.equal(normalized.tables[0].fields[0].defaultExpression, original.tables[0].columns[0].default_expression);
  assert.deepEqual(importDraftSchema(normalized), original);
  assert.deepEqual(importDraftSchema(await parseSchemaInput(schemaToJson(normalized), { format: 'json' })), original);
  const { structuredDraft, ...viewOnly } = normalized;
  const column = importDraftSchema(viewOnly).tables[0].columns[0];
  assert.equal(column.default_expression, original.tables[0].columns[0].default_expression);
  assert.equal(column.identity, 'by default');
  assert.deepEqual(importDraftSchema(viewOnly).tables[0].inspection_metadata, original.tables[0].inspection_metadata);
});
