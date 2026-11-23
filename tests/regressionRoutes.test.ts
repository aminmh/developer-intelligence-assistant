import assert from 'node:assert/strict';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import test from 'node:test';
import express from 'express';
import { registerRegressionRoutes } from '../src/server/regressionRoutes';

type Reply = { status: number; body?: unknown };

/** A stub Python API recording what reached it, and a web app with a fixed signed-in user. */
async function withServers(user: string, reply: Reply, run: (webUrl: string, received: any[]) => Promise<void>) {
  const received: any[] = [];
  const python = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      received.push({ method: req.method, url: req.url, headers: req.headers, body: raw ? JSON.parse(raw) : null });
      if (reply.body === undefined) return res.writeHead(reply.status).end();
      res.writeHead(reply.status, { 'Content-Type': 'application/json' }).end(JSON.stringify(reply.body));
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
