import assert from 'node:assert/strict';
import test from 'node:test';
import { registerCodeQualityRoutes } from '../src/server/codeQualityRoutes';

test('benchmark proxy keeps OS credential retrieval purpose- and target-scoped', async () => {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = {
    get: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`GET ${path}`, handler),
    post: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`POST ${path}`, handler),
    delete: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`DELETE ${path}`, handler),
    put: (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`PUT ${path}`, handler),
  };
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.endsWith('/code-quality/performance/targets/target-1')) {
      return new Response(JSON.stringify({ target: { target_id: 'target-1', auth_mode: 'bearer' } }), { status: 200 });
    }
    if (url.endsWith('/code-quality-bearer-use')) {
      return new Response(JSON.stringify({ token: 'transient-secret' }), { status: 200 });
    }
    if (url.endsWith('/performance/runs')) {
      assert.equal(JSON.parse(String(init?.body)).bearer_token, 'transient-secret');
      return new Response(JSON.stringify({ run_id: 'run-1', complete: true }), { status: 200 });
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
    await handler({ params: { projectId: 'pizza' }, body: { target_id: 'target-1', routes: ['/api/items'] } }, response);

    assert.equal(response.statusCode, 200);
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
