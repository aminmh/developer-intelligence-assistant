import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseSchemaInput } from '../src/utils/erdTransformer';
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

test('DBML and normalized JSON imports produce structured drafts', async () => {
  const dbml = await parseSchemaInput('Table users {\n id int [pk]\n email varchar [unique]\n}', { format: 'dbml' });
  const json = await parseSchemaInput(JSON.stringify({ tables: [{ id: 't', name: 'users', fields: [{ id: 'c', name: 'id', type: 'int', primaryKey: true }] }], refs: [] }), { format: 'json' });
  assert.equal(importDraftSchema(dbml).tables[0].keys.length, 2);
  assert.equal(importDraftSchema(json).tables[0].keys[0].column_ids[0], 'c');
});

test('normalized JSON retains explicitly supplied column definitions', async () => {
  const mysqlDefinition = { unsigned: true, charset: null, collation: null, comment: null, default_kind: 'none', other_attributes_absent: true };
  const schema = await parseSchemaInput({ tables: [{ id: 't', name: 'users', fields: [{ id: 'c', name: 'id', type: 'int', auto_increment: true, mysql_definition: mysqlDefinition, collation: 'pg_catalog.C' }] }], refs: [] }, { format: 'json' });
  const column = importDraftSchema(schema).tables[0].columns[0];
  assert.deepEqual(column.mysql_definition, mysqlDefinition);
  assert.equal(column.collation, 'pg_catalog.C');
  assert.equal(column.auto_increment, true);
});

test('invalid schemas and unresolved or ambiguous references cannot become drafts', async () => {
  const invalid = await parseSchemaInput('not sql', { format: 'sql' });
  assert.throws(() => importDraftSchema(invalid), /valid schema/);
  const unresolved = await parseSchemaInput('CREATE TABLE posts (user_id INTEGER REFERENCES missing(id));', { format: 'sql' });
  assert.throws(() => importDraftSchema(unresolved), /unsupported references/);
  const ambiguous = await parseSchemaInput({ tables: [{ id: 'a', name: 'users', schema: 'a', fields: [{ id: 'a.id', name: 'id', type: 'int' }] }, { id: 'b', name: 'users', schema: 'b', fields: [{ id: 'b.id', name: 'id', type: 'int' }] }], refs: [{ id: 'r', sourceTable: 'users', sourceColumn: 'id', targetTable: 'users', targetColumn: 'id' }] }, { format: 'json' });
  assert.throws(() => importDraftSchema(ambiguous), /uniquely resolve/);
});
