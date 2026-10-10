<script setup lang="ts">
import { Activity, AlertTriangle, ArrowUpRight, BookOpen, CheckCircle2, RefreshCw, Server, Trash2, XCircle } from 'lucide-vue-next';
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import ConfirmModal from './ConfirmModal.vue';
import { makeRequest } from '../ui/makeRequest';
import { subscribeRuntimeUpdates } from '../ui/runtimeEvents';
import type { RuntimeDatabaseCall, RuntimeProjectRecord, RuntimeRequest, RuntimeRequestDetail, RuntimeSnapshotData } from '../types';

const emit = defineEmits<{
  (event: 'openDefinition', projectId: string, revisionId: string, symbolId: string): void;
}>();

const projects = ref<RuntimeProjectRecord[]>([]);
const selectedKey = ref('');
const loadedDetailKey = ref('');
const detail = ref<RuntimeSnapshotData | null>(null);
const requestList = ref<{ available: boolean; reason?: string | null; sampled?: boolean; window_hours?: number; requests: RuntimeRequest[] } | null>(null);
const requestListLoading = ref(false);
const requestListError = ref('');
const hiddenTraceIds = ref(new Set<string>());
const deleteTarget = ref<{ key: string; projectId: string; revisionId: string; traceIds: string[] } | null>(null);
const deletingRequests = ref(false);
const requestDeleteError = ref('');
const requestDeleteNotice = ref('');
const expandedTraceId = ref('');
const requestDetails = ref<Record<string, RuntimeRequestDetail>>({});
const requestDetailLoading = ref<Record<string, boolean>>({});
const requestDetailErrors = ref<Record<string, string>>({});
const loadingProjects = ref(false);
const loadingDetail = ref(false);
const error = ref('');
const setupState = ref<'idle' | 'starting' | 'running' | 'complete' | 'cancelled' | 'error'>('idle');
const setupMessage = ref('');
const activeOperation = ref<'setup' | 'cleanup' | 'remove' | ''>('');
const savedSetup = ref<any>(null);
const deadOnly = ref(false);
const showGuide = ref(false);
const ciTokenSnippet = 'TELEMETRY_CI_PROJECT_TOKENS={"billing":"replace-with-a-32-plus-character-random-token"}';
const snapshotUploadSnippet = [
  'CI_COMMIT_SHA="$(git rev-parse HEAD)"',
  'git archive --format=zip --output=source.zip "$CI_COMMIT_SHA"',
  'curl --fail-with-body --request PUT \\',
  '  --header "Authorization: Bearer $INDEXER_CI_TOKEN" \\',
  "  --header 'Content-Type: application/zip' \\",
  '  --data-binary @source.zip \\',
  '  "$INDEXER_API/telemetry/projects/$INDEXER_PROJECT/snapshots/$CI_COMMIT_SHA/source"',
  '',
  '# Check snapshot state',
  'curl --fail-with-body \\',
  '  --header "Authorization: Bearer $INDEXER_CI_TOKEN" \\',
  '  "$INDEXER_API/telemetry/projects/$INDEXER_PROJECT/snapshots/$CI_COMMIT_SHA"',
].join('\n');
const runtimeSetupCommands = [
  'just runtime-setup',
  'just runtime-status',
  'just runtime-cleanup',
].join('\n');
const runtimeCollectorSnippet = 'cd api\ndocker compose --profile runtime up -d';
const spanAttributesSnippet = [
  'indexer.project_id: billing',
  'indexer.revision_id: rev_0123456789abcdef0123456789abcdef',
  'indexer.symbol_id: canonical-symbol-id-from-the-indexer',
  'indexer.code_dirty: false',
  'indexer.schema.version: indexer-telemetry-v2',
].join('\n');
let stopRuntimeUpdates: (() => void) | undefined;

const options = computed(() => {
  const byKey = new Map<string, RuntimeProjectRecord & { key: string; commit_sha: string }>();
  for (const project of projects.value) {
    const commits = [...new Set([project.commit_sha, ...(project.running_commit_shas || [])]
      .filter((commit): commit is string => typeof commit === 'string' && commit.length > 0))];
    for (const commit_sha of commits) {
      const isSnapshot = project.commit_sha === commit_sha;
      const revision_id = isSnapshot ? project.revision_id : null;
      const key = revision_id ? `${project.project_id}:${revision_id}` : `${project.project_id}:docker-only`;
      const current = byKey.get(key);
      const option = {
        ...project,
        key,
        commit_sha,
        revision_id,
        state: isSnapshot ? project.state : 'awaiting_source',
      };
      // Prefer the real snapshot row when it exists; otherwise make active
      // deployments selectable even before their source snapshot is uploaded.
      if (!current || isSnapshot) byKey.set(key, option);
    }
  }
  return [...byKey.values()];
});
const selected = computed(() => options.value.find((item) => item.key === selectedKey.value) || null);
const visibleRequests = computed(() => (requestList.value?.requests || []).filter((request) => !hiddenTraceIds.value.has(request.trace_id)));
const deleteTargetMessage = computed(() => {
  const count = deleteTarget.value?.traceIds.length || 0;
  return `Delete telemetry for ${count} loaded request trace${count === 1 ? '' : 's'}? This removes all stored spans for these traces in the selected project and revision, including database-call spans. Runtime aggregate metrics for those spans will decrease. This cannot be undone.`;
});
const metricsBySymbol = computed(() => new Map((detail.value?.telemetry.symbols || []).map((item) => [item.symbol_id, item])));
const symbols = computed(() => {
  const metrics = metricsBySymbol.value;
  const eligible = (detail.value?.definitions || []).filter((item) => ['method', 'function'].includes(item.kind) && item.symbol_id);
  const rows = eligible.map((item) => ({ ...item, metric: metrics.get(item.symbol_id!) || null }));
  return deadOnly.value ? rows.filter((item) => !item.metric) : rows;
});
const deadCount = computed(() => (detail.value?.definitions || []).filter((item) =>
  ['method', 'function'].includes(item.kind) && item.symbol_id && !metricsBySymbol.value.has(item.symbol_id),
).length);
const hotCount = computed(() => (detail.value?.telemetry.symbols || []).filter((item) => Number(item.runs) >= 100).length);

const loadProjects = async () => {
  loadingProjects.value = true;
  try {
    const response = await makeRequest('/api/runtime/projects');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load runtime project inventory.');
    projects.value = data.projects || [];
    if (!selectedKey.value || !options.value.some((item) => item.key === selectedKey.value)) {
      selectedKey.value = options.value[0]?.key || '';
    }
  } catch (cause: any) {
    error.value = cause?.message || 'Could not load runtime project inventory.';
  } finally {
    loadingProjects.value = false;
  }
};

const loadDetail = async () => {
  if (!selected.value?.commit_sha) {
    loadedDetailKey.value = '';
    detail.value = null;
    requestList.value = null;
    requestListError.value = '';
    hiddenTraceIds.value = new Set();
    requestDeleteError.value = '';
    requestDeleteNotice.value = '';
    expandedTraceId.value = '';
    requestDetails.value = {};
    requestDetailLoading.value = {};
    requestDetailErrors.value = {};
    requestListLoading.value = false;
    loadingDetail.value = false;
    return;
  }
  const selectedKeyAtStart = selected.value.key;
  const isSelectionChange = loadedDetailKey.value !== selectedKeyAtStart;
  loadingDetail.value = true;
  error.value = '';
  requestListError.value = '';
  if (isSelectionChange) {
    detail.value = null;
    requestList.value = null;
    hiddenTraceIds.value = new Set();
    deleteTarget.value = null;
    requestDeleteError.value = '';
    requestDeleteNotice.value = '';
    expandedTraceId.value = '';
    requestDetails.value = {};
    requestDetailLoading.value = {};
    requestDetailErrors.value = {};
  }
  try {
    const { project_id, commit_sha, revision_id } = selected.value;
    const identityPath = revision_id
      ? `revisions/${encodeURIComponent(revision_id)}`
      : `snapshots/${encodeURIComponent(commit_sha)}`;
    const path = `/api/runtime/projects/${encodeURIComponent(project_id)}/${identityPath}`;
    const response = await makeRequest(path);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load runtime snapshot.');
    if (selected.value?.key !== selectedKeyAtStart) return;
    detail.value = data as RuntimeSnapshotData;
    loadedDetailKey.value = selectedKeyAtStart;
    if (revision_id) await loadRequestList(project_id, revision_id, selectedKeyAtStart);
    else requestList.value = null;
  } catch (cause: any) {
    if (selected.value?.key === selectedKeyAtStart) {
      if (!detail.value || loadedDetailKey.value !== selectedKeyAtStart) {
        detail.value = null;
        error.value = cause?.message || 'Could not load runtime snapshot.';
      }
    }
  } finally {
    if (selected.value?.key === selectedKeyAtStart) loadingDetail.value = false;
  }
};

const loadRequestList = async (projectId = selected.value?.project_id, revisionId = selected.value?.revision_id, expectedKey = selected.value?.key) => {
  if (!projectId || !revisionId) { requestList.value = null; return; }
  requestListLoading.value = true;
  requestListError.value = '';
  try {
    const path = `/api/runtime/projects/${encodeURIComponent(projectId)}/revisions/${encodeURIComponent(revisionId)}/requests`;
    const response = await makeRequest(path);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load recent requests.');
    if (selected.value?.key === expectedKey) {
      requestList.value = data;
      const currentTraceIds = new Set((data.requests || []).map((request: RuntimeRequest) => request.trace_id));
      hiddenTraceIds.value = new Set([...hiddenTraceIds.value].filter((traceId) => currentTraceIds.has(traceId)));
    }
  } catch (cause: any) {
    if (selected.value?.key === expectedKey) requestListError.value = cause?.message || 'Could not load recent requests.';
  } finally {
    if (selected.value?.key === expectedKey) requestListLoading.value = false;
  }
};

const toggleRequest = async (request: RuntimeRequest, retry = false) => {
  const existing = requestDetails.value[request.trace_id];
  if (expandedTraceId.value === request.trace_id && !retry) { expandedTraceId.value = ''; return; }
  expandedTraceId.value = request.trace_id;
  if (requestDetailLoading.value[request.trace_id] || (existing && !retry)) return;
  if (existing && retry) {
    const remaining = { ...requestDetails.value };
    delete remaining[request.trace_id];
    requestDetails.value = remaining;
  }
  const current = selected.value;
  if (!current?.revision_id) return;
  const expectedKey = current.key;
  requestDetailLoading.value = { ...requestDetailLoading.value, [request.trace_id]: true };
  requestDetailErrors.value = { ...requestDetailErrors.value, [request.trace_id]: '' };
  try {
    const path = `/api/runtime/projects/${encodeURIComponent(current.project_id)}/revisions/${encodeURIComponent(current.revision_id)}/requests/${encodeURIComponent(request.trace_id)}`;
    const response = await makeRequest(path);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load request details.');
    if (selected.value?.key === expectedKey) requestDetails.value = { ...requestDetails.value, [request.trace_id]: data };
  } catch (cause: any) {
    if (selected.value?.key === expectedKey) requestDetailErrors.value = { ...requestDetailErrors.value, [request.trace_id]: cause?.message || 'Could not load request details.' };
  } finally {
    if (selected.value?.key === expectedKey) requestDetailLoading.value = { ...requestDetailLoading.value, [request.trace_id]: false };
  }
};

const formatMilliseconds = (value: number | null) => value == null ? 'Unavailable' : `${Number(value).toFixed(2)} ms`;
const formatPhpCpu = (value: number | null) => value == null ? 'Unavailable' : `${(value / 1000).toFixed(2)} ms`;
const formatPhpMemory = (value: number | null) => value == null ? 'Unavailable' : value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(2)} MiB` : `${(value / 1024).toFixed(1)} KiB`;
const displayTable = (tableId: string) => tableId.replace(/^table:/, '').split('.').slice(1).join('.') || tableId;
const displaySymbol = (symbolId: string | null) => {
  if (!symbolId) return 'Unavailable';
  const definition = detail.value?.definitions.find((item) => item.symbol_id === symbolId);
  return definition?.fqn || definition?.name || symbolId;
};
const requestMethod = (call: RuntimeDatabaseCall) => displaySymbol(call.caller_symbol_id || call.symbol_id);

const refresh = async () => { await loadProjects(); await loadDetail(); };
const clearRequestRows = () => {
  const traceIds = visibleRequests.value.map((request) => request.trace_id);
  if (!traceIds.length) return;
  hiddenTraceIds.value = new Set([...hiddenTraceIds.value, ...traceIds]);
  expandedTraceId.value = '';
  requestDeleteNotice.value = '';
};
const restoreHiddenRequestRows = () => { hiddenTraceIds.value = new Set(); };
const cancelDeleteRequestRows = () => {
  if (deletingRequests.value) return;
  deleteTarget.value = null;
  requestDeleteError.value = '';
};
const confirmDeleteRequestRows = () => {
  const current = selected.value;
  const traceIds = [...new Set((requestList.value?.requests || []).map((request) => request.trace_id))];
  if (!current?.revision_id || !current.can_delete_telemetry || !traceIds.length) return;
  requestDeleteError.value = '';
  requestDeleteNotice.value = '';
  deleteTarget.value = {
    key: current.key,
    projectId: current.project_id,
    revisionId: current.revision_id,
    traceIds,
  };
};
const deleteStoredRequestRows = async () => {
  const target = deleteTarget.value;
  if (!target || deletingRequests.value || selected.value?.key !== target.key) return;
  deletingRequests.value = true;
  requestDeleteError.value = '';
  try {
    const path = `/api/runtime/projects/${encodeURIComponent(target.projectId)}/revisions/${encodeURIComponent(target.revisionId)}/requests`;
    const response = await makeRequest(path, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trace_ids: target.traceIds }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not delete request telemetry.');
    const deleted = new Set(target.traceIds);
    requestList.value = requestList.value
      ? { ...requestList.value, requests: requestList.value.requests.filter((request) => !deleted.has(request.trace_id)) }
      : null;
    hiddenTraceIds.value = new Set([...hiddenTraceIds.value].filter((traceId) => !deleted.has(traceId)));
    const remainingDetails = { ...requestDetails.value };
    const remainingErrors = { ...requestDetailErrors.value };
    const remainingLoading = { ...requestDetailLoading.value };
    for (const traceId of deleted) {
      delete remainingDetails[traceId];
      delete remainingErrors[traceId];
      delete remainingLoading[traceId];
    }
    requestDetails.value = remainingDetails;
    requestDetailErrors.value = remainingErrors;
    requestDetailLoading.value = remainingLoading;
    expandedTraceId.value = '';
    requestDeleteNotice.value = `Deleted telemetry for ${target.traceIds.length} request trace${target.traceIds.length === 1 ? '' : 's'}. Runtime aggregates are refreshing.`;
    deleteTarget.value = null;
    await refresh();
  } catch (cause: any) {
    requestDeleteError.value = cause?.message || 'Could not delete request telemetry.';
  } finally {
    deletingRequests.value = false;
  }
};
const getRuntimeHelperAuth = async () => {
  const response = await makeRequest('/api/runtime/setup-authorization', { method: 'POST' });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Runtime setup authorization is unavailable.');
  return data.token as string;
};
const getLocalContainerRemovalAuth = async (projectId: string) => {
  const response = await makeRequest('/api/runtime/local-container-removal-authorization', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Local Runtime container removal is not authorized.');
  return data.token as string;
};
const loadSavedSetup = async (showExistingNotice = true) => {
  try {
    const token = await getRuntimeHelperAuth();
    const response = await fetch('http://127.0.0.1:47831/setup-state', {
      headers: { Authorization: `Bearer ${token}` }, cache: 'no-store',
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok) {
      savedSetup.value = data.configured ? data : null;
      if (data.configured && showExistingNotice) {
        setupState.value = 'complete';
        setupMessage.value = `Existing setup: ${data.project_id} · ${data.environment} (${data.phase}).`;
      }
    }
  } catch {
    // Runtime inventory remains available to users who cannot run host setup.
  }
};
const startRuntimeSetup = async () => {
  if (savedSetup.value) {
    const phase = savedSetup.value.phase || 'incomplete';
    setupState.value = phase === 'active' ? 'complete' : 'error';
    setupMessage.value = phase === 'active'
      ? `Runtime is already active for ${savedSetup.value.project_id} · ${savedSetup.value.environment}. Clean it up before setting up a different project.`
      : `Runtime recovery is in phase '${phase}'. Run just runtime-status, then Clean up Runtime before starting another setup.`;
    return;
  }
  activeOperation.value = 'setup';
  setupState.value = 'starting';
  setupMessage.value = 'Authorizing local Runtime setup…';
  try {
    let token = await getRuntimeHelperAuth();
    const helperUrl = 'http://127.0.0.1:47831';
    let authorizationAt = Date.now();
    const headers = () => ({ Authorization: `Bearer ${token}` });
    const startResponse = await fetch(`${helperUrl}/setup`, { method: 'POST', headers: headers(), cache: 'no-store' });
    const started = await startResponse.json().catch(() => ({}));
    if (!startResponse.ok) throw new Error(started.detail || 'Could not start the local Runtime setup helper. Run just up and try again.');
    setupState.value = 'running';
    while (true) {
      if (Date.now() - authorizationAt > 120_000) {
        token = await getRuntimeHelperAuth();
        authorizationAt = Date.now();
      }
      const statusResponse = await fetch(`${helperUrl}/status`, { headers: headers(), cache: 'no-store' });
      const status = await statusResponse.json().catch(() => ({}));
      if (!statusResponse.ok) throw new Error(status.detail || 'Could not read Runtime setup progress.');
      setupMessage.value = status.message || 'Runtime setup is running…';
      if (status.status === 'complete') { setupState.value = 'complete'; await loadSavedSetup(); await refresh(); break; }
      if (status.status === 'cancelled') { setupState.value = 'cancelled'; break; }
      if (status.status === 'error') { setupState.value = 'error'; break; }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  } catch (cause: any) {
    setupState.value = 'error';
    setupMessage.value = cause?.message || 'Runtime setup failed.';
  }
};
const cleanupRuntime = async () => {
  activeOperation.value = 'cleanup';
  setupState.value = 'starting';
  setupMessage.value = 'Authorizing local Runtime cleanup…';
  try {
    let token = await getRuntimeHelperAuth();
    const helperUrl = 'http://127.0.0.1:47831';
    let authorizationAt = Date.now();
    const headers = () => ({ Authorization: `Bearer ${token}` });
    const startResponse = await fetch(`${helperUrl}/cleanup`, { method: 'POST', headers: headers(), cache: 'no-store' });
    const started = await startResponse.json().catch(() => ({}));
    if (startResponse.status === 404) throw new Error('The local Runtime helper is out of date. Run just up to restart it, then reload this page.');
    if (!startResponse.ok) throw new Error(started.detail || 'Could not start local Runtime cleanup.');
    setupState.value = 'running';
    while (true) {
      if (Date.now() - authorizationAt > 120_000) {
        token = await getRuntimeHelperAuth();
        authorizationAt = Date.now();
      }
      const statusResponse = await fetch(`${helperUrl}/status`, { headers: headers(), cache: 'no-store' });
      const status = await statusResponse.json().catch(() => ({}));
      if (!statusResponse.ok) throw new Error(status.detail || 'Could not read Runtime cleanup progress.');
      setupMessage.value = status.message || 'Runtime cleanup is running…';
      if (status.status === 'complete') {
        setupState.value = 'complete';
        await loadSavedSetup(false);
        // Cleanup recreates selected services before stopping the host reporter;
        // give its event reconciliation a moment to close the old deployment rows.
        if (activeOperation.value === 'cleanup') await new Promise((resolve) => setTimeout(resolve, 1500));
        await refresh();
        break;
      }
      if (status.status === 'cancelled') { setupState.value = 'cancelled'; await loadSavedSetup(false); break; }
      if (status.status === 'error') { setupState.value = 'error'; await loadSavedSetup(false); break; }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  } catch (cause: any) {
    setupState.value = 'error';
    setupMessage.value = cause?.message || 'Runtime cleanup failed.';
  }
};
const removeLocalContainers = async () => {
  const projectId = selected.value?.project_id;
  if (!projectId || !selected.value?.can_remove) return;
  activeOperation.value = 'remove';
  setupState.value = 'starting';
  setupMessage.value = `Authorizing local container removal for ${projectId}…`;
  try {
    let token = await getLocalContainerRemovalAuth(projectId);
    const helperUrl = 'http://127.0.0.1:47831';
    let authorizationAt = Date.now();
    const headers = () => ({ Authorization: `Bearer ${token}` });
    const startResponse = await fetch(`${helperUrl}/remove-project-containers`, {
      method: 'POST',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId }),
      cache: 'no-store',
    });
    const started = await startResponse.json().catch(() => ({}));
    if (startResponse.status === 404) throw new Error('The local Runtime helper is out of date. Run just up to restart it, then reload this page.');
    const resumingExistingRemoval = startResponse.status === 409;
    if (!startResponse.ok && !resumingExistingRemoval) throw new Error(started.detail || 'Could not start local container removal.');
    if (resumingExistingRemoval) setupMessage.value = 'Reconnecting to the active local container removal…';
    setupState.value = 'running';
    while (true) {
      if (Date.now() - authorizationAt > 120_000) {
        token = await getLocalContainerRemovalAuth(projectId);
        authorizationAt = Date.now();
      }
      const statusResponse = await fetch(`${helperUrl}/status`, { headers: headers(), cache: 'no-store' });
      const status = await statusResponse.json().catch(() => ({}));
      if (!statusResponse.ok) {
        throw new Error(resumingExistingRemoval
          ? started.detail || status.detail || 'Could not reconnect to local container removal.'
          : status.detail || 'Could not read local container removal progress.');
      }
      setupMessage.value = status.message || 'Local container removal is running…';
      if (Array.isArray(status.failed_containers) && status.failed_containers.length) {
        setupMessage.value += ` Container IDs: ${status.failed_containers.join(', ')}.`;
      }
      if (status.status === 'complete') {
        setupState.value = 'complete';
        await refresh();
        break;
      }
      if (status.status === 'cancelled') {
        setupState.value = 'cancelled';
        await refresh();
        break;
      }
      if (status.status === 'error') {
        setupState.value = 'error';
        await refresh();
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  } catch (cause: any) {
    setupState.value = 'error';
    setupMessage.value = cause?.message || 'Local container removal failed.';
    await refresh();
  }
};
const formatCount = (value: number | string) => Number(value || 0).toLocaleString();
const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : 'No observed calls';
const symbolMetric = (symbolId: string) => metricsBySymbol.value.get(symbolId);
const callersFor = (symbolId: string) => (detail.value?.telemetry.callers || []).filter((row) => row.symbol_id === symbolId);
const routesFor = (symbolId: string) => (detail.value?.telemetry.symbol_routes || []).filter((row) => row.symbol_id === symbolId);
const tablesFor = (symbolId: string) => (detail.value?.telemetry.symbol_tables || []).filter((row) => row.symbol_id === symbolId);
const openDefinition = (symbolId?: string | null) => {
  if (selected.value?.revision_id && symbolId) emit('openDefinition', selected.value.project_id, selected.value.revision_id, symbolId);
};

watch(selectedKey, loadDetail);
onMounted(async () => {
  await Promise.all([refresh(), loadSavedSetup()]);
  stopRuntimeUpdates = subscribeRuntimeUpdates(() => { void refresh(); });
});
onUnmounted(() => stopRuntimeUpdates?.());
</script>

<template>
  <section class="flex-1 overflow-y-auto p-6 space-y-5">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h3 class="flex items-center gap-2 text-lg font-bold text-white"><Activity class="h-5 w-5 text-cyan-400" />Runtime</h3>
        <p class="mt-1 text-xs text-slate-500">Opt-in local Docker deployments, span flow, code matching, routes, and database tables.</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <button type="button" @click="startRuntimeSetup" :disabled="!!savedSetup || setupState === 'starting' || setupState === 'running'" class="rounded-xl border border-emerald-500/30 px-3 py-2 text-xs text-emerald-200 hover:bg-emerald-500/10 disabled:opacity-50"><Activity class="mr-1 inline h-3.5 w-3.5" :class="(setupState === 'starting' || setupState === 'running') && activeOperation === 'setup' ? 'animate-pulse' : ''" />{{ (setupState === 'starting' || setupState === 'running') && activeOperation === 'setup' ? 'Setting up…' : 'Set up Runtime' }}</button>
        <button type="button" @click="cleanupRuntime" :disabled="setupState === 'starting' || setupState === 'running'" title="Checkbox acknowledgements are optional; Continue approves all applicable cleanup actions. Cancel preserves recovery state." class="rounded-xl border border-rose-500/30 px-3 py-2 text-xs text-rose-200 hover:bg-rose-500/10 disabled:opacity-50"><Activity class="mr-1 inline h-3.5 w-3.5" :class="(setupState === 'starting' || setupState === 'running') && activeOperation === 'cleanup' ? 'animate-pulse' : ''" />{{ (setupState === 'starting' || setupState === 'running') && activeOperation === 'cleanup' ? 'Cleaning up…' : 'Clean up Runtime' }}</button>
        <button type="button" @click="showGuide = !showGuide" class="rounded-xl border border-cyan-500/30 px-3 py-2 text-xs text-cyan-200 hover:bg-cyan-500/10"><BookOpen class="mr-1 inline h-3.5 w-3.5" />{{ showGuide ? 'Hide guide' : 'Get Started' }}</button>
        <select v-model="selectedKey" :disabled="setupState === 'starting' || setupState === 'running'" class="max-w-[24rem] rounded-xl border border-slate-800 bg-[#11162d] px-3 py-2 text-xs text-slate-200 disabled:opacity-50">
          <option value="">Select project / indexed revision</option>
          <option v-for="item in options" :key="item.key" :value="item.key">{{ item.project_id }} · {{ item.revision_id ? `Revision ${item.revision_id.slice(4, 12)}` : 'Docker only · no indexed revision' }}</option>
        </select>
        <button v-if="selected?.can_remove" type="button" @click="removeLocalContainers" :disabled="setupState === 'starting' || setupState === 'running'" title="Stops and removes only matching local containers. Runtime history and remote containers remain." class="rounded-xl border border-rose-500/30 px-3 py-2 text-xs text-rose-200 hover:bg-rose-500/10 disabled:opacity-50"><Trash2 class="mr-1 inline h-3.5 w-3.5" :class="(setupState === 'starting' || setupState === 'running') && activeOperation === 'remove' ? 'animate-pulse' : ''" />{{ (setupState === 'starting' || setupState === 'running') && activeOperation === 'remove' ? 'Removing containers…' : 'Remove local containers' }}</button>
        <button type="button" @click="refresh" :disabled="loadingProjects || loadingDetail || setupState === 'starting' || setupState === 'running'" class="rounded-xl border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"><RefreshCw class="mr-1 inline h-3.5 w-3.5" :class="loadingProjects || loadingDetail ? 'animate-spin' : ''" />Refresh</button>
      </div>
    </header>

    <div v-if="setupState !== 'idle'" class="rounded-xl border p-3 text-xs" :class="setupState === 'error' ? 'border-rose-500/25 bg-rose-950/20 text-rose-300' : setupState === 'complete' ? 'border-emerald-500/25 bg-emerald-950/20 text-emerald-300' : setupState === 'cancelled' ? 'border-amber-500/25 bg-amber-950/20 text-amber-300' : 'border-cyan-500/25 bg-cyan-950/20 text-cyan-200'">
      <span class="font-semibold">{{ setupState === 'complete' ? (activeOperation === 'cleanup' ? 'Cleanup complete' : activeOperation === 'remove' ? 'Local container removal complete' : savedSetup ? 'Existing setup record' : 'Setup complete') : setupState === 'cancelled' ? (activeOperation === 'cleanup' ? 'Cleanup cancelled' : activeOperation === 'remove' ? 'Local container removal cancelled' : 'Setup cancelled') : setupState === 'error' ? (activeOperation === 'cleanup' ? 'Cleanup needs attention' : activeOperation === 'remove' ? 'Local container removal needs attention' : 'Setup failed') : activeOperation === 'cleanup' ? 'Runtime cleanup' : activeOperation === 'remove' ? 'Local container removal' : 'Runtime setup' }}</span>
      <span class="ml-2">{{ setupMessage }}</span>
      <span v-if="savedSetup" class="mt-1 block">Compose project: {{ savedSetup.compose_project || 'unknown' }} · Services: {{ (savedSetup.services || []).join(', ') || 'none recorded' }}. Available containers are shown in the Runtime inventory below.</span>
    </div>

    <div v-if="error" class="rounded-xl border border-rose-500/25 bg-rose-950/20 p-4 text-xs text-rose-300">{{ error }}</div>
    <div v-else-if="!options.length" class="rounded-2xl border border-dashed border-slate-700 p-10 text-center">
      <Server class="mx-auto h-7 w-7 text-slate-600" />
      <p class="mt-3 text-sm text-slate-300">No Docker containers are reporting yet.</p>
      <p class="mt-1 text-xs text-slate-500">Start the local host agent and opt in your Compose services. Open Get Started for setup steps; a source snapshot is optional for container visibility.</p>
    </div>

    <section v-if="showGuide" class="rounded-2xl border border-cyan-500/20 bg-cyan-500/[0.04] p-5">
      <div class="flex items-start gap-3">
        <BookOpen class="mt-0.5 h-5 w-5 shrink-0 text-cyan-300" />
        <div class="min-w-0 flex-1 space-y-4">
          <div>
            <h4 class="text-sm font-semibold text-white">Get started with Runtime</h4>
            <p class="mt-1 text-xs leading-5 text-slate-400">Start with your local Docker containers: opt-in labels plus the host agent report what is running. You do not need to upload source code to see containers. Snapshots and OpenTelemetry spans add code, route, and database activity.</p>
          </div>
          <ol class="space-y-3">
            <li class="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
              <p class="text-xs font-semibold text-cyan-200">1. Listen to your local Docker project</p>
              <p class="mt-2 text-[11px] leading-5 text-slate-400">Use Set up Runtime above or run the setup command below from a terminal opened at this repository checkout. The native dialogs discover local Compose projects, ask which services to track, show a compact Compose summary, and request one grouped approval before changing files or recreating services. Only opted-in containers are reported.</p>
              <details open class="mt-3 rounded-lg border border-slate-800 bg-black/20 p-3">
                <summary class="cursor-pointer text-[11px] font-medium text-slate-300">Local setup commands and access</summary>
                <div class="mt-3 space-y-2 text-[10px] leading-4 text-slate-400">
                  <pre class="overflow-x-auto rounded-lg bg-black/30 p-2 text-slate-300"><code>{{ runtimeSetupCommands }}</code></pre>
                  <p>The browser never reads your Docker daemon or local project files. The helper keeps Docker project discovery separate, then shows a compact Compose summary and one grouped checklist covering the setup actions. Recovery state is written before mutations. It stores a private recovery record in your local state directory.</p>
                  <p>After setup, choose the project and indexed revision here. No source snapshot is needed to list containers. The agent reconciles about every 60 seconds; use Refresh to reload sooner. Snapshots and spans are optional additions for code matching and route/database activity. For remote hosts, use managed certificates and follow <code>api/telemetry-agent/README.md</code>.</p>
                </div>
              </details>
            </li>
            <li class="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
              <p class="text-xs font-semibold text-cyan-200">2. Add a source snapshot (optional, for code matching)</p>
              <p class="mt-2 text-[11px] leading-5 text-slate-400">A source snapshot connects observed spans to indexed functions and methods. Git SHA remains an internal source-upload and deployment key; Developer Assistant assigns an immutable revision ID when it registers the snapshot. Use the Gum PHP installer to index this project directly or select a ready revision and configure the PHP app.</p>
              <details class="mt-3 rounded-lg border border-slate-800 bg-black/20 p-3">
                <summary class="cursor-pointer text-[11px] font-medium text-slate-300">Show external CI upload setup</summary>
                <div class="mt-3 space-y-2 text-[10px] leading-4 text-slate-400">
                  <p>For local indexing, the Gum flow creates and stores the project token after confirmation. This example is only for external CI: provision a separate high-entropy bearer token (at least 32 characters) for the project on the API backend. Keep the real token in a secret manager; the value below is a placeholder.</p>
                  <pre class="overflow-x-auto rounded-lg bg-black/30 p-2 text-slate-300"><code>{{ ciTokenSnippet }}</code></pre>
                  <p>Set <code>INDEXER_API</code>, <code>INDEXER_PROJECT=billing</code>, and <code>INDEXER_CI_TOKEN</code> as protected CI variables. The archive must be repository-rooted and match the deployed commit:</p>
                  <pre class="overflow-x-auto rounded-lg bg-black/30 p-2 text-slate-300"><code>{{ snapshotUploadSnippet }}</code></pre>
                  <p>Upload returns while indexing runs. The snapshot becomes ready after its matching deployment report and indexing finish.</p>
                </div>
              </details>
            </li>
            <li class="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
              <p class="text-xs font-semibold text-cyan-200">3. Enable code, route, and database activity (optional)</p>
              <p class="mt-2 text-[11px] leading-5 text-slate-400">Container reports work without application spans. For code matching and route/table activity, start the local Collector and instrument the app to export OTLP/HTTP to <code>http://localhost:24318/v1/traces</code> from the host or <code>http://host.docker.internal:24318/v1/traces</code> from a container.</p>
              <details class="mt-3 rounded-lg border border-slate-800 bg-black/20 p-3">
                <summary class="cursor-pointer text-[11px] font-medium text-slate-300">Show Collector command and span attributes</summary>
                <div class="mt-3 space-y-2 text-[10px] leading-4 text-slate-400">
                  <pre class="overflow-x-auto rounded-lg bg-black/30 p-2 text-slate-300"><code>{{ runtimeCollectorSnippet }}</code></pre>
                  <p>Each source-attributed span must include the project and immutable revision identity plus the canonical PHP symbol ID from the indexer:</p>
                  <pre class="overflow-x-auto rounded-lg bg-black/30 p-2 text-slate-300"><code>{{ spanAttributesSnippet }}</code></pre>
                  <p>Add <code>http.route</code> and <code>http.request.method</code> to request spans, and <code>db.operation.name</code> plus <code>indexer.db.table</code> to database spans. Do not send raw SQL or request data. App instrumentation is not bundled; see <code>api/docs/telemetry/attributes.md</code>.</p>
                </div>
              </details>
            </li>
          </ol>
          <div class="rounded-2xl border border-slate-700 bg-[#0b1020] p-4">
            <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h5 class="text-xs font-semibold text-slate-200">Example Runtime view</h5>
                <p class="mt-1 font-mono text-[10px] text-slate-500">billing @ rev_01234567… · staging</p>
              </div>
              <span class="rounded-full border border-indigo-500/20 bg-indigo-500/5 px-2 py-1 text-[9px] uppercase tracking-wide text-indigo-300">Example only</span>
            </div>
            <div class="grid grid-cols-2 gap-2 md:grid-cols-5">
              <article class="rounded-xl border border-slate-800 bg-slate-900/50 p-3"><p class="text-[9px] uppercase text-slate-500">Running containers</p><p class="mt-1 text-lg font-semibold text-white">2</p><p class="text-[9px] text-slate-500">2 match this revision</p></article>
              <article class="rounded-xl border border-slate-800 bg-slate-900/50 p-3"><p class="text-[9px] uppercase text-slate-500">Observed spans</p><p class="mt-1 text-lg font-semibold text-white">1,024</p><p class="text-[9px] text-slate-500">sample window</p></article>
              <article class="rounded-xl border border-slate-800 bg-slate-900/50 p-3"><p class="text-[9px] uppercase text-slate-500">Matched to code</p><p class="mt-1 text-lg font-semibold text-cyan-300">82%</p><p class="text-[9px] text-slate-500">840 / 1,024 spans</p><div class="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800"><div class="h-full w-[82%] rounded-full bg-cyan-400"></div></div><div class="mt-1 flex justify-between text-[8px] text-slate-600"><span>0%</span><span>100%</span></div></article>
              <article class="rounded-xl border border-slate-800 bg-slate-900/50 p-3"><p class="text-[9px] uppercase text-slate-500">Possible dead code</p><p class="mt-1 text-lg font-semibold text-amber-300">14</p><p class="text-[9px] text-slate-500">not observed</p></article>
              <article class="rounded-xl border border-slate-800 bg-slate-900/50 p-3"><p class="text-[9px] uppercase text-slate-500">Hot symbols</p><p class="mt-1 text-lg font-semibold text-orange-300">3</p><p class="text-[9px] text-slate-500">100+ calls</p></article>
            </div>
            <div class="mt-3 rounded-xl border border-slate-800 bg-slate-900/30 p-3">
              <p class="mb-2 text-[9px] font-semibold uppercase tracking-wide text-slate-500">Reported containers</p>
              <div class="flex flex-wrap items-center gap-2 border-t border-slate-800 py-2 text-[10px]"><CheckCircle2 class="h-3 w-3 text-emerald-400" /><span class="font-semibold text-slate-200">api</span><span class="text-slate-500">staging</span><span class="ml-auto font-mono text-slate-500">c4a8e21…</span></div>
              <div class="flex flex-wrap items-center gap-2 border-t border-slate-800 py-2 text-[10px]"><CheckCircle2 class="h-3 w-3 text-emerald-400" /><span class="font-semibold text-slate-200">worker</span><span class="text-slate-500">staging</span><span class="ml-auto font-mono text-slate-500">c4a8e21…</span></div>
            </div>
            <p class="mt-2 text-[9px] leading-4 text-slate-500">82% means 82 of every 100 observed spans matched an indexed symbol in this example; it is a match rate, not a health score.</p>
          </div>
          <p class="border-t border-slate-800 pt-3 text-[10px] leading-4 text-slate-500">The host agent reconciles containers about every 60 seconds. This view updates when the backend reports new runtime data and after you select Refresh. Aggregates use spans received before Collector tail sampling; PHP SDK head sampling may reduce what reaches the Collector. Container visibility works without spans; code matching, routes, and table activity need spans.</p>
        </div>
      </div>
    </section>

    <template v-if="options.length && detail && !error">
      <div class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-800 bg-slate-900/30 p-3 text-[11px]">
        <span class="font-mono text-slate-200">{{ detail.project_id }} · {{ detail.revision_id || 'Docker only · no indexed revision' }}</span>
        <span class="ml-auto rounded-full border px-2 py-1" :class="detail.snapshot.state === 'ready' ? 'border-emerald-500/20 bg-emerald-500/5 text-emerald-300' : 'border-amber-500/20 bg-amber-500/5 text-amber-300'">{{ detail.snapshot.state === 'awaiting_source' ? 'Docker only · no source snapshot' : detail.snapshot.state }}</span>
        <span v-if="detail.deployment_mismatch" class="inline-flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/5 px-2 py-1 text-amber-300"><AlertTriangle class="h-3 w-3" />Running containers use another revision or unindexed code</span>
      </div>

      <div v-if="detail.snapshot.state !== 'awaiting_source'" class="grid grid-cols-2 gap-3 xl:grid-cols-7">
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Running containers</p><p class="mt-2 text-2xl font-semibold text-white">{{ detail.deployments.length }}</p><p class="mt-1 text-[10px] text-slate-500">{{ detail.matching_deployments.length }} match this revision</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Observed spans</p><p class="mt-2 text-2xl font-semibold text-white">{{ formatCount(detail.telemetry.total_spans) }}</p><p class="mt-1 text-[10px] text-slate-500">Last received {{ formatTime(detail.telemetry.last_seen) }}</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Matched to code</p><p class="mt-2 text-2xl font-semibold text-white">{{ detail.telemetry.match_percent }}%</p><p class="mt-1 text-[10px] text-slate-500">{{ formatCount(detail.telemetry.matched_spans) }} / {{ formatCount(detail.telemetry.symbol_spans) }} symbol spans</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Unmatched symbols</p><p class="mt-2 text-2xl font-semibold text-amber-300">{{ detail.telemetry.unmatched_percent }}%</p><p class="mt-1 text-[10px] text-slate-500">{{ formatCount(detail.telemetry.unmatched_spans) }} spans need mapping</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Span input</p><p class="mt-2 text-lg font-semibold text-white">{{ detail.telemetry.pipeline_health.replace('_', ' ') }}</p><p class="mt-1 text-[10px] text-slate-500">Rolling p95 window: {{ detail.telemetry.aggregation_window_hours }}h</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Possible dead code</p><p class="mt-2 text-2xl font-semibold text-amber-300">{{ formatCount(deadCount) }}</p><p class="mt-1 text-[10px] text-slate-500">No calls observed in this window</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Hot symbols</p><p class="mt-2 text-2xl font-semibold text-orange-300">{{ formatCount(hotCount) }}</p><p class="mt-1 text-[10px] text-slate-500">100+ observed calls in window</p></article>
      </div>

      <div v-else class="rounded-xl border border-cyan-500/20 bg-cyan-500/5 p-4 text-xs text-cyan-100">
        <p class="font-semibold">Docker-only view is active</p>
        <p class="mt-1 text-slate-400">The host agent is reporting {{ detail.deployments.length }} running container(s) for this project. Source upload and application spans are optional; add a source snapshot only when you want code matching and code-level views.</p>
      </div>

      <div v-if="detail.snapshot.state !== 'awaiting_source' && detail.telemetry.reason" class="rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-xs text-amber-200">{{ detail.telemetry.reason }} Start the local runtime telemetry profile and instrument the PHP application with the Phase 1 identity attributes. The synthetic Phase 0 collector does not accept application spans.</div>
      <div v-if="detail.snapshot.state !== 'awaiting_source' && detail.symbol_index_available === false" class="rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-xs text-amber-200">{{ detail.symbol_index_reason || 'The symbol index is unavailable.' }} Runtime spans may still be present, but code-match and dead-code views require the indexed snapshot.</div>

      <section v-if="detail.revision_id" class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div><h4 class="text-xs font-semibold uppercase tracking-wide text-slate-300">Recent sampled requests</h4><p class="mt-1 text-[10px] text-slate-500">At most 50 requests from the last {{ requestList?.window_hours || detail.telemetry.aggregation_window_hours }} hours. PHP CPU and memory are worker-level estimates.</p></div>
          <div class="flex flex-wrap items-center gap-2">
            <button type="button" @click="clearRequestRows" :disabled="!visibleRequests.length" class="rounded-lg border border-slate-700 px-2 py-1.5 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-50">Clear</button>
            <button v-if="selected?.can_delete_telemetry && requestList?.requests.length" type="button" @click="confirmDeleteRequestRows" class="rounded-lg border border-rose-500/40 px-2 py-1.5 text-[10px] text-rose-300 hover:bg-rose-500/10"><Trash2 class="mr-1 inline h-3 w-3" />Delete stored traces ({{ requestList.requests.length }})</button>
            <button type="button" @click="loadRequestList()" :disabled="requestListLoading" class="rounded-lg border border-slate-700 px-2 py-1.5 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-50"><RefreshCw class="mr-1 inline h-3 w-3" :class="requestListLoading ? 'animate-spin' : ''" />Refresh requests</button>
          </div>
        </div>
        <p v-if="requestDeleteNotice" role="status" class="text-xs text-emerald-300">{{ requestDeleteNotice }}</p>
        <p v-if="requestListLoading && !requestList" class="text-xs text-slate-500">Loading recent sampled requests…</p>
        <div v-if="requestListError" class="flex flex-wrap items-center gap-2 text-xs text-rose-300"><span>{{ requestListError }}</span><button type="button" @click="loadRequestList()" class="underline">Retry</button></div>
        <p v-if="requestList?.available === false" class="text-xs text-amber-300">{{ requestList.reason || 'Request data is unavailable.' }}</p>
        <p v-else-if="requestList && !requestList.requests.length" class="text-xs text-slate-500">No sampled request spans were observed in this window.</p>
        <div v-else-if="requestList && !visibleRequests.length" class="flex flex-wrap items-center gap-2 text-xs text-slate-500"><span>All loaded request rows are hidden in this view.</span><button type="button" @click="restoreHiddenRequestRows" class="underline">Show hidden requests ({{ requestList.requests.length }})</button></div>
        <div v-else-if="visibleRequests.length" class="space-y-2">
          <article v-for="request in visibleRequests" :key="request.trace_id" class="overflow-hidden rounded-xl border border-slate-800 bg-slate-950/30">
            <button type="button" :aria-expanded="expandedTraceId === request.trace_id" @click="toggleRequest(request)" class="flex w-full flex-wrap items-center gap-x-3 gap-y-1 p-3 text-left hover:bg-slate-800/30">
              <span class="rounded bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-300">{{ request.method || 'HTTP' }}</span>
              <span class="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-200">{{ request.route || 'Route unavailable' }}</span>
              <span class="text-[10px] text-slate-400">{{ request.status_code ?? 'Status unavailable' }}</span>
              <span class="text-[10px] text-cyan-300">{{ formatMilliseconds(request.duration_ms) }}</span>
              <span class="text-[10px] text-slate-500">{{ formatTime(request.timestamp) }}</span>
              <span class="w-full text-[10px] text-slate-500">PHP CPU estimate {{ formatPhpCpu(request.php_cpu_time_us) }} · PHP-managed peak above request start {{ formatPhpMemory(request.php_peak_memory_bytes) }}</span>
            </button>
            <div v-if="expandedTraceId === request.trace_id" class="space-y-2 border-t border-slate-800 p-3">
              <p class="text-[10px] text-slate-500">Estimates cover this PHP worker’s request interval. Memory is PHP-managed allocator peak above request start; neither value represents whole-container CPU or RSS.</p>
              <p v-if="requestDetailLoading[request.trace_id]" class="text-xs text-slate-500">Loading database calls…</p>
              <div v-else-if="requestDetailErrors[request.trace_id]" class="flex flex-wrap items-center gap-2 text-xs text-rose-300"><span>{{ requestDetailErrors[request.trace_id] }}</span><button type="button" @click="toggleRequest(request, true)" class="underline">Retry</button></div>
              <template v-else-if="requestDetails[request.trace_id]?.available">
                <section v-if="requestDetails[request.trace_id].ai_summary" class="rounded-lg border border-cyan-500/20 bg-cyan-500/5 p-3">
                  <p class="text-[10px] font-semibold uppercase tracking-wide text-cyan-200">Trace summary</p>
                  <p class="mt-1 text-[11px] text-slate-300">{{ requestDetails[request.trace_id].ai_summary?.actionable_insight }}</p>
                  <div class="mt-2 flex flex-wrap gap-2">
                    <span v-for="(duration, category) in requestDetails[request.trace_id].ai_summary?.otel_breakdown" :key="category" class="rounded bg-slate-900/70 px-2 py-1 text-[10px] text-slate-400">
                      {{ category.replaceAll('_', ' ') }} · {{ formatMilliseconds(duration) }}
                    </span>
                  </div>
                  <div v-if="requestDetails[request.trace_id].ai_summary?.excimer_hotspots.length" class="mt-2">
                    <p class="text-[9px] uppercase text-slate-500">Excimer hotspots · {{ requestDetails[request.trace_id].ai_summary?.profile_event_type === 'EXCIMER_CPU' ? 'CPU samples' : 'wall-clock samples' }}</p>
                    <p v-for="hotspot in requestDetails[request.trace_id].ai_summary?.excimer_hotspots" :key="hotspot.frame" class="mt-1 truncate font-mono text-[10px] text-orange-200">
                      {{ hotspot.sample_percentage.toFixed(1) }}% · {{ hotspot.frame }}
                    </p>
                  </div>
                  <p v-else class="mt-2 text-[10px] text-slate-500">No retained Excimer profile for this trace.</p>
                  <p class="mt-2 text-[9px] text-slate-600">Instrumented MySQL/Redis spans are de-overlapped estimates; remaining time can include uninstrumented work. Excimer percentages describe retained samples, not exact request time.</p>
                </section>
                <p v-if="!requestDetails[request.trace_id].database_calls.length" class="text-xs text-slate-500">No database calls were captured for this request.</p>
                <div v-else class="space-y-1">
                  <div class="grid grid-cols-[1fr_auto_auto] gap-2 px-2 text-[9px] uppercase text-slate-500"><span>Database call · table · attributed method</span><span>Operation</span><span>Duration</span></div>
                  <div v-for="call in requestDetails[request.trace_id].database_calls" :key="call.span_id" class="grid grid-cols-[1fr_auto_auto] items-start gap-2 rounded-lg bg-slate-900/70 px-2 py-2 text-[10px]">
                    <div class="min-w-0"><p class="truncate text-slate-300">{{ call.db_system || 'Database unavailable' }}<span v-if="call.db_namespace"> · {{ call.db_namespace }}</span></p><p class="truncate font-mono text-cyan-300">{{ call.table_ids.length ? call.table_ids.map(displayTable).join(', ') : 'Table unavailable' }}</p><p class="truncate text-slate-500">{{ requestMethod(call) }}</p></div>
                    <span class="text-indigo-300">{{ call.operation || 'Unavailable' }}</span><span class="text-slate-400">{{ formatMilliseconds(call.duration_ms) }}</span>
                  </div>
                </div>
              </template>
              <div v-else class="flex flex-wrap items-center gap-2 text-xs text-amber-300"><span>{{ requestDetails[request.trace_id]?.reason || 'Request details are unavailable.' }}</span><button type="button" @click="toggleRequest(request, true)" class="underline">Retry</button></div>
            </div>
          </article>
        </div>
        <p class="text-[10px] text-slate-600">Sampled telemetry can omit requests. SQL text, bindings, request bodies, and headers are not shown.</p>
      </section>

      <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2"><h4 class="text-xs font-semibold uppercase tracking-wide text-slate-300">Containers</h4><span class="text-[10px] text-slate-500">Deployment reports refresh every 60 seconds at the host agent</span></div>
        <div v-if="!detail.deployments.length" class="text-xs text-slate-500">No running containers have reported for this project.</div>
        <div v-for="container in detail.deployments" :key="container.container_id" class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-800 p-3 text-[10px]">
          <CheckCircle2 class="h-3.5 w-3.5 text-emerald-400" /><span class="font-semibold text-slate-200">{{ container.service }}</span><span class="text-slate-500">{{ container.environment }}</span><span class="font-mono text-slate-500">{{ container.container_id.slice(0, 12) }}</span><span class="ml-auto max-w-full font-mono text-slate-500">{{ container.revision_id ? `Revision ${container.revision_id.slice(4, 12)}` : 'Unindexed code' }}</span><XCircle v-if="container.commit_sha !== detail.commit_sha" class="h-3.5 w-3.5 text-amber-400" />
        </div>
      </section>

      <div v-if="detail.snapshot.state !== 'awaiting_source'" class="grid gap-4 xl:grid-cols-2">
        <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
          <div class="flex items-center justify-between gap-2"><h4 class="text-xs font-semibold uppercase tracking-wide text-slate-300">Dead-code candidates</h4><label class="flex items-center gap-2 text-[10px] text-slate-400"><input v-model="deadOnly" type="checkbox" class="accent-amber-500" />Show only unobserved</label></div>
          <p class="text-[10px] text-slate-500">Absence here means no matching spans in the current aggregation window; it does not prove code is unused.</p>
          <div class="max-h-80 space-y-1 overflow-auto">
            <button v-for="symbol in symbols.slice(0, 300)" :key="symbol.symbol_id" type="button" @click="openDefinition(symbol.symbol_id)" class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-slate-800/50">
              <span class="min-w-0 flex-1 truncate font-mono text-[10px] text-slate-300">{{ symbol.fqn || symbol.name }}</span>
              <span v-if="symbol.metric" class="text-[10px] text-cyan-300">{{ formatCount(symbol.metric.runs) }} calls</span><span v-else class="text-[10px] text-amber-300">No observed calls</span><ArrowUpRight class="h-3 w-3 shrink-0 text-slate-600" />
            </button>
            <p v-if="!symbols.length" class="py-4 text-center text-xs text-slate-500">{{ deadOnly ? 'No symbols without observations.' : 'No indexed methods or functions in this snapshot.' }}</p>
          </div>
        </section>

        <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
          <h4 class="text-xs font-semibold uppercase tracking-wide text-slate-300">Routes and database tables</h4>
          <div v-if="!detail.telemetry.routes.length && !detail.telemetry.tables.length" class="text-xs text-slate-500">No route or database-operation spans were observed.</div>
          <div v-if="detail.telemetry.routes.length" class="space-y-1"><p class="text-[10px] uppercase text-slate-500">Routes</p><div v-for="route in detail.telemetry.routes.slice(0, 30)" :key="`${route.method}|${route.route}`" class="flex gap-2 rounded-lg bg-slate-950/40 px-2 py-2 text-[10px]"><span class="text-indigo-300">{{ route.method }}</span><span class="min-w-0 flex-1 truncate font-mono text-slate-300">{{ route.route }}</span><span class="text-slate-500">{{ formatCount(route.requests) }}</span></div></div>
          <div v-if="detail.telemetry.tables.length" class="space-y-1"><p class="text-[10px] uppercase text-slate-500">Tables</p><div v-for="table in detail.telemetry.tables.slice(0, 30)" :key="`${table.table_name}|${table.operation}`" class="flex gap-2 rounded-lg bg-slate-950/40 px-2 py-2 text-[10px]"><span class="min-w-0 flex-1 truncate font-mono text-slate-300">{{ table.table_name }}</span><span class="text-cyan-300">{{ table.operation || 'operation' }}</span><span class="text-slate-500">{{ formatCount(table.operations) }}</span></div></div>
        </section>
      </div>
    </template>
  </section>
  <ConfirmModal
    :show="!!deleteTarget"
    title="Delete stored request telemetry?"
    :message="deleteTargetMessage"
    confirm-label="Delete traces"
    :busy="deletingRequests"
    :error="requestDeleteError"
    @confirm="deleteStoredRequestRows"
    @cancel="cancelDeleteRequestRows"
  />
</template>
