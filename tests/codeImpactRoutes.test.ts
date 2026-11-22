import assert from 'node:assert/strict';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import test from 'node:test';
import express from 'express';
import { codeImpactBodyParser, registerCodeImpactRoutes } from '../src/server/codeImpactRoutes';

type Reply = { status: number; body: unknown };

/** A stub Python API that records what reached /code-tools/impact. */
async function withServers(reply: Reply, run: (webUrl: string, received: any[]) => Promise<void>) {
  const received: any[] = [];
  const python = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      received.push({ url: req.url, body: JSON.parse(raw || '{}') });
      res.writeHead(reply.status, { 'Content-Type': 'application/json' }).end(JSON.stringify(reply.body));
    });
  });
  await new Promise<void>((resolve) => python.listen(0, '127.0.0.1', resolve));
  const app = express();
  app.use('/api/code/impact', codeImpactBodyParser());
  app.use(express.json({ limit: '100mb' }));
  app.use((_req, res, next) => { res.locals.authUser = { sub: 'alice' }; next(); });
  registerCodeImpactRoutes(app, {
    apiUrl: `http://127.0.0.1:${(python.address() as AddressInfo).port}`,
    getUser: (res) => res.locals.authUser,
    canReadProject: (user, projectId) => user?.sub === 'alice' && projectId === 'shop',
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

const post = (url: string, body: unknown) => fetch(`${url}/api/code/impact`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: typeof body === 'string' ? body : JSON.stringify(body),
});

test('an authorized impact request forwards only the documented fields', async () => {
  const report = { success: true, impacted: [{ symbol: 'Cart.total', distance: 1 }] };
  await withServers({ status: 200, body: report }, async (url, received) => {
    const res = await post(url, {
      project_id: 'shop', revision: 'latest', symbols: ['Cart.add'], depth: 3, max_nodes: 100,
      collection: 'other-project', filename: 'x.zip', upload_session_id: 'abc',
    });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), report);
    assert.equal(received.length, 1);
    assert.equal(received[0].url, '/code-tools/impact');
    assert.deepEqual(received[0].body, { project_id: 'shop', revision: 'latest', symbols: ['Cart.add'], depth: 3, max_nodes: 100 });
  });
});

test('a project the user cannot read is refused and nothing is forwarded', async () => {
  await withServers({ status: 200, body: {} }, async (url, received) => {
    const denied = await post(url, { project_id: 'payroll', symbols: ['Pay.run'] });
    assert.equal(denied.status, 403);
    assert.equal((await denied.json()).error, 'Project access denied.');
    const missing = await post(url, { symbols: ['Pay.run'] });
    assert.equal(missing.status, 400);
    assert.equal(received.length, 0);
  });
});

test('422 and 409 from Python pass through with their message', async () => {
  await withServers({ status: 422, body: { detail: 'Give exactly one of symbols, diff or base_revision_id.' } }, async (url) => {
    const res = await post(url, { project_id: 'shop', symbols: ['A'], diff: '--- a' });
    assert.equal(res.status, 422);
    assert.deepEqual(await res.json(), { success: false, error: 'Give exactly one of symbols, diff or base_revision_id.' });
  });
  await withServers({ status: 409, body: { detail: 'Revision rev_x is not ready.' } }, async (url) => {
    const res = await post(url, { project_id: 'shop', revision_id: 'rev_x', symbols: ['A'] });
    assert.equal(res.status, 409);
    assert.equal((await res.json()).error, 'Revision rev_x is not ready.');
  });
});

test('request bodies over 210 KB are refused before reaching Python', async () => {
  await withServers({ status: 200, body: {} }, async (url, received) => {
    const accepted = await post(url, { project_id: 'shop', diff: 'x'.repeat(200 * 1024) });
    assert.equal(accepted.status, 200);
    const tooLarge = await post(url, { project_id: 'shop', diff: 'x'.repeat(215 * 1024) });
    assert.equal(tooLarge.status, 413);
    assert.equal(received.length, 1);
  });
});
