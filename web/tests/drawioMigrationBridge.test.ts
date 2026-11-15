import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readMigrationRequest, readSchemaExportRequest, readInspectionRequest } from '../src/utils/drawioMigrationBridge';
import { MAX_LAYOUT_BYTES } from '../src/utils/drawioBridge';

test('migration bridge accepts only bounded requests from the editor window and origin', () => {
  const popup = {} as Window;
  const origin = 'https://drawio.example';
  const payload = { event: 'schema-migration-preview', requestId: 'request-1', xml: '<mxGraphModel/>', framework: 'laravel', database: 'mysql', timestamp: '2026_11_02_120000' };
  const event = (data: unknown, source = popup, sender = origin) => ({ data: JSON.stringify(data), source, origin: sender });
  assert.deepEqual(readMigrationRequest(event(payload), origin, popup), payload);
  assert.equal(readMigrationRequest(event(payload, {} as Window), origin, popup), null);
  assert.equal(readMigrationRequest(event(payload, popup, 'https://evil.example'), origin, popup), null);
  assert.equal(readMigrationRequest(event({ ...payload, framework: 'unknown' }), origin, popup), null);
  assert.equal(readMigrationRequest(event({ ...payload, requestId: '../invalid' }), origin, popup), null);
  assert.equal(readMigrationRequest(event({ ...payload, xml: 'x'.repeat(MAX_LAYOUT_BYTES + 1) }), origin, popup), null);
  assert.equal(readMigrationRequest(event({ event: 'schema-migration-download', requestId: 'a', xml: '<mxfile/>', confirmed: 'true' }), origin, popup), null);
});

test('schema export accepts only bounded requests from the connected editor', () => {
  const popup = {} as Window;
  const origin = 'https://drawio.example';
  const payload = { event: 'schema-export-json', requestId: 'request-1', xml: '<mxGraphModel/>' };
  const event = (data: unknown, source = popup, sender = origin) => ({ data: JSON.stringify(data), source, origin: sender });
  assert.deepEqual(readSchemaExportRequest(event(payload), origin, popup), payload);
  assert.deepEqual(readSchemaExportRequest(event({ ...payload, event: 'schema-export-dbml' }), origin, popup), { ...payload, event: 'schema-export-dbml' });
  assert.equal(readSchemaExportRequest(event(payload, {} as Window), origin, popup), null);
  assert.equal(readSchemaExportRequest(event(payload, popup, 'https://evil.example'), origin, popup), null);
  assert.equal(readSchemaExportRequest(event({ ...payload, requestId: '../bad' }), origin, popup), null);
  assert.equal(readSchemaExportRequest(event({ ...payload, xml: 'é'.repeat(MAX_LAYOUT_BYTES / 2 + 1) }), origin, popup), null);
  assert.equal(readSchemaExportRequest(event({ ...payload, event: 'schema-migration-preview' }), origin, popup), null);
});


test('inspection bridge requires consent, a configured target name, and the connected editor', () => {
  const popup = {} as Window, origin = 'https://drawio.example';
  const payload = { event: 'schema-inspect', requestId: 'request-1', xml: '<mxGraphModel/>', target: 'read_only', consent: true };
  const event = (data: unknown, source = popup, sender = origin) => ({ data: JSON.stringify(data), source, origin: sender });
  assert.deepEqual(readInspectionRequest(event(payload), origin, popup), payload);
  assert.equal(readInspectionRequest(event({ ...payload, consent: false }), origin, popup), null);
  assert.equal(readInspectionRequest(event({ ...payload, target: 'host:5432' }), origin, popup), null);
  assert.equal(readInspectionRequest(event(payload, {} as Window), origin, popup), null);
  assert.equal(readInspectionRequest(event(payload, popup, 'https://evil.example'), origin, popup), null);
});
