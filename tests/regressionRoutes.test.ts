import assert from 'node:assert/strict';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import test from 'node:test';
import express from 'express';
import { registerRegressionRoutes } from '../src/server/regressionRoutes';

type Reply = { status: number; body?: unknown };

/** A stub Python API recording what reached it, and a web app with a fixed signed-in user. */
async function withServers(user: string, reply: Reply | ((url: string) => Reply), run: (webUrl: string, received: any[]) => Promise<void>,
  bearerToken?: (targetId: string) => Promise<{ status: number; token?: string; detail?: string }>) {
  const received: any[] = [];
  const python = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      received.push({ method: req.method, url: req.url, headers: req.headers, body: raw ? JSON.parse(raw) : null });
      const answer = typeof reply === 'function' ? reply(req.url || '') : reply;
      if (answer.body === undefined) return res.writeHead(answer.status).end();
      res.writeHead(answer.status, { 'Content-Type': 'application/json' }).end(JSON.stringify(answer.body));
    });
  });
  await new Promise<void>((resolve) => python.listen(0, '127.0.0.1', resolve));
  const app = express();
  app.use(express.json());
  app.use((_req, res, next) => { res.locals.authUser = { sub: user }; next(); });
  registerRegressionRoutes(app, {
    apiUrl: `http://127.0.0.1:${(python.address() as AddressInfo).port}`,
    internalSecret: 'secret',
    getUser: (res) => res.locals.authUser,
    canReadProject: (u, projectId) => ['alice', 'viewer'].includes(u?.sub || '') && projectId === 'shop',
    canOperateProject: (u) => u?.sub === 'alice',
    bearerToken,
  });
  const web = app.listen(0, '127.0.0.1');
  await new Promise((resolve) => web.once('listening', resolve));
  try {
    await run(`http://127.0.0.1:${(web.address() as AddressInfo).port}`, received);
  } finally {
    web.close();
    python.close();
  }
}

test('an operator starts a check with only the documented fields and the internal headers', async () => {
  await withServers('alice', { status: 202, body: { check_id: 'rc_1', state: 'pending' } }, async (url, received) => {
    const response = await fetch(`${url}/api/regression/projects/shop/checks`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_revision_id: 'rev_a', candidate_revision_id: 'rev_b', extra: 'dropped' }),
    });
    assert.equal(response.status, 202);
    assert.equal((await response.json()).check_id, 'rc_1');
    assert.equal(received[0].url, '/projects/shop/regression-checks');
    assert.deepEqual(received[0].body, { base_revision_id: 'rev_a', candidate_revision_id: 'rev_b' });
    assert.equal(received[0].headers['x-internal-secret'], 'secret');
    assert.equal(received[0].headers['x-user-id'], 'alice');
  });
});

test('a viewer reads checks but cannot start or delete one', async () => {
  await withServers('viewer', { status: 200, body: { checks: [] } }, async (url, received) => {
    assert.equal((await fetch(`${url}/api/regression/projects/shop/checks`)).status, 200);
    assert.equal((await fetch(`${url}/api/regression/projects/shop/checks/rc_1`)).status, 200);
    assert.equal(received[1].url, '/projects/shop/regression-checks/rc_1');
    const start = await fetch(`${url}/api/regression/projects/shop/checks`, { method: 'POST' });
    assert.equal(start.status, 403);
    assert.equal((await fetch(`${url}/api/regression/projects/shop/checks/rc_1`, { method: 'DELETE' })).status, 403);
    assert.equal(received.length, 2);
  });
});

test('another project is refused before reaching Python', async () => {
  await withServers('alice', { status: 200, body: {} }, async (url, received) => {
    assert.equal((await fetch(`${url}/api/regression/projects/other/checks`)).status, 403);
    assert.equal(received.length, 0);
  });
});

test('a busy project passes the active check id through and a delete passes 204 through', async () => {
  await withServers('alice', { status: 409, body: { detail: { detail: 'still running', active_check_id: 'rc_0' } } }, async (url) => {
    const response = await fetch(`${url}/api/regression/projects/shop/checks`, { method: 'POST' });
    assert.equal(response.status, 409);
    assert.deepEqual(await response.json(), { detail: 'still running', active_check_id: 'rc_0' });
  });
  await withServers('alice', { status: 204 }, async (url) => {
    assert.equal((await fetch(`${url}/api/regression/projects/shop/checks/rc_1`, { method: 'DELETE' })).status, 204);
  });
});

test('a candidate replay of a Bearer target fetches the token and forwards only documented fields', async () => {
  const tokens: string[] = [];
  const reply = (url: string): Reply => {
    if (url.endsWith('/replay')) return { status: 200, body: { settings: { candidate_target_id: 't-tok' } } };
    if (url.includes('/performance/targets/t-tok')) return { status: 200, body: { target: { auth_mode: 'bearer' } } };
    return { status: 202, body: { run_id: 'r1', state: 'running' } };
  };
  await withServers('alice', reply, async (url, received) => {
    const response = await fetch(`${url}/api/regression/projects/shop/checks/rc_1/replay-runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'candidate', bearer_token: 'from-browser' }),
    });
    assert.equal(response.status, 202);
    assert.deepEqual(received.map((r) => r.url), ['/projects/shop/regression-checks/rc_1/replay',
      '/code-quality/projects/shop/performance/targets/t-tok', '/projects/shop/regression-checks/rc_1/replay-runs']);
    assert.deepEqual(received[2].body, { kind: 'candidate', target_id: 't-tok', bearer_token: 'helper-token' });
    assert.deepEqual(tokens, ['t-tok']);
  }, async (targetId) => { tokens.push(targetId); return { status: 200, token: 'helper-token' }; });
});

test('a replay without a token from the helper is refused before reaching Python', async () => {
  const reply = (url: string): Reply => url.includes('/performance/targets/')
    ? { status: 200, body: { target: { auth_mode: 'bearer' } } } : { status: 202, body: {} };
  await withServers('alice', reply, async (url, received) => {
    const response = await fetch(`${url}/api/regression/projects/shop/checks/rc_1/replay-runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'baseline', target_id: 't-tok' }),
    });
    assert.equal(response.status, 404);
    assert.equal((await response.json()).detail, 'No token saved.');
    assert.equal(received.length, 1);
  }, async () => ({ status: 404, detail: 'No token saved.' }));
});

test('viewers read replay state but cannot change probes, settings or start runs', async () => {
  await withServers('viewer', { status: 200, body: { probes: [] } }, async (url, received) => {
    assert.equal((await fetch(`${url}/api/regression/projects/shop/checks/rc_1/replay`)).status, 200);
    for (const [method, path] of [['PUT', 'probes'], ['POST', 'probes/generate'], ['POST', 'probes/import'],
      ['PUT', 'replay-settings'], ['POST', 'replay-runs']]) {
      const response = await fetch(`${url}/api/regression/projects/shop/checks/rc_1/${path}`, { method });
      assert.equal(response.status, 403, path);
    }
    assert.equal(received.length, 1);
  });
});

test('probe saves and imports forward only their documented fields', async () => {
  await withServers('alice', { status: 200, body: {} }, async (url, received) => {
    await fetch(`${url}/api/regression/projects/shop/checks/rc_1/probes`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ probes: [], extra: 1 }),
    });
    await fetch(`${url}/api/regression/projects/shop/checks/rc_1/probes/import`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: '{}', target_id: 't', mode: 'replace', other: 2 }),
    });
    assert.deepEqual(received[0].body, { probes: [] });
    assert.deepEqual(received[1].body, { text: '{}', target_id: 't', mode: 'replace' });
  });
});
