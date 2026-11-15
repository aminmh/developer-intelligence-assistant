import assert from 'node:assert/strict';
import test from 'node:test';
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

test('assessment retrieves the SonarQube token once and forwards it only to the authorized assessment API', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const credentialId = '12345678-1234-5678-1234-567812345678';
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith('/code-quality/projects/pizza/outline')) return new Response(JSON.stringify({ connection: null }));
    if (url.endsWith('/code-quality/projects/pizza/external-analyzers')) return new Response(JSON.stringify({
      settings: { sonarqube: { enabled: true, credential_id: credentialId } },
    }));
    if (url.endsWith('/code-quality-sonarqube-use')) return new Response(JSON.stringify({ token: 'transient-sonar-token' }));
    if (url.endsWith('/code-quality/projects/pizza/revisions/rev_123/assess')) {
      assert.deepEqual(JSON.parse(String(init?.body)), { sonarqube_token: 'transient-sonar-token' });
      return new Response(JSON.stringify({ report_id: 'qa_123' }));
    }
    throw new Error(`Unexpected request: ${url}`);
  }) as typeof fetch;
  try {
    const handler = handlers.get('POST /api/code-quality/projects/:projectId/revisions/:revisionId/assess');
    assert.ok(handler);
    await handler({ params: { projectId: 'pizza', revisionId: 'rev_123' } }, response);
    assert.equal(response.statusCode, 200);
    assert.equal(JSON.stringify(response.body).includes('transient-sonar-token'), false);
    const helper = calls.find((call) => call.url.endsWith('/code-quality-sonarqube-use'));
    assert.ok(helper);
    const auth = (helper.init?.headers as Record<string, string>).Authorization;
    const claims = JSON.parse(Buffer.from(auth.split('.')[1], 'base64url').toString());
    assert.equal(claims.project_id, 'pizza');
    assert.equal(claims.operation, 'code-quality-sonarqube-fetch');
    assert.equal(claims.target_id, credentialId);
  } finally { globalThis.fetch = originalFetch; }
});

test('worker connection checks forward only the selected project worker settings through the authenticated API', async () => {
  const { handlers, response } = routeHarness();
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify({ status: 'ready', analyzers: { joern: { status: 'ready' } } }));
  }) as typeof fetch;
  try {
    const handler = handlers.get('POST /api/code-quality/projects/:projectId/external-analyzers/worker/check');
    assert.ok(handler);
    const worker = { endpoint: 'https://worker.example.test', certificate_sha256: 'a'.repeat(64), source_transfer_consent: true };
    await handler({ params: { projectId: 'pizza' }, body: { worker } }, response);
    assert.equal(response.statusCode, 200);
    assert.equal(calls.length, 1);
    assert.ok(calls[0].url.endsWith('/code-quality/projects/pizza/external-analyzers/worker/check'));
    assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { worker });
    assert.equal((calls[0].init?.headers as Record<string, string>)['X-User-Id'], 'alice');
  } finally { globalThis.fetch = originalFetch; }
});
