import assert from 'node:assert/strict';
import test from 'node:test';
import { registerCodeQualityRoutes } from '../src/server/codeQualityRoutes';

const CREDENTIAL = '12345678-1234-5678-1234-567812345678';

function routeHarness(options: { canAccess?: boolean; ruleSyncIntervalMs?: number } = {}) {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = Object.fromEntries(['get', 'post', 'put', 'delete', 'patch'].map((method) => [method,
    (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`${method.toUpperCase()} ${path}`, handler)]));
  registerCodeQualityRoutes(app as any, {
    apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40), helperUrl: 'http://127.0.0.1:47831',
    webOrigin: 'http://localhost:3000', getUser: () => ({ sub: 'alice' }),
    canAccessProject: () => options.canAccess ?? true, isGlobalAdmin: () => false,
    ruleSyncIntervalMs: options.ruleSyncIntervalMs ?? 0,
  });
  const response = { statusCode: 200, body: undefined as any,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } };
  return { handlers, response };
}

test('Sync now reads the Outline token from the OS helper and never returns it', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push(url);
    if (url.endsWith('/code-quality/projects/pizza/outline')) return new Response(JSON.stringify({ connection: { credential_id: CREDENTIAL } }));
    if (url.endsWith('/code-quality-outline-use')) return new Response(JSON.stringify({ token: 'transient-outline-secret' }));
    if (url.endsWith('/code-quality/projects/pizza/outline/sync')) {
      assert.equal(JSON.parse(String(init?.body)).token, 'transient-outline-secret');
      return new Response(JSON.stringify({ sync: { status: 'synchronized', synced_at: '2026-11-07T10:00:00Z' }, events: [] }));
    }
    throw new Error(`Unexpected request: ${url}`);
  }) as typeof fetch;
  try {
    const handler = handlers.get('POST /api/code-quality/projects/:projectId/outline/sync');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza' } }, response);
    assert.equal(response.statusCode, 200);
    assert.equal(response.body.sync.status, 'synchronized');
    assert.equal(JSON.stringify(response.body).includes('transient-outline-secret'), false);
  } finally { globalThis.fetch = originalFetch; }
});

test('rule knowledge routes refuse projects the user cannot access', async () => {
  const { handlers, response } = routeHarness({ canAccess: false });
  const originalFetch = globalThis.fetch;
  let called = false;
  globalThis.fetch = (async () => { called = true; return new Response('{}'); }) as typeof fetch;
  try {
    for (const key of ['GET /api/code-quality/projects/:projectId/rule-knowledge/links',
      'POST /api/code-quality/projects/:projectId/rule-knowledge/impact',
      'POST /api/code-quality/projects/:projectId/outline/sync']) {
      const handler = handlers.get(key);
      assert.ok(handler, key);
      await handler({ params: { projectId: 'pizza' }, query: {}, body: {} }, response);
      assert.equal(response.statusCode, 403);
    }
    assert.equal(called, false);
  } finally { globalThis.fetch = originalFetch; }
});

test('link lookups forward repeated symbol ids as query parameters', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  let forwarded = '';
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    forwarded = String(input);
    return new Response(JSON.stringify({ links: [] }));
  }) as typeof fetch;
  try {
    const handler = handlers.get('GET /api/code-quality/projects/:projectId/rule-knowledge/links');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza' }, query: { symbol_id: ['App\\A::b', 'route:GET /x'], other: 'ignored' } }, response);
    assert.equal(response.statusCode, 200);
    const url = new URL(forwarded);
    assert.equal(url.pathname, '/code-quality/projects/pizza/rule-knowledge/links');
    assert.deepEqual(url.searchParams.getAll('symbol_id'), ['App\\A::b', 'route:GET /x']);
    assert.equal(url.searchParams.has('other'), false);
  } finally { globalThis.fetch = originalFetch; }
});

test('the scheduler syncs connected projects and skips quietly when the helper is down', async () => {
  const originalFetch = globalThis.fetch;
  const synced: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/code-quality/outline/connections')) {
      assert.equal((init?.headers as Record<string, string>)['X-User-Id'], 'system:business-rule-sync');
      return new Response(JSON.stringify({ connections: [
        { project_id: 'down', credential_id: CREDENTIAL }, { project_id: 'pizza', credential_id: CREDENTIAL }] }));
    }
    if (url.endsWith('/code-quality-outline-use')) {
      const body = JSON.parse(String(init?.body));
      if (body.project_id === 'down') throw new TypeError('fetch failed');
      return new Response(JSON.stringify({ token: 'transient-outline-secret' }));
    }
    if (url.endsWith('/outline/sync')) { synced.push(url); return new Response(JSON.stringify({ events: [] })); }
    throw new Error(`Unexpected request: ${url}`);
  }) as typeof fetch;
  try {
    routeHarness({ ruleSyncIntervalMs: 20 });
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.ok(synced.some((url) => url.endsWith('/code-quality/projects/pizza/outline/sync')));
    assert.equal(synced.some((url) => url.includes('/projects/down/')), false);
  } finally { globalThis.fetch = originalFetch; }
});
