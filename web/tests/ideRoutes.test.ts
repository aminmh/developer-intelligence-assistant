import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { githubRepository, registerIdeApi, registerIdeTokenRoutes } from '../src/server/ideRoutes';

const ORIGIN = 'http://localhost:3000';
const EVENT = 'ce_' + 'b'.repeat(32);
const SOURCE = 'cs_' + 'a'.repeat(32);
const VERSION = 'cv_' + 'c'.repeat(32);

type Handler = (...args: any[]) => any;

function setup(access: string[], { tokenUser = { sub: 'alice' } as { sub: string } | null } = {}) {
  const routes = new Map<string, Handler>();
  let middleware: Handler | undefined;
  const tokens: any[] = [];
  const app = {
    use: (_path: string, handler: Handler) => { middleware = handler; },
    ...Object.fromEntries(['get', 'post', 'put', 'delete'].map((method) => [method,
      (route: string, ...handlers: Handler[]) => routes.set(`${method.toUpperCase()} ${route}`, handlers.at(-1)!)])),
  };
  const deps = {
    apiUrl: 'http://python-api', internalSecret: 's'.repeat(40), webOrigin: ORIGIN,
    userForToken: (header: string | undefined) => (header === 'Bearer good' ? tokenUser : null),
    canAccessProject: (_user: unknown, projectId: string) => access.includes(projectId),
    getUser: () => ({ sub: 'alice' }),
    createToken: (_user: unknown, name: string) => {
      const row = { token_id: 'f'.repeat(24), name, created_at: '', expires_at: '', last_used_at: null };
      tokens.push(row);
      return { token: 'ide_secret', row };
    },
    listTokens: () => tokens,
    revokeToken: (_user: unknown, tokenId: string) => tokens.some((row) => row.token_id === tokenId),
  };
  registerIdeApi(app as any, deps);
  registerIdeTokenRoutes(app as any, deps);
  const response = () => ({ statusCode: 200, body: undefined as any, locals: {} as Record<string, unknown>,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } });
  /** Run the token middleware (for /api/ide routes) and then the route handler. */
  const call = async (route: string, req: Record<string, any> = {}) => {
    const res = response();
    const headers: Record<string, string> = { authorization: 'Bearer good', origin: ORIGIN, ...(req.headers || {}) };
    const request = { method: route.split(' ')[0], params: {}, query: {}, body: {}, ...req, get: (name: string) => headers[name.toLowerCase()] };
    let passed = !route.includes('/api/ide/');
    if (!passed) await middleware!(request, res, () => { passed = true; });
    if (passed) await routes.get(route)!(request, res);
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

test('git remotes are reduced to GitHub owner/name', () => {
  for (const remote of ['https://github.com/acme/shop-web.git', 'git@github.com:acme/shop-web.git',
    'ssh://git@github.com/acme/shop-web', 'https://token@github.com/acme/shop-web/']) {
    assert.equal(githubRepository(remote), 'acme/shop-web');
  }
  assert.equal(githubRepository('https://gitlab.com/acme/shop-web.git'), null);
  assert.equal(githubRepository('acme/shop-web'), null);
});

test('IDE routes need a valid token', async () => {
  const { call } = setup(['web']);
  await withPython(() => ({}), async (calls) => {
    const res = await call('GET /api/ide/projects/:projectId/events', { params: { projectId: 'web' }, headers: { authorization: 'Bearer bad' } });
    assert.equal(res.statusCode, 401);
    assert.equal(calls.length, 0);
  });
});

test('events are read as the token owner, only for projects the owner can access', async () => {
  const { call } = setup(['web']);
  await withPython(() => ({ events: [] }), async (calls) => {
    assert.equal((await call('GET /api/ide/projects/:projectId/events', { params: { projectId: 'admin' } })).statusCode, 403);
    assert.equal((await call('GET /api/ide/projects/:projectId/events', { params: { projectId: '../x' } })).statusCode, 400);
    assert.equal(calls.length, 0);
    const ok = await call('GET /api/ide/projects/:projectId/events', { params: { projectId: 'web' }, query: { after: '12', user_id: 'mallory' } });
    assert.equal(ok.statusCode, 200);
    assert.deepEqual(calls, ['GET http://python-api/contracts/projects/web/events?after=12&limit=50&user_id=alice']);
  });
});

test('acknowledging records the token owner, never a user from the request', async () => {
  const { call } = setup(['web']);
  const bodies: unknown[] = [];
  await withPython((_url, init) => { bodies.push(JSON.parse(String(init?.body))); return { acknowledged: true }; }, async (calls) => {
    assert.equal((await call('POST /api/ide/projects/:projectId/events/:eventId/ack',
      { params: { projectId: 'web', eventId: 'nope' } })).statusCode, 400);
    const ok = await call('POST /api/ide/projects/:projectId/events/:eventId/ack',
      { params: { projectId: 'web', eventId: EVENT }, body: { user_id: 'mallory' } });
    assert.equal(ok.statusCode, 200);
    assert.deepEqual(calls, [`POST http://python-api/contracts/projects/web/events/${EVENT}/ack`]);
    assert.deepEqual(bodies, [{ user_id: 'alice' }]);
  });
});

test('workspace matching only returns accessible projects of the same repository', async () => {
  const { call } = setup(['web', 'web-staging']);
  await withPython(() => ({ sources: [
    { project_id: 'web', repository: 'Acme/Shop-Web' }, { project_id: 'web-private', repository: 'acme/shop-web' },
    { project_id: 'web-staging', repository: 'acme/shop-web' }, { project_id: 'api', repository: 'acme/shop-api' }] }), async () => {
    const res = await call('GET /api/ide/projects/match', { query: { remote: 'git@github.com:acme/shop-web.git' } });
    assert.deepEqual(res.body, { repository: 'acme/shop-web', projects: ['web', 'web-staging'] });
  });
});

test('code generation needs access to a project that owns or consumes the contract', async () => {
  const { call } = setup(['web']);
  const route = 'POST /api/ide/contracts/sources/:sourceId/versions/:versionId/generate';
  await withPython((url) => (url.endsWith(`/sources/${SOURCE}`)
    ? { source: { project_id: 'backend', attachments: [{ project_id: 'other' }] } } : { files: [] }), async (calls) => {
    assert.equal((await call(route, { params: { sourceId: SOURCE, versionId: VERSION } })).statusCode, 404);
    assert.equal(calls.length, 1);
  });
  await withPython((url) => (url.endsWith(`/sources/${SOURCE}`)
    ? { source: { project_id: 'backend', attachments: [{ project_id: 'web' }] } } : { files: [] }), async (calls) => {
    const ok = await call(route, { params: { sourceId: SOURCE, versionId: VERSION }, body: { target: 'vue' } });
    assert.equal(ok.statusCode, 200);
    assert.equal(calls[1], `POST http://python-api/contracts/sources/${SOURCE}/versions/${VERSION}/generate`);
  });
});

test('the event stream refuses projects the owner cannot access', async () => {
  const { call } = setup(['web']);
  assert.equal((await call('GET /api/ide/events/stream', { query: { projects: 'web,admin' } })).statusCode, 403);
  assert.equal((await call('GET /api/ide/events/stream', { query: { projects: '' } })).statusCode, 400);
});

test('token management requires the application origin for changes', async () => {
  const { call } = setup(['web']);
  assert.equal((await call('POST /api/ide-tokens', { headers: { origin: 'http://evil' }, body: { name: 'x' } })).statusCode, 403);
  const created = await call('POST /api/ide-tokens', { body: { name: 'Laptop' } });
  assert.equal(created.body.token, 'ide_secret');
  assert.equal((await call('GET /api/ide-tokens')).body.tokens.length, 1);
  assert.equal((await call('DELETE /api/ide-tokens/:tokenId', { params: { tokenId: 'zz' } })).statusCode, 400);
  assert.equal((await call('DELETE /api/ide-tokens/:tokenId', { params: { tokenId: 'f'.repeat(24) } })).statusCode, 200);
});

test('stored tokens verify, expire, and revoke', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ide-tokens-'));
  process.env.AUTH_STORE_PATH = path.join(dir, 'auth-store.json');
  const auth = await import('../src/server/oidcAuth');
  const alice = { sub: 'alice', name: 'Alice' };
  const { token, row } = auth.authCreateIdeToken(alice, 'Laptop');
  assert.match(token, /^ide_[0-9a-f]{24}\.[A-Za-z0-9_-]{43}$/);
  assert.equal(JSON.stringify(auth.authStoreSnapshot()).includes(token), false, 'only a hash is stored');
  assert.equal(auth.authUserForIdeToken(`Bearer ${token}`)?.sub, 'alice');
  assert.equal(auth.authUserForIdeToken(`Bearer ${token.slice(0, -1)}x`), null);
  assert.equal(auth.authUserForIdeToken(token), null);
  assert.equal(auth.authRevokeIdeToken({ sub: 'mallory' }, row.token_id), false);
  assert.equal(auth.authRevokeIdeToken(alice, row.token_id), true);
  assert.equal(auth.authUserForIdeToken(`Bearer ${token}`), null);
  fs.rmSync(dir, { recursive: true, force: true });
});
