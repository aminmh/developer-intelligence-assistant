import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

test('restored project selections are checked against the inventory', async () => {
  const originalWindow = globalThis.window;
  const originalStorage = globalThis.localStorage;
  const originalFetch = globalThis.fetch;
  try {
    let href = 'http://localhost/?project=missing';
    let stored = JSON.stringify({ project: 'missing', revision: 'latest' });
    const listeners: Record<string, () => void> = {};
    Object.defineProperty(globalThis, 'window', { configurable: true, value: {
      location: { get href() { return href; }, get search() { return new URL(href).search; } },
      history: { state: null, replaceState: (_state: unknown, _title: string, url: URL) => { href = url.href; }, pushState: (_state: unknown, _title: string, url: URL) => { href = url.href; } },
      addEventListener: (name: string, listener: () => void) => { listeners[name] = listener; },
    } });
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
      getItem: () => stored, setItem: (_key: string, value: string) => { stored = value; },
    } });
    let resolveFetch!: (response: Response) => void;
    globalThis.fetch = (() => new Promise<Response>((resolve) => { resolveFetch = resolve; })) as typeof fetch;
    const bundle = await build({ entryPoints: ['src/composables/useProjectScope.ts'], bundle: true, platform: 'node', format: 'esm', write: false, define: { 'import.meta.env': '{}' } });
    const { useProjectScope } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString('base64')}`);
    const scope = useProjectScope();
    assert.equal(scope.projectId.value, '', 'unverified IDs never appear while loading');
    resolveFetch(new Response(JSON.stringify({ projects: [{ project_id: 'existing', revisions: [] }] }), { status: 200 }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(scope.projectId.value, '');
    assert.equal(new URL(href).searchParams.has('project'), false);
    assert.equal(JSON.parse(stored).project, '');

    href = 'http://localhost/?project=existing';
    listeners.popstate();
    assert.equal(scope.projectId.value, 'existing', 'known projects without indexed revisions remain selectable');
    scope.select('new-project');
    assert.equal(scope.projectId.value, 'new-project', 'explicit new project remains usable');
    href = 'http://localhost/?project=missing';
    listeners.popstate();
    assert.equal(scope.projectId.value, '', 'history cannot restore a stale project');
    href = 'http://localhost/?project=new-project';
    listeners.popstate();
    assert.equal(scope.projectId.value, 'new-project', 'history preserves projects started in this session');
    scope.select('existing');
    globalThis.fetch = (async () => new Response(JSON.stringify({ projects: [] }), { status: 200 })) as typeof fetch;
    await scope.load();
    assert.equal(scope.projectId.value, '', 'refresh clears a project removed from the inventory');
  } finally {
    Object.defineProperty(globalThis, 'window', { configurable: true, value: originalWindow });
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: originalStorage });
    globalThis.fetch = originalFetch;
  }
});
