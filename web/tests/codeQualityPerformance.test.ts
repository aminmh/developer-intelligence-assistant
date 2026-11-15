import assert from 'node:assert/strict';
import test from 'node:test';
import { registerCodeQualityRoutes } from '../src/server/codeQualityRoutes';

test('benchmark proxy keeps OS credential retrieval purpose- and target-scoped', async () => {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = {
    get: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`GET ${path}`, handler),
    post: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`POST ${path}`, handler),
    patch: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PATCH ${path}`, handler),
    delete: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`DELETE ${path}`, handler),
    put: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PUT ${path}`, handler),
  };
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.endsWith('/code-quality/projects/pizza/performance/targets/target-1')) {
      return new Response(JSON.stringify({ target: { target_id: 'target-1', auth_mode: 'bearer' } }), { status: 200 });
    }
    if (url.endsWith('/code-quality-bearer-use')) {
      return new Response(JSON.stringify({ token: 'transient-secret' }), { status: 200 });
    }
    if (url.endsWith('/performance/runs')) {
      assert.equal(JSON.parse(String(init?.body)).bearer_token, 'transient-secret');
      assert.equal(JSON.parse(String(init?.body)).revision_id, 'rev_1');
      return new Response(JSON.stringify({ run_id: 'run-1', state: 'running' }), { status: 202 });
    }
    throw new Error(`Unexpected upstream call: ${url}`);
  }) as typeof fetch;

  try {
    registerCodeQualityRoutes(app as any, {
      apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40),
      helperUrl: 'http://127.0.0.1:47831', webOrigin: 'http://localhost:3000',
      getUser: () => ({ sub: 'alice' }), canAccessProject: () => true,
    });
    const response: { statusCode: number; body?: unknown; status(code: number): any; json(value: unknown): any } = {
      statusCode: 200,
      status(code) { this.statusCode = code; return this; },
      json(value) { this.body = value; return this; },
    };
    const handler = handlers.get('POST /api/code-quality/projects/:projectId/performance/runs');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza' }, body: { target_id: 'target-1', routes: ['/api/items'], revision_id: 'rev_1' } }, response);

    assert.equal(response.statusCode, 202);
    assert.equal(JSON.stringify(response.body).includes('transient-secret'), false);
    const helperCall = calls.find((item) => item.url.endsWith('/code-quality-bearer-use'));
    assert.ok(helperCall);
    assert.match(String((helperCall.init?.headers as Record<string, string>).Authorization), /^Bearer /);
    const claims = JSON.parse(Buffer.from(String((helperCall.init?.headers as Record<string, string>).Authorization).split('.')[1], 'base64url').toString());
    assert.equal(claims.operation, 'code-quality-benchmark-use');
    assert.equal(claims.target_id, 'target-1');
    assert.equal(calls.some((item) => item.url.endsWith('/performance/runs')), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('assessment history deletion is authenticated and project-scoped', async () => {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = {
    get: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`GET ${path}`, handler),
    post: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`POST ${path}`, handler),
    patch: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PATCH ${path}`, handler),
    delete: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`DELETE ${path}`, handler),
    put: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PUT ${path}`, handler),
  };
  const originalFetch = globalThis.fetch;
  let call: { url: string; init?: RequestInit } | undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    call = { url: String(input), init };
    return new Response(JSON.stringify({ deleted: true }), { status: 200 });
  }) as typeof fetch;

  try {
    registerCodeQualityRoutes(app as any, {
      apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40),
      helperUrl: 'http://127.0.0.1:47831', webOrigin: 'http://localhost:3000',
      getUser: () => ({ sub: 'alice' }), canAccessProject: (_user, projectId) => projectId === 'pizza',
    });
    const response: { statusCode: number; body?: unknown; status(code: number): any; json(value: unknown): any } = {
      statusCode: 200,
      status(code) { this.statusCode = code; return this; },
      json(value) { this.body = value; return this; },
    };
    const handler = handlers.get('DELETE /api/code-quality/projects/:projectId/reports/:reportId');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza', reportId: 'report-1' } }, response);

    assert.equal(response.statusCode, 200);
    assert.deepEqual(response.body, { deleted: true });
    assert.equal(call?.url, 'http://python-api/code-quality/projects/pizza/reports/report-1');
    assert.equal(call?.init?.method, 'DELETE');
    assert.equal((call?.init?.headers as Record<string, string>)['X-User-Id'], 'alice');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('revision source proxy checks project access and forwards the exact source location', async () => {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = {
    get: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`GET ${path}`, handler),
    post: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`POST ${path}`, handler),
    patch: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PATCH ${path}`, handler),
    delete: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`DELETE ${path}`, handler),
    put: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PUT ${path}`, handler),
  };
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify({ file: 'app/Controller.php', requested_line: 11 }), { status: 200 });
  }) as typeof fetch;

  try {
    registerCodeQualityRoutes(app as any, {
      apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40),
      helperUrl: 'http://127.0.0.1:47831', webOrigin: 'http://localhost:3000',
      getUser: () => ({ sub: 'alice' }), canAccessProject: (_user, projectId) => projectId === 'pizza',
    });
    const response: { statusCode: number; body?: unknown; status(code: number): any; json(value: unknown): any } = {
      statusCode: 200,
      status(code) { this.statusCode = code; return this; },
      json(value) { this.body = value; return this; },
    };
    const handler = handlers.get('GET /api/code-quality/projects/:projectId/revisions/:revisionId/source');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza', revisionId: 'rev_0123456789abcdef0123456789abcdef' },
      query: { file_path: 'app/Controller.php', line: '11', start_line: '1' } }, response);

    assert.equal(response.statusCode, 200);
    assert.deepEqual(response.body, { file: 'app/Controller.php', requested_line: 11 });
    assert.equal(calls[0].url, 'http://python-api/code-quality/projects/pizza/revisions/rev_0123456789abcdef0123456789abcdef/source?file_path=app%2FController.php&line=11&start_line=1');
    assert.equal((calls[0].init?.headers as Record<string, string>)['X-User-Id'], 'alice');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('revision source proxy rejects access to a project before contacting the API', async () => {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = {
    get: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`GET ${path}`, handler),
    post: () => undefined, patch: () => undefined, delete: () => undefined, put: () => undefined,
  };
  const originalFetch = globalThis.fetch;
  let called = false;
  globalThis.fetch = (async () => { called = true; return new Response('{}'); }) as typeof fetch;

  try {
    registerCodeQualityRoutes(app as any, {
      apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40),
      helperUrl: 'http://127.0.0.1:47831', webOrigin: 'http://localhost:3000',
      getUser: () => ({ sub: 'alice' }), canAccessProject: () => false,
    });
    const response: { statusCode: number; body?: unknown; status(code: number): any; json(value: unknown): any } = {
      statusCode: 200,
      status(code) { this.statusCode = code; return this; },
      json(value) { this.body = value; return this; },
    };
    const handler = handlers.get('GET /api/code-quality/projects/:projectId/revisions/:revisionId/source');
    assert.ok(handler);
    await handler({ params: { projectId: 'other', revisionId: 'rev_0123456789abcdef0123456789abcdef' },
      query: { file_path: 'app/Controller.php', line: '11' } }, response);

    assert.equal(response.statusCode, 403);
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('project exclusion proxy authenticates project access and forwards saved patterns', async () => {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = {
    get: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`GET ${path}`, handler),
    post: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`POST ${path}`, handler),
    patch: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PATCH ${path}`, handler),
    delete: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`DELETE ${path}`, handler),
    put: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PUT ${path}`, handler),
  };
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify({ patterns: ['tests/fixtures/**'] }), { status: 200 });
  }) as typeof fetch;

  try {
    registerCodeQualityRoutes(app as any, {
      apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40),
      helperUrl: 'http://127.0.0.1:47831', webOrigin: 'http://localhost:3000',
      getUser: () => ({ sub: 'alice' }), canAccessProject: (_user, projectId) => projectId === 'pizza',
    });
    const makeResponse = () => ({
      statusCode: 200, body: undefined as unknown,
      status(code: number) { this.statusCode = code; return this; },
      json(value: unknown) { this.body = value; return this; },
    });
    const get = handlers.get('GET /api/code-quality/projects/:projectId/exclusions');
    const put = handlers.get('PUT /api/code-quality/projects/:projectId/exclusions');
    assert.ok(get && put);

    const getResponse = makeResponse();
    await get({ params: { projectId: 'pizza' } }, getResponse);
    assert.equal(getResponse.statusCode, 200);
    assert.deepEqual(getResponse.body, { patterns: ['tests/fixtures/**'] });

    const putResponse = makeResponse();
    await put({ params: { projectId: 'pizza' }, body: { patterns: ['tests/fixtures/**'] } }, putResponse);
    assert.equal(putResponse.statusCode, 200);
    assert.deepEqual(JSON.parse(String(calls[1].init?.body)), { patterns: ['tests/fixtures/**'] });
    assert.equal((calls[1].init?.headers as Record<string, string>)['X-User-Id'], 'alice');

    const deniedResponse = makeResponse();
    await get({ params: { projectId: 'other' } }, deniedResponse);
    assert.equal(deniedResponse.statusCode, 403);
    assert.equal(calls.length, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('viewers can read benchmark results but cannot configure targets or start runs', async () => {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = Object.fromEntries(['get', 'post', 'put', 'delete', 'patch'].map((method) => [method,
    (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`${method.toUpperCase()} ${path}`, handler)]));
  const originalFetch = globalThis.fetch;
  const upstream: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => { upstream.push(String(input)); return new Response(JSON.stringify({ runs: [] })); }) as typeof fetch;
  try {
    registerCodeQualityRoutes(app as any, {
      apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40), helperUrl: 'http://127.0.0.1:47831',
      webOrigin: 'http://localhost:3000', getUser: () => ({ sub: 'viewer' }), canAccessProject: () => true,
      canOperateProject: () => false, isGlobalAdmin: () => false, ruleSyncIntervalMs: 0, gitReconcileIntervalMs: 0,
    });
    const response = () => ({ statusCode: 200, body: undefined as any,
      status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } });
    for (const key of ['POST /api/code-quality/projects/:projectId/performance/runs',
      'POST /api/code-quality/projects/:projectId/performance/targets',
      'DELETE /api/code-quality/projects/:projectId/performance/targets/:targetId',
      'POST /api/code-quality/projects/:projectId/performance/targets/:targetId/credential',
      'DELETE /api/code-quality/projects/:projectId/performance/runs/:runId']) {
      const handler = handlers.get(key);
      assert.ok(handler, key);
      const res = response();
      await handler({ params: { projectId: 'pizza', targetId: 't', runId: 'r' }, body: { target_id: 't' } }, res);
      assert.equal(res.statusCode, 403, key);
    }
    assert.deepEqual(upstream, []);
    const read = response();
    await handlers.get('GET /api/code-quality/projects/:projectId/performance/runs')!({ params: { projectId: 'pizza' } }, read);
    assert.equal(read.statusCode, 200);
  } finally { globalThis.fetch = originalFetch; }
});
