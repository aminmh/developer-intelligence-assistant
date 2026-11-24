import assert from 'node:assert/strict';
import test from 'node:test';
import { createHmac } from 'node:crypto';
import { registerCodeQualityRoutes } from '../src/server/codeQualityRoutes';

function routeHarness() {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = Object.fromEntries(['get', 'post', 'put', 'delete', 'patch'].map((method) => [method,
    (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`${method.toUpperCase()} ${path}`, handler)]));
  registerCodeQualityRoutes(app as any, {
    apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40), helperUrl: 'http://127.0.0.1:47831',
    webOrigin: 'http://localhost:3000', getUser: () => ({ sub: 'alice' }), canAccessProject: () => true,
  });
  const response = { statusCode: 200, body: undefined as any,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } };
  return { handlers, response };
}

test('Git synchronization forwards a one-use project-scoped OS token without returning it', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith('/telemetry/git/projects/pizza')) return new Response(JSON.stringify({ source: { credential_id: '12345678-1234-5678-1234-567812345678' } }));
    if (url.endsWith('/code-quality-github')) return new Response(JSON.stringify({ saved: true }));
    if (url.endsWith('/code-quality-github-use')) return new Response(JSON.stringify({ token: 'transient-github-secret' }));
    if (url.endsWith('/telemetry/git/projects/pizza/sync')) {
      assert.equal(JSON.parse(String(init?.body)).token, 'transient-github-secret');
      return new Response(JSON.stringify({ commit_sha: 'a'.repeat(40), revision_id: 'rev_123', state: 'queued' }));
    }
    throw new Error(`Unexpected request: ${url}`);
  }) as typeof fetch;
  try {
    const handler = handlers.get('POST /api/code-quality/projects/:projectId/git-source/sync');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza' } }, response);
    assert.equal(response.statusCode, 200);
    assert.equal(JSON.stringify(response.body).includes('transient-github-secret'), false);
    const helper = calls.find((call) => call.url.endsWith('/code-quality-github-use'));
    assert.ok(helper);
    const auth = (helper.init?.headers as Record<string, string>).Authorization;
    const claims = JSON.parse(Buffer.from(auth.split('.')[1], 'base64url').toString());
    assert.equal(claims.project_id, 'pizza');
    assert.equal(claims.operation, 'code-quality-github-fetch');
    assert.equal(claims.target_id, '12345678-1234-5678-1234-567812345678');
  } finally { globalThis.fetch = originalFetch; }
});

test('public Git repositories synchronize without retrieving a stored token', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith('/telemetry/git/projects/pizza')) return new Response(JSON.stringify({ source: { credential_id: '12345678-1234-5678-1234-567812345678' } }));
    if (url.endsWith('/code-quality-github')) return new Response(JSON.stringify({ saved: false }));
    if (url.endsWith('/telemetry/git/projects/pizza/sync')) {
      assert.deepEqual(JSON.parse(String(init?.body)), {});
      return new Response(JSON.stringify({ commit_sha: 'a'.repeat(40), revision_id: 'rev_123', state: 'queued' }));
    }
    throw new Error(`Unexpected request: ${url}`);
  }) as typeof fetch;
  try {
    const handler = handlers.get('POST /api/code-quality/projects/:projectId/git-source/sync');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza' } }, response);
    assert.equal(response.statusCode, 200);
    assert.equal(calls.some((call) => call.url.endsWith('/code-quality-github-use')), false);
  } finally { globalThis.fetch = originalFetch; }
});

test('GitHub webhook rejects an invalid HMAC before making API requests', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  const originalSecret = process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET;
  process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET = 's'.repeat(40);
  let apiCalled = false;
  globalThis.fetch = (async () => { apiCalled = true; return new Response('{}'); }) as typeof fetch;
  try {
    const handler = handlers.get('POST /api/code-quality/github/webhook');
    assert.ok(handler);
    await handler({ body: {}, rawBody: Buffer.from('{"ref":"refs/heads/main"}'),
      header: (name: string) => name.toLowerCase() === 'x-hub-signature-256' ? 'sha256=bad' : 'push' }, response);
    assert.equal(response.statusCode, 401);
    assert.equal(apiCalled, false);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalSecret === undefined) delete process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET;
    else process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET = originalSecret;
  }
});

test('verified push webhook resolves configured branch and asks API to fetch it', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  const originalSecret = process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET;
  const secret = 's'.repeat(40);
  process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET = secret;
  const raw = Buffer.from(JSON.stringify({
    ref: 'refs/heads/main', after: 'b'.repeat(40), repository: { full_name: 'Owner/Repo' },
  }));
  const signature = `sha256=${createHmac('sha256', secret).update(raw).digest('hex')}`;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.includes('/telemetry/git/webhook-target?')) return new Response(JSON.stringify({
      project_id: 'pizza', user_id: 'alice', repository: 'owner/repo', branch: 'main',
      credential_id: '12345678-1234-5678-1234-567812345678',
    }));
    if (url.endsWith('/code-quality-github')) return new Response(JSON.stringify({ saved: true }));
    if (url.endsWith('/code-quality-github-use')) return new Response(JSON.stringify({ token: 'transient-token' }));
    if (url.endsWith('/telemetry/git/projects/pizza/sync')) {
      assert.equal(JSON.parse(String(init?.body)).token, 'transient-token');
      assert.equal(JSON.stringify(init?.body).includes('b'.repeat(40)), false);
      return new Response(JSON.stringify({ commit_sha: 'c'.repeat(40), state: 'queued' }));
    }
    throw new Error(`Unexpected request: ${url}`);
  }) as typeof fetch;
  try {
    const handler = handlers.get('POST /api/code-quality/github/webhook');
    assert.ok(handler);
    await handler({ body: {}, rawBody: raw, header: (name: string) => {
      if (name.toLowerCase() === 'x-hub-signature-256') return signature;
      if (name.toLowerCase() === 'x-github-event') return 'push';
      return undefined;
    } }, response);
    assert.equal(response.statusCode, 202);
    assert.equal(calls.some((call) => call.url.endsWith('/telemetry/git/projects/pizza/sync')), true);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalSecret === undefined) delete process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET;
    else process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET = originalSecret;
  }
});

test('repository inspection forwards the URL and a one-use token, and never reads the saved credential', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push(url);
    if (!url.endsWith('/telemetry/git/inspect')) throw new Error(`Unexpected request: ${url}`);
    assert.deepEqual(JSON.parse(String(init?.body)), { repository: 'git@github.com:o/r.git', token: 'once' });
    return new Response(JSON.stringify({ default_branch: 'main', branches: [{ name: 'main', commit: 'a'.repeat(40), is_default: true }] }));
  }) as typeof fetch;
  try {
    const handler = handlers.get('POST /api/code-quality/projects/:projectId/git-source/inspect');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza' }, body: { repository: 'git@github.com:o/r.git', token: 'once' } }, response);
    assert.equal(response.statusCode, 200);
    assert.equal(response.body.default_branch, 'main');
    assert.deepEqual(calls, ['http://python-api/telemetry/git/inspect']);
  } finally { globalThis.fetch = originalFetch; }
});

test('re-indexing a revision needs project administrator access and forwards to the API', async () => {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = Object.fromEntries(['get', 'post', 'put', 'delete', 'patch'].map((method) => [method,
    (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`${method.toUpperCase()} ${path}`, handler)]));
  let manage = false;
  registerCodeQualityRoutes(app as any, {
    apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40), helperUrl: 'http://127.0.0.1:47831',
    webOrigin: 'http://localhost:3000', getUser: () => ({ sub: 'alice' }), canAccessProject: () => true,
    canManageProject: () => manage,
  } as any);
  const response = { statusCode: 200, body: undefined as any,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } };
  const handler = handlers.get('POST /api/code-quality/projects/:projectId/revisions/:revisionId/reindex');
  assert.ok(handler);
  await handler({ params: { projectId: 'pizza', revisionId: 'rev_1' } }, response);
  assert.equal(response.statusCode, 403);
  manage = true;
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; method?: string }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), method: init?.method });
    return new Response(JSON.stringify({ queued: true }));
  }) as typeof fetch;
  try {
    await handler({ params: { projectId: 'pizza', revisionId: 'rev_1' } }, response);
    assert.equal(response.statusCode, 200);
    assert.deepEqual(calls, [{ url: 'http://python-api/code-quality/projects/pizza/revisions/rev_1/reindex', method: 'POST' }]);
  } finally { globalThis.fetch = originalFetch; }
});
