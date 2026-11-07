<script setup lang="ts">
import { Activity, AlertTriangle, BookOpen, CheckCircle2, RefreshCw, Server, Trash2, XCircle } from 'lucide-vue-next';
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import ConfirmModal from './ConfirmModal.vue';
import RemoteHostMonitor from './RemoteHostMonitor.vue';
import ToggleCard from './ToggleCard.vue';
import { makeRequest } from '../ui/makeRequest';
import { subscribeRuntimeUpdates } from '../ui/runtimeEvents';
import { useUrlTab } from '../composables/useUrlTab';
import type { RuntimeAnalysisFinding, RuntimeDatabaseCall, RuntimeDatabaseGroup, RuntimeDatabaseSummary, RuntimeDiffPreview, RuntimeProjectRecord, RuntimeRequest, RuntimeRequestAnalysis, RuntimeRequestDetail, RuntimeSnapshotData } from '../types';

const props = defineProps<{ useRemote: boolean }>();

const projects = ref<RuntimeProjectRecord[]>([]);
const selectedKey = ref('');
const loadedDetailKey = ref('');
const detail = ref<RuntimeSnapshotData | null>(null);
type RequestOutcome = 'all' | 'errors' | 'slow';
const requestOutcomes: { id: RequestOutcome; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'errors', label: 'Errors' },
  { id: 'slow', label: 'Slow' },
];
const requestOutcome = ref<RequestOutcome>('all');
const requestList = ref<{ available: boolean; reason?: string | null; sampled?: boolean; sampling_ratio?: number | null; window_hours?: number; outcome?: RequestOutcome; slow_threshold_ms?: number | null; requests: RuntimeRequest[] } | null>(null);
const requestListLoading = ref(false);
const requestListError = ref('');
const requestPage = ref(1);
const REQUESTS_PER_PAGE = 10;
const hiddenTraceIds = ref(new Set<string>());
const deleteTarget = ref<{ key: string; projectId: string; revisionId: string; traceIds: string[] } | null>(null);
const deletingRequests = ref(false);
const requestDeleteError = ref('');
const requestDeleteNotice = ref('');
const expandedTraceId = ref('');
const requestDetailTabs = [
  { id: 'overview', label: 'Overview' },
  { id: 'database', label: 'Database' },
  { id: 'methods', label: 'Method activity' },
  { id: 'analysis', label: 'AI analysis' },
] as const;
type RequestDetailTab = typeof requestDetailTabs[number]['id'];
const activeRequestDetailTab = useUrlTab<RequestDetailTab>({ values: requestDetailTabs.map((tab) => tab.id), fallback: 'overview' });
const requestDetails = ref<Record<string, RuntimeRequestDetail>>({});
const requestDetailLoading = ref<Record<string, boolean>>({});
const requestDetailErrors = ref<Record<string, string>>({});
const requestAnalyses = ref<Record<string, RuntimeRequestAnalysis>>({});
const analysisLoading = ref<Record<string, boolean>>({});
const analysisErrors = ref<Record<string, string>>({});
const selectedFindings = ref<Record<string, string>>({});
const diffPreviews = ref<Record<string, RuntimeDiffPreview | undefined>>({});
const diffLoading = ref<Record<string, boolean>>({});
const diffErrors = ref<Record<string, string>>({});
const loadingProjects = ref(false);
const loadingDetail = ref(false);
const error = ref('');
const setupState = ref<'idle' | 'starting' | 'running' | 'complete' | 'cancelled' | 'error'>('idle');
const setupMessage = ref('');
const activeOperation = ref<'setup' | 'cleanup' | 'remove' | ''>('');
const savedSetup = ref<any>(null);
const showGuide = ref(false);
const autoRefresh = ref(false);
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
const requestAnalysisKey = (traceId: string) => `${selected.value?.project_id || ''}:${selected.value?.revision_id || ''}:${traceId}`;
const visibleRequests = computed(() => (requestList.value?.requests || []).filter((request) => !hiddenTraceIds.value.has(request.trace_id)));
const requestPageCount = computed(() => Math.max(1, Math.ceil(visibleRequests.value.length / REQUESTS_PER_PAGE)));
const pagedRequests = computed(() => visibleRequests.value.slice((requestPage.value - 1) * REQUESTS_PER_PAGE, requestPage.value * REQUESTS_PER_PAGE));
const firstVisibleRequest = computed(() => visibleRequests.value.length ? (requestPage.value - 1) * REQUESTS_PER_PAGE + 1 : 0);
const lastVisibleRequest = computed(() => Math.min(requestPage.value * REQUESTS_PER_PAGE, visibleRequests.value.length));
const deleteTargetMessage = computed(() => {
  const count = deleteTarget.value?.traceIds.length || 0;
  return `Delete telemetry for ${count} loaded request trace${count === 1 ? '' : 's'}? This removes all stored spans for these traces in the selected project and revision, including database-call spans. Runtime aggregate metrics for those spans will decrease. This cannot be undone.`;
});
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
    requestAnalyses.value = {};
    analysisLoading.value = {};
    analysisErrors.value = {};
    selectedFindings.value = {};
    diffPreviews.value = {};
    diffLoading.value = {};
    diffErrors.value = {};
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
    requestAnalyses.value = {};
    analysisLoading.value = {};
    analysisErrors.value = {};
    selectedFindings.value = {};
    diffPreviews.value = {};
    diffLoading.value = {};
    diffErrors.value = {};
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
    const outcome = requestOutcome.value;
    const path = `/api/runtime/projects/${encodeURIComponent(projectId)}/revisions/${encodeURIComponent(revisionId)}/requests${outcome === 'all' ? '' : `?outcome=${outcome}`}`;
    const response = await makeRequest(path);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load recent requests.');
    // A slower response for a filter the user has since changed must not overwrite the newer list.
    if (outcome !== requestOutcome.value) return;
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
  if (expandedTraceId.value !== request.trace_id) activeRequestDetailTab.value = 'overview';
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

const analyzeRequest = async (traceId: string, retry = false) => {
  const current = selected.value;
  if (!current?.revision_id) return;
  const key = requestAnalysisKey(traceId);
  if (analysisLoading.value[key]) return;
  if (requestAnalyses.value[key] && !retry) return;
  analysisLoading.value = { ...analysisLoading.value, [key]: true };
  analysisErrors.value = { ...analysisErrors.value, [key]: '' };
  try {
    const path = `/api/runtime/projects/${encodeURIComponent(current.project_id)}/revisions/${encodeURIComponent(current.revision_id)}/requests/${encodeURIComponent(traceId)}/analysis`;
    const response = await makeRequest(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: props.useRemote ? 'remote' : 'local' }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Request analysis failed. Retry the request.');
    if (selected.value?.key === current.key) {
      requestAnalyses.value = { ...requestAnalyses.value, [key]: data };
      selectedFindings.value = { ...selectedFindings.value, [key]: '' };
      diffPreviews.value = { ...diffPreviews.value, [key]: undefined };
      diffErrors.value = { ...diffErrors.value, [key]: '' };
    }
  } catch (cause: any) {
    if (selected.value?.key === current.key) analysisErrors.value = { ...analysisErrors.value, [key]: cause?.message || 'Request analysis failed. Retry the request.' };
  } finally {
    if (selected.value?.key === current.key) analysisLoading.value = { ...analysisLoading.value, [key]: false };
  }
};

const selectFinding = (traceId: string, finding: RuntimeAnalysisFinding) => {
  const key = requestAnalysisKey(traceId);
  selectedFindings.value = { ...selectedFindings.value, [key]: finding.id };
  diffPreviews.value = { ...diffPreviews.value, [key]: undefined };
  diffErrors.value = { ...diffErrors.value, [key]: '' };
};

const previewFindingDiff = async (traceId: string) => {
  const current = selected.value;
  if (!current?.revision_id) return;
  const key = requestAnalysisKey(traceId);
  const analysis = requestAnalyses.value[key];
  const finding = analysis?.findings.find((item) => item.id === selectedFindings.value[key]);
  if (!analysis || !finding || diffLoading.value[key]) return;
  diffLoading.value = { ...diffLoading.value, [key]: true };
  diffErrors.value = { ...diffErrors.value, [key]: '' };
  diffPreviews.value = { ...diffPreviews.value, [key]: undefined };
  try {
    const path = `/api/runtime/projects/${encodeURIComponent(current.project_id)}/revisions/${encodeURIComponent(current.revision_id)}/requests/${encodeURIComponent(traceId)}/analysis/diff`;
    const response = await makeRequest(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ analysis, finding }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Diff preview failed. Retry the request.');
    if (selected.value?.key === current.key) diffPreviews.value = { ...diffPreviews.value, [key]: data };
  } catch (cause: any) {
    if (selected.value?.key === current.key) diffErrors.value = { ...diffErrors.value, [key]: cause?.message || 'Diff preview failed. Retry the request.' };
  } finally {
    if (selected.value?.key === current.key) diffLoading.value = { ...diffLoading.value, [key]: false };
  }
};

const formatMilliseconds = (value: number | null) => value == null ? 'Unavailable' : `${Number(value).toFixed(2)} ms`;
const formatPercent = (ratio: number) => `${Number((ratio * 100).toFixed(2))}%`;
const formatPhpCpu = (value: number | null) => value == null ? 'Unavailable' : `${(value / 1000).toFixed(2)} ms`;
const formatPhpMemory = (value: number | null) => value == null ? 'Unavailable' : value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(2)} MiB` : `${(value / 1024).toFixed(1)} KiB`;
const displayTable = (tableId: string) => tableId.replace(/^table:/, '').split('.').slice(1).join('.') || tableId;
const displaySymbol = (symbolId: string | null) => symbolId || 'Unavailable';
const databaseShape = (item: { operation: string | null; table_ids: string[] }) =>
  `${item.operation || 'Query'} ${item.table_ids.length ? item.table_ids.map(displayTable).join(', ') : '(table unavailable)'}`;
const databaseFindings = (traceId: string) =>
  (requestAnalyses.value[requestAnalysisKey(traceId)]?.findings || []).filter((finding) => finding.category === 'database');
const groupHasFinding = (traceId: string, group: RuntimeDatabaseGroup) => Boolean(group.symbol_id)
  && databaseFindings(traceId).some((finding) => [...finding.evidence_refs, ...finding.source_refs].includes(group.symbol_id!));
const openFindingInAnalysis = (traceId: string, finding: RuntimeAnalysisFinding) => {
  selectFinding(traceId, finding);
  activeRequestDetailTab.value = 'analysis';
};
const databaseGroupKey = (group: RuntimeDatabaseGroup) => `${group.operation}|${group.table_ids.join(',')}|${group.symbol_id}`;
const databaseShareLabel = (summary: RuntimeDatabaseSummary) =>
  summary.request_share == null ? 'Share of request time unavailable' : `${(summary.request_share * 100).toFixed(0)}% of the request time`;
const goToRequestPage = (page: number) => {
  requestPage.value = Math.max(1, Math.min(page, requestPageCount.value));
};
const requestMethod = (call: RuntimeDatabaseCall) => displaySymbol(call.caller_symbol_id || call.symbol_id);
const handleRequestDetailTabKeydown = (event: KeyboardEvent, index: number) => {
  let nextIndex: number;
  if (event.key === 'ArrowRight') nextIndex = (index + 1) % requestDetailTabs.length;
  else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + requestDetailTabs.length) % requestDetailTabs.length;
  else if (event.key === 'Home') nextIndex = 0;
  else if (event.key === 'End') nextIndex = requestDetailTabs.length - 1;
  else return;

  event.preventDefault();
  activeRequestDetailTab.value = requestDetailTabs[nextIndex].id;
  const tabs = (event.currentTarget as HTMLElement).parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
  tabs?.[nextIndex]?.focus();
};

const refresh = async () => { await loadProjects(); await loadDetail(); };
const updateRuntimeSubscription = (enabled: boolean) => {
  if (!enabled) {
    stopRuntimeUpdates?.();
    stopRuntimeUpdates = undefined;
    return;
  }
  if (!stopRuntimeUpdates) stopRuntimeUpdates = subscribeRuntimeUpdates(() => { void refresh(); });
};
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
watch(selectedKey, () => {
  requestPage.value = 1;
  void loadDetail();
});
watch(requestPageCount, (pageCount) => {
  if (requestPage.value > pageCount) requestPage.value = pageCount;
});
watch(requestOutcome, () => {
  requestPage.value = 1;
  void loadRequestList();
});
watch(autoRefresh, updateRuntimeSubscription);
onMounted(async () => {
  await Promise.all([refresh(), loadSavedSetup()]);
  updateRuntimeSubscription(autoRefresh.value);
});
onUnmounted(() => stopRuntimeUpdates?.());
</script>

<template>
  <section class="flex-1 overflow-y-auto p-6 space-y-5">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h3 class="flex items-center gap-2 text-lg font-bold text-white"><Activity class="h-5 w-5 text-cyan-400" />Runtime</h3>
        <p class="mt-1 text-xs text-slate-500">Opt-in local Docker deployments, sampled requests, and request-level runtime details.</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <button type="button" @click="startRuntimeSetup" :disabled="!!savedSetup || setupState === 'starting' || setupState === 'running'" class="inline-flex h-10 items-center justify-center rounded-xl border border-emerald-500/30 px-3 text-xs text-emerald-200 hover:bg-emerald-500/10 disabled:opacity-50"><Activity class="mr-1 inline h-3.5 w-3.5" :class="(setupState === 'starting' || setupState === 'running') && activeOperation === 'setup' ? 'animate-pulse' : ''" />{{ (setupState === 'starting' || setupState === 'running') && activeOperation === 'setup' ? 'Setting up…' : 'Set up Runtime' }}</button>
        <button type="button" @click="cleanupRuntime" :disabled="setupState === 'starting' || setupState === 'running'" title="Checkbox acknowledgements are optional; Continue approves all applicable cleanup actions. Cancel preserves recovery state." class="inline-flex h-10 items-center justify-center rounded-xl border border-rose-500/30 px-3 text-xs text-rose-200 hover:bg-rose-500/10 disabled:opacity-50"><Activity class="mr-1 inline h-3.5 w-3.5" :class="(setupState === 'starting' || setupState === 'running') && activeOperation === 'cleanup' ? 'animate-pulse' : ''" />{{ (setupState === 'starting' || setupState === 'running') && activeOperation === 'cleanup' ? 'Cleaning up…' : 'Clean up Runtime' }}</button>
        <button type="button" @click="showGuide = !showGuide" class="inline-flex h-10 items-center justify-center rounded-xl border border-cyan-500/30 px-3 text-xs text-cyan-200 hover:bg-cyan-500/10"><BookOpen class="mr-1 inline h-3.5 w-3.5" />{{ showGuide ? 'Hide guide' : 'Get Started' }}</button>
        <select v-model="selectedKey" :disabled="setupState === 'starting' || setupState === 'running'" class="h-10 max-w-[24rem] rounded-xl border border-slate-800 bg-[#11162d] px-3 text-xs text-slate-200 disabled:opacity-50">
          <option value="">Select project / indexed revision</option>
          <option v-for="item in options" :key="item.key" :value="item.key">{{ item.project_id }} · {{ item.revision_id ? `Revision ${item.revision_id.slice(4, 12)}` : 'Docker only · no indexed revision' }}</option>
        </select>
        <button v-if="selected?.can_remove" type="button" @click="removeLocalContainers" :disabled="setupState === 'starting' || setupState === 'running'" title="Stops and removes only matching local containers. Runtime history and remote containers remain." class="inline-flex h-10 items-center justify-center rounded-xl border border-rose-500/30 px-3 text-xs text-rose-200 hover:bg-rose-500/10 disabled:opacity-50"><Trash2 class="mr-1 inline h-3.5 w-3.5" :class="(setupState === 'starting' || setupState === 'running') && activeOperation === 'remove' ? 'animate-pulse' : ''" />{{ (setupState === 'starting' || setupState === 'running') && activeOperation === 'remove' ? 'Removing containers…' : 'Remove local containers' }}</button>
        <button type="button" @click="refresh" :disabled="loadingProjects || loadingDetail || setupState === 'starting' || setupState === 'running'" class="inline-flex h-10 items-center justify-center rounded-xl border border-slate-700 px-3 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"><RefreshCw class="mr-1 inline h-3.5 w-3.5" :class="loadingProjects || loadingDetail ? 'animate-spin' : ''" />Refresh</button>
        <ToggleCard v-model="autoRefresh" label="Auto-refresh" description="Live Runtime updates" density="compact" />
      </div>
    </header>

    <RemoteHostMonitor />

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

    <Transition name="app-panel">
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
          <p class="border-t border-slate-800 pt-3 text-[10px] leading-4 text-slate-500">The host agent reconciles containers about every 60 seconds. This view updates when the backend reports new runtime data and after you select Refresh. Aggregates use every span that reaches the Collector. PHP SDK head sampling (10% in production by default) decides which requests are recorded; the request list says when it applies. Container visibility works without spans; code matching, routes, and table activity need spans.</p>
        </div>
      </div>
      </section>
    </Transition>

    <template v-if="options.length && detail && !error">
      <div v-if="detail.snapshot.state !== 'awaiting_source' && detail.telemetry.reason" class="rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-xs text-amber-200">{{ detail.telemetry.reason }} Start the local runtime telemetry profile and instrument the PHP application with the Phase 1 identity attributes. The synthetic Phase 0 collector does not accept application spans.</div>
      <div v-if="detail.snapshot.state !== 'awaiting_source' && detail.symbol_index_available === false" class="rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-xs text-amber-200">{{ detail.symbol_index_reason || 'The symbol index is unavailable.' }} Runtime spans may still be present, but source-linked request details and code analysis require the indexed snapshot.</div>

      <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
        <div class="rounded-xl border border-slate-800 bg-slate-950/30 p-3 space-y-2">
          <div class="flex flex-wrap items-center justify-between gap-2"><h5 class="text-[10px] font-semibold uppercase tracking-wide text-slate-300">Containers</h5><span class="text-[10px] text-slate-500">Deployment reports refresh every 60 seconds at the host agent</span></div>
          <p v-if="detail.deployment_mismatch" class="flex items-center gap-1 text-[10px] text-amber-300"><AlertTriangle class="h-3 w-3 shrink-0" />Running containers use another revision or unindexed code.</p>
          <div v-if="!detail.deployments.length" class="text-xs text-slate-500">No running containers have reported for this project.</div>
          <div v-for="container in detail.deployments" :key="container.container_id" class="flex flex-wrap items-center gap-2 rounded-lg border border-slate-800 p-2 text-[10px]">
            <CheckCircle2 class="h-3.5 w-3.5 text-emerald-400" /><span class="font-semibold text-slate-200">{{ container.service }}</span><span class="text-slate-500">{{ container.environment }}</span><span class="font-mono text-slate-500">{{ container.container_id.slice(0, 12) }}</span><span class="ml-auto max-w-full font-mono text-slate-500">{{ container.revision_id ? `Revision ${container.revision_id.slice(4, 12)}` : 'Unindexed code' }}</span><XCircle v-if="container.commit_sha !== detail.commit_sha" class="h-3.5 w-3.5 text-amber-400" />
          </div>
        </div>
      </section>

      <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
        <div v-if="detail.revision_id" class="flex flex-wrap items-center justify-between gap-2">
          <div><h4 class="text-xs font-semibold uppercase tracking-wide text-slate-300">Recent sampled requests</h4><p class="mt-1 text-[10px] text-slate-500">10 requests per page, up to 50 from the last {{ requestList?.window_hours || detail.telemetry.aggregation_window_hours }} hours. PHP CPU and memory are worker-level estimates.</p></div>
          <div class="flex flex-wrap items-center gap-2">
            <div role="radiogroup" aria-label="Filter requests by outcome" class="inline-flex overflow-hidden rounded-lg border border-slate-700">
              <button v-for="option in requestOutcomes" :key="option.id" type="button" role="radio" :aria-checked="requestOutcome === option.id" @click="requestOutcome = option.id" :disabled="requestListLoading" :class="requestOutcome === option.id ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:bg-slate-800'" class="px-2 py-1.5 text-[10px] disabled:opacity-50">{{ option.label }}</button>
            </div>
            <button type="button" @click="clearRequestRows" :disabled="!visibleRequests.length" class="rounded-lg border border-slate-700 px-2 py-1.5 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-50">Clear</button>
            <button v-if="selected?.can_delete_telemetry && requestList?.requests.length" type="button" @click="confirmDeleteRequestRows" class="rounded-lg border border-rose-500/40 px-2 py-1.5 text-[10px] text-rose-300 hover:bg-rose-500/10"><Trash2 class="mr-1 inline h-3 w-3" />Delete stored traces ({{ requestList.requests.length }})</button>
            <button type="button" @click="loadRequestList()" :disabled="requestListLoading" class="rounded-lg border border-slate-700 px-2 py-1.5 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-50"><RefreshCw class="mr-1 inline h-3 w-3" :class="requestListLoading ? 'animate-spin' : ''" />Refresh requests</button>
          </div>
        </div>
        <template v-if="detail.revision_id">
        <p v-if="requestDeleteNotice" role="status" class="text-xs text-emerald-300">{{ requestDeleteNotice }}</p>
        <p v-if="requestListLoading && !requestList" class="text-xs text-slate-500">Loading recent sampled requests…</p>
        <div v-if="requestListError" class="flex flex-wrap items-center gap-2 text-xs text-rose-300"><span>{{ requestListError }}</span><button type="button" @click="loadRequestList()" class="underline">Retry</button></div>
        <p v-if="requestList?.available !== false && requestList?.requests.length && requestList.sampling_ratio != null && requestList.sampling_ratio < 1" class="flex items-center gap-1 text-[10px] text-amber-300"><AlertTriangle class="h-3 w-3 shrink-0" />The app records {{ formatPercent(requestList.sampling_ratio) }} of requests (head sampling), so real traffic is about {{ Math.round(1 / requestList.sampling_ratio) }}× what is listed here.</p>
        <p v-else-if="requestList?.available !== false && requestList?.requests.length && requestList.sampling_ratio == null && requestList.requests.some((request) => request.sampling_ratio == null)" class="text-[10px] text-slate-500">The app does not report its sampling ratio, so these requests may be a sample of real traffic.</p>
        <p v-if="requestList?.available !== false && requestOutcome === 'slow' && requestList?.slow_threshold_ms != null" class="text-[10px] text-slate-500">Showing requests at or above their route's p95 server duration in this window. Routes with fewer than 20 requests use the revision p95 of {{ formatMilliseconds(requestList.slow_threshold_ms) }}.</p>
        <p v-else-if="requestList?.available !== false && requestOutcome === 'errors'" class="text-[10px] text-slate-500">Showing failed requests and 5xx responses.</p>
        <p v-if="requestList?.available === false" class="text-xs text-amber-300">{{ requestList.reason || 'Request data is unavailable.' }}</p>
        <p v-else-if="requestList && !requestList.requests.length" class="text-xs text-slate-500">{{ requestOutcome === 'errors' ? 'No failed requests were observed in this window.' : requestOutcome === 'slow' ? 'No slow requests were observed in this window.' : 'No sampled request spans were observed in this window.' }}</p>
        <div v-else-if="requestList && !visibleRequests.length" class="flex flex-wrap items-center gap-2 text-xs text-slate-500"><span>All loaded request rows are hidden in this view.</span><button type="button" @click="restoreHiddenRequestRows" class="underline">Show hidden requests ({{ requestList.requests.length }})</button></div>
        <div v-else-if="visibleRequests.length" class="space-y-2">
          <article v-for="request in pagedRequests" :key="request.trace_id" class="overflow-hidden rounded-xl border border-slate-800 bg-slate-950/30">
            <button type="button" :aria-expanded="expandedTraceId === request.trace_id" @click="toggleRequest(request)" class="flex w-full flex-wrap items-center gap-x-3 gap-y-1 p-3 text-left hover:bg-slate-800/30">
              <span class="rounded bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-300">{{ request.method || 'HTTP' }}</span>
              <span class="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-200">{{ request.route || 'Route unavailable' }}</span>
              <span class="text-[10px] text-slate-400">{{ request.status_code ?? 'Status unavailable' }}</span>
              <span class="text-[10px] text-cyan-300">{{ formatMilliseconds(request.duration_ms) }}</span>
              <span class="text-[10px] text-slate-500">{{ formatTime(request.timestamp) }}</span>
              <span class="w-full text-[10px] text-slate-500">PHP CPU estimate {{ formatPhpCpu(request.php_cpu_time_us) }} · PHP-managed peak above request start {{ formatPhpMemory(request.php_peak_memory_bytes) }}</span>
            </button>
            <div v-if="expandedTraceId === request.trace_id" class="space-y-3 border-t border-slate-800 p-4 text-xs">
              <p class="text-xs leading-5 text-slate-400">Estimates cover this PHP worker’s request interval. Memory is PHP-managed allocator peak above request start; neither value represents whole-container CPU or RSS.</p>
              <p v-if="requestDetailLoading[request.trace_id]" class="text-sm text-slate-400">Loading request details…</p>
              <div v-else-if="requestDetailErrors[request.trace_id]" class="flex flex-wrap items-center gap-2 text-sm text-rose-300"><span>{{ requestDetailErrors[request.trace_id] }}</span><button type="button" @click="toggleRequest(request, true)" class="underline">Retry</button></div>
              <template v-else-if="requestDetails[request.trace_id]?.available">
                <div role="tablist" aria-label="Request detail categories" class="flex flex-wrap gap-1 border-b border-slate-800">
                  <button
                    v-for="(tab, index) in requestDetailTabs"
                    :id="`request-detail-tab-${request.trace_id}-${tab.id}`"
                    :key="tab.id"
                    type="button"
                    role="tab"
                    :aria-selected="activeRequestDetailTab === tab.id"
                    :aria-controls="`request-detail-panel-${request.trace_id}`"
                    :tabindex="activeRequestDetailTab === tab.id ? 0 : -1"
                    @click="activeRequestDetailTab = tab.id"
                    @keydown="handleRequestDetailTabKeydown($event, index)"
                    class="rounded-t-lg border-b-2 px-3 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400"
                    :class="activeRequestDetailTab === tab.id ? 'border-cyan-300 text-cyan-200' : 'border-transparent text-slate-400 hover:text-slate-200'"
                  >{{ tab.label }}</button>
                </div>
                <div
                  :id="`request-detail-panel-${request.trace_id}`"
                  role="tabpanel"
                  :aria-labelledby="`request-detail-tab-${request.trace_id}-${activeRequestDetailTab}`"
                  tabindex="0"
                  class="rounded-lg border border-slate-800 bg-slate-950/30 p-3 outline-none focus-visible:ring-2 focus-visible:ring-cyan-400"
                >
                  <p v-if="requestDetails[request.trace_id].truncated" class="mb-3 flex items-center gap-1 text-xs text-amber-300"><AlertTriangle class="h-3.5 w-3.5 shrink-0" />This trace has more than {{ requestDetails[request.trace_id].span_limit }} spans; only the first {{ requestDetails[request.trace_id].span_limit }} are shown.</p>
                  <section v-if="activeRequestDetailTab === 'overview'" class="space-y-3">
                    <div v-if="requestDetails[request.trace_id].ai_summary" class="space-y-3">
                      <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
                        <div><p class="text-xs font-medium text-slate-500">Trace ID</p><p class="mt-1 break-all font-mono text-xs text-slate-200">{{ requestDetails[request.trace_id].ai_summary?.trace_id }}</p></div>
                        <div><p class="text-xs font-medium text-slate-500">Endpoint</p><p class="mt-1 break-words text-sm text-slate-200">{{ requestDetails[request.trace_id].ai_summary?.endpoint }}</p></div>
                        <div><p class="text-xs font-medium text-slate-500">Total latency</p><p class="mt-1 text-sm text-slate-200">{{ formatMilliseconds(requestDetails[request.trace_id].ai_summary?.total_latency_ms ?? null) }}</p></div>
                      </div>
                      <section class="rounded-lg border border-cyan-500/20 bg-cyan-500/5 p-3">
                        <h5 class="text-sm font-semibold text-cyan-200">Trace summary</h5>
                        <p class="mt-1 text-sm leading-6 text-slate-300">{{ requestDetails[request.trace_id].ai_summary?.actionable_insight }}</p>
                      </section>
                      <section>
                        <h5 class="text-xs font-semibold uppercase tracking-wide text-slate-400">Observed time breakdown</h5>
                        <div class="mt-2 flex flex-wrap gap-2">
                          <span v-for="(duration, category) in requestDetails[request.trace_id].ai_summary?.otel_breakdown" :key="category" class="rounded bg-slate-900/70 px-2 py-1 text-xs text-slate-300">
                            {{ category.replaceAll('_', ' ') }} · {{ formatMilliseconds(duration) }}
                          </span>
                        </div>
                      </section>
                      <section v-if="requestDetails[request.trace_id].ai_summary?.excimer_hotspots.length">
                        <h5 class="text-xs font-semibold uppercase tracking-wide text-slate-400">Excimer hotspots · {{ requestDetails[request.trace_id].ai_summary?.profile_event_type === 'EXCIMER_CPU' ? 'CPU samples' : 'wall-clock samples' }}</h5>
                        <p v-for="hotspot in requestDetails[request.trace_id].ai_summary?.excimer_hotspots" :key="hotspot.frame" class="mt-2 break-words font-mono text-xs text-orange-200">
                          {{ hotspot.sample_percentage.toFixed(1) }}% · {{ hotspot.frame }}
                        </p>
                      </section>
                      <p v-else class="text-sm text-slate-400">No retained Excimer profile for this trace.</p>
                      <p class="text-xs leading-5 text-slate-500">Instrumented MySQL/Redis spans are de-overlapped estimates; remaining time can include uninstrumented work. Excimer percentages describe retained samples, not exact request time.</p>
                    </div>
                    <p v-else class="text-sm text-slate-400">Trace summary is unavailable for this request.</p>
                  </section>

                  <section v-else-if="activeRequestDetailTab === 'database'" class="space-y-4">
                    <p v-if="!requestDetails[request.trace_id].database_calls.length" class="text-sm text-slate-400">No database calls were captured for this request.</p>
                    <template v-else>
                      <div v-if="requestDetails[request.trace_id].database_summary" class="grid grid-cols-1 gap-3 sm:grid-cols-3">
                        <div class="rounded-lg bg-slate-900/70 p-3">
                          <p class="text-xs font-medium text-slate-500">Queries</p>
                          <p class="mt-1 text-lg font-semibold text-slate-100">{{ requestDetails[request.trace_id].database_summary!.query_count }}</p>
                          <p class="text-xs text-slate-400">{{ requestDetails[request.trace_id].database_summary!.groups.length }} distinct {{ requestDetails[request.trace_id].database_summary!.groups.length === 1 ? 'shape' : 'shapes' }}</p>
                          <p v-if="requestDetails[request.trace_id].database_summary!.framework_query_count" class="text-xs text-slate-500">{{ requestDetails[request.trace_id].database_summary!.app_query_count }} app · {{ requestDetails[request.trace_id].database_summary!.framework_query_count }} framework</p>
                        </div>
                        <div class="rounded-lg bg-slate-900/70 p-3">
                          <p class="text-xs font-medium text-slate-500">Database time</p>
                          <p class="mt-1 text-lg font-semibold text-slate-100">{{ formatMilliseconds(requestDetails[request.trace_id].database_summary!.total_ms) }}</p>
                          <p class="text-xs text-slate-400">{{ databaseShareLabel(requestDetails[request.trace_id].database_summary!) }}</p>
                          <p v-if="requestDetails[request.trace_id].database_summary!.framework_query_count" class="text-xs text-slate-500">{{ formatMilliseconds(requestDetails[request.trace_id].database_summary!.app_total_ms ?? 0) }} app · {{ formatMilliseconds(requestDetails[request.trace_id].database_summary!.framework_total_ms ?? 0) }} framework</p>
                        </div>
                        <div class="rounded-lg bg-slate-900/70 p-3">
                          <p class="text-xs font-medium text-slate-500">Slowest query</p>
                          <template v-if="requestDetails[request.trace_id].database_summary!.slowest">
                            <p class="mt-1 text-lg font-semibold text-slate-100">{{ formatMilliseconds(requestDetails[request.trace_id].database_summary!.slowest!.duration_ms) }}</p>
                            <p class="break-words text-xs text-slate-400">{{ databaseShape(requestDetails[request.trace_id].database_summary!.slowest!) }}<span v-if="requestDetails[request.trace_id].database_summary!.slowest!.framework" class="ml-1 text-slate-500">· framework</span></p>
                          </template>
                          <p v-else class="mt-1 text-sm text-slate-400">Unavailable</p>
                        </div>
                      </div>

                      <section class="space-y-2 rounded-lg border border-violet-400/30 bg-violet-500/5 p-3" :aria-label="`AI explanation of database activity for ${request.method} ${request.route}`">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                          <h5 class="text-sm font-semibold text-violet-200">AI explanation</h5>
                          <button type="button" class="rounded-lg border border-violet-400/40 px-3 py-1.5 text-xs font-medium text-violet-100 hover:bg-violet-500/10 disabled:cursor-wait disabled:opacity-60" :disabled="analysisLoading[requestAnalysisKey(request.trace_id)]" @click="analyzeRequest(request.trace_id, Boolean(requestAnalyses[requestAnalysisKey(request.trace_id)]))">
                            {{ analysisLoading[requestAnalysisKey(request.trace_id)] ? 'Analyzing…' : requestAnalyses[requestAnalysisKey(request.trace_id)] ? 'Analyze again' : 'Explain with AI' }}
                          </button>
                        </div>
                        <p v-if="!requestAnalyses[requestAnalysisKey(request.trace_id)]" class="text-xs leading-5 text-slate-400">Sends query counts, operations, tables, timings, method IDs and matching source excerpts to the {{ props.useRemote ? 'remote' : 'local' }} chat provider. SQL text, bindings and request data are not sent. The same analysis appears in the AI analysis tab.</p>
                        <p v-if="analysisErrors[requestAnalysisKey(request.trace_id)]" role="alert" class="text-sm text-rose-300">{{ analysisErrors[requestAnalysisKey(request.trace_id)] }}</p>
                        <template v-if="requestAnalyses[requestAnalysisKey(request.trace_id)]">
                          <p v-if="!databaseFindings(request.trace_id).length" class="text-sm text-slate-400">The analysis found nothing to report about database activity. Other findings are in the AI analysis tab.</p>
                          <article v-for="finding in databaseFindings(request.trace_id)" :key="finding.id" class="rounded bg-slate-900/70 px-3 py-2">
                            <p class="text-sm font-medium text-slate-100">{{ finding.title }} <span class="ml-1 text-xs font-normal uppercase text-slate-400">{{ finding.severity }} · AI inference</span></p>
                            <p class="mt-1 text-sm leading-6 text-slate-300">{{ finding.summary }}</p>
                            <ul v-if="finding.recommendations.length" class="ml-4 mt-1 list-disc space-y-1 text-sm text-slate-300"><li v-for="recommendation in finding.recommendations" :key="recommendation">{{ recommendation }}</li></ul>
                            <p v-if="finding.evidence_refs.length" class="mt-1 break-words text-xs text-cyan-200">Evidence: {{ finding.evidence_refs.map(displaySymbol).join(', ') }}</p>
                            <button v-if="finding.source_refs.length" type="button" class="mt-2 text-xs text-cyan-200 underline hover:text-cyan-100" @click="openFindingInAnalysis(request.trace_id, finding)">Preview a code change</button>
                          </article>
                        </template>
                      </section>

                      <section v-if="requestDetails[request.trace_id].database_summary?.repeated_groups" class="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
                        <h5 class="text-sm font-semibold text-amber-200">Repeated queries</h5>
                        <p class="text-xs leading-5 text-slate-400">The same operation on the same tables from the same method ran {{ requestDetails[request.trace_id].database_summary!.repeated_min }} or more times. This is often an N+1 query or a cache read per call. SQL text is not stored, so the queries may still differ in columns or conditions.</p>
                        <div v-for="group in requestDetails[request.trace_id].database_summary!.groups.filter((item) => item.repeated)" :key="databaseGroupKey(group)" class="rounded bg-slate-900/70 px-3 py-2 text-sm">
                          <p class="text-amber-100"><span class="font-semibold">{{ group.count }}×</span> {{ databaseShape(group) }} · {{ formatMilliseconds(group.total_ms) }} total</p>
                          <p class="mt-1 break-words font-mono text-xs text-slate-300">{{ group.symbol_id || 'Not attributed to application code (framework or package)' }}</p>
                          <p v-if="group.caller_symbol_ids.length" class="mt-1 break-words text-xs text-slate-500">called from {{ group.caller_symbol_ids.join(', ') }}</p>
                        </div>
                      </section>

                      <section v-if="requestDetails[request.trace_id].database_summary?.groups.length" class="space-y-2">
                        <h5 class="text-sm font-semibold text-slate-200">Queries by shape</h5>
                        <div class="overflow-x-auto rounded-lg bg-slate-900/70">
                          <table class="w-full text-left text-sm">
                            <thead class="text-xs text-slate-500">
                              <tr><th class="px-3 py-2 font-medium">Count</th><th class="px-3 py-2 font-medium">Operation · tables · method</th><th class="px-3 py-2 text-right font-medium">Time</th></tr>
                            </thead>
                            <tbody>
                              <tr v-for="group in requestDetails[request.trace_id].database_summary!.groups" :key="databaseGroupKey(group)" class="border-t border-slate-800 align-top">
                                <td class="whitespace-nowrap px-3 py-2" :class="group.repeated ? 'font-semibold text-amber-200' : 'text-slate-200'">{{ group.count }}×</td>
                                <td class="min-w-0 px-3 py-2">
                                  <p class="break-words text-indigo-200">{{ databaseShape(group) }}<span v-if="group.framework" class="ml-2 rounded bg-slate-700/60 px-1.5 py-0.5 text-xs text-slate-300" title="The framework's own work on its tables (sessions, cache, queues, debugging tools); no application method issued it.">Framework</span><span v-if="groupHasFinding(request.trace_id, group)" class="ml-2 rounded bg-violet-500/15 px-1.5 py-0.5 text-xs text-violet-200">AI finding</span></p>
                                  <p class="mt-0.5 break-all font-mono text-xs" :class="group.symbol_id ? 'text-slate-300' : 'text-slate-500'">{{ group.symbol_id || (group.framework ? 'framework tables, not application code' : 'framework or package') }}</p>
                                </td>
                                <td class="whitespace-nowrap px-3 py-2 text-right">
                                  <p class="text-slate-200">{{ formatMilliseconds(group.total_ms) }}</p>
                                  <p v-if="group.count > 1" class="text-xs text-slate-500">max {{ formatMilliseconds(group.max_ms) }}</p>
                                </td>
                              </tr>
                            </tbody>
                          </table>
                        </div>
                      </section>

                      <details class="group space-y-3" :open="!requestDetails[request.trace_id].database_summary">
                        <summary class="cursor-pointer text-sm font-semibold text-slate-200">All {{ requestDetails[request.trace_id].database_calls.length }} database calls in order</summary>
                        <div class="mt-3 space-y-3">
                          <article v-for="call in requestDetails[request.trace_id].database_calls" :key="call.span_id" class="grid grid-cols-1 gap-3 rounded-lg bg-slate-900/70 p-3 text-sm sm:grid-cols-3" :class="call.span_id === requestDetails[request.trace_id].database_summary?.slowest?.span_id ? 'ring-1 ring-amber-500/40' : ''">
                            <div><p class="text-xs font-medium text-slate-500">Database</p><p class="mt-1 break-words text-slate-200">{{ call.db_system || 'Database unavailable' }}<span v-if="call.db_namespace"> · {{ call.db_namespace }}</span></p></div>
                            <div><p class="text-xs font-medium text-slate-500">Operation</p><p class="mt-1 break-words text-indigo-200">{{ call.operation || 'Unavailable' }}</p></div>
                            <div><p class="text-xs font-medium text-slate-500">Duration</p><p class="mt-1 text-slate-200">{{ formatMilliseconds(call.duration_ms) }}</p></div>
                            <div class="sm:col-span-3"><p class="text-xs font-medium text-slate-500">Table · attributed method</p><p class="mt-1 break-words font-mono text-cyan-200">{{ call.table_ids.length ? call.table_ids.map(displayTable).join(', ') : 'Table unavailable' }}</p><p class="mt-1 break-words text-slate-400">{{ requestMethod(call) }}</p></div>
                          </article>
                        </div>
                      </details>
                    </template>
                  </section>

                  <section v-else-if="activeRequestDetailTab === 'methods'" class="space-y-3">
                    <div class="flex flex-wrap items-start justify-between gap-2">
                      <div><h5 class="text-sm font-semibold text-violet-200">Observed method activity</h5><p class="mt-1 text-sm leading-6 text-slate-400">Bounded hook counts show configured caller-to-callee counts. They do not show call order or complete execution.</p></div>
                      <span class="rounded bg-violet-500/10 px-2 py-1 text-xs text-violet-200">{{ requestDetails[request.trace_id].method_activity?.available ? 'Instrumentation observation' : 'Coverage gap' }}</span>
                    </div>
                    <div v-if="requestDetails[request.trace_id].method_activity?.edges.length" class="space-y-2">
                      <div v-for="edge in requestDetails[request.trace_id].method_activity?.edges" :key="`${edge.caller_symbol_id}:${edge.symbol_id}`" class="flex flex-wrap items-center gap-2 rounded bg-slate-900/70 px-3 py-2 text-sm">
                        <span class="min-w-0 break-all text-left font-mono text-slate-300">{{ displaySymbol(edge.caller_symbol_id) }}</span>
                        <span aria-hidden="true" class="text-slate-500">→</span>
                        <span class="min-w-0 break-all text-left font-mono text-cyan-200">{{ displaySymbol(edge.symbol_id) }}</span>
                        <span class="ml-auto text-violet-200">{{ formatCount(edge.calls) }} calls</span>
                      </div>
                    </div>
                    <p v-else class="text-sm text-amber-200">{{ requestDetails[request.trace_id].method_activity?.reason || 'No method-hook data was captured. This does not mean no code ran.' }}</p>
                    <p v-if="requestDetails[request.trace_id].dropped_hook_calls" class="flex items-center gap-1 text-xs text-amber-300"><AlertTriangle class="h-3.5 w-3.5 shrink-0" />{{ requestDetails[request.trace_id].dropped_hook_calls }} method call{{ requestDetails[request.trace_id].dropped_hook_calls === 1 ? ' was' : 's were' }} not recorded because the request exceeded the library's edge or call-depth limit.</p>
                  </section>

                  <section v-else class="space-y-3 rounded-lg border border-slate-700 bg-slate-900/50 p-3" :aria-label="`AI analysis for ${request.method} ${request.route}`">
                    <div><h5 class="text-sm font-semibold text-slate-200">Evidence-backed request analysis</h5><p class="mt-1 text-sm leading-6 text-slate-400">Before sending, review what the selected {{ props.useRemote ? 'remote' : 'local' }} chat provider receives: request method, route, status, timing estimates, database system/table/operation and durations, method IDs/counts, profile samples, and matching indexed source excerpts. Request/response bodies, headers, credentials, SQL text/bindings, cache keys, and exception messages are excluded. Results are temporary and disappear after reload.</p></div>
                    <button type="button" class="rounded-lg border border-violet-400/40 px-3 py-2 text-xs font-medium text-violet-100 hover:bg-violet-500/10 disabled:cursor-wait disabled:opacity-60" :disabled="analysisLoading[requestAnalysisKey(request.trace_id)]" @click="analyzeRequest(request.trace_id, Boolean(requestAnalyses[requestAnalysisKey(request.trace_id)]))">
                      {{ analysisLoading[requestAnalysisKey(request.trace_id)] ? 'Analyzing request…' : requestAnalyses[requestAnalysisKey(request.trace_id)] ? 'Analyze again' : 'Analyze request' }}
                    </button>
                    <p v-if="analysisErrors[requestAnalysisKey(request.trace_id)]" role="alert" class="text-sm text-rose-300">{{ analysisErrors[requestAnalysisKey(request.trace_id)] }} <button type="button" class="underline" @click="analyzeRequest(request.trace_id, true)">Retry</button></p>
                    <div v-if="requestAnalyses[requestAnalysisKey(request.trace_id)]" class="space-y-3" aria-live="polite">
                      <p class="text-sm text-slate-400">Model used: {{ requestAnalyses[requestAnalysisKey(request.trace_id)].provider }} · {{ requestAnalyses[requestAnalysisKey(request.trace_id)].model }}<span v-if="requestAnalyses[requestAnalysisKey(request.trace_id)].requested_provider !== requestAnalyses[requestAnalysisKey(request.trace_id)].provider"> (configured provider fell back to {{ requestAnalyses[requestAnalysisKey(request.trace_id)].provider }})</span></p>
                      <p v-if="!requestAnalyses[requestAnalysisKey(request.trace_id)].findings.length" class="text-sm text-slate-400">No findings were returned for the available evidence.</p>
                      <article v-for="finding in requestAnalyses[requestAnalysisKey(request.trace_id)].findings" :key="finding.id" class="rounded-lg border border-slate-700 p-3">
                        <label class="flex cursor-pointer items-start gap-2"><input type="radio" :name="`finding-${request.trace_id}`" :checked="selectedFindings[requestAnalysisKey(request.trace_id)] === finding.id" @change="selectFinding(request.trace_id, finding)" class="mt-1 accent-violet-400" /><span><span class="text-sm font-medium text-slate-200">{{ finding.title }}</span><span class="ml-2 text-xs uppercase text-slate-400">{{ finding.severity }} · AI inference</span><span class="mt-1 block text-sm leading-6 text-slate-300">{{ finding.summary }}</span></span></label>
                        <ul v-if="finding.recommendations.length" class="ml-6 mt-2 list-disc space-y-1 text-sm text-slate-300"><li v-for="recommendation in finding.recommendations" :key="recommendation">{{ recommendation }}</li></ul>
                        <p v-if="finding.evidence_refs.length" class="ml-6 mt-2 break-words text-xs text-cyan-200">Evidence: {{ finding.evidence_refs.map(displaySymbol).join(', ') }}</p>
                      </article>
                      <ul v-if="requestAnalyses[requestAnalysisKey(request.trace_id)].coverage_gaps.length" class="list-disc space-y-1 pl-4 text-sm text-amber-200"><li v-for="gap in requestAnalyses[requestAnalysisKey(request.trace_id)].coverage_gaps" :key="gap">Coverage gap: {{ gap }}</li></ul>
                      <div v-if="selectedFindings[requestAnalysisKey(request.trace_id)]" class="space-y-2 pt-1">
                        <button type="button" class="rounded-lg border border-cyan-500/40 px-3 py-2 text-xs text-cyan-200 hover:bg-cyan-500/10 disabled:opacity-60" :disabled="diffLoading[requestAnalysisKey(request.trace_id)] || !requestAnalyses[requestAnalysisKey(request.trace_id)].source_available" @click="previewFindingDiff(request.trace_id)">{{ diffLoading[requestAnalysisKey(request.trace_id)] ? 'Preparing preview…' : 'Preview code diff' }}</button>
                        <p v-if="!requestAnalyses[requestAnalysisKey(request.trace_id)].source_available" class="text-sm text-amber-200">No exact-revision source mapping is available; diff preview is disabled.</p>
                        <p v-if="diffErrors[requestAnalysisKey(request.trace_id)]" role="alert" class="text-sm text-rose-300">{{ diffErrors[requestAnalysisKey(request.trace_id)] }}</p>
                        <div v-if="diffPreviews[requestAnalysisKey(request.trace_id)]" class="rounded-lg border border-cyan-500/20 bg-slate-950 p-3">
                          <p class="break-words text-xs leading-5 text-cyan-200">Read-only preview · {{ diffPreviews[requestAnalysisKey(request.trace_id)].file_path }} · revision {{ diffPreviews[requestAnalysisKey(request.trace_id)].revision_id }} · {{ diffPreviews[requestAnalysisKey(request.trace_id)].provider }} / {{ diffPreviews[requestAnalysisKey(request.trace_id)].model }}</p>
                          <pre tabindex="0" class="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-slate-300">{{ diffPreviews[requestAnalysisKey(request.trace_id)].diff }}</pre>
                        </div>
                      </div>
                    </div>
                  </section>
                </div>
              </template>
              <div v-else class="flex flex-wrap items-center gap-2 text-sm text-amber-200"><span>{{ requestDetails[request.trace_id]?.reason || 'Request details are unavailable.' }}</span><button type="button" @click="toggleRequest(request, true)" class="underline">Retry</button></div>
            </div>
          </article>
        </div>
        <nav v-if="visibleRequests.length > REQUESTS_PER_PAGE" aria-label="Recent sampled request pages" class="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800 pt-3">
          <p class="text-[10px] text-slate-500">Showing {{ firstVisibleRequest }}–{{ lastVisibleRequest }} of {{ visibleRequests.length }}</p>
          <div class="flex items-center gap-2">
            <button type="button" @click="goToRequestPage(requestPage - 1)" :disabled="requestPage <= 1" class="rounded-lg border border-slate-700 px-2.5 py-1.5 text-[10px] text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50">Previous</button>
            <span class="text-[10px] text-slate-400">Page {{ requestPage }} of {{ requestPageCount }}</span>
            <button type="button" @click="goToRequestPage(requestPage + 1)" :disabled="requestPage >= requestPageCount" class="rounded-lg border border-slate-700 px-2.5 py-1.5 text-[10px] text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50">Next</button>
          </div>
        </nav>
        <p class="text-[10px] text-slate-600">Sampled telemetry can omit requests. SQL text, bindings, request bodies, and headers are not shown.</p>
        </template>
        <p v-else class="text-xs text-slate-500">Upload or select an indexed source revision to view sampled request details.</p>
      </section>
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
