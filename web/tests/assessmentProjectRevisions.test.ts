import assert from 'node:assert/strict';
import test from 'node:test';
import { assessmentRevisions } from '../src/code-quality/projectRevisions';
import { registerCodeQualityRoutes } from '../src/server/codeQualityRoutes';
import type { UploadedFile } from '../src/types';

test('assessment includes unindexed knowledge base uploads and deduplicates exact project commits', () => {
  const files = [
    { projectId: 'pizza', commitSha: 'a'.repeat(40) },
    { projectId: 'pizza', commitSha: 'b'.repeat(40) },
    { projectId: 'pizza', commitSha: 'a'.repeat(40) },
    { projectId: 'other', commitSha: 'a'.repeat(40) },
    { name: 'old-upload-without-revision' },
  ] as UploadedFile[];
  const revisions = assessmentRevisions(files, [{ project_id: 'pizza', commit_sha: 'a'.repeat(40),
    revision_id: 'rev_pizza', state: 'ready' }]);
  assert.equal(revisions.length, 3);
  assert.equal(revisions.find((row) => row.project_id === 'pizza' && row.commit_sha?.startsWith('a'))?.revision_id, 'rev_pizza');
  assert.equal(revisions.find((row) => row.project_id === 'other')?.revision_id, null);
  assert.equal(revisions.find((row) => row.commit_sha?.startsWith('b'))?.state, 'not_indexed');
});

test('automatic upload revisions appear without Git commits and remain isolated by project and source', () => {
  const files = [
    { projectId: 'pizza', sourceRef: 'upload_a', revisionId: 'rev_a' },
    { projectId: 'pizza', sourceRef: 'upload_a', revisionId: 'rev_a' },
    { projectId: 'pizza', sourceRef: 'upload_b', revisionId: 'rev_b' },
    { projectId: 'other', sourceRef: 'upload_a', revisionId: 'rev_other' },
  ] as UploadedFile[];
  const rows = assessmentRevisions(files, [{ project_id: 'pizza', commit_sha: null,
    source_ref: 'upload_a', revision_id: 'rev_a', state: 'ready' }]);
  assert.equal(rows.length, 3);
  assert.equal(rows.find((row) => row.revision_id === 'rev_a')?.state, 'ready');
  assert.equal(rows.find((row) => row.revision_id === 'rev_b')?.state, 'not_indexed');
  assert.equal(rows.find((row) => row.project_id === 'other')?.revision_id, 'rev_other');
  assert.ok(rows.every((row) => row.commit_sha === null));
});

function harness(canAccess: boolean) {
  const handlers = new Map<string, (...args: any[]) => Promise<unknown>>();
  const app = Object.fromEntries(['get', 'post', 'put', 'delete', 'patch'].map((method) => [method,
    (path: string, handler: (...args: any[]) => Promise<unknown>) => handlers.set(`${method.toUpperCase()} ${path}`, handler)]));
  registerCodeQualityRoutes(app as any, { apiUrl: 'http://python-api', internalSecret: 'x'.repeat(40),
    getUser: () => ({ sub: 'alice' }), canAccessProject: () => canAccess, isGlobalAdmin: () => false,
    helperUrl: 'http://helper', webOrigin: 'http://localhost:3000' });
  const response = { statusCode: 200, body: undefined as any,
    status(code: number) { this.statusCode = code; return this; }, json(body: unknown) { this.body = body; return this; } };
  return { handlers, response };
}

test('assessment precheck enforces project access and forwards only the selected revision', async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input, init) => {
    calls.push(String(input));
    assert.equal((init?.headers as Record<string, string>)['X-User-Id'], 'alice');
    return new Response(JSON.stringify({ ready: false, status: 'index_required', reason: 'Index first.' }));
  }) as typeof fetch;
  try {
    for (const canAccess of [false, true]) {
      const { handlers, response } = harness(canAccess);
      await handlers.get('GET /api/code-quality/projects/:projectId/revisions/:revisionId/precheck')!(
        { params: { projectId: 'pizza', revisionId: 'rev_pizza' } }, response);
      assert.equal(response.statusCode, canAccess ? 200 : 403);
      if (!canAccess) assert.equal(calls.length, 0);
      else assert.equal(response.body.ready, false);
    }
    assert.deepEqual(calls, ['http://python-api/code-quality/projects/pizza/revisions/rev_pizza/precheck']);
  } finally { globalThis.fetch = originalFetch; }
});
