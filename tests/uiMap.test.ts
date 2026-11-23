import assert from 'node:assert/strict';
import test from 'node:test';
import { registerFrontendRoutes } from '../src/server/frontendRoutes';
import { frameworkLabel, matches, requestsOf, routesOf, usedBy, type UiMap } from '../src/utils/uiMap';

const map = {
  framework: 'vue', meta_framework: 'nuxt', version: '^3.4.0',
  routes: [{ path: '/orders', component_id: 'a.vue#Orders' }, { path: '/', component_id: 'h.vue#Home' }],
  components: [
    { id: 'a.vue#Orders', name: 'Orders', uses: ['c.vue#Card'], http_calls: [1], via_imports: [0, 1] },
    { id: 'c.vue#Card', name: 'Card', uses: [], http_calls: [], via_imports: [] },
  ],
  http_calls: [{ id: 0, method: 'GET', url: '/api/orders' }, { id: 1, method: 'POST', url: null }],
} as unknown as UiMap;

test('ui map lookups', () => {
  assert.equal(frameworkLabel(map), 'Nuxt 3.4.0');
  assert.deepEqual(usedBy(map, 'c.vue#Card').map((c) => c.name), ['Orders']);
  assert.deepEqual(routesOf(map, 'a.vue#Orders').map((r) => r.path), ['/orders']);
  assert.deepEqual(requestsOf(map, map.components[0]).map((r) => [r.id, r.direct]), [[1, true], [0, false]]);
  assert.ok(matches(' ORD ', null, '/orders')); assert.ok(matches('', 'x')); assert.ok(!matches('zzz', 'x', undefined));
});

test('ui map route checks access and validates its query', async () => {
  let handler: (req: any, res: any) => Promise<any> = async () => undefined;
  const app = { get: (_path: string, fn: typeof handler) => { handler = fn; } };
  registerFrontendRoutes(app as any, { apiUrl: 'http://python-api', internalSecret: 'secret',
    getUser: () => ({ sub: 'alice' }), canAccessProject: (_user, projectId) => projectId === 'web' });
  const response = () => ({ statusCode: 200, body: undefined as any,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } });
  const original = globalThis.fetch;
  const urls: string[] = [];
  globalThis.fetch = (async (url: string) => { urls.push(url); return new Response('{"framework":"vue"}'); }) as typeof fetch;
  try {
    const run = async (projectId: string, query: Record<string, string> = {}) => {
      const res = response(); await handler({ params: { projectId }, query }, res); return res;
    };
    assert.equal((await run('backend')).statusCode, 403);
    assert.equal((await run('web', { revision_id: 'latest' })).statusCode, 400);
    assert.equal((await run('web', { app: '../x' })).statusCode, 400);
    assert.equal(urls.length, 0);
    const ok = await run('web', { revision_id: 'rev_' + 'a'.repeat(32), app: 'apps/web' });
    assert.equal(ok.body.framework, 'vue');
    assert.equal(urls[0], `http://python-api/frontend/projects/web/ui-map?revision_id=rev_${'a'.repeat(32)}&app=apps%2Fweb`);
  } finally { globalThis.fetch = original; }
});

test('check findings group by file and the checks route validates access', async () => {
  const { findingsByFile } = await import('../src/utils/uiMap');
  const finding = (file: string, area: string, line: number) => ({ file, area, line, rule: 'r', severity: 'WARNING', message: `m ${file}` });
  const checks = { findings: [finding('a.vue', 'accessibility', 1), finding('b.vue', 'accessibility', 2), finding('b.vue', 'accessibility', 3), finding('b.vue', 'security', 4)] } as any;
  assert.deepEqual(findingsByFile(checks, 'accessibility').map((group) => [group.file, group.findings.length]), [['b.vue', 2], ['a.vue', 1]]);
  assert.deepEqual(findingsByFile(checks, 'accessibility', 'A.VUE').map((group) => group.file), ['a.vue']);

  const handlers = new Map<string, (req: any, res: any) => Promise<any>>();
  registerFrontendRoutes({ get: (path: string, fn: any) => handlers.set(path, fn) } as any, { apiUrl: 'http://python-api', internalSecret: 'secret',
    getUser: () => ({ sub: 'alice' }), canAccessProject: (_user, projectId) => projectId === 'web' });
  const run = async (projectId: string, query: Record<string, string> = {}) => {
    const res = { statusCode: 200, body: undefined as any, status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } };
    await handlers.get('/api/projects/:projectId/frontend-checks')!({ params: { projectId }, query }, res); return res;
  };
  const original = globalThis.fetch; const urls: string[] = [];
  globalThis.fetch = (async (url: string) => { urls.push(url); return new Response('{"findings":[]}'); }) as typeof fetch;
  try {
    assert.equal((await run('other')).statusCode, 403);
    assert.equal((await run('web', { revision_id: 'x' })).statusCode, 400);
    assert.equal((await run('web', { revision_id: 'rev_' + 'b'.repeat(32) })).statusCode, 200);
    assert.deepEqual(urls, [`http://python-api/frontend/projects/web/checks?revision_id=rev_${'b'.repeat(32)}`]);
  } finally { globalThis.fetch = original; }
});

test('screen helpers grade vitals and find the component behind a screen name', async () => {
  const { vitalTone, milliseconds, componentForScreen } = await import('../src/utils/uiMap');
  assert.deepEqual([vitalTone('lcp', 2332), vitalTone('lcp', 3000), vitalTone('inp', 600), vitalTone('cls', null)], ['ok', 'warn', 'bad', 'dim']);
  assert.deepEqual([milliseconds(182), milliseconds(2332), milliseconds(null)], ['182 ms', '2.3 s', '–']);
  const ui = { routes: [{ path: '/orders/:id', component_id: 'a#OrderDetail' }, { path: '/x', component_id: null }],
    components: [{ id: 'a#OrderDetail', name: 'OrderDetail', file: 'src/views/OrderDetail.vue' },
      { id: 'b#Orders', name: 'Orders', file: 'resources/js/pages/Admin/Orders.vue' }] } as any;
  assert.equal(componentForScreen(ui, '/orders/:id')?.name, 'OrderDetail');   // a client route
  assert.equal(componentForScreen(ui, 'Admin/Orders')?.name, 'Orders');       // an Inertia page name
  assert.equal(componentForScreen(ui, 'Orders'), null); assert.equal(componentForScreen(null, 'x'), null);
});
