import assert from 'node:assert/strict';
import { once } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import express from 'express';
import { registerDataAdminRoutes, scopeItem } from '../src/server/dataAdminRoutes';

async function listen(app: express.Express) {
  const server = app.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('No test server address');
  return { server, base: `http://127.0.0.1:${address.port}` };
}

const item = (over: Record<string, unknown>) => ({ store: 'qdrant', id: 'x', owner: 'revision', project_id: 'shop',
  deletable: false, detail: {}, count: 1, ...over }) as any;

test('viewers see their own items and only their slice of shared data', () => {
  const mine = new Set(['shop']);
  assert.equal(scopeItem(item({ project_id: 'other' }), mine), null);
  assert.equal(scopeItem(item({ owner: 'orphan', project_id: null }), mine), null);
  assert.equal(scopeItem(item({ owner: 'global', project_id: null }), mine), null);
  const shared = scopeItem(item({ owner: 'shared', project_id: null, detail: { projects: { shop: 3, other: 9 } } }), mine);
  assert.deepEqual(shared?.detail.projects, { shop: 3 });
  assert.equal(shared?.count, 3);
  assert.equal(scopeItem(item({ deletable: true }), mine)?.deletable, false);
});

test('only global administrators can plan or execute deletions', async () => {
  const calls: string[] = [];
  const python = express();
  python.use(express.json());
  python.get('/data-admin/projects', (_req, res) => res.json({ projects: [
    { project_id: 'shop', stores: { qdrant: { items: 1, count: 5, bytes: 0 } } },
    { project_id: 'other', stores: { qdrant: { items: 1, count: 7, bytes: 0 } } }] }));
  python.post('/data-admin/deletions/plan', (req, res) => { calls.push(`plan:${req.get('x-user-id')}`); res.json({ hash: 'h', steps: [] }); });
  python.post('/data-admin/deletions/execute', (req, res) => { calls.push('execute'); res.json({ job_id: 'dj_1', state: 'running' }); });
  python.get('/data-admin/jobs/:id', (_req, res) => res.json({ job_id: 'dj_1', state: 'succeeded', target: { kind: 'project', project_id: 'shop' } }));
  const upstream = await listen(python);

  let current: { sub: string } = { sub: 'viewer' };
  const removed: string[] = [];
  const app = express();
  app.use(express.json());
  app.use((_req, res, next) => { res.locals.authUser = current; next(); });
  registerDataAdminRoutes(app, {
    apiUrl: upstream.base, internalSecret: 'x'.repeat(40), getUser: (res) => res.locals.authUser,
    isGlobalAdmin: (user) => user?.sub === 'admin', projectsFor: (user) => (user?.sub === 'viewer' ? ['shop'] : []),
    removeProjectMemberships: (projectId) => removed.push(projectId),
  });
  const web = await listen(app);
  const post = (url: string, body: unknown) => fetch(`${web.base}${url}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  try {
    const projects = await (await fetch(`${web.base}/api/data-admin/projects`)).json() as any;
    assert.deepEqual(projects.projects.map((row: any) => row.project_id), ['shop']);
    assert.equal(projects.projects[0].can_delete, false);
    const summary = await (await fetch(`${web.base}/api/data-admin/summary`)).json() as any;
    assert.equal(summary.stores.qdrant.count, 5);
    assert.equal((await post('/api/data-admin/deletions/plan', { target: { kind: 'project', project_id: 'shop' } })).status, 403);
    assert.equal((await post('/api/data-admin/deletions/execute', { target: { kind: 'project', project_id: 'shop' }, hash: 'h' })).status, 403);
    assert.equal((await fetch(`${web.base}/api/data-admin/orphans`)).status, 403);
    assert.deepEqual(calls, []);

    current = { sub: 'admin' };
    assert.equal((await post('/api/data-admin/deletions/plan', { target: { kind: 'project', project_id: 'shop' } })).status, 200);
    assert.equal((await post('/api/data-admin/deletions/execute', { target: { kind: 'project', project_id: 'shop' }, hash: 'h' })).status, 400);
    assert.equal((await post('/api/data-admin/deletions/execute', { target: { kind: 'project', project_id: 'shop' }, hash: 'h', confirm: 'shop' })).status, 200);
    assert.deepEqual(calls, ['plan:admin', 'execute']);
    await fetch(`${web.base}/api/data-admin/jobs/dj_1`);
    await fetch(`${web.base}/api/data-admin/jobs/dj_1`);
    assert.deepEqual(removed, ['shop']);
  } finally {
    web.server.close();
    upstream.server.close();
  }
});

test('the last global administrator cannot be removed', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'auth-store-'));
  const previous = { store: process.env.AUTH_STORE_PATH, env: process.env.NODE_ENV, bootstrap: process.env.OIDC_BOOTSTRAP_ADMIN_SUB };
  process.env.AUTH_STORE_PATH = path.join(dir, 'auth-store.json');
  process.env.OIDC_BOOTSTRAP_ADMIN_SUB = '';
  process.env.NODE_ENV = 'development';
  const auth = await import(`../src/server/oidcAuth.ts?global-admins=${Date.now()}`);
  const app = express();
  auth.installOidcAuth(app);
  const web = await listen(app);
  try {
    assert.equal((await fetch(`${web.base}/api/auth/global-admins/alice`, { method: 'PUT' })).status, 200);
    const listed = await (await fetch(`${web.base}/api/auth/global-admins`)).json() as any;
    assert.deepEqual(listed.admins, [{ subject: 'alice', bootstrap: false }]);
    assert.equal(auth.authIsGlobalAdmin({ sub: 'alice' }), true);
    assert.equal(auth.authRoleFor({ sub: 'alice' }, 'any-project'), 'admin');
    assert.equal((await fetch(`${web.base}/api/auth/global-admins/alice`, { method: 'DELETE' })).status, 409);
    const me = await (await fetch(`${web.base}/api/auth/me`)).json() as any;
    assert.equal(me.globalAdmin, true);
  } finally {
    web.server.close();
    for (const [key, value] of [['AUTH_STORE_PATH', previous.store], ['NODE_ENV', previous.env], ['OIDC_BOOTSTRAP_ADMIN_SUB', previous.bootstrap]] as const) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
