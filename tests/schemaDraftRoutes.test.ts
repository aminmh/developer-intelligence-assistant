import assert from 'node:assert/strict';
import test from 'node:test';
import { registerSchemaDraftRoutes } from '../src/server/schemaDraftRoutes';

test('schema draft proxy enforces administrator and origin before forwarding', async () => {
  const routes = new Map<string, (...args: any[]) => Promise<any>>();
  const app = Object.fromEntries(['get', 'post', 'put'].map((method) => [method,
    (path: string, handler: (...args: any[]) => Promise<any>) => routes.set(`${method.toUpperCase()} ${path}`, handler)]));
  let admin = false;
  registerSchemaDraftRoutes(app as any, { apiUrl: 'http://python-api', internalSecret: 's'.repeat(40),
    webOrigin: 'http://localhost:3000', getUser: () => ({ sub: 'alice' }), isGlobalAdmin: () => admin });
  const response = () => ({ statusCode: 200, body: undefined as any,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } });
  const req = { method: 'POST', params: { analysisId: 'analysis-1' }, get: () => 'http://localhost:3000' };
  let calls = 0;
  const original = globalThis.fetch;
  globalThis.fetch = (async (url, init) => {
    calls++;
    assert.equal(url, 'http://python-api/schema-drafts/analysis/analysis-1');
    assert.equal((init?.headers as Record<string, string>)['X-User-Id'], 'alice');
    return new Response(JSON.stringify({ id: 'draft-1' }), { status: 201 });
  }) as typeof fetch;
  try {
    const handler = routes.get('POST /api/schema-drafts/analysis/:analysisId')!;
    const denied = response(); await handler(req, denied);
    assert.equal(denied.statusCode, 403); assert.equal(calls, 0);
    admin = true;
    const crossOrigin = response(); await handler({ ...req, get: () => 'http://other' }, crossOrigin);
    assert.equal(crossOrigin.statusCode, 403); assert.equal(calls, 0);
    const allowed = response(); await handler(req, allowed);
    assert.equal(allowed.statusCode, 201); assert.equal(allowed.body.id, 'draft-1'); assert.equal(calls, 1);
  } finally { globalThis.fetch = original; }
});

test('migration proxy forwards JSON and streams only safely named attachments', async () => {
  const routes = new Map<string, (...args: any[]) => Promise<any>>();
  const app = Object.fromEntries(['get', 'post', 'put'].map((method) => [method,
    (path: string, handler: (...args: any[]) => Promise<any>) => routes.set(`${method.toUpperCase()} ${path}`, handler)]));
  registerSchemaDraftRoutes(app as any, { apiUrl: 'http://python-api', internalSecret: 'secret', webOrigin: 'https://app', getUser: () => ({ sub: 'alice' }), isGlobalAdmin: () => true });
  const response = () => ({ statusCode: 200, body: undefined as any, headers: {} as Record<string, string>,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; },
    setHeader(name: string, value: string) { this.headers[name] = value; }, send(body: unknown) { this.body = body; return this; } });
  const payload = { framework: 'laravel', database: 'mysql', revision: 2, migration_timestamp: '2026_11_02_120000', preview_token: 'a'.repeat(64), confirmed: true, reviewed_preview: { files: [{ name: 'migration.php', content: '<?php' }] } };
  const req = { method: 'POST', params: { draftId: 'draft/1' }, body: payload, get: () => 'https://app' };
  let disposition = 'attachment; filename="migration.php"';
  const original = globalThis.fetch;
  globalThis.fetch = (async (url, init) => {
    assert.equal(url, 'http://python-api/schema-drafts/draft%2F1/migration-download');
    assert.deepEqual(JSON.parse(init!.body as string), payload);
    assert.equal((init!.headers as Record<string, string>)['Content-Type'], 'application/json');
    return new Response('<?php', { headers: { 'Content-Disposition': disposition } });
  }) as typeof fetch;
  try {
    const handler = routes.get('POST /api/schema-drafts/:draftId/migration-download')!;
    const ok = response(); await handler(req, ok);
    assert.equal(ok.body.toString(), '<?php');
    assert.equal(ok.headers['Content-Disposition'], disposition);
    disposition = 'attachment; filename="../unsafe.php"';
    const bad = response(); await handler(req, bad);
    assert.equal(bad.statusCode, 502);
    const denied = response(); await handler({ ...req, get: () => 'https://other' }, denied);
    assert.equal(denied.statusCode, 403);
  } finally { globalThis.fetch = original; }
});

test('migration preview forwards its saved revision and enforces the request limit', async () => {
  const routes = new Map<string, (...args: any[]) => Promise<any>>();
  const app = Object.fromEntries(['get', 'post', 'put'].map((method) => [method,
    (path: string, handler: (...args: any[]) => Promise<any>) => routes.set(`${method.toUpperCase()} ${path}`, handler)]));
  registerSchemaDraftRoutes(app as any, { apiUrl: 'http://python-api', internalSecret: 'secret', webOrigin: 'https://app', getUser: () => ({ sub: 'alice' }), isGlobalAdmin: () => true });
  const response = () => ({ statusCode: 200, body: undefined as any, status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } });
  const payload = { framework: 'symfony', database: 'postgresql', revision: 3, migration_timestamp: '2026_11_02_120000' };
  let calls = 0;
  const original = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  let timeout = 0;
  AbortSignal.timeout = (milliseconds: number) => { timeout = milliseconds; return originalTimeout(milliseconds); };
  globalThis.fetch = (async (url, init) => {
    calls++;
    assert.equal(url, 'http://python-api/schema-drafts/draft/migration-preview');
    assert.deepEqual(JSON.parse(init!.body as string), payload);
    return new Response(JSON.stringify({ generation_blocked: true, generation_error: 'unsupported' }), { status: 200 });
  }) as typeof fetch;
  try {
    const handler = routes.get('POST /api/schema-drafts/:draftId/migration-preview')!;
    const req = { method: 'POST', params: { draftId: 'draft' }, body: payload, get: () => 'https://app' };
    const ok = response(); await handler(req, ok);
    assert.equal(ok.body.generation_blocked, true);
    assert.equal(timeout, 75000);
    const large = response(); await handler({ ...req, body: { value: 'x'.repeat(9 * 1024 * 1024) } }, large);
    assert.equal(large.statusCode, 413); assert.equal(calls, 1);
  } finally { globalThis.fetch = original; AbortSignal.timeout = originalTimeout; }
});
