import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { trackSchemaIdentities } from '../src/utils/schemaIdentity';

test('retains identities across deletion and permits restoration', () => {
  const ledger = new Map<string, string>();
  const schema = { tables: [{ id: 'table', columns: [{ id: 'column' }], keys: [], indexes: [] }], relationships: [] };
  trackSchemaIdentities(schema, ledger);
  trackSchemaIdentities({ tables: [], relationships: [] }, ledger);
  trackSchemaIdentities(schema, ledger);
  assert.throws(() => trackSchemaIdentities({ tables: [{ id: 'other', columns: [{ id: 'column' }], keys: [], indexes: [] }], relationships: [] }, ledger), /cannot be reused/);
  assert.throws(() => trackSchemaIdentities({ tables: [], relationships: [{ id: 'column' }] }, ledger), /cannot be reused/);
});

test('does not record new identities when another identity conflicts', () => {
  const ledger = new Map<string, string>();
  trackSchemaIdentities({ tables: [{ id: 'original', columns: [], keys: [], indexes: [] }], relationships: [] }, ledger);
  assert.throws(() => trackSchemaIdentities({ tables: [{ id: 'new', columns: [{ id: 'original' }], keys: [], indexes: [] }], relationships: [] }, ledger), /cannot be reused/);
  assert.equal(ledger.has('new'), false);
  trackSchemaIdentities({ tables: [{ id: 'new', columns: [], keys: [], indexes: [] }], relationships: [] }, ledger);
  assert.equal(ledger.has('new'), true);
});
