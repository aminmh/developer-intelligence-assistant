import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import test from 'node:test';
import { registerRuntimeSchemaRoutes } from '../src/server/runtimeSchemaRoutes';
import { registerSchemaDraftRoutes } from '../src/server/schemaDraftRoutes';

const source = { project_id: 'shop', environment: 'development', host_id: 'a'.repeat(64),
  container_id: 'b'.repeat(64), image_id: 'sha256:' + 'c'.repeat(64), service: 'database',
  state: 'running', updated_at: '2026-11-14 12:00:00' };
const secret = 's'.repeat(40);
const origin = 'https://app.example';
function harness() {
  const routes = new Map<string, (req: any, res: any) => Promise<any>>();
  const app = Object.fromEntries(['get', 'post', 'put', 'delete'].map(method => [method,
    (path: string, handler: (req: any, res: any) => Promise<any>) => routes.set(`${method.toUpperCase()} ${path}`, handler)]));
  let admin = true;
  let user: { sub: string } | null = { sub: 'alice' };
  const dependencies = { apiUrl: 'http://python-api', internalSecret: secret, webOrigin: origin,
    getUser: () => user, isGlobalAdmin: () => admin };
  registerRuntimeSchemaRoutes(app as any, dependencies);
  registerSchemaDraftRoutes(app as any, dependencies);
  return { routes, setAdmin: (value: boolean) => { admin = value; }, setUser: (value: typeof user) => { user = value; } };
}
function response() {
  return { statusCode: 200, body: undefined as any, headers: {} as Record<string, string>,
    status(value: number) { this.statusCode = value; return this; },
    json(value: unknown) { this.body = value; return this; },
    setHeader(key: string, value: string) { this.headers[key] = value; } };
}
const request = (body: unknown) => ({ method: 'POST', body, get: () => origin });

test('schema authorization binds every execution field and checks current inventory', async () => {
  const { routes } = harness();
  const original = globalThis.fetch;
  let inventory = [source];
  let calls = 0;
  globalThis.fetch = (async (url, init) => {
    calls++;
    const target = new URL(String(url));
    assert.equal(target.pathname, '/schema-drafts/runtime/sources');
    assert.equal(target.searchParams.get('project_id'), 'shop');
    assert.equal(target.searchParams.get('environment'), 'development');
    assert.equal((init?.headers as Record<string, string>)['X-User-Id'], 'alice');
    return Response.json({ sources: inventory });
  }) as typeof fetch;
  try {
    const handler = routes.get('POST /api/runtime/schema-authorization')!;
    const payload = { source, operation: 'extract', database: 'app', schemas: ['public'] };
    const ok = response(); await handler(request(payload), ok);
    assert.equal(ok.statusCode, 200);
    assert.equal(ok.headers['Cache-Control'], 'no-store');
    const [head, body, signature] = ok.body.token.split('.');
    const claims = JSON.parse(Buffer.from(body, 'base64url').toString());
    assert.deepEqual(claims.schemas, ['public']);
    for (const key of ['project_id', 'environment', 'host_id', 'container_id', 'image_id'] as const) assert.equal(claims[key], source[key]);
    assert.equal(claims.database, 'app'); assert.equal(claims.sub, 'alice');
    assert.equal(claims.origin, origin); assert.equal(claims.operation, 'runtime-schema-extract');
    assert.ok(claims.exp > Date.now() / 1000 && claims.exp <= Date.now() / 1000 + 120);
    const key = crypto.createHmac('sha256', secret).update('devassistant-runtime-helper-v1').digest();
    assert.equal(signature, crypto.createHmac('sha256', key).update(`${head}.${body}`).digest('base64url'));
    for (const change of [{ host_id: 'd'.repeat(64) }, { image_id: 'sha256:replaced' }, { container_id: 'd'.repeat(64) }]) {
      const stale = response(); await handler(request({ ...payload, source: { ...source, ...change } }), stale);
      assert.equal(stale.statusCode, 409);
    }
    inventory = [];
    const absent = response(); await handler(request(payload), absent);
    assert.equal(absent.statusCode, 409); assert.equal(calls, 5);
  } finally { globalThis.fetch = original; }
});

test('schema discovery cannot be authorized by another origin, nonadmin, or malformed scope', async () => {
  const h = harness();
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => { calls++; return Response.json({ sources: [source] }); }) as typeof fetch;
  try {
    const handler = h.routes.get('POST /api/runtime/schema-authorization')!;
    const payload = { source, operation: 'discover' };
    const foreign = response(); await handler({ ...request(payload), get: () => 'https://evil.example' }, foreign);
    assert.equal(foreign.statusCode, 403);
    h.setAdmin(false);
    const nonadmin = response(); await handler(request(payload), nonadmin); assert.equal(nonadmin.statusCode, 403);
    h.setAdmin(true); h.setUser(null);
    const anonymous = response(); await handler(request(payload), anonymous); assert.equal(anonymous.statusCode, 401);
    h.setUser({ sub: 'alice' });
    for (const bad of [
      { ...payload, operation: 'setup' }, { ...payload, source: { ...source, container_id: 'b'.repeat(12) } },
      { ...payload, database: 'app\nINJECT' }, { ...payload, schemas: ['public', 'public'] },
      { ...payload, operation: 'extract' }, { ...payload, password: 'never-accepted' },
    ]) {
      const denied = response(); await handler(request(bad), denied); assert.equal(denied.statusCode, 400);
    }
    assert.equal(calls, 0);
  } finally { globalThis.fetch = original; }
});

test('source listing filters project/environment and refuses malformed inventory', async () => {
  const { routes } = harness(); const original = globalThis.fetch;
  let inventory: unknown = [source, { ...source, project_id: 'other' }, { ...source, environment: 'production' }];
  globalThis.fetch = (async () => Response.json({ sources: inventory })) as typeof fetch;
  try {
    const handler = routes.get('GET /api/runtime/schema-sources')!;
    const req = { method: 'GET', query: { project_id: 'shop', environment: 'development' } };
    const ok = response(); await handler(req, ok); assert.deepEqual(ok.body.sources, [source]);
    inventory = 'bad';
    const invalid = response(); await handler(req, invalid); assert.equal(invalid.statusCode, 502);
    inventory = []; const empty = response(); await handler(req, empty); assert.deepEqual(empty.body, { sources: [] });
  } finally { globalThis.fetch = original; }
});

test('runtime draft proxy sends signed envelope and trusted origin to persistence', async () => {
  const { routes } = harness(); const original = globalThis.fetch;
  const envelope = { schema: { version: 1, tables: [], relationships: [] }, provenance: { project_id: 'shop' }, receipt: 'receipt', sub: 'alice', origin, expires_at: 123 };
  globalThis.fetch = (async (url, init) => {
    assert.equal(url, 'http://python-api/schema-drafts/runtime');
    assert.equal((init?.headers as Record<string, string>)['X-Application-Origin'], origin);
    assert.deepEqual(JSON.parse(init?.body as string), envelope);
    return Response.json({ id: 'saved' }, { status: 201 });
  }) as typeof fetch;
  try {
    const result = response(); await routes.get('POST /api/schema-drafts/runtime')!(request(envelope), result);
    assert.equal(result.statusCode, 201); assert.equal(result.body.id, 'saved');
  } finally { globalThis.fetch = original; }
});
