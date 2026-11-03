import assert from 'node:assert/strict';
import { once } from 'node:events';
import test from 'node:test';
import express from 'express';
import { authIsBootstrapAdmin, authRoleFor, installOidcAuth } from '../../src/server/oidcAuth';

test('only development mode grants an unauthenticated local administrator', async () => {
  const previous = process.env.NODE_ENV;
  const app = express();
  installOidcAuth(app);
  app.get('/api/protected', (_req, res) => res.json({ user: res.locals.authUser }));
  const server = app.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('No test server address');
  const base = `http://127.0.0.1:${address.port}`;
  try {
    process.env.NODE_ENV = 'development';
    const identityResponse = await fetch(`${base}/api/auth/me`);
    assert.equal(identityResponse.status, 200);
    const identity = await identityResponse.json() as any;
    assert.equal(identity.developmentMode, true);
    assert.equal(identity.user.sub, 'local-development');
    assert.deepEqual(identity.projects, [{ project_id: '*', role: 'admin' }]);
    assert.equal(authRoleFor(identity.user, 'any-project'), 'admin');
    assert.equal((await fetch(`${base}/api/protected`)).status, 200);

    for (const mode of ['production', 'test']) {
      process.env.NODE_ENV = mode;
      assert.equal(authIsBootstrapAdmin(identity.user), false);
      assert.equal((await fetch(`${base}/api/auth/me`)).status, 401);
      assert.notEqual((await fetch(`${base}/api/protected`)).status, 200);
    }
    delete process.env.NODE_ENV;
    assert.equal((await fetch(`${base}/api/auth/me`)).status, 401);
    assert.notEqual((await fetch(`${base}/api/protected`)).status, 200);
  } finally {
    if (previous === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = previous;
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
