import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { parse, compileScript } from '@vue/compiler-sfc';
import { transformSync } from 'esbuild';
import { nextTick, reactive } from 'vue';
import { runtimeSchemaNavigation, runtimeSchemaOperation, saveRuntimeSchema } from '../src/utils/runtimeSchema';
import type { RuntimeSchemaSource } from '../src/types';

const source: RuntimeSchemaSource = { project_id: 'pizza', environment: 'production', host_id: 'cert-1', container_id: 'a'.repeat(64), image_id: 'sha256:image', service: 'database', state: 'running', updated_at: '2026-11-14T12:00:00Z' };
const selection = { source, database: 'pizza', schemas: ['app', 'public'] };
const reply = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });

test('every helper operation requests fresh scoped authorization before contacting localhost', async () => {
  const calls: string[] = [];
  const request = async (url: string, init?: RequestInit) => {
    calls.push(url);
    assert.deepEqual(JSON.parse(init!.body as string), { ...selection, operation: 'discover' });
    return reply({ token: 'scoped-token' });
  };
  const helper = async (url: string, init?: RequestInit) => {
    calls.push(url);
    assert.equal((init!.headers as Record<string, string>).Authorization, 'Bearer scoped-token');
    assert.deepEqual(JSON.parse(init!.body as string), selection);
    return reply({ engine: 'postgresql' });
  };
  await runtimeSchemaOperation(request, 'discover', selection, undefined, helper);
  await runtimeSchemaOperation(request, 'discover', selection, undefined, helper);
  assert.deepEqual(calls, ['/api/runtime/schema-authorization', 'http://127.0.0.1:47831/runtime-schema/discover', '/api/runtime/schema-authorization', 'http://127.0.0.1:47831/runtime-schema/discover']);
});

test('authorization failure cannot contact the helper or create a draft', async () => {
  let helperCalls = 0;
  await assert.rejects(saveRuntimeSchema(async () => reply({ detail: 'Global administrator required.' }, 403), selection, async () => { helperCalls++; return reply({}); }), /Global administrator required/);
  assert.equal(helperCalls, 0);
});

test('saving forwards the complete unmodified signed extraction envelope', async () => {
  const envelope = { schema: { version: 1, tables: [], relationships: [] }, provenance: { project_id: 'pizza', coverage: { complete: false, warnings: ['Trigger retained as metadata.'] } }, receipt: 'signed', sub: 'admin', origin: 'https://app.example', expires_at: 12345, future_field: 'retain' };
  const saved = { id: 'runtime-draft', schema: envelope.schema, revision: 1 };
  const paths: string[] = [];
  const result = await saveRuntimeSchema(async (url, init) => {
    paths.push(url);
    if (url.endsWith('schema-authorization')) return reply({ token: 'token' });
    assert.equal(url, '/api/schema-drafts/runtime');
    assert.deepEqual(JSON.parse(init!.body as string), envelope);
    return reply(saved);
  }, selection, async () => reply(envelope));
  assert.deepEqual(result, saved);
  assert.deepEqual(paths, ['/api/runtime/schema-authorization', '/api/schema-drafts/runtime']);
});

test('Runtime schema navigation preserves project, environment and selected container', () => {
  const url = new URL(runtimeSchemaNavigation('https://app.example/?tab=runtime&revision_id=rev-a&panel=request', 'pizza', 'production', source.container_id));
  assert.equal(url.searchParams.get('tab'), 'schema');
  assert.equal(url.searchParams.get('project_id'), 'pizza');
  assert.equal(url.searchParams.get('environment'), 'production');
  assert.equal(url.searchParams.get('container_id'), source.container_id);
  assert.equal(url.searchParams.get('revision_id'), 'rev-a');
  assert.equal(url.searchParams.has('panel'), false);
});

// Execute the real compiled Vue setup functions with injected network/window
// boundaries. Vue refs and watchers remain real; no browser or database needed.
function loaderHarness(save: (...args: any[]) => Promise<any>, accepts = true, options: { sources?: RuntimeSchemaSource[]; discover?: (scope: any) => Promise<any> } = {}) {
  const require = createRequire(import.meta.url);
  const filename = fileURLToPath(new URL('../src/components/runtime/RuntimeSchemaLoader.vue', import.meta.url));
  const script = compileScript(parse(readFileSync(filename, 'utf8'), { filename }).descriptor, { id: 'runtime-schema-test' }).content;
  const code = transformSync(script, { loader: 'ts', format: 'cjs' }).code;
  const calls: string[] = [];
  const adopted: any[] = [];
  const helperCalls: any[] = [];
  const popup = { close: () => calls.push('close'), closed: false };
  let popupBlocked = false;
  const module = { exports: {} as any };
  runInNewContext(code, {
    module, exports: module.exports, URLSearchParams, console,
    window: { location: { search: '?project_id=pizza&environment=production' }, open: (url: string) => { assert.equal(url, 'about:blank'); calls.push('reserve'); return popupBlocked ? null : popup; } },
    require: (name: string) => {
      if (name === '../../ui/makeRequest') return { makeRequest: async () => { calls.push('sources'); return reply({ sources: options.sources || [source] }); } };
      if (name === '../../utils/runtimeSchema') return {
        saveRuntimeSchema: (...args: any[]) => { calls.push('extract'); return save(...args); },
        discoverRuntimeSchema: async (_request: unknown, scope: any) => {
          calls.push(`discover:${scope.database || ''}`);
          if (options.discover) return options.discover(scope);
          return { source, engine: 'postgresql', version: '16', databases: ['pizza', 'other'], configured_database: 'pizza', schemas: scope.database === 'other' ? ['other_schema'] : ['app', 'public'], configured_schemas: scope.database === 'other' ? ['other_schema'] : ['public'] };
        },
        runtimeSchemaOperation: async (...args: any[]) => { helperCalls.push(args); return { host: 'db.example', ssh_user: 'ops', ssh_port: 22, fingerprint: 'SHA256:known-key' }; },
      };
      return require(name);
    },
  });
  const props = reactive({ projectId: 'pizza', canLoad: () => true, acceptDraft: (...args: any[]) => { calls.push('adopt'); adopted.push(args); return accepts; } });
  const state = module.exports.default.setup(props, { expose: () => {} });
  const idle = async () => { for (let i = 0; i < 20 && state.busy.value; i++) await new Promise(resolve => setImmediate(resolve)); assert.equal(state.busy.value, ''); await nextTick(); };
  return { state, props, calls, adopted, helperCalls, idle, blockPopup: () => { popupBlocked = true; } };
}

test('discovery is manual and PostgreSQL database changes require new schema discovery', async () => {
  const h = loaderHarness(async () => { throw new Error('Unexpected extraction'); });
  assert.deepEqual(h.calls, []);
  await h.state.findContainers(); await nextTick();
  assert.deepEqual(h.calls, ['sources', 'discover:']);
  assert.equal(h.state.canExtract.value, true);
  assert.deepEqual(Array.from(h.state.schemas.value), ['public']);
  h.state.database.value = 'other'; await nextTick();
  assert.equal(h.state.canExtract.value, false);
  assert.deepEqual(Array.from(h.state.schemas.value), []);
  await h.state.discover('other');
  assert.deepEqual(Array.from(h.state.schemas.value), ['other_schema']);
  assert.equal(h.state.canExtract.value, true);
  const key = h.state.selected.value;
  h.state.selected.value = '';
  h.state.selected.value = key;
  assert.equal(h.state.database.value, 'other', 'cached schemas stay paired with the database that was inspected');
  assert.deepEqual(Array.from(h.state.schemas.value), ['other_schema']);
  assert.deepEqual(h.calls, ['sources', 'discover:', 'discover:other']);
});

test('manual discovery lists verified engines separately from unverified SSH candidates', async () => {
  const mysql = { ...source, container_id: 'b'.repeat(64), service: 'mysql' };
  const unknown = { ...source, container_id: 'c'.repeat(64), service: 'web' };
  const h = loaderHarness(async () => ({}), true, { sources: [source, mysql, unknown], discover: async scope => {
    if (scope.source.container_id === unknown.container_id) throw new Error('Host is not associated.');
    return { source: scope.source, engine: scope.source.container_id === mysql.container_id ? 'mysql' : 'postgresql', version: '16', databases: ['pizza'], configured_database: 'pizza', schemas: ['public'] };
  } });
  await h.state.findContainers();
  assert.deepEqual(Array.from(h.state.databaseGroups.value, (group: any) => group.engine), ['postgresql', 'mysql']);
  assert.deepEqual(Array.from(h.state.unverifiedSources.value, (item: any) => item.container_id), [unknown.container_id]);
  h.state.candidateSelection.value = h.state.sourceKey(unknown);
  assert.equal(h.state.canExtract.value, false);
  assert.equal(h.state.verification.value[h.state.selected.value].error, 'Host is not associated.');
});

test('no verified database directs the user to include its container in Runtime setup', async () => {
  const h = loaderHarness(async () => ({}), true, { discover: async () => { throw new Error('No supported engine.'); } });
  await h.state.findContainers();
  assert.equal(h.state.databaseGroups.value.length, 0);
  assert.equal(h.state.unverifiedSources.value.length, 1);
  assert.match(h.state.message.value, /database container must be included in Runtime setup/);
});

test('discovery results are discarded if project selection changes during verification', async () => {
  let resolveDiscovery!: (result: any) => void;
  const h = loaderHarness(async () => ({}), true, { discover: () => new Promise(resolve => { resolveDiscovery = resolve; }) });
  const pending = h.state.findContainers();
  await new Promise(resolve => setImmediate(resolve));
  h.props.projectId = 'another-project';
  resolveDiscovery({ source, engine: 'postgresql', version: '16', databases: ['pizza'], schemas: ['public'] });
  await pending;
  assert.equal(h.state.project.value, 'another-project');
  assert.equal(h.state.sources.value.length, 0);
  assert.equal(h.state.discovery.value, null);
});

test('a saved extraction cannot replace current edits after the project changes', async () => {
  let resolveSave!: (draft: any) => void;
  const h = loaderHarness(() => new Promise(resolve => { resolveSave = resolve; }));
  await h.state.findContainers();
  h.state.loadSchema();
  h.props.projectId = 'another-project';
  resolveSave({ id: 'saved-for-old-source' });
  await h.idle();
  assert.equal(h.adopted.length, 0);
  assert.equal(h.state.savedDraft.value.id, 'saved-for-old-source');
  assert.ok(h.calls.includes('close'));
  assert.match(h.state.message.value, /previous source/);
});

test('load reserves the tab synchronously and adopts only after the draft has saved', async () => {
  let resolveSave!: (draft: any) => void;
  const h = loaderHarness(() => new Promise(resolve => { resolveSave = resolve; }));
  await h.state.findContainers(); await nextTick(); await h.state.discover();
  h.calls.length = 0;
  h.state.loadSchema();
  assert.deepEqual(h.calls, ['reserve', 'extract']);
  assert.deepEqual(h.adopted, []);
  h.state.loadSchema();
  assert.deepEqual(h.calls, ['reserve', 'extract'], 'duplicate click cannot extract twice');
  const draft = { id: 'saved-runtime', schema: { tables: [] } };
  resolveSave(draft); await h.idle();
  assert.deepEqual(h.calls, ['reserve', 'extract', 'adopt']);
  assert.equal(h.adopted[0][0], draft);
});

test('extraction or import failure preserves the current draft and enables retry', async () => {
  let attempt = 0;
  const h = loaderHarness(async () => { if (++attempt === 1) throw new Error('Receipt expired; retry.'); return { id: 'saved' }; });
  await h.state.findContainers(); await nextTick(); await h.state.discover();
  h.state.loadSchema(); await h.idle();
  assert.match(h.state.error.value, /Receipt expired/);
  assert.deepEqual(h.adopted, []);
  assert.equal(h.state.savedDraft.value, null);
  h.state.loadSchema(); await h.idle();
  assert.equal(h.adopted.length, 1);
});

test('blocked popup retains the saved draft and retries opening without extracting again', async () => {
  const h = loaderHarness(async () => ({ id: 'saved-runtime' }), false);
  await h.state.findContainers(); await nextTick(); await h.state.discover();
  h.blockPopup();
  h.state.loadSchema(); await h.idle();
  assert.equal(h.state.savedDraft.value.id, 'saved-runtime');
  assert.match(h.state.message.value, /new draft is saved/);
  const extractions = h.calls.filter(call => call === 'extract').length;
  h.state.openSavedDraft();
  assert.equal(h.calls.filter(call => call === 'extract').length, extractions);
  assert.equal(h.adopted.length, 2);
});

test('SSH association requires scanning and explicit fingerprint confirmation', async () => {
  const h = loaderHarness(async () => ({}));
  await h.state.findContainers(); await nextTick();
  h.state.sshHost.value = 'db.example'; await nextTick();
  await h.state.associateHost();
  assert.equal(h.helperCalls.length, 0);
  await h.state.scanHost();
  assert.equal(h.helperCalls[0][1], 'ssh-scan');
  await h.state.associateHost();
  assert.equal(h.helperCalls.length, 1);
  h.state.confirmed.value = true;
  await h.state.associateHost();
  assert.equal(h.helperCalls[1][1], 'associate');
  assert.deepEqual(JSON.parse(JSON.stringify(h.helperCalls[1][3])), { host: 'db.example', ssh_port: 22, host_fingerprint: 'SHA256:known-key', confirmed: true });
});
