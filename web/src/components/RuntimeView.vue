<script setup lang="ts">
import {
  Activity, AlertTriangle, BookOpen, Box, Check, CheckCircle2, ChevronDown, ChevronsUpDown, Container, Copy, Database, Eraser, EyeOff,
  FileCode, FileDiff, GitFork, LayoutDashboard, RadioTower, RefreshCw, Repeat, Scale, Send, Server, Sparkles, Trash2, WandSparkles, X,
} from 'lucide-vue-next';
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import ConfirmModal from './ConfirmModal.vue';
import RemoteHostMonitor from './RemoteHostMonitor.vue';
import RuntimeSetupWizard from './runtime/RuntimeSetupWizard.vue';
import ToggleCard from './ToggleCard.vue';
import { makeRequest } from '../ui/makeRequest';
import { runtimeOptionLabel, runtimeProjectOptions } from '../utils/runtimeProjects';
import { subscribeRuntimeUpdates } from '../ui/runtimeEvents';
import { useUrlTab } from '../composables/useUrlTab';
import { useProjectScope } from '../composables/useProjectScope';
import { toast, toastMessage } from '../composables/useToasts';
import RuleLinksPanel from './code-quality/RuleLinksPanel.vue';
import RuleImpactResults from './code-quality/RuleImpactResults.vue';
import { ruleKnowledgePath, ruleRequest, type ImpactResult } from './code-quality/ruleKnowledge';
import type { RuntimeAnalysisFinding, RuntimeDatabaseCall, RuntimeDatabaseGroup, RuntimeDatabaseSummary, RuntimeDiffPreview, RuntimeProjectRecord, RuntimeRequest, RuntimeRequestAnalysis, RuntimeRequestDetail, RuntimeSnapshotData } from '../types';

const props = defineProps<{ useRemote: boolean }>();
const runtimePageUrl = window.location.href;
// Database containers are reported with an all-zero commit, since they run no indexed source.
const isDatabaseContainer = (container: { commit_sha: string }) => /^0{40}$/.test(container.commit_sha);

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
const hideUnmatched404 = ref(false);
const requestList = ref<{ available: boolean; reason?: string | null; sampled?: boolean; sampling_ratio?: number | null; window_hours?: number; outcome?: RequestOutcome; slow_threshold_ms?: number | null; requests: RuntimeRequest[] } | null>(null);
const requestListLoading = ref(false);
const requestListError = ref('');
const requestPage = ref(1);
const REQUESTS_PER_PAGE = 10;
const hiddenTraceIds = ref(new Set<string>());
const deleteTarget = ref<{ key: string; projectId: string; revisionId: string; traceIds: string[] } | null>(null);
const deletingRequests = ref(false);
const requestDeleteError = ref('');
const requestDeleteNotice = toastMessage('success');
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
const activeOperation = ref<'setup' | 'cleanup' | 'remove' | 'install' | ''>('');
type SavedSetup = { phase: string; project_id: string; environment: string; commit?: string | null; compose_project?: string | null; services?: string[] };
const savedSetups = ref<SavedSetup[]>([]);
const setupKey = (item: Pick<SavedSetup, 'project_id' | 'environment'>) => `${item.project_id}@${item.environment}`;
const setupLabel = (item: Pick<SavedSetup, 'project_id' | 'environment'>) => `${item.project_id} · ${item.environment}`;
// The first setup, for the banner that describes the most relevant one.
const savedSetup = computed(() => savedSetups.value[0] ?? null);
const unfinishedSetup = computed(() => savedSetups.value.find((item) => (item.phase || 'incomplete') !== 'active') ?? null);
const loadingSetup = ref(true);
const showSetupWizard = ref(false);
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

const options = computed(() => runtimeProjectOptions(projects.value));
const selected = computed(() => options.value.find((item) => item.key === selectedKey.value) || null);
// The header switcher picks the project; this page lists that project's revisions and deployments.
const scope = useProjectScope();
const scopedOptions = computed(() => scope.projectId.value
  ? options.value.filter((item) => item.project_id === scope.projectId.value)
  : options.value);
const preferredKey = () => {
  const list = scopedOptions.value;
  const wanted = scope.revision.value === 'latest' ? scope.resolvedRevision.value?.revision_id : scope.revision.value;
  return (wanted && list.find((item) => item.revision_id === wanted)?.key)
    || list.find((item) => item.revision_id)?.key || list[0]?.key || '';
};
let syncingFromScope = false;
const applyScope = (force: boolean) => {
  if (!force && scopedOptions.value.some((item) => item.key === selectedKey.value)) return;
  const next = preferredKey();
  if (next === selectedKey.value) return;
  syncingFromScope = true;
  selectedKey.value = next;
  void nextTick(() => { syncingFromScope = false; });
};
let keepLocalPick = false;
watch(() => [scope.projectId.value, scope.revision.value, scope.resolvedRevision.value?.revision_id] as const, () => {
  if (keepLocalPick) { keepLocalPick = false; return; }
  applyScope(true);
});
watch(options, () => applyScope(false));
// Picking a revision here becomes the app-wide choice; the newest indexed one stays "latest".
watch(selectedKey, (key) => {
  if (syncingFromScope || !key) return;
  const picked = options.value.find((item) => item.key === key);
  if (!picked) return;
  const newest = scope.projects.value.find((item) => item.project_id === picked.project_id)?.revisions.find((rev) => rev.indexed)?.revision_id;
  const nextRevision = !picked.revision_id || picked.revision_id === newest ? 'latest' : picked.revision_id;
  // Keep this exact pick (e.g. a Docker-only deployment) rather than snapping to the newest revision.
  if (picked.project_id !== scope.projectId.value || nextRevision !== scope.revision.value) keepLocalPick = true;
  scope.select(picked.project_id, nextRevision);
});
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
const loadProjects = async (quiet = false) => {
  loadingProjects.value = true;
  try {
    const response = await makeRequest('/api/runtime/projects');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load runtime project inventory.');
    error.value = '';
    projects.value = data.projects || [];
    applyScope(false);
  } catch (cause: any) {
    error.value = cause?.message || 'Could not load runtime project inventory.';
    if (!quiet) toast.error(error.value);
  } finally {
    loadingProjects.value = false;
  }
};

const loadDetail = async () => {
  if (!selected.value || (!selected.value.revision_id && !selected.value.commit_sha)) {
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
        toast.error(error.value);
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
    const hideUnmatched = hideUnmatched404.value;
    const query = new URLSearchParams({ outcome, hide_unmatched_404: String(hideUnmatched) });
    const path = `/api/runtime/projects/${encodeURIComponent(projectId)}/revisions/${encodeURIComponent(revisionId)}/requests?${query}`;
    const response = await makeRequest(path);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load recent requests.');
    // A slower response for a filter the user has since changed must not overwrite the newer list.
    if (outcome !== requestOutcome.value || hideUnmatched !== hideUnmatched404.value) return;
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
    if (selected.value?.key === current.key) {
      diffErrors.value = { ...diffErrors.value, [key]: cause?.message || 'Diff preview failed. Retry the request.' };
      toast.error(diffErrors.value[key]);
    }
  } finally {
    if (selected.value?.key === current.key) diffLoading.value = { ...diffLoading.value, [key]: false };
  }
};

// Canonical ids of the code a request touched, for the business rules linked to it.
const requestSymbolIds = (detail: RuntimeRequestDetail) => {
  const ids = new Set<string>();
  if (detail.request?.route && detail.request.route !== '(unmatched)') ids.add(`route:${detail.request.method.toUpperCase()} ${detail.request.route}`);
  for (const edge of detail.method_activity?.edges || []) { ids.add(edge.caller_symbol_id); ids.add(edge.symbol_id); }
  for (const call of detail.database_calls || []) {
    for (const value of [call.caller_symbol_id, call.symbol_id, ...(call.table_ids || [])]) if (value) ids.add(value);
  }
  return [...ids].filter(Boolean).slice(0, 200);
};
const ruleImpacts = ref<Record<string, ImpactResult | undefined>>({});
const ruleImpactLoading = ref<Record<string, boolean>>({});
const ruleImpactErrors = ref<Record<string, string>>({});
const checkDiffAgainstRules = async (traceId: string) => {
  const current = selected.value;
  const key = requestAnalysisKey(traceId);
  const preview = diffPreviews.value[key];
  if (!current?.revision_id || !preview || ruleImpactLoading.value[key]) return;
  ruleImpactLoading.value = { ...ruleImpactLoading.value, [key]: true };
  ruleImpactErrors.value = { ...ruleImpactErrors.value, [key]: '' };
  try {
    const result = await ruleRequest<ImpactResult>(ruleKnowledgePath(current.project_id, '/impact'),
      { method: 'POST', json: { revision_id: current.revision_id, diff: preview.diff } });
    if (selected.value?.key === current.key) ruleImpacts.value = { ...ruleImpacts.value, [key]: result };
  } catch (cause: any) {
    if (selected.value?.key === current.key) {
      ruleImpactErrors.value = { ...ruleImpactErrors.value, [key]: cause?.message || 'The diff could not be checked against business rules.' };
      toast.error(ruleImpactErrors.value[key]);
    }
  } finally { ruleImpactLoading.value = { ...ruleImpactLoading.value, [key]: false }; }
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
// Setup and cleanup recreate python-api, so the inventory is briefly unavailable. Retry quietly until it answers.
const refreshWhenApiIsBack = async (timeoutMs = 60_000) => {
  const deadline = Date.now() + timeoutMs;
  while (true) {
    await loadProjects(true);
    if (!error.value) return loadDetail();
    if (Date.now() >= deadline) { toast.error(error.value); return; }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
};
// Setup restarts python-api, and the host agent's first report arrives some seconds later.
// Keep reloading the inventory until that project reports running containers, then select it.
const FIRST_REPORT_TIMEOUT_MS = 120_000;
const waitingForFirstReport = ref('');
const waitForFirstReport = async (projectId: string, commit?: string | null) => {
  waitingForFirstReport.value = projectId;
  try {
    const deadline = Date.now() + FIRST_REPORT_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await loadProjects();
      // Containers from before the setup may still be reporting: wait for ones labeled with the applied commit.
      const reporting = options.value.filter((item) => item.project_id === projectId && (item.running_project_containers || 0) > 0
        && (!commit || (item.running_commit_shas || []).includes(commit)));
      const reported = reporting.find((item) => item.commit_sha === commit) || reporting[0];
      if (!error.value && reported) {
        if (selectedKey.value === reported.key) await loadDetail();
        else selectedKey.value = reported.key; // the selectedKey watcher loads its containers
        return true;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    await refresh();
    return false;
  } finally {
    waitingForFirstReport.value = '';
  }
};
const knownProjectIds = computed(() => [...new Set(projects.value.map((item) => item.project_id).filter(Boolean))].sort());
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
      // Older helpers return one setup at the top level; newer ones return `setups`.
      savedSetups.value = Array.isArray(data.setups) ? data.setups : data.configured ? [data] : [];
      if (savedSetups.value.length && showExistingNotice && !setupBusy.value) {
        const unfinished = unfinishedSetup.value;
        setupState.value = unfinished ? 'error' : 'complete';
        setupMessage.value = unfinished
          ? `Setup needs attention: ${setupLabel(unfinished)} (${unfinished.phase}). Clean it up before starting another setup.`
          : `Existing setups: ${savedSetups.value.map(setupLabel).join(', ')}.`;
      }
    }
  } catch {
    // Runtime inventory remains available to users who cannot run host setup.
  } finally {
    loadingSetup.value = false;
  }
};
const startRuntimeSetup = async () => {
  // Several setups can be active together; only an unfinished one (it may have left files half changed) must be cleaned up first.
  if (unfinishedSetup.value) {
    setupState.value = 'error';
    setupMessage.value = `Runtime recovery for ${setupLabel(unfinishedSetup.value)} is in phase '${unfinishedSetup.value.phase || 'incomplete'}'. Run just runtime-status, then clean it up before starting another setup.`;
    return;
  }
  setupState.value = 'idle';
  setupMessage.value = '';
  showSetupWizard.value = true;
};
// Apply a preview made in the setup wizard; the helper asks for approval in a dialog on this computer.
const applySetupPlan = async (planId: string) => {
  showSetupWizard.value = false;
  activeOperation.value = 'setup';
  setupState.value = 'starting';
  setupMessage.value = 'Authorizing local Runtime setup…';
  const knownSetups = new Set(savedSetups.value.map(setupKey));
  try {
    let token = await getRuntimeHelperAuth();
    const helperUrl = 'http://127.0.0.1:47831';
    let authorizationAt = Date.now();
    const headers = () => ({ Authorization: `Bearer ${token}` });
    const startResponse = await fetch(`${helperUrl}/setup/apply`, {
      method: 'POST', cache: 'no-store',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_id: planId }),
    });
    const started = await startResponse.json().catch(() => ({}));
    if (startResponse.status === 404) throw new Error('The local Runtime helper is out of date. Run just up to restart it, then reload this page.');
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
      if (status.status === 'complete') {
        await loadSavedSetup(false);
        const created = savedSetups.value.find((item) => !knownSetups.has(setupKey(item))) ?? savedSetups.value[savedSetups.value.length - 1];
        const projectId = created?.project_id;
        if (projectId) {
          setupMessage.value = `Setup applied. Waiting for the host agent's first report for ${projectId}…`;
          const reported = await waitForFirstReport(projectId, created?.commit);
          setupMessage.value = reported
            ? `${status.message || 'Runtime setup is complete.'} Containers for ${projectId} are reporting.`
            : `${status.message || 'Runtime setup is complete.'} No containers have reported for ${projectId} yet; select Refresh in a minute, or check docker logs devassistant-telemetry-agent-agent-1.`;
        } else {
          setupMessage.value = status.message || 'Runtime setup is complete.';
          await refresh();
        }
        setupState.value = 'complete';
        break;
      }
      if (status.status === 'cancelled') { setupState.value = 'cancelled'; break; }
      if (status.status === 'error') { setupState.value = 'error'; break; }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  } catch (cause: any) {
    setupState.value = 'error';
    setupMessage.value = cause?.message || 'Runtime setup failed.';
  }
};
// Install the Indexr library in the app with the helper; it asks for approval in a dialog on this computer.
const installIndexrLibrary = async (request: { project_name: string; project_id: string }) => {
  showSetupWizard.value = false;
  activeOperation.value = 'install';
  setupState.value = 'starting';
  setupMessage.value = 'Authorizing the library install…';
  try {
    let token = await getRuntimeHelperAuth();
    const helperUrl = 'http://127.0.0.1:47831';
    let authorizationAt = Date.now();
    const headers = () => ({ Authorization: `Bearer ${token}` });
    const startResponse = await fetch(`${helperUrl}/instrumentation/install`, {
      method: 'POST', cache: 'no-store', headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    const started = await startResponse.json().catch(() => ({}));
    if (startResponse.status === 404) throw new Error('The local Runtime helper is out of date. Run just up to restart it, then reload this page.');
    if (!startResponse.ok) throw new Error(started.detail || 'Could not start the library install.');
    setupState.value = 'running';
    while (true) {
      if (Date.now() - authorizationAt > 120_000) { token = await getRuntimeHelperAuth(); authorizationAt = Date.now(); }
      const statusResponse = await fetch(`${helperUrl}/status`, { headers: headers(), cache: 'no-store' });
      const status = await statusResponse.json().catch(() => ({}));
      if (!statusResponse.ok) throw new Error(status.detail || 'Could not read the install progress.');
      setupMessage.value = status.message || 'Installing the Indexr library…';
      if (status.status === 'complete') { setupState.value = 'complete'; await refreshWhenApiIsBack(); break; }
      if (status.status === 'cancelled') { setupState.value = 'cancelled'; break; }
      if (status.status === 'error') { setupState.value = 'error'; break; }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  } catch (cause: any) {
    setupState.value = 'error';
    setupMessage.value = cause?.message || 'The library install failed.';
  }
};
const cleanupRuntime = async (target?: Pick<SavedSetup, 'project_id' | 'environment'>) => {
  // With one setup there is nothing to choose; with several, each has its own button.
  const chosen = target ?? (savedSetups.value.length === 1 ? savedSetups.value[0] : undefined);
  activeOperation.value = 'cleanup';
  setupState.value = 'starting';
  setupMessage.value = chosen ? `Authorizing local Runtime cleanup for ${setupLabel(chosen)}…` : 'Authorizing local Runtime cleanup…';
  try {
    let token = await getRuntimeHelperAuth();
    const helperUrl = 'http://127.0.0.1:47831';
    let authorizationAt = Date.now();
    const headers = () => ({ Authorization: `Bearer ${token}` });
    const startResponse = await fetch(`${helperUrl}/cleanup`, {
      method: 'POST', cache: 'no-store',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify(chosen ? { project_id: chosen.project_id, environment: chosen.environment } : {}),
    });
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
        await refreshWhenApiIsBack();
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
watch([requestOutcome, hideUnmatched404], () => {
  requestPage.value = 1;
  void loadRequestList();
});
watch(autoRefresh, updateRuntimeSubscription);
onMounted(async () => {
  await Promise.all([refresh(), loadSavedSetup()]);
  updateRuntimeSubscription(autoRefresh.value);
});
onUnmounted(() => stopRuntimeUpdates?.());

// ---- Presentation helpers ----------------------------------------------------
const requestDetailIcons: Record<RequestDetailTab, unknown> = { overview: LayoutDashboard, database: Database, methods: GitFork, analysis: Sparkles };
const methodTone = (method?: string | null) => {
  const value = (method || '').toUpperCase();
  if (value === 'GET' || value === 'HEAD') return 'get';
  if (value === 'POST') return 'post';
  if (value === 'PUT' || value === 'PATCH') return 'put';
  if (value === 'DELETE') return 'delete';
  return 'other';
};
const statusTone = (code?: number | null) => (code == null ? undefined : code >= 500 ? 'bad' : code >= 400 ? 'warn' : code >= 200 && code < 400 ? 'ok' : undefined);
const pageMaxDuration = computed(() => Math.max(1, ...pagedRequests.value.map((request) => Number(request.duration_ms) || 0)));
const durationScale = (value?: number | null) => Math.min(1, Math.max(0.02, (Number(value) || 0) / pageMaxDuration.value));
const isSlowRequest = (value?: number | null) => requestList.value?.slow_threshold_ms != null && Number(value) >= requestList.value.slow_threshold_ms;

// Time breakdown: MySQL/Redis buckets are observed span time; php_app_time_ms is the residual estimate.
const BREAKDOWN_COLORS = ['#22d3ee', '#818cf8', '#34d399', '#f472b6', '#38bdf8'];
const breakdownParts = (breakdown?: Record<string, number> | null) => {
  if (!breakdown) return [];
  const entries = Object.entries(breakdown).filter(([, value]) => Number(value) > 0);
  const total = entries.reduce((sum, [, value]) => sum + Number(value), 0) || 1;
  let colorIndex = 0;
  return entries.map(([key, value]) => {
    const residual = key === 'php_app_time_ms';
    return {
      key,
      label: residual ? 'PHP and other work (residual estimate)' : key.replace(/_ms$/, '').replaceAll('_', ' '),
      ms: Number(value),
      share: (Number(value) / total) * 100,
      residual,
      color: residual ? '#fbbf24' : BREAKDOWN_COLORS[colorIndex++ % BREAKDOWN_COLORS.length],
    };
  });
};

const copiedSnippet = ref('');
const copySnippet = async (key: string, text: string) => {
  try {
    await navigator.clipboard.writeText(text);
    copiedSnippet.value = key;
    setTimeout(() => { if (copiedSnippet.value === key) copiedSnippet.value = ''; }, 1600);
  } catch { /* clipboard blocked */ }
};
const setupBusy = computed(() => setupState.value === 'starting' || setupState.value === 'running');
const setupBannerTone = computed(() => (setupState.value === 'error' ? 'bad' : setupState.value === 'complete' ? 'ok' : setupState.value === 'cancelled' ? 'warn' : 'run'));
</script>

<template>
  <section class="rt-page" aria-label="Runtime">
    <div class="rt-wrap">
      <!-- Toolbar: every control is 40 px tall -->
      <div class="rt-bar">
        <label class="rt-select">
          <Box aria-hidden="true" />
          <span class="sr-only">Project and indexed revision</span>
          <select v-model="selectedKey" :disabled="setupBusy">
            <option value="">{{ scope.projectId.value && !scopedOptions.length ? `No runtime data for ${scope.projectId.value}` : 'Select project / indexed revision' }}</option>
            <option v-for="item in scopedOptions" :key="item.key" :value="item.key">{{ runtimeOptionLabel(item) }}</option>
          </select>
          <ChevronsUpDown aria-hidden="true" />
        </label>
        <button type="button" class="rt-btn rt-btn-icon" title="Refresh" aria-label="Refresh" :disabled="loadingProjects || loadingDetail || setupBusy" @click="refresh">
          <RefreshCw :class="{ 'rt-spin': loadingProjects || loadingDetail }" aria-hidden="true" />
        </button>
        <ToggleCard v-model="autoRefresh" class="rt-toggle" label="Auto-refresh" description="Live Runtime updates" density="compact" />

        <div class="rt-bar-end">
          <div class="rt-group" role="group" aria-label="Runtime setup">
            <button type="button" class="rt-btn" :disabled="loadingSetup || showSetupWizard || setupBusy" @click="startRuntimeSetup">
              <RefreshCw v-if="loadingSetup || (setupBusy && activeOperation === 'setup')" class="rt-spin" aria-hidden="true" /><WandSparkles v-else aria-hidden="true" />{{ loadingSetup ? 'Checking setup…' : setupBusy && activeOperation === 'setup' ? 'Setting up…' : 'Set up Runtime' }}
            </button>
            <button type="button" class="rt-btn" :aria-expanded="showGuide" @click="showGuide = !showGuide">
              <BookOpen aria-hidden="true" />{{ showGuide ? 'Hide guide' : 'Get started' }}
            </button>
            <template v-if="savedSetups.length > 1">
              <button v-for="item in savedSetups" :key="setupKey(item)" type="button" class="rt-btn" :disabled="setupBusy" :title="`Restores the Compose files of ${setupLabel(item)} after you allow it in a dialog on this computer. The other setups keep running. Cancel keeps the recovery record.`" @click="cleanupRuntime(item)">
                <Eraser :class="{ 'rt-pulse': setupBusy && activeOperation === 'cleanup' }" aria-hidden="true" />{{ setupBusy && activeOperation === 'cleanup' ? 'Cleaning up…' : `Clean up ${setupLabel(item)}` }}
              </button>
            </template>
            <button v-else type="button" class="rt-btn" :disabled="setupBusy" title="Restores your Compose files and stops the local agent after you allow it in a dialog on this computer. Cancel keeps the recovery record." @click="cleanupRuntime()">
              <Eraser :class="{ 'rt-pulse': setupBusy && activeOperation === 'cleanup' }" aria-hidden="true" />{{ setupBusy && activeOperation === 'cleanup' ? 'Cleaning up…' : 'Clean up Runtime' }}
            </button>
          </div>
          <button v-if="selected?.can_remove" type="button" class="rt-btn" data-variant="danger" :disabled="setupBusy" title="Stops and removes only matching local containers. Runtime history and remote containers remain." @click="removeLocalContainers">
            <Trash2 :class="{ 'rt-pulse': setupBusy && activeOperation === 'remove' }" aria-hidden="true" />{{ setupBusy && activeOperation === 'remove' ? 'Removing containers…' : 'Remove local containers' }}
          </button>
        </div>
      </div>

      <div v-if="setupState !== 'idle'" class="rt-banner" :data-tone="setupBannerTone" :aria-busy="setupBusy" role="status">
        <CheckCircle2 v-if="setupState === 'complete'" aria-hidden="true" />
        <AlertTriangle v-else-if="setupState === 'error' || setupState === 'cancelled'" aria-hidden="true" />
        <RefreshCw v-else class="rt-spin" aria-hidden="true" />
        <div class="rt-banner-text">
          <b v-if="activeOperation === 'install'">{{ setupState === 'complete' ? 'Indexr library installed' : setupState === 'cancelled' ? 'Install cancelled' : setupState === 'error' ? 'Library install needs attention' : 'Installing the Indexr library' }}</b>
          <b v-else>{{ setupState === 'complete' ? (activeOperation === 'cleanup' ? 'Cleanup complete' : activeOperation === 'remove' ? 'Local container removal complete' : activeOperation === 'setup' ? 'Setup complete' : savedSetup ? 'Existing setup record' : 'Setup complete') : setupState === 'cancelled' ? (activeOperation === 'cleanup' ? 'Cleanup cancelled' : activeOperation === 'remove' ? 'Local container removal cancelled' : 'Setup cancelled') : setupState === 'error' ? (activeOperation === 'cleanup' ? 'Cleanup needs attention' : activeOperation === 'remove' ? 'Local container removal needs attention' : 'Setup failed') : activeOperation === 'cleanup' ? 'Runtime cleanup' : activeOperation === 'remove' ? 'Local container removal' : 'Runtime setup' }}</b>
          <span class="rt-block">{{ setupMessage }}</span>
          <span v-for="item in savedSetups" :key="setupKey(item)" class="rt-block">{{ setupLabel(item) }} — Compose project: {{ item.compose_project || 'unknown' }} · Services: {{ (item.services || []).join(', ') || 'none recorded' }}.</span>
          <span v-if="savedSetups.length" class="rt-block">Available containers are listed below.</span>
        </div>
      </div>

      <RemoteHostMonitor />

      <RuntimeSetupWizard v-if="showSetupWizard" :authorize="getRuntimeHelperAuth" :known-project-ids="knownProjectIds" :indexed-revisions="projects" :inventory-available="!loadingProjects && !error" @close="showSetupWizard = false" @apply="applySetupPlan" @install="installIndexrLibrary" />

      <div v-if="!error && !options.length && !loadingProjects && !waitingForFirstReport" class="rt-empty">
        <span class="rt-empty-icon"><Server aria-hidden="true" /></span>
        <h2>No Docker containers are reporting yet</h2>
        <p>Start the local host agent and opt in your Compose services. You don't need to upload source code to see containers.</p>
        <div class="rt-empty-actions">
          <button type="button" class="rt-btn" data-variant="primary" :disabled="loadingSetup || showSetupWizard || setupBusy" @click="startRuntimeSetup"><WandSparkles aria-hidden="true" />Set up Runtime</button>
          <button v-if="!showGuide" type="button" class="rt-btn" @click="showGuide = true"><BookOpen aria-hidden="true" />Open the guide</button>
        </div>
      </div>

      <!-- Guide -->
      <Transition name="app-panel">
        <section v-if="showGuide" class="rt-card" aria-labelledby="rt-guide-title">
          <div class="rt-sec-head"><h2 id="rt-guide-title" class="rt-sec-title"><BookOpen aria-hidden="true" />Get started with Runtime</h2></div>
          <p class="rt-hint">Start with your local Docker containers: opt-in labels plus the host agent report what's running. Snapshots and OpenTelemetry spans add code, route and database activity.</p>
          <ol class="rt-steps">
            <li class="rt-step">
              <span class="rt-step-n">1</span>
              <h3>Listen to your local Docker project</h3>
              <p>Use <b>Set up Runtime</b>: choose a Compose project and its services, review the exact label lines that will be added, then apply. A dialog on this computer asks you to allow the changes before any file is edited or service recreated. Only opted-in containers are reported.</p>
              <details open>
                <summary>Local setup commands and access</summary>
                <div class="rt-cmd"><button type="button" class="rt-copy" @click="copySnippet('setup', runtimeSetupCommands)">{{ copiedSnippet === 'setup' ? 'Copied' : 'Copy' }}</button><pre><code>{{ runtimeSetupCommands }}</code></pre></div>
                <p class="rt-hint">The browser never talks to Docker directly: the helper on this computer lists Compose projects, previews the change and shows only the label lines it adds. Approval happens in a desktop dialog (or the helper terminal), so a web page alone can't apply it. Recovery state is written first, in a private record in your local state directory.</p>
                <p class="rt-hint">After setup, choose the project and revision above. The agent reconciles about every 60 seconds; use Refresh to reload sooner. For remote hosts, use managed certificates and follow <code>api/telemetry-agent/README.md</code>.</p>
              </details>
            </li>
            <li class="rt-step">
              <span class="rt-step-n" data-optional>2</span>
              <h3>Add a source snapshot <em>(optional)</em></h3>
              <p>Connects observed spans to indexed functions and methods. Developer Assistant assigns an immutable revision ID when it registers the snapshot. Use the Gum PHP installer, or select a ready revision and configure the PHP app.</p>
              <details>
                <summary>External CI upload setup</summary>
                <p class="rt-hint">For local indexing, the Gum flow creates and stores the project token after confirmation. This example is only for external CI: provision a separate high-entropy bearer token (32+ characters) on the API backend and keep it in a secret manager; the value below is a placeholder.</p>
                <div class="rt-cmd"><button type="button" class="rt-copy" @click="copySnippet('ci', ciTokenSnippet)">{{ copiedSnippet === 'ci' ? 'Copied' : 'Copy' }}</button><pre><code>{{ ciTokenSnippet }}</code></pre></div>
                <p class="rt-hint">Set <code>INDEXER_API</code>, <code>INDEXER_PROJECT=billing</code> and <code>INDEXER_CI_TOKEN</code> as protected CI variables. The archive must be repository-rooted and match the deployed commit:</p>
                <div class="rt-cmd"><button type="button" class="rt-copy" @click="copySnippet('upload', snapshotUploadSnippet)">{{ copiedSnippet === 'upload' ? 'Copied' : 'Copy' }}</button><pre><code>{{ snapshotUploadSnippet }}</code></pre></div>
                <p class="rt-hint">Upload returns while indexing runs. The snapshot becomes ready after its matching deployment report and indexing finish.</p>
              </details>
            </li>
            <li class="rt-step">
              <span class="rt-step-n" data-optional>3</span>
              <h3>Enable code, route and database activity <em>(optional)</em></h3>
              <p>Start the local Collector and export OTLP/HTTP to <code>http://localhost:24318/v1/traces</code> from the host, or <code>http://host.docker.internal:24318/v1/traces</code> from a container.</p>
              <details>
                <summary>Collector command and span attributes</summary>
                <div class="rt-cmd"><button type="button" class="rt-copy" @click="copySnippet('collector', runtimeCollectorSnippet)">{{ copiedSnippet === 'collector' ? 'Copied' : 'Copy' }}</button><pre><code>{{ runtimeCollectorSnippet }}</code></pre></div>
                <p class="rt-hint">Each source-attributed span must include the project and immutable revision identity plus the canonical PHP symbol ID from the indexer:</p>
                <div class="rt-cmd"><button type="button" class="rt-copy" @click="copySnippet('attrs', spanAttributesSnippet)">{{ copiedSnippet === 'attrs' ? 'Copied' : 'Copy' }}</button><pre><code>{{ spanAttributesSnippet }}</code></pre></div>
                <p class="rt-hint">Add <code>http.route</code> and <code>http.request.method</code> to request spans, and <code>db.operation.name</code> plus <code>indexer.db.table</code> to database spans. Don't send raw SQL or request data. See <code>api/docs/telemetry/attributes.md</code>.</p>
              </details>
            </li>
          </ol>
          <p class="rt-hint">The host agent reconciles containers about every 60 seconds. PHP SDK head sampling (10% in production by default) decides which requests are recorded; the request list says when it applies. Container visibility works without spans; code matching, routes and table activity need spans.</p>
        </section>
      </Transition>

      <!-- Loading -->
      <section v-if="!error && !detail && (loadingProjects || loadingDetail || waitingForFirstReport)" class="rt-card" aria-busy="true" aria-live="polite">
        <div class="rt-sec-head"><h2 class="rt-sec-title"><RefreshCw class="rt-spin" aria-hidden="true" />Containers</h2><span class="rt-hint rt-m0">{{ waitingForFirstReport ? `Waiting for the host agent's first report for ${waitingForFirstReport}…` : 'Loading containers…' }}</span></div>
        <div class="rt-containers"><div v-for="row in 2" :key="row" class="rt-skeleton"></div></div>
      </section>

      <template v-if="options.length && detail && !error">
        <div v-if="detail.snapshot.state !== 'awaiting_source' && detail.telemetry.reason" class="rt-banner" data-tone="warn"><AlertTriangle aria-hidden="true" /><span class="rt-banner-text">{{ detail.telemetry.reason }} Start the local runtime telemetry profile and instrument the PHP application with the Phase 1 identity attributes. The synthetic Phase 0 collector doesn't accept application spans.</span></div>
        <div v-if="detail.snapshot.state !== 'awaiting_source' && detail.symbol_index_available === false" class="rt-banner" data-tone="warn"><AlertTriangle aria-hidden="true" /><span class="rt-banner-text">{{ detail.symbol_index_reason || 'The symbol index is unavailable.' }} Runtime spans may still be present, but source-linked request details and code analysis need the indexed snapshot.</span></div>

        <!-- Containers (directly before the request list) -->
        <section class="rt-card" aria-labelledby="rt-containers-title">
          <div class="rt-sec-head">
            <h2 id="rt-containers-title" class="rt-sec-title"><Container aria-hidden="true" />Containers</h2>
            <span v-if="detail.deployments.length" class="rt-tag">{{ detail.deployments.length }} reporting</span>
            <span class="rt-hint rt-m0 rt-push">Deployment reports refresh every 60 seconds at the host agent</span>
          </div>
          <p v-if="detail.deployment_mismatch" class="rt-note" data-tone="warn"><AlertTriangle aria-hidden="true" />Running containers use another revision or unindexed code.</p>
          <div v-if="!detail.deployments.length && waitingForFirstReport" class="rt-note" aria-live="polite"><RefreshCw class="rt-spin" aria-hidden="true" />Waiting for the host agent's first report for {{ waitingForFirstReport }}…</div>
          <p v-else-if="!detail.deployments.length" class="rt-hint">No running containers have reported for this project.</p>
          <div v-else class="rt-containers">
            <div v-for="container in detail.deployments" :key="container.container_id" class="rt-container" :data-mismatch="!isDatabaseContainer(container) && container.commit_sha !== detail.commit_sha">
              <span class="rt-container-icon"><Server aria-hidden="true" /></span>
              <div class="min-w-0">
                <b>{{ container.service }}</b>
                <p>{{ container.environment }} · {{ container.container_id.slice(0, 12) }} · {{ isDatabaseContainer(container) ? 'database' : container.revision_id ? `rev ${container.revision_id.slice(4, 12)}` : 'unindexed code' }}</p>
              </div>
              <span v-if="isDatabaseContainer(container)" class="rt-tag"><Database aria-hidden="true" />database</span>
              <span v-else-if="container.commit_sha !== detail.commit_sha" class="rt-tag" data-tone="warn"><AlertTriangle aria-hidden="true" />other revision</span>
              <span v-else class="rt-tag" data-tone="ok"><Check aria-hidden="true" />match</span>
            </div>
          </div>
        </section>

        <!-- Recent sampled requests -->
        <section class="rt-card" aria-labelledby="rt-requests-title">
          <template v-if="detail.revision_id">
            <div class="rt-sec-head">
              <h2 id="rt-requests-title" class="rt-sec-title"><RadioTower aria-hidden="true" />Recent sampled requests</h2>
              <div role="radiogroup" aria-label="Filter requests by outcome" class="rt-seg">
                <button v-for="option in requestOutcomes" :key="option.id" type="button" role="radio" :aria-checked="requestOutcome === option.id" :disabled="requestListLoading" @click="requestOutcome = option.id">{{ option.label }}</button>
              </div>
              <div class="rt-actions">
                <button type="button" class="rt-btn rt-btn-sm" :aria-pressed="hideUnmatched404" :data-variant="hideUnmatched404 ? 'primary' : undefined" :disabled="requestListLoading" title="Hides unmatched 404s unless the span is marked Error. Traces remain stored." @click="hideUnmatched404 = !hideUnmatched404"><EyeOff aria-hidden="true" />Hide unmatched 404s</button>
              </div>
              <div class="rt-push rt-actions">
                <button type="button" class="rt-btn rt-btn-sm" :disabled="requestListLoading" @click="loadRequestList()"><RefreshCw :class="{ 'rt-spin': requestListLoading }" aria-hidden="true" />Refresh requests</button>
                <button type="button" class="rt-btn rt-btn-sm" :disabled="!visibleRequests.length" @click="clearRequestRows"><EyeOff aria-hidden="true" />Clear</button>
                <button v-if="selected?.can_delete_telemetry && requestList?.requests.length" type="button" class="rt-btn rt-btn-sm" data-variant="danger" @click="confirmDeleteRequestRows"><Trash2 aria-hidden="true" />Delete stored traces ({{ requestList.requests.length }})</button>
              </div>
            </div>
            <p class="rt-hint">10 requests per page, up to 50 from the last {{ requestList?.window_hours || detail.telemetry.aggregation_window_hours }} hours. PHP CPU and memory are worker-level estimates.</p>

            <p v-if="hideUnmatched404" class="rt-hint">Unmatched 404s are hidden unless marked Error. Stored traces and other response statuses are unchanged. Turn off the filter to review missing pages.</p>
            <p v-if="requestListLoading && !requestList" class="rt-hint">Loading recent sampled requests…</p>
            <div v-if="requestListError" class="rt-note" data-tone="bad" role="alert"><AlertTriangle aria-hidden="true" /><span>{{ requestListError }}</span><button type="button" class="rt-link" @click="loadRequestList()">Retry</button></div>
            <p v-if="requestList?.available !== false && requestList?.requests.length && requestList.sampling_ratio != null && requestList.sampling_ratio < 1" class="rt-banner rt-banner-sm" data-tone="warn"><AlertTriangle aria-hidden="true" /><span class="rt-banner-text">The app records {{ formatPercent(requestList.sampling_ratio) }} of requests (head sampling), so real traffic is about {{ Math.round(1 / requestList.sampling_ratio) }}× what's listed here.</span></p>
            <p v-else-if="requestList?.available !== false && requestList?.requests.length && requestList.sampling_ratio == null && requestList.requests.some((request) => request.sampling_ratio == null)" class="rt-hint">The app doesn't report its sampling ratio, so these requests may be a sample of real traffic.</p>
            <p v-if="requestList?.available !== false && requestOutcome === 'slow' && requestList?.slow_threshold_ms != null" class="rt-hint">Showing requests at or above their route's p95 server duration in this window. Routes with fewer than 20 requests use the revision p95 of {{ formatMilliseconds(requestList.slow_threshold_ms) }}.</p>
            <p v-else-if="requestList?.available !== false && requestOutcome === 'errors'" class="rt-hint">Showing failed requests and 5xx responses.</p>
            <p v-if="requestList?.available === false" class="rt-note" data-tone="warn"><AlertTriangle aria-hidden="true" />{{ requestList.reason || 'Request data is unavailable.' }}</p>
            <template v-else-if="requestList && !requestList.requests.length">
              <p class="rt-hint">{{ requestOutcome === 'errors' ? 'No failed requests were observed in this window.' : requestOutcome === 'slow' ? 'No slow requests were observed in this window.' : hideUnmatched404 ? 'No sampled requests match these filters in this window.' : 'No sampled request spans were observed in this window.' }}</p>
              <p v-if="requestOutcome === 'all' && !hideUnmatched404" class="rt-hint">Check that the PHP app's <code>INDEXER_PROJECT_ID</code> and <code>INDEXER_REVISION_ID</code> match this project and revision. Container setup alone does not enable PHP request tracing.</p>
            </template>
            <div v-else-if="requestList && !visibleRequests.length" class="rt-note"><span>All loaded request rows are hidden in this view.</span><button type="button" class="rt-link" @click="restoreHiddenRequestRows">Show hidden requests ({{ requestList.requests.length }})</button></div>

            <div v-else-if="visibleRequests.length" class="rt-requests">
              <div class="rt-req-head" aria-hidden="true"><span>method</span><span>route</span><span>status</span><span>duration</span><span>time</span><span></span></div>
              <article v-for="request in pagedRequests" :key="request.trace_id" class="rt-req" :data-open="expandedTraceId === request.trace_id">
                <button type="button" class="rt-req-row" :aria-expanded="expandedTraceId === request.trace_id" @click="toggleRequest(request)">
                  <span class="rt-method" :data-method="methodTone(request.method)">{{ request.method || 'HTTP' }}</span>
                  <span class="rt-route">{{ request.route || 'Route unavailable' }}</span>
                  <span class="rt-status" :data-tone="statusTone(request.status_code)">{{ request.status_code ?? 'Status unavailable' }}</span>
                  <span class="rt-dur" :data-slow="isSlowRequest(request.duration_ms)"><span class="rt-bar-track" aria-hidden="true"><i :style="{ transform: `scaleX(${durationScale(request.duration_ms)})` }"></i></span>{{ formatMilliseconds(request.duration_ms) }}</span>
                  <span class="rt-time">{{ formatTime(request.timestamp) }}</span>
                  <ChevronDown class="rt-chev" aria-hidden="true" />
                  <span class="rt-req-est">PHP CPU estimate {{ formatPhpCpu(request.php_cpu_time_us) }} · PHP-managed peak above request start {{ formatPhpMemory(request.php_peak_memory_bytes) }}</span>
                </button>

                <div v-if="expandedTraceId === request.trace_id" class="rt-detail">
                  <p class="rt-hint">Estimates cover this PHP worker's request interval. Memory is the PHP-managed allocator peak above request start; neither value represents whole-container CPU or RSS.</p>
                  <p v-if="requestDetailLoading[request.trace_id]" class="rt-note"><RefreshCw class="rt-spin" aria-hidden="true" />Loading request details…</p>
                  <div v-else-if="requestDetailErrors[request.trace_id]" class="rt-note" data-tone="bad" role="alert"><AlertTriangle aria-hidden="true" /><span>{{ requestDetailErrors[request.trace_id] }}</span><button type="button" class="rt-link" @click="toggleRequest(request, true)">Retry</button></div>
                  <template v-else-if="requestDetails[request.trace_id]?.available">
                    <div role="tablist" aria-label="Request detail categories" class="rt-dtabs">
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
                      ><component :is="requestDetailIcons[tab.id]" aria-hidden="true" />{{ tab.label }}</button>
                    </div>
                    <div :id="`request-detail-panel-${request.trace_id}`" role="tabpanel" :aria-labelledby="`request-detail-tab-${request.trace_id}-${activeRequestDetailTab}`" tabindex="0" class="rt-panel">
                      <p v-if="requestDetails[request.trace_id].truncated" class="rt-note" data-tone="warn"><AlertTriangle aria-hidden="true" />This trace has more than {{ requestDetails[request.trace_id].span_limit }} spans; only the first {{ requestDetails[request.trace_id].span_limit }} are shown.</p>

                      <!-- Overview -->
                      <section v-if="activeRequestDetailTab === 'overview'">
                        <template v-if="requestDetails[request.trace_id].ai_summary">
                          <div class="rt-facts">
                            <div class="rt-fact"><span>Trace ID</span><b class="rt-mono">{{ requestDetails[request.trace_id].ai_summary?.trace_id }}</b></div>
                            <div class="rt-fact"><span>Endpoint</span><b>{{ requestDetails[request.trace_id].ai_summary?.endpoint }}</b></div>
                            <div class="rt-fact"><span>Total latency <i class="rt-kind" data-kind="observed">observed</i></span><b>{{ formatMilliseconds(requestDetails[request.trace_id].ai_summary?.total_latency_ms ?? null) }}</b></div>
                          </div>
                          <div class="rt-insight">
                            <h3>Trace summary <i class="rt-kind" data-kind="derived">derived from spans</i></h3>
                            <p>{{ requestDetails[request.trace_id].ai_summary?.actionable_insight }}</p>
                          </div>
                          <h3 class="rt-h">Time breakdown <i class="rt-kind" data-kind="observed">observed spans</i><i class="rt-kind" data-kind="estimate">residual estimate</i></h3>
                          <div class="rt-stack" role="img" :aria-label="breakdownParts(requestDetails[request.trace_id].ai_summary?.otel_breakdown).map((part) => `${part.label} ${formatMilliseconds(part.ms)}`).join(', ')">
                            <i v-for="part in breakdownParts(requestDetails[request.trace_id].ai_summary?.otel_breakdown)" :key="part.key" :data-residual="part.residual" :style="{ width: `${part.share}%`, '--c': part.color }"></i>
                          </div>
                          <div class="rt-legend">
                            <span v-for="part in breakdownParts(requestDetails[request.trace_id].ai_summary?.otel_breakdown)" :key="part.key"><i :data-residual="part.residual" :style="{ '--c': part.color }"></i>{{ part.label }} · {{ formatMilliseconds(part.ms) }}</span>
                          </div>
                          <template v-if="requestDetails[request.trace_id].ai_summary?.excimer_hotspots.length">
                            <h3 class="rt-h">Excimer hotspots <i class="rt-kind" data-kind="estimate">{{ requestDetails[request.trace_id].ai_summary?.profile_event_type === 'EXCIMER_CPU' ? 'CPU samples' : 'wall-clock samples' }}</i></h3>
                            <div v-for="hotspot in requestDetails[request.trace_id].ai_summary?.excimer_hotspots" :key="hotspot.frame" class="rt-hot">
                              <span>{{ hotspot.sample_percentage.toFixed(1) }}%</span>
                              <span class="rt-bar-track" aria-hidden="true"><i :style="{ transform: `scaleX(${Math.min(1, hotspot.sample_percentage / 100)})` }"></i></span>
                              <span class="rt-hot-frame">{{ hotspot.frame }}</span>
                            </div>
                          </template>
                          <p v-else class="rt-hint">No retained Excimer profile for this trace.</p>
                          <p class="rt-hint">Instrumented MySQL/Redis spans are de-overlapped estimates; the remaining time can include uninstrumented work. Excimer percentages describe retained samples, not exact request time.</p>
                        </template>
                        <p v-else class="rt-hint">The trace summary is unavailable for this request.</p>
                      </section>

                      <!-- Database -->
                      <section v-else-if="activeRequestDetailTab === 'database'">
                        <p v-if="!requestDetails[request.trace_id].database_calls.length" class="rt-hint">No database calls were captured for this request.</p>
                        <template v-else>
                          <div v-if="requestDetails[request.trace_id].database_summary" class="rt-facts">
                            <div class="rt-fact">
                              <span>Queries</span><b>{{ requestDetails[request.trace_id].database_summary!.query_count }}</b>
                              <small>{{ requestDetails[request.trace_id].database_summary!.groups.length }} distinct {{ requestDetails[request.trace_id].database_summary!.groups.length === 1 ? 'shape' : 'shapes' }}<template v-if="requestDetails[request.trace_id].database_summary!.framework_query_count"> · {{ requestDetails[request.trace_id].database_summary!.app_query_count }} app · {{ requestDetails[request.trace_id].database_summary!.framework_query_count }} framework</template></small>
                            </div>
                            <div class="rt-fact">
                              <span>Database time</span><b>{{ formatMilliseconds(requestDetails[request.trace_id].database_summary!.total_ms) }}</b>
                              <small>{{ databaseShareLabel(requestDetails[request.trace_id].database_summary!) }}<template v-if="requestDetails[request.trace_id].database_summary!.framework_query_count"> · {{ formatMilliseconds(requestDetails[request.trace_id].database_summary!.app_total_ms ?? 0) }} app · {{ formatMilliseconds(requestDetails[request.trace_id].database_summary!.framework_total_ms ?? 0) }} framework</template></small>
                            </div>
                            <div class="rt-fact">
                              <span>Slowest query</span>
                              <template v-if="requestDetails[request.trace_id].database_summary!.slowest">
                                <b>{{ formatMilliseconds(requestDetails[request.trace_id].database_summary!.slowest!.duration_ms) }}</b>
                                <small>{{ databaseShape(requestDetails[request.trace_id].database_summary!.slowest!) }}<template v-if="requestDetails[request.trace_id].database_summary!.slowest!.framework"> · framework</template></small>
                              </template>
                              <b v-else>Unavailable</b>
                            </div>
                          </div>

                          <section class="rt-ai-box" :aria-label="`AI explanation of database activity for ${request.method} ${request.route}`">
                            <div class="rt-sec-head">
                              <h3 class="rt-h rt-m0"><Sparkles aria-hidden="true" />AI explanation</h3>
                              <button type="button" class="rt-btn rt-btn-sm rt-push" data-variant="ai" :disabled="analysisLoading[requestAnalysisKey(request.trace_id)]" @click="analyzeRequest(request.trace_id, Boolean(requestAnalyses[requestAnalysisKey(request.trace_id)]))">
                                {{ analysisLoading[requestAnalysisKey(request.trace_id)] ? 'Analyzing…' : requestAnalyses[requestAnalysisKey(request.trace_id)] ? 'Analyze again' : 'Explain with AI' }}
                              </button>
                            </div>
                            <p v-if="!requestAnalyses[requestAnalysisKey(request.trace_id)]" class="rt-hint">Sends query counts, operations, tables, timings, method IDs and matching source excerpts to the {{ props.useRemote ? 'remote' : 'local' }} chat provider. SQL text, bindings and request data aren't sent. The same analysis appears in the AI analysis tab.</p>
                            <p v-if="analysisErrors[requestAnalysisKey(request.trace_id)]" role="alert" class="rt-note" data-tone="bad">{{ analysisErrors[requestAnalysisKey(request.trace_id)] }} <button type="button" class="rt-link" @click="analyzeRequest(request.trace_id, true)">Retry</button></p>
                            <template v-if="requestAnalyses[requestAnalysisKey(request.trace_id)]">
                              <p v-if="!databaseFindings(request.trace_id).length" class="rt-hint">The analysis found nothing to report about database activity. Other findings are in the AI analysis tab.</p>
                              <article v-for="finding in databaseFindings(request.trace_id)" :key="finding.id" class="rt-finding">
                                <div class="rt-finding-top"><b>{{ finding.title }}</b><span class="rt-tag">{{ finding.severity }}</span><i class="rt-kind" data-kind="ai">AI inference</i></div>
                                <p>{{ finding.summary }}</p>
                                <ul v-if="finding.recommendations.length"><li v-for="recommendation in finding.recommendations" :key="recommendation">{{ recommendation }}</li></ul>
                                <p v-if="finding.evidence_refs.length" class="rt-evidence">Evidence: {{ finding.evidence_refs.map(displaySymbol).join(', ') }}</p>
                                <button v-if="finding.source_refs.length" type="button" class="rt-link" @click="openFindingInAnalysis(request.trace_id, finding)">Preview a code change</button>
                              </article>
                            </template>
                          </section>

                          <section v-if="requestDetails[request.trace_id].database_summary?.repeated_groups" class="rt-repeat">
                            <h3 class="rt-h rt-m0"><Repeat aria-hidden="true" />Repeated queries</h3>
                            <p class="rt-hint">The same operation on the same tables from the same method ran {{ requestDetails[request.trace_id].database_summary!.repeated_min }} or more times. This is often an N+1 query or a cache read per call. SQL text isn't stored, so the queries may still differ in columns or conditions.</p>
                            <div v-for="group in requestDetails[request.trace_id].database_summary!.groups.filter((item) => item.repeated)" :key="databaseGroupKey(group)" class="rt-repeat-row">
                              <p><b>{{ group.count }}×</b> {{ databaseShape(group) }} · {{ formatMilliseconds(group.total_ms) }} total</p>
                              <p class="rt-mono">{{ group.symbol_id || 'Not attributed to application code (framework or package)' }}</p>
                              <p v-if="group.caller_symbol_ids.length" class="rt-hint rt-m0">called from {{ group.caller_symbol_ids.join(', ') }}</p>
                            </div>
                          </section>

                          <template v-if="requestDetails[request.trace_id].database_summary?.groups.length">
                            <h3 class="rt-h">Queries by shape</h3>
                            <div class="rt-table" role="table" aria-label="Queries by shape">
                              <div class="rt-shape rt-shape-head" role="row"><span role="columnheader">Count</span><span role="columnheader">Operation · tables · method</span><span role="columnheader">Time</span></div>
                              <div v-for="group in requestDetails[request.trace_id].database_summary!.groups" :key="databaseGroupKey(group)" class="rt-shape" role="row" :data-repeated="group.repeated">
                                <span class="rt-shape-count" role="cell">{{ group.count }}×</span>
                                <div class="min-w-0" role="cell">
                                  <p class="rt-shape-op">{{ databaseShape(group) }}<span v-if="group.framework" class="rt-tag" title="The framework's own work on its tables (sessions, cache, queues, debugging tools); no application method issued it.">Framework</span><i v-if="groupHasFinding(request.trace_id, group)" class="rt-kind" data-kind="ai">AI finding</i></p>
                                  <p class="rt-mono" :data-dim="!group.symbol_id">{{ group.symbol_id || (group.framework ? 'framework tables, not application code' : 'framework or package') }}</p>
                                </div>
                                <div class="rt-shape-time" role="cell">
                                  {{ formatMilliseconds(group.total_ms) }}
                                  <span v-if="group.count > 1" class="rt-hint rt-m0">max {{ formatMilliseconds(group.max_ms) }}</span>
                                  <span class="rt-bar-track" aria-hidden="true"><i :style="{ transform: `scaleX(${Math.min(1, group.total_ms / Math.max(1, requestDetails[request.trace_id].database_summary!.total_ms))})` }"></i></span>
                                </div>
                              </div>
                            </div>
                          </template>

                          <details class="rt-calls" :open="!requestDetails[request.trace_id].database_summary">
                            <summary>All {{ requestDetails[request.trace_id].database_calls.length }} database calls in order</summary>
                            <article v-for="call in requestDetails[request.trace_id].database_calls" :key="call.span_id" class="rt-call" :data-slowest="call.span_id === requestDetails[request.trace_id].database_summary?.slowest?.span_id">
                              <div><span>Database</span><p>{{ call.db_system || 'Database unavailable' }}<template v-if="call.db_namespace"> · {{ call.db_namespace }}</template></p></div>
                              <div><span>Operation</span><p class="rt-shape-op">{{ call.operation || 'Unavailable' }}</p></div>
                              <div><span>Duration</span><p>{{ formatMilliseconds(call.duration_ms) }}</p></div>
                              <div class="rt-call-wide"><span>Table · attributed method</span><p class="rt-mono rt-accent">{{ call.table_ids.length ? call.table_ids.map(displayTable).join(', ') : 'Table unavailable' }}</p><p>{{ requestMethod(call) }}</p></div>
                            </article>
                          </details>
                        </template>
                      </section>

                      <!-- Method activity -->
                      <section v-else-if="activeRequestDetailTab === 'methods'">
                        <div class="rt-sec-head">
                          <h3 class="rt-h rt-m0"><GitFork aria-hidden="true" />Observed method activity</h3>
                          <i class="rt-kind rt-push" :data-kind="requestDetails[request.trace_id].method_activity?.available ? 'observed' : 'estimate'">{{ requestDetails[request.trace_id].method_activity?.available ? 'instrumentation observation' : 'coverage gap' }}</i>
                        </div>
                        <p class="rt-hint">Bounded hook counts show configured caller-to-callee counts. They don't show call order or complete execution.</p>
                        <div v-if="requestDetails[request.trace_id].method_activity?.edges.length" class="rt-table">
                          <div v-for="edge in requestDetails[request.trace_id].method_activity?.edges" :key="`${edge.caller_symbol_id}:${edge.symbol_id}`" class="rt-edge">
                            <span class="rt-mono">{{ displaySymbol(edge.caller_symbol_id) }}</span>
                            <span aria-hidden="true">→</span>
                            <span class="rt-mono rt-accent">{{ displaySymbol(edge.symbol_id) }}</span>
                            <span class="rt-edge-calls"><span class="rt-bar-track" aria-hidden="true"><i :style="{ transform: `scaleX(${Math.min(1, edge.calls / Math.max(1, ...(requestDetails[request.trace_id].method_activity?.edges || []).map((item) => item.calls)))})` }"></i></span>{{ formatCount(edge.calls) }} calls</span>
                          </div>
                        </div>
                        <p v-else class="rt-note" data-tone="warn"><AlertTriangle aria-hidden="true" />{{ requestDetails[request.trace_id].method_activity?.reason || "No method-hook data was captured. This doesn't mean no code ran." }}</p>
                        <p v-if="requestDetails[request.trace_id].dropped_hook_calls" class="rt-note" data-tone="warn"><AlertTriangle aria-hidden="true" />{{ requestDetails[request.trace_id].dropped_hook_calls }} method call{{ requestDetails[request.trace_id].dropped_hook_calls === 1 ? ' was' : 's were' }} not recorded because the request exceeded the library's edge or call-depth limit.</p>
                        <RuleLinksPanel v-if="selected?.revision_id" :project-id="selected.project_id" :revision-id="selected.revision_id" :symbol-ids="requestSymbolIds(requestDetails[request.trace_id])" />
                      </section>

                      <!-- AI analysis -->
                      <section v-else :aria-label="`AI analysis for ${request.method} ${request.route}`">
                        <div class="rt-sends">
                          <h3 class="rt-h rt-m0"><Send aria-hidden="true" />Before you analyze: what goes to the {{ props.useRemote ? 'remote' : 'local' }} chat provider</h3>
                          <div class="rt-sends-cols">
                            <div><span class="rt-sends-label">Sent</span><ul data-kind="yes">
                              <li><Check aria-hidden="true" />Request method, route, status and timing estimates</li>
                              <li><Check aria-hidden="true" />Database system, tables, operations and durations</li>
                              <li><Check aria-hidden="true" />Method IDs and counts, profile samples</li>
                              <li><Check aria-hidden="true" />Matching indexed source excerpts</li>
                            </ul></div>
                            <div><span class="rt-sends-label">Never sent</span><ul data-kind="no">
                              <li><X aria-hidden="true" />Request and response bodies, headers, credentials</li>
                              <li><X aria-hidden="true" />SQL text and bindings, cache keys</li>
                              <li><X aria-hidden="true" />Exception messages</li>
                            </ul></div>
                          </div>
                          <div class="rt-sends-foot">
                            <button type="button" class="rt-btn rt-btn-sm" data-variant="ai" :disabled="analysisLoading[requestAnalysisKey(request.trace_id)]" @click="analyzeRequest(request.trace_id, Boolean(requestAnalyses[requestAnalysisKey(request.trace_id)]))">
                              <Sparkles aria-hidden="true" />{{ analysisLoading[requestAnalysisKey(request.trace_id)] ? 'Analyzing request…' : requestAnalyses[requestAnalysisKey(request.trace_id)] ? 'Analyze again' : 'Analyze request' }}
                            </button>
                            <span class="rt-hint rt-m0">Results are temporary and disappear after reload.</span>
                          </div>
                        </div>
                        <p v-if="analysisErrors[requestAnalysisKey(request.trace_id)]" role="alert" class="rt-note" data-tone="bad"><AlertTriangle aria-hidden="true" />{{ analysisErrors[requestAnalysisKey(request.trace_id)] }} <button type="button" class="rt-link" @click="analyzeRequest(request.trace_id, true)">Retry</button></p>
                        <div v-if="requestAnalyses[requestAnalysisKey(request.trace_id)]" aria-live="polite">
                          <p class="rt-hint">Model used: {{ requestAnalyses[requestAnalysisKey(request.trace_id)].provider }} · {{ requestAnalyses[requestAnalysisKey(request.trace_id)].model }}<span v-if="requestAnalyses[requestAnalysisKey(request.trace_id)].requested_provider !== requestAnalyses[requestAnalysisKey(request.trace_id)].provider"> (the configured provider fell back to {{ requestAnalyses[requestAnalysisKey(request.trace_id)].provider }})</span></p>
                          <p v-if="!requestAnalyses[requestAnalysisKey(request.trace_id)].findings.length" class="rt-hint">No findings were returned for the available evidence.</p>
                          <article v-for="finding in requestAnalyses[requestAnalysisKey(request.trace_id)].findings" :key="finding.id" class="rt-finding" :data-picked="selectedFindings[requestAnalysisKey(request.trace_id)] === finding.id">
                            <label class="rt-finding-pick">
                              <input type="radio" :name="`finding-${request.trace_id}`" :checked="selectedFindings[requestAnalysisKey(request.trace_id)] === finding.id" @change="selectFinding(request.trace_id, finding)" />
                              <span class="min-w-0">
                                <span class="rt-finding-top"><b>{{ finding.title }}</b><span class="rt-tag">{{ finding.severity }}</span><i class="rt-kind" data-kind="ai">AI inference</i></span>
                                <span class="rt-finding-text">{{ finding.summary }}</span>
                              </span>
                            </label>
                            <ul v-if="finding.recommendations.length"><li v-for="recommendation in finding.recommendations" :key="recommendation">{{ recommendation }}</li></ul>
                            <p v-if="finding.evidence_refs.length" class="rt-evidence">Evidence: {{ finding.evidence_refs.map(displaySymbol).join(', ') }}</p>
                          </article>
                          <ul v-if="requestAnalyses[requestAnalysisKey(request.trace_id)].coverage_gaps.length" class="rt-gaps"><li v-for="gap in requestAnalyses[requestAnalysisKey(request.trace_id)].coverage_gaps" :key="gap">Coverage gap: {{ gap }}</li></ul>
                          <div v-if="selectedFindings[requestAnalysisKey(request.trace_id)]" class="rt-diff-area">
                            <button type="button" class="rt-btn rt-btn-sm" :disabled="diffLoading[requestAnalysisKey(request.trace_id)] || !requestAnalyses[requestAnalysisKey(request.trace_id)].source_available" @click="previewFindingDiff(request.trace_id)"><FileDiff aria-hidden="true" />{{ diffLoading[requestAnalysisKey(request.trace_id)] ? 'Preparing preview…' : 'Preview code diff' }}</button>
                            <p v-if="!requestAnalyses[requestAnalysisKey(request.trace_id)].source_available" class="rt-note" data-tone="warn"><AlertTriangle aria-hidden="true" />No exact-revision source mapping is available, so diff preview is disabled.</p>
                            <div v-if="diffPreviews[requestAnalysisKey(request.trace_id)]" class="rt-diff">
                              <div class="rt-diff-head"><FileCode aria-hidden="true" />Read-only preview · {{ diffPreviews[requestAnalysisKey(request.trace_id)].file_path }} · revision {{ diffPreviews[requestAnalysisKey(request.trace_id)].revision_id }} · {{ diffPreviews[requestAnalysisKey(request.trace_id)].provider }} / {{ diffPreviews[requestAnalysisKey(request.trace_id)].model }}</div>
                              <pre tabindex="0"><span v-for="(line, index) in diffPreviews[requestAnalysisKey(request.trace_id)].diff.split('\n')" :key="index" :data-line="line.startsWith('@@') ? 'hunk' : line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'remove' : undefined">{{ line || ' ' }}</span></pre>
                              <div class="rt-diff-foot">
                                <button type="button" class="rt-btn rt-btn-sm" :disabled="ruleImpactLoading[requestAnalysisKey(request.trace_id)]" @click="checkDiffAgainstRules(request.trace_id)"><Scale aria-hidden="true" />{{ ruleImpactLoading[requestAnalysisKey(request.trace_id)] ? 'Checking business rules…' : 'Check against business rules' }}</button>
                                <RuleImpactResults v-if="ruleImpacts[requestAnalysisKey(request.trace_id)]" :result="ruleImpacts[requestAnalysisKey(request.trace_id)]!" :instance-url="null" />
                              </div>
                            </div>
                          </div>
                        </div>
                      </section>
                    </div>
                  </template>
                  <div v-else class="rt-note" data-tone="warn"><AlertTriangle aria-hidden="true" /><span>{{ requestDetails[request.trace_id]?.reason || 'Request details are unavailable.' }}</span><button type="button" class="rt-link" @click="toggleRequest(request, true)">Retry</button></div>
                </div>
              </article>
            </div>

            <nav v-if="visibleRequests.length > REQUESTS_PER_PAGE" aria-label="Recent sampled request pages" class="rt-pager">
              <span>Showing {{ firstVisibleRequest }}–{{ lastVisibleRequest }} of {{ visibleRequests.length }}</span>
              <div class="rt-pages">
                <button type="button" aria-label="Previous page" :disabled="requestPage <= 1" @click="goToRequestPage(requestPage - 1)">‹</button>
                <button v-for="page in requestPageCount" :key="page" type="button" :aria-current="requestPage === page ? 'page' : undefined" :aria-label="`Page ${page}`" @click="goToRequestPage(page)">{{ page }}</button>
                <button type="button" aria-label="Next page" :disabled="requestPage >= requestPageCount" @click="goToRequestPage(requestPage + 1)">›</button>
              </div>
            </nav>
            <p class="rt-hint">Sampled telemetry can omit requests. SQL text, bindings, request bodies and headers aren't shown.</p>
          </template>
          <template v-else>
            <div class="rt-sec-head"><h2 id="rt-requests-title" class="rt-sec-title"><RadioTower aria-hidden="true" />Recent sampled requests</h2></div>
            <p class="rt-hint">No indexed source revision is selected. Container monitoring works now; request tracing needs indexed source and a configured application adapter.</p>
            <p class="rt-hint">For PHP, run <code>just php-otel-setup /path/to/project --project-id {{ selected?.project_id }}</code> from the Developer Assistant folder. It indexes committed source and configures tracing. It also reports MySQL, MariaDB and PostgreSQL Compose services so Database Schema can load them. Then open a page in your app, refresh Runtime and select the matching revision.</p>
          </template>
        </section>
      </template>
    </div>
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
