import assert from 'node:assert/strict';
import test from 'node:test';
import { registerContractRoutes } from '../src/server/contractRoutes';

const SOURCE = 'cs_' + 'a'.repeat(32);
const ORIGIN = 'http://localhost:3000';

function setup(roles: Record<string, 'viewer' | 'operator'>) {
  const routes = new Map<string, (...args: any[]) => Promise<any>>();
  const app = Object.fromEntries(['get', 'post', 'put', 'delete'].map((method) => [method,
    (path: string, ...handlers: Array<(...args: any[]) => Promise<any>>) => routes.set(`${method.toUpperCase()} ${path}`, handlers.at(-1)!)]));
  registerContractRoutes(app as any, {
    apiUrl: 'http://python-api', internalSecret: 's'.repeat(40), webOrigin: ORIGIN,
    getUser: () => ({ sub: 'alice' }),
    canAccessProject: (_user, projectId) => projectId in roles,
    canOperateProject: (_user, projectId) => roles[projectId] === 'operator',
  });
  const response = () => ({ statusCode: 200, body: undefined as any,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } });
  const call = async (route: string, req: Record<string, unknown>) => {
    const res = response();
    await routes.get(route)!({ get: () => ORIGIN, params: {}, body: {}, ...req }, res);
    return res;
  };
  return { call };
}

async function withPython(handler: (url: string, init?: RequestInit) => unknown, run: (calls: string[]) => Promise<void>) {
  const original = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method || 'GET'} ${url}`);
    assert.equal((init?.headers as Record<string, string>)['X-Internal-Secret'], 's'.repeat(40));
    return new Response(JSON.stringify(handler(url, init) ?? {}), { status: 200 });
  }) as typeof fetch;
  try { await run(calls); } finally { globalThis.fetch = original; }
}

test('contract changes need operator access and the application origin', async () => {
  const { call } = setup({ web: 'viewer', backend: 'operator' });
  await withPython(() => ({ source: { source_id: SOURCE } }), async (calls) => {
    const create = 'POST /api/projects/:projectId/contracts/sources';
    assert.equal((await call(create, { params: { projectId: 'web' }, body: { kind: 'upload', name: 'A' } })).statusCode, 403);
    assert.equal((await call(create, { params: { projectId: 'backend' }, get: () => 'http://evil' })).statusCode, 403);
    assert.equal((await call(create, { params: { projectId: '../x' } })).statusCode, 400);
    assert.equal(calls.length, 0);
    const ok = await call(create, { params: { projectId: 'backend' }, body: { kind: 'upload', name: 'A', user_id: 'mallory' } });
    assert.equal(ok.statusCode, 200);
    assert.deepEqual(calls, ['POST http://python-api/contracts/projects/backend/sources']);
    // A viewer can still read.
    assert.equal((await call('GET /api/projects/:projectId/contracts', { params: { projectId: 'web' } })).statusCode, 200);
    assert.equal((await call('GET /api/projects/:projectId/contracts', { params: { projectId: 'other' } })).statusCode, 403);
  });
});

test('the creating user comes from the session, not the request body', async () => {
  const { call } = setup({ backend: 'operator' });
  let sent: any;
  await withPython((_url, init) => { sent = JSON.parse(init!.body as string); return {}; }, async () => {
    await call('POST /api/projects/:projectId/contracts/sources',
      { params: { projectId: 'backend' }, body: { kind: 'url', name: 'A', url: 'https://x', user_id: 'mallory', extra: 1 } });
    assert.deepEqual(sent, { kind: 'url', name: 'A', url: 'https://x', user_id: 'alice' });
  });
});

test('a contract is visible only through an accessible project', async () => {
  const hidden = { source: { source_id: SOURCE, project_id: 'backend', attachments: [{ project_id: 'admin-ui' }] } };
  const versions = 'GET /api/contracts/sources/:sourceId/versions';
  await withPython((url) => url.endsWith('/versions') ? { versions: [] } : hidden, async (calls) => {
    const outsider = setup({ web: 'operator' });
    assert.equal((await outsider.call(versions, { params: { sourceId: SOURCE } })).statusCode, 404);
    assert.equal((await outsider.call(versions, { params: { sourceId: 'cs_bad' } })).statusCode, 400);
    assert.ok(!calls.some((entry) => entry.endsWith('/versions')));
    // Access to an attached project is enough to read; attaching needs the owning project.
    const consumer = setup({ 'admin-ui': 'operator' });
    assert.equal((await consumer.call(versions, { params: { sourceId: SOURCE } })).statusCode, 200);
    const attach = 'POST /api/projects/:projectId/contracts/attachments';
    const body = { source_id: SOURCE, role: 'consumer' };
    assert.equal((await consumer.call(attach, { params: { projectId: 'admin-ui' }, body })).statusCode, 403);
    const both = setup({ web: 'operator', backend: 'viewer' });
    assert.equal((await both.call(attach, { params: { projectId: 'web' }, body })).statusCode, 200);
  });
});

test('the source list shows only contracts of accessible projects', async () => {
  const { call } = setup({ backend: 'viewer' });
  await withPython(() => ({ sources: [{ source_id: SOURCE, project_id: 'backend' }, { source_id: 'cs_x', project_id: 'secret' }] }), async () => {
    const res = await call('GET /api/contracts/sources', {});
    assert.deepEqual(res.body.sources.map((row: any) => row.project_id), ['backend']);
  });
});

test('an uploaded document must be text and is forwarded as bytes', async () => {
  const { call } = setup({ backend: 'operator' });
  const route = 'POST /api/projects/:projectId/contracts/sources/:sourceId/document';
  let body: unknown;
  await withPython((_url, init) => { body = init!.body; return { outcome: 'created' }; }, async (calls) => {
    assert.equal((await call(route, { params: { projectId: 'backend', sourceId: SOURCE }, body: {} })).statusCode, 400);
    assert.equal(calls.length, 0);
    const ok = await call(route, { params: { projectId: 'backend', sourceId: SOURCE }, body: 'openapi: 3.0.0' });
    assert.equal(ok.body.outcome, 'created'); assert.equal(body, 'openapi: 3.0.0');
  });
});

test('reports need project access and validate their query', async () => {
  const ATTACHMENT = 'ca_' + 'b'.repeat(32);
  const VERSION = 'cv_' + 'c'.repeat(32);
  const { call } = setup({ web: 'viewer' });
  await withPython(() => ({ available: true }), async (calls) => {
    const impact = 'GET /api/projects/:projectId/contracts/attachments/:attachmentId/impact';
    const conformance = 'GET /api/projects/:projectId/contracts/attachments/:attachmentId/conformance';
    assert.equal((await call(conformance, { params: { projectId: 'other', attachmentId: ATTACHMENT }, query: {} })).statusCode, 403);
    assert.equal((await call(conformance, { params: { projectId: 'web', attachmentId: 'ca_x' }, query: {} })).statusCode, 400);
    assert.equal((await call(impact, { params: { projectId: 'web', attachmentId: ATTACHMENT }, query: { from_version: 'latest' } })).statusCode, 400);
    assert.equal(calls.length, 0);
    assert.equal((await call(conformance, { params: { projectId: 'web', attachmentId: ATTACHMENT }, query: {} })).statusCode, 200);
    await call(impact, { params: { projectId: 'web', attachmentId: ATTACHMENT }, query: { from_version: VERSION, to_version: VERSION } });
    assert.deepEqual(calls, [
      `GET http://python-api/contracts/projects/web/attachments/${ATTACHMENT}/conformance?`,
      `GET http://python-api/contracts/projects/web/attachments/${ATTACHMENT}/impact?from_version=${VERSION}&to_version=${VERSION}`,
    ]);
  });
});

test('live traffic report is forwarded for project members', async () => {
  const ATTACHMENT = 'ca_' + 'd'.repeat(32);
  const { call } = setup({ backend: 'viewer' });
  await withPython(() => ({ available: false }), async (calls) => {
    const route = 'GET /api/projects/:projectId/contracts/attachments/:attachmentId/runtime';
    assert.equal((await call(route, { params: { projectId: 'web', attachmentId: ATTACHMENT }, query: {} })).statusCode, 403);
    const ok = await call(route, { params: { projectId: 'backend', attachmentId: ATTACHMENT }, query: { revision_id: 'rev_' + 'a'.repeat(32) } });
    assert.equal(ok.statusCode, 200);
    assert.deepEqual(calls, [`GET http://python-api/contracts/projects/backend/attachments/${ATTACHMENT}/runtime?revision_id=rev_${'a'.repeat(32)}`]);
  });
});

test('code generation needs a visible contract and forwards only its own fields', async () => {
  const VERSION = 'cv_' + 'e'.repeat(32);
  const route = 'POST /api/contracts/sources/:sourceId/versions/:versionId/generate';
  let sent: any;
  await withPython((url, init) => {
    if (url.endsWith('/generate')) { sent = JSON.parse(init!.body as string); return { files: [] }; }
    return { source: { source_id: SOURCE, project_id: 'backend', attachments: [{ project_id: 'web' }] } };
  }, async (calls) => {
    const outsider = setup({ other: 'operator' });
    assert.equal((await outsider.call(route, { params: { sourceId: SOURCE, versionId: VERSION }, body: { target: 'vue' } })).statusCode, 404);
    assert.equal((await outsider.call(route, { params: { sourceId: SOURCE, versionId: 'x' }, body: {} })).statusCode, 400);
    assert.ok(!calls.some((entry) => entry.endsWith('/generate')));
    const consumer = setup({ web: 'viewer' });   // a viewer of an attached project may generate
    const ok = await consumer.call(route, { params: { sourceId: SOURCE, versionId: VERSION }, body: { target: 'vue', search: 'orders', evil: 1 } });
    assert.equal(ok.statusCode, 200);
    assert.deepEqual(sent, { target: 'vue', search: 'orders' });
  });
});
