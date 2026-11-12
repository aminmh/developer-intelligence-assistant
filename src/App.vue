<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch, onMounted, onUnmounted } from 'vue';
import { 
  FileText, 
  Settings, 
  Terminal, 
  BookOpen, 
  X,
  CheckCircle,
  AlertTriangle,
  ScrollText,
  Database,
  Network,
  Activity,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck
} from 'lucide-vue-next';
import type { RAGConfig, SearchMatch, LLMUsage, ContextUsage, ChatBusinessRuleSource, ToolCallView, ToolAvailability, CitationPreview, ApprovalView } from './types';
import { mergeToolCalls } from './utils/agentTools';
import { describeRevision, parseRevisionOption, relativeAge, revisionOptionValue, type ChatProjectRevisions, type RevisionView } from './utils/chatRevisions';
import { makeRequest } from './ui/makeRequest';
import { useProjectFiles } from './composables/useProjectFiles';
import { validateUploadIdentity } from './services/uploadIdentity';
import { useAppearancePreferences } from './composables/useAppearancePreferences';

import RAGChat from './components/RAGChat.vue';
import CodeCitationModal from './components/CodeCitationModal.vue';
import type { DomainConcept, AnalysisSession } from './types';
import type { PipelineStep } from './components/PipelineProgress.vue';

const KnowledgeBaseView = defineAsyncComponent(() => import('./components/KnowledgeBaseView.vue'));
const RAGConfigComponent = defineAsyncComponent(() => import('./components/RAGConfig.vue'));
const LogsView = defineAsyncComponent(() => import('./components/LogsView.vue'));
const DatabaseSchemaView = defineAsyncComponent(() => import('./components/DatabaseSchemaView.vue'));
const DomainKnowledgeView = defineAsyncComponent(() => import('./components/DomainKnowledgeView.vue'));
const RuntimeView = defineAsyncComponent(() => import('./components/RuntimeView.vue'));
const CodeQualityView = defineAsyncComponent(() => import('./components/CodeQualityView.vue'));

type AppTab = 'chat' | 'files' | 'config' | 'logs' | 'schema' | 'domain' | 'runtime' | 'code-quality';
const VALID_TABS: AppTab[] = ['chat', 'files', 'config', 'logs', 'schema', 'domain', 'runtime', 'code-quality'];
const tabFromUrl = (): AppTab => {
  const value = new URLSearchParams(window.location.search).get('tab');
  return VALID_TABS.includes(value as AppTab) ? value as AppTab : 'chat';
};
const activeTab = ref<AppTab>(tabFromUrl());
const uploadProjectId = ref('');
const uploadCommitSha = ref('');
const authChecked = ref(false);
const developmentAuth = ref(false);
const authUser = ref<{ sub: string; email?: string; name?: string } | null>(null);
const authProjects = ref<Array<{ project_id: string; role: 'viewer' | 'operator' | 'admin' }>>([]);
const memberPanelOpen = ref(false);
const memberSubject = ref('');
const memberRole = ref<'viewer' | 'operator' | 'admin'>('viewer');
const members = ref<Array<{ subject: string; role: 'viewer' | 'operator' | 'admin' }>>([]);
// Per-project consent for sending source to the external model; null = not loaded.
const remoteSourceAllowed = ref<boolean | null>(null);
const remoteSourceSaving = ref(false);
// Revision retention: keep the latest N ready revisions plus pinned ones; null = not loaded.
const retention = ref<{ projectId: string; keep_latest: number; pinned: string[] } | null>(null);
const retentionSaving = ref(false);
// Working-tree sync grants: a desktop helper uploads a chosen folder for this project.
type WorkingTreeGrant = { grant_id: string; user: { sub: string; email?: string; name?: string };
  created_at: string; revoked_at: string | null; last_upload_at: string | null; last_status: string | null };
const workingTreeGrants = ref<WorkingTreeGrant[]>([]);
const workingTreeBusy = ref(false);
const RUNTIME_HELPER_URL = 'http://127.0.0.1:47831';
const sidebarCollapsed = ref(false);
const appearance = useAppearancePreferences();
let handlingHistoryNavigation = false;

watch(activeTab, (tab) => {
  if (handlingHistoryNavigation) return;
  const url = new URL(window.location.href);
  if (tab === 'chat') url.searchParams.delete('tab');
  else url.searchParams.set('tab', tab);
  url.searchParams.delete('panel');
  window.history.pushState(window.history.state, '', url);
}, { flush: 'sync' });

const syncTabFromHistory = () => {
  handlingHistoryNavigation = true;
  activeTab.value = tabFromUrl();
  handlingHistoryNavigation = false;
};
// Revision mode: chat reads a project's latest (or a pinned) indexed revision instead of an
// uploaded file, so Git-synced and working-tree revisions are selectable too.
const chatRevisions = ref<ChatProjectRevisions[]>([]);
const activeRevision = ref<{ projectId: string; revision: string } | null>(null);
const fetchChatRevisions = async () => {
  try {
    const res = await makeRequest('/api/chat/revisions');
    const data = await res.json().catch(() => ({}));
    if (res.ok && Array.isArray(data.projects)) chatRevisions.value = data.projects;
  } catch { /* the picker keeps uploaded files only */ }
};
const activeSourceValue = () => activeRevision.value
  ? revisionOptionValue(activeRevision.value.projectId, activeRevision.value.revision)
  : activeFileId.value;
const activeSourceFields = () => {
  if (activeRevision.value) return { project_id: activeRevision.value.projectId, revision: activeRevision.value.revision };
  const file = files.value.find((item) => item.id === activeFileId.value);
  return file ? { upload_session_id: file.uploadSessionId || file.id, project_id: file.projectId, commit_sha: file.commitSha } : {};
};

const loadProjectMembers = async () => {
  if (!uploadProjectId.value.trim()) return;
  const response = await makeRequest(`/api/auth/projects/${encodeURIComponent(uploadProjectId.value.trim())}/members`);
  if (response.ok) members.value = (await response.json()).members ?? [];
  await Promise.all([loadRemoteSource(), loadRetention(), fetchChatRevisions(), loadWorkingTreeGrants()]);
};
const loadWorkingTreeGrants = async () => {
  const projectId = uploadProjectId.value.trim();
  workingTreeGrants.value = [];
  if (!projectId) return;
  const response = await makeRequest(`/api/projects/${encodeURIComponent(projectId)}/working-tree/grants`);
  if (response.ok && uploadProjectId.value.trim() === projectId) {
    workingTreeGrants.value = ((await response.json()).grants ?? []).filter((grant: WorkingTreeGrant) => !grant.revoked_at);
  }
};
const revokeWorkingTreeGrant = async (projectId: string, grantId: string) => {
  const response = await makeRequest(`/api/projects/${encodeURIComponent(projectId)}/working-tree/grants/${grantId}`, { method: 'DELETE' });
  if (!response.ok) triggerToast((await response.json().catch(() => ({}))).detail || 'The grant could not be revoked.', false);
  await loadWorkingTreeGrants();
};
/** Grant sync for this project; the desktop helper asks the user to choose and approve the folder. */
const syncWorkingTree = async () => {
  const projectId = uploadProjectId.value.trim();
  if (!projectId || workingTreeBusy.value) return;
  workingTreeBusy.value = true;
  let grantId = '';
  try {
    const response = await makeRequest(`/api/projects/${encodeURIComponent(projectId)}/working-tree/grants`, { method: 'POST' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'The working-tree grant could not be created.');
    grantId = data.grant.grant_id;
    let helper: Response;
    try {
      helper = await fetch(`${RUNTIME_HELPER_URL}/working-tree/grant`, {
        method: 'POST', cache: 'no-store',
        headers: { Authorization: `Bearer ${data.helper_authorization}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId, grant_id: grantId, grant_token: data.grant.token }),
      });
    } catch {
      throw new Error('The desktop helper is not running. Start it with `python3 api/telemetry-dev/runtime_setup.py helper`, then retry.');
    }
    const result = await helper.json().catch(() => ({}));
    if (!helper.ok) throw new Error(result.detail || 'The desktop helper did not accept the folder.');
    grantId = '';
    triggerToast(`Syncing ${result.folder} to ${projectId}. Edits are indexed a minute after they settle.`, true);
  } catch (cause: any) {
    triggerToast(cause?.message || 'Working-tree sync could not start.', false);
  } finally {
    // A grant the helper never stored is useless; revoke it rather than leave it active.
    if (grantId) await revokeWorkingTreeGrant(projectId, grantId).catch(() => undefined);
    workingTreeBusy.value = false;
    await loadWorkingTreeGrants();
  }
};
const loadRetention = async () => {
  const projectId = uploadProjectId.value.trim();
  retention.value = null;
  if (!projectId) return;
  const response = await makeRequest(`/api/auth/projects/${encodeURIComponent(projectId)}/retention`);
  if (!response.ok || uploadProjectId.value.trim() !== projectId) return;
  const data = await response.json();
  retention.value = { projectId, keep_latest: data.keep_latest, pinned: Array.isArray(data.pinned) ? data.pinned : [] };
};
const saveRetention = async (change: { keep_latest?: number; pinned?: string[] }) => {
  // Saves to the project the panel loaded, even if the project field was edited since.
  const projectId = retention.value?.projectId;
  if (!projectId || retentionSaving.value) return;
  retentionSaving.value = true;
  try {
    const response = await makeRequest(`/api/auth/projects/${encodeURIComponent(projectId)}/retention`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(change),
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok) retention.value = { projectId, keep_latest: data.keep_latest, pinned: data.pinned ?? [] };
    else triggerToast(data.detail || 'The setting could not be saved.', false);
  } finally {
    retentionSaving.value = false;
  }
};
const changeKeepLatest = (event: Event) => {
  const value = Number((event.target as HTMLInputElement).value);
  if (Number.isInteger(value) && value >= 1 && value <= 100 && value !== retention.value?.keep_latest) {
    void saveRetention({ keep_latest: value });
  }
};
const toggleRevisionPin = (revisionId: string) => {
  if (!retention.value) return;
  const pinned = new Set(retention.value.pinned);
  if (pinned.has(revisionId)) pinned.delete(revisionId);
  else pinned.add(revisionId);
  void saveRetention({ pinned: [...pinned] });
};
const retentionRevisions = computed(() =>
  chatRevisions.value.find((project) => project.project_id === retention.value?.projectId)?.revisions ?? []);
const loadRemoteSource = async () => {
  const projectId = uploadProjectId.value.trim();
  remoteSourceAllowed.value = null;
  if (!projectId) return;
  const response = await makeRequest(`/api/auth/projects/${encodeURIComponent(projectId)}/remote-source`);
  if (response.ok && uploadProjectId.value.trim() === projectId) remoteSourceAllowed.value = (await response.json()).allowed === true;
};
const toggleRemoteSource = async () => {
  const projectId = uploadProjectId.value.trim();
  if (!projectId || remoteSourceAllowed.value === null || remoteSourceSaving.value) return;
  remoteSourceSaving.value = true;
  try {
    const response = await makeRequest(`/api/auth/projects/${encodeURIComponent(projectId)}/remote-source`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ allowed: !remoteSourceAllowed.value }),
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok) remoteSourceAllowed.value = data.allowed === true;
    else triggerToast(data.detail || 'The setting could not be saved.', false);
  } finally {
    remoteSourceSaving.value = false;
  }
};
const saveProjectMember = async () => {
  if (!memberSubject.value.trim() || !uploadProjectId.value.trim()) return;
  const response = await makeRequest(`/api/auth/projects/${encodeURIComponent(uploadProjectId.value.trim())}/members/${encodeURIComponent(memberSubject.value.trim())}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role: memberRole.value }),
  });
  if (response.ok) { memberSubject.value = ''; await loadProjectMembers(); }
  else triggerToast((await response.json().catch(() => ({}))).error || 'Membership update failed.', false);
};
const deleteProjectMember = async (subject: string) => {
  const response = await makeRequest(`/api/auth/projects/${encodeURIComponent(uploadProjectId.value.trim())}/members/${encodeURIComponent(subject)}`, { method: 'DELETE' });
  if (response.ok) await loadProjectMembers();
};
const logout = async () => {
  await makeRequest('/api/auth/logout', { method: 'POST' });
  authUser.value = null;
};

const toggleSidebar = () => {
  sidebarCollapsed.value = !sidebarCollapsed.value;
  localStorage.setItem('dev-assistant-sidebar-collapsed', String(sidebarCollapsed.value));
};

const config = ref<RAGConfig>({
  embeddingProvider: 'ollama',
  cohereApiKey: '',
  cloudflareAccountId: '',
  cloudflareApiToken: '',
  cloudflareEmbeddingModel: '@cf/baai/bge-base-en-v1.5',
  ollamaBaseUrl: 'http://host.docker.internal:11434',
  ollamaEmbedModel: 'BGE-M3',
  embeddingBaseUrl: '',
  ollamaLlmModel: 'gemma3-4b',
  ollamaEvalModel: 'gemma3-4b',
  qdrantUrl: '',
  qdrantApiKey: '',
  llmRemoteBaseUrl: '',
  llmRemoteModel: '',
  llmRemoteApiKey: '',
  docRetrievalEnabled: true,
  docContext7Enabled: true,
  docGitmcpEnabled: true,
  docStrictMode: false,
  docContext7TimeoutS: 10,
  docGitmcpTimeoutS: 15,
  temperature: 0.7,
  topP: 0.9,
  contextPreviewMode: 'off',
  rerankerEnabled: false,
  rerankerModel: 'BAAI/bge-reranker-v2-m3',
  rerankerMaxCandidates: 40,
  rerankerTimeoutS: 10,
});

// Route chat answers to the remote GPU LLM instead of the local Ollama model.
// Once an external OpenAI-compatible model is configured, it is used for final
// answers by default (the chat toggle can still override it per session).
const useRemote = ref(false);

watch(
  () => config.value.llmRemoteBaseUrl,
  (url) => {
    useRemote.value = Boolean((url ?? '').trim());
  },
  { immediate: true },
);

const loading = ref(false);
const showSuccessToast = ref(false);
const successMessage = ref('');
const showErrorToast = ref(false);
const errorMessage = ref('');
const orchestrationSteps = ref<PipelineStep[]>([]);
const generatingAnswer = ref(false);
let pollTimerId: ReturnType<typeof setTimeout> | null = null;

// Minimum time the loading state stays on screen so it doesn't flash away.
const MIN_LOADING_MS = 1800;
let loadingStartedAt = 0;

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'orchestration';
  content: string;
  timestamp: string;
  matches?: SearchMatch[];
  businessRules?: ChatBusinessRuleSource[];
  llmUsed?: string;
  durationMs?: number;
  usage?: LLMUsage | null;
  context_usage?: ContextUsage | null;
  orchestration?: any;
  query?: string;
  queryId?: string;
  routeName?: string;
  streaming?: boolean;
  code_validation?: any[];
  contextPreview?: string;
  toolCalls?: ToolCallView[];
  tools?: ToolAvailability | null;
  approval?: ApprovalView | null;
  /** The project revision the answer was read from (revision mode only). */
  revision?: RevisionView | null;
}

const ROUTES = ['code_explain', 'code_refactor', 'debug', 'search', 'knowledge', 'chat'];

const messages = ref<Message[]>([]);

const triggerToast = (msg: string, isSuccess = true) => {
  if (isSuccess) {
    successMessage.value = msg;
    showSuccessToast.value = true;
    setTimeout(() => { showSuccessToast.value = false; }, 3000);
  } else {
    errorMessage.value = msg;
    showErrorToast.value = true;
    setTimeout(() => { showErrorToast.value = false; }, 4000);
  }
};

const {
  files,
  uploading,
  uploadProgress,
  parsingFileId,
  indexingProgress,
  indexedFileIds,
  activeFileName,
  activeFileId,
  fetchFiles,
  pollWhileProcessing,
  processFiles,
  deleteFile,
  indexFile,
  rebuildCohereEmbeddings,
  attachZip,
  clearAttach,
  selectAttachment,
} = useProjectFiles({
  triggerToast,
  getUploadIdentity: () => validateUploadIdentity(uploadProjectId.value, uploadCommitSha.value).identity,
});

const mapSteps = (raw: any[]): PipelineStep[] =>
  raw.map((s: any) => {
    let detail: string | null = s.detail ?? null;
    if (s.file_count != null && typeof s.file_count === 'number') {
      detail = `${s.file_count} files`;
    } else if (s.file_count) {
      detail = String(s.file_count);
    }
    return {
      id: s.key || s.id,
      label: s.label || '',
      status: s.status || 'pending',
      detail,
    };
  });

const fetchConfig = async () => {
  try {
    const res = await makeRequest('/api/config');
    const data = await res.json();
    if (data.success || data.config) {
      config.value = data.config;
    }
  } catch (err: any) {
    console.error('Failed to load configurations:', err);
  }
};

const saveConfig = async () => {
  try {
    const res = await makeRequest('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config.value),
    });
    const data = await res.json();
    if (data.success) {
      if (data.config) config.value = data.config;
      triggerToast('Configurations applied and saved successfully!');
    } else {
      triggerToast(data.error || 'Failed to save configuration', false);
    }
  } catch (err: any) {
    triggerToast(`Error: ${err.message}`, false);
  }
};

const scrollToBottom = () => {
  setTimeout(() => {
    const el = document.getElementById('chat-scroll-container');
    if (el) el.scrollTop = el.scrollHeight;
  }, 50);
};

// Keep the loading state visible for at least MIN_LOADING_MS.
const ensureMinLoading = async () => {
  const remaining = MIN_LOADING_MS - (Date.now() - loadingStartedAt);
  if (remaining > 0) await new Promise((r) => setTimeout(r, remaining));
};

const resetPollTimer = () => {
  if (pollTimerId) { clearTimeout(pollTimerId); pollTimerId = null; }
};

// ===== Context-window preview (inline / gate modes) =======================

// A question that has produced a context window and is now WAITING for the
// user to confirm (gate mode). Until confirmed/cancelled the model is never
// called. `jobId` is set when the window came from an orchestrated project.
interface GateTurn {
  text: string;
  markdown: string;
  jobId: string | null;
  queryId?: string;
  domainRequest?: DomainContextRequest;
}
const gatePreview = ref<GateTurn | null>(null);

interface DomainContextRequest {
  filename: string;
  conceptId: string;
  analysisId: string;
}

const domainRequestFields = (request?: DomainContextRequest) => request
  ? {
      filename: request.filename,
      domain_context_mode: 'expanded',
      domain_concept_id: request.conceptId,
      domain_analysis_id: request.analysisId,
    }
  : {};

// Stream an answer via SSE (no orchestration job, no polling). The leading
// `context` event (emitted whenever preview mode != off) is captured so the
// answer message can carry the collapsible context window disclosure.
// Read an answer SSE stream into one assistant message. Used for the first
// answer and for the continuation after a tool approval decision.
const consumeAnswerStream = async (res: Response, msgId: string) => {
  if (!res.body) throw new Error('Stream unavailable');
  let llmUsed = messages.value.find((x) => x.id === msgId)?.llmUsed || '';
  let deltas = 0;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = raw.split('\n').find((l) => l.startsWith('data: '));
      if (!line) continue;
      let evt: any;
      try {
        evt = JSON.parse(line.slice(6));
      } catch {
        continue;
      }
      const m = messages.value.find((x) => x.id === msgId);
      if (!m) return;
      if (evt.type === 'start') {
        llmUsed = evt.provider === 'remote'
          ? `Remote GPU LLM (${evt.model})`
          : `Local LLM (${evt.model})`;
      } else if (evt.type === 'tool_started') {
        m.toolCalls = [...(m.toolCalls ?? []), {
          id: String(evt.id ?? `${evt.name}-${m.toolCalls?.length ?? 0}`),
          name: String(evt.name ?? 'tool'),
          arguments: evt.arguments && typeof evt.arguments === 'object' ? evt.arguments : {},
          status: 'running',
        }];
        scrollToBottom();
      } else if (evt.type === 'tool_completed') {
        m.toolCalls = mergeToolCalls(m.toolCalls, [evt]);
      } else if (evt.type === 'approval_required') {
        m.approval = toApprovalView(evt);
        scrollToBottom();
      } else if (evt.type === 'approval_decided') {
        if (m.approval?.id === evt.approval_id) m.approval = { ...m.approval, state: evt.decision === 'approved' ? 'approved' : 'denied' };
      } else if (evt.type === 'context') {
        if (typeof evt.preview === 'string') m.contextPreview = evt.preview;
      } else if (evt.type === 'delta') {
        m.content += evt.text;
        if (++deltas % 8 === 0) scrollToBottom();
      } else if (evt.type === 'validation') {
        m.code_validation = evt.code_validation || [];
      } else if (evt.type === 'error') {
        if (!m.content) m.content = `❌ **Error:** ${evt.detail}`;
        else m.content += `\n\n❌ **Error:** ${evt.detail}`;
      } else if (evt.type === 'done') {
        m.content = evt.answer || m.content;
        if (Array.isArray(evt.matches)) m.matches = evt.matches;
        if (Array.isArray(evt.business_rules)) m.businessRules = evt.business_rules;
        m.llmUsed = llmUsed;
        m.durationMs = evt.duration_ms;
        m.usage = evt.usage || null;
        m.context_usage = evt.context_usage || null;
        m.toolCalls = mergeToolCalls(m.toolCalls, evt.tool_calls);
        if (evt.tools && typeof evt.tools === 'object') m.tools = evt.tools;
        if (evt.paused && evt.approval) m.approval = toApprovalView(evt.approval);
        if (evt.revision && typeof evt.revision === 'object') m.revision = evt.revision;
        m.streaming = false;
      }
    }
  }
};

// Stream an answer via SSE (no orchestration job, no polling). The leading
// `context` event (emitted whenever preview mode != off) is captured so the
// answer message can carry the collapsible context window disclosure.
const streamAnswer = async (text: string, domainRequest?: DomainContextRequest) => {
  const msgId = `assistant_${Date.now()}`;
  messages.value.push({
    id: msgId,
    role: 'assistant',
    content: '',
    timestamp: new Date().toLocaleTimeString(),
    query: text,
    streaming: true,
  });
  scrollToBottom();

  try {
    const res = await makeRequest('/api/query/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: text, use_remote: useRemote.value, ...activeSourceFields(), ...domainRequestFields(domainRequest) }),
    });
    if (!res.ok || !res.body) {
      throw new Error(`Stream unavailable (${res.status})`);
    }
    await consumeAnswerStream(res, msgId);
    const m = messages.value.find((x) => x.id === msgId);
    if (m) {
      m.streaming = false;
      if (!m.content && !m.approval) m.content = '_(empty response)_';
    }
  } catch (err: any) {
    const m = messages.value.find((x) => x.id === msgId);
    if (m) {
      m.content = m.content || `❌ **Stream error:** ${err.message}`;
      m.streaming = false;
    }
  }
};

// A paused answer waits for the user to approve or deny a side-effecting tool
// call; the decision response is the rest of the answer, streamed into the
// same message.
const toApprovalView = (evt: any): ApprovalView => ({
  id: String(evt.approval_id),
  calls: Array.isArray(evt.calls) ? evt.calls.map((c: any) => ({
    id: String(c.id), name: String(c.name), arguments: c.arguments && typeof c.arguments === 'object' ? c.arguments : {},
    invalid: typeof c.invalid === 'string' ? c.invalid : null,
  })) : [],
  expiresAt: typeof evt.expires_at === 'string' ? evt.expires_at : null,
  state: 'pending',
});

const decideApproval = async (msgId: string, decision: 'approve' | 'deny') => {
  const m = messages.value.find((x) => x.id === msgId);
  if (!m?.approval || m.approval.state !== 'pending') return;
  const approvalId = m.approval.id;
  m.approval = { ...m.approval, state: 'deciding' };
  m.streaming = true;
  try {
    const res = await makeRequest(`/api/tool-approvals/${approvalId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision }),
    });
    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `Approval failed (${res.status})`);
    }
    await consumeAnswerStream(res, msgId);
  } catch (err: any) {
    const current = messages.value.find((x) => x.id === msgId);
    if (current?.approval?.id === approvalId) current.approval = { ...current.approval, state: 'error', error: err.message };
  } finally {
    const current = messages.value.find((x) => x.id === msgId);
    if (current) current.streaming = false;
  }
};

// Push a finished assistant bubble from an /api/query/result payload.
const pushJobAnswer = (rData: any, queryId: string | undefined, text: string) => {
  messages.value.push({
    id: `assistant_${Date.now()}`,
    role: 'assistant',
    content: rData.answer,
    timestamp: new Date().toLocaleTimeString(),
    matches: rData.matches,
    llmUsed: rData.llmUsed || undefined,
    durationMs: rData.durationMs,
    usage: rData.usage || null,
    context_usage: rData.context_usage || null,
    contextPreview: rData.context_preview,
    orchestration: rData,
    query: text,
    queryId,
    routeName: rData.route_name || null,
    toolCalls: mergeToolCalls([], rData.tool_calls),
    tools: rData.tools || null,
    approval: rData.paused && rData.approval ? toApprovalView(rData.approval) : null,
  });
};

// `path:line` citation preview: the chat asks for the lines around a cited
// location of the attached project.
const citationPreview = ref<CitationPreview | null>(null);
const openCitation = async (path: string, line: number, messageId?: string) => {
  // A revision-mode answer cites its own revision, which may differ from the current selection.
  const answered = messages.value.find((message) => message.id === messageId)?.revision;
  citationPreview.value = { path, line, loading: true };
  try {
    const res = await makeRequest('/api/code/read', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(answered?.project_id
        ? { project_id: answered.project_id, revision: answered.revision_id, path, line }
        : activeRevision.value
          ? { ...activeSourceFields(), path, line }
          : { filename: activeFileName.value, ...activeSourceFields(), path, line }),
    });
    const data = await res.json().catch(() => ({}));
    if (citationPreview.value?.path !== path || citationPreview.value.line !== line) return;
    citationPreview.value = res.ok
      ? { path: data.path || path, line, startLine: data.start_line, content: data.content, totalLines: data.total_lines, loading: false }
      : { path, line, loading: false, error: data.error || `Source unavailable (${res.status})` };
  } catch (err: any) {
    citationPreview.value = { path, line, loading: false, error: err?.message || 'Source unavailable.' };
  }
};

// GATE, no project attached: POST the question with preview:true to the
// single-shot JSON endpoint (python /answer). The model is skipped; only the
// assembled context window comes back.
const gatePreviewDirect = async (text: string) => {
  try {
    const res = await makeRequest('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: text,
        use_remote: useRemote.value,
        preview: true,
        preview_mode: 'gate',
      }),
    });
    const data = await res.json();
    if (data && data.context_preview && data.preview_only) {
      gatePreview.value = { text, markdown: data.context_preview, jobId: null };
    } else if (data && data.answer) {
      // Backend ignored the preview flag (mode drifted to off) — surface answer.
      await ensureMinLoading();
      messages.value.push({
        id: `assistant_${Date.now()}`,
        role: 'assistant',
        content: data.answer,
        timestamp: new Date().toLocaleTimeString(),
        matches: data.matches,
        llmUsed: data.llmUsed || undefined,
        durationMs: data.durationMs,
        usage: data.usage || null,
        context_usage: data.context_usage || null,
        contextPreview: data.context_preview,
        query: text,
        queryId: data.query_id,
      });
    } else {
      messages.value.push({
        id: `assistant_${Date.now()}`,
        role: 'assistant',
        content: `❌ **Error:** ${data.error || 'Context preview unavailable.'}`,
        timestamp: new Date().toLocaleTimeString(),
      });
    }
  } catch (err: any) {
    messages.value.push({
      id: `assistant_${Date.now()}`,
      role: 'assistant',
      content: `❌ **Connection failure:** Failed to build the context window.

Details: ${err.message}`,
      timestamp: new Date().toLocaleTimeString(),
    });
  }
  loading.value = false;
  orchestrationSteps.value = [];
  generatingAnswer.value = false;
  scrollToBottom();
};

// GATE, project attached: orchestration runs first (retrieval/indexing — no
// model call); once the job is done, ask python for a preview-only answer.
const gatePreviewJob = async (jobId: string, queryId: string | undefined, text: string, domainRequest?: DomainContextRequest) => {
  let stopped = false;

  const poll = async () => {
    if (stopped) return;
    try {
      const rRes = await makeRequest('/api/query/result', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          job_id: jobId,
          user_query: text,
          use_remote: useRemote.value,
          preview: true,
          preview_mode: 'gate',
          ...domainRequestFields(domainRequest),
        }),
      });
      const rData = await rRes.json();

      if (rData.steps && rData.steps.length > 0) {
        orchestrationSteps.value = mapSteps(rData.steps);
      }

      if (rData.phase === 'done') {
        stopped = true;
        resetPollTimer();
        await ensureMinLoading();
        orchestrationSteps.value = [];
        generatingAnswer.value = false;
        if (rData.context_preview && rData.preview_only) {
          gatePreview.value = { text, markdown: rData.context_preview, jobId, queryId, domainRequest };
        } else if (rData.answer) {
          // Real answer instead of preview (server mode off) — surface normally.
          pushJobAnswer(rData, queryId, text);
        } else {
          messages.value.push({
            id: `assistant_${Date.now()}`,
            role: 'assistant',
            content: `❌ **Error:** ${rData.error || 'Context preview unavailable.'}`,
            timestamp: new Date().toLocaleTimeString(),
          });
        }
        loading.value = false;
        scrollToBottom();
        return;
      }
    } catch {
      // Polling error — keep trying
    }
    if (!stopped) {
      pollTimerId = setTimeout(poll, 1000);
    }
  };

  pollTimerId = setTimeout(poll, 1000);

  // Safety timeout after 5 minutes
  setTimeout(() => {
    stopped = true;
    resetPollTimer();
    if (loading.value) {
      loading.value = false;
      orchestrationSteps.value = [];
      generatingAnswer.value = false;
      triggerToast('Context preview timed out.', false);
    }
  }, 300000);
};

// GATE confirm for an orchestrated project: re-poll the SAME job without the
// preview flag so python runs the model now.
const generateAnswerForJob = async (turn: GateTurn) => {
  const { jobId, queryId, text, domainRequest } = turn;
  if (!jobId) return;
  loading.value = true;
  generatingAnswer.value = true;
  loadingStartedAt = Date.now();
  orchestrationSteps.value = [];
  scrollToBottom();

  let stopped = false;
  const poll = async () => {
    if (stopped) return;
    try {
      const rRes = await makeRequest('/api/query/result', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: jobId, user_query: text, use_remote: useRemote.value, ...domainRequestFields(domainRequest) }),
      });
      const rData = await rRes.json();

      if (rData.phase === 'done' && rData.answer) {
        stopped = true;
        resetPollTimer();
        await ensureMinLoading();
        pushJobAnswer(rData, queryId, text);
        loading.value = false;
        generatingAnswer.value = false;
        scrollToBottom();
        return;
      }
    } catch {
      // Polling error — keep trying
    }
    if (!stopped) {
      pollTimerId = setTimeout(poll, 1000);
    }
  };

  pollTimerId = setTimeout(poll, 1000);

  // Safety timeout after 5 minutes
  setTimeout(() => {
    stopped = true;
    resetPollTimer();
    if (loading.value) {
      loading.value = false;
      generatingAnswer.value = false;
      triggerToast('Answer generation timed out.', false);
    }
  }, 300000);
};

// User clicked "Send to model" on a gate preview — release the turn through the
// normal send machinery (stream for direct chat, job poll for projects).
const confirmGateSend = async () => {
  const turn = gatePreview.value;
  if (!turn) return;
  gatePreview.value = null;
  scrollToBottom();
  if (turn.jobId) {
    await generateAnswerForJob(turn);
  } else {
    loading.value = true;
    loadingStartedAt = Date.now();
    orchestrationSteps.value = [];
    generatingAnswer.value = false;
    await streamAnswer(turn.text, turn.domainRequest);
    loading.value = false;
    generatingAnswer.value = false;
    scrollToBottom();
  }
};

// User dismissed the gate preview — the model is never called.
const cancelGateSend = () => {
  gatePreview.value = null;
  triggerToast('Preview cancelled — no model request was sent.');
};

const askAboutDomainConcept = (concept: DomainConcept, analysis: AnalysisSession) => {
  activeTab.value = 'chat';
  void sendMessage(
    `Explain the domain concept "${concept.canonical_name}" and how it is used in this project.`,
    { filename: analysis.filename, conceptId: concept.identity_key, analysisId: analysis.id },
  );
};

const sendMessage = async (text: string, domainRequest?: DomainContextRequest) => {
  if (!text.trim() || loading.value || gatePreview.value) return;

  messages.value.push({
    id: `msg_${Date.now()}`,
    role: 'user',
    content: text,
    timestamp: new Date().toLocaleTimeString(),
  });

  loading.value = true;
  loadingStartedAt = Date.now();
  orchestrationSteps.value = [];
  generatingAnswer.value = false;
  resetPollTimer();
  scrollToBottom();

  const previewMode: RAGConfig['contextPreviewMode'] = config.value.contextPreviewMode || 'off';
  // A revision is answered on the streaming path; only uploaded files run the orchestration job.
  const requestFilename = domainRequest?.filename || (activeRevision.value ? null : activeFileName.value);

  // Explicit saved-analysis explanations use the durable analysis bundle;
  // they must not re-extract an upload that may already have expired.
  if (domainRequest) {
    await streamAnswer(text, domainRequest);
    loading.value = false;
    generatingAnswer.value = false;
    scrollToBottom();
    return;
  }

  // ---- GATE + attached project: orchestrate (retrieval, no model call) then
  // ask python for the context window before any model runs.
  if (previewMode === 'gate' && requestFilename) {
    try {
      const res = await makeRequest('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: text,
          limit: 4,
          filename: requestFilename,
          use_remote: useRemote.value,
          ...activeSourceFields(),
          ...domainRequestFields(domainRequest),
        }),
      });
      const data = await res.json();
      if (data.phase === 'orchestrating' && data.job_id) {
        await gatePreviewJob(data.job_id, data.query_id, text, domainRequest);
        return;
      }
      // Orchestration did not start — fall through to the JSON preview path.
    } catch {
      // Request failed — fall through; the normal flow reports the error.
    }
  }

  // ---- GATE, no project attached: single-shot JSON preview (model skipped).
  if (previewMode === 'gate' && !requestFilename) {
    await gatePreviewDirect(text);
    return;
  }

  // ---- off / inline — existing send flow (inline additionally captures the
  // `context` SSE event / context_preview JSON field onto the answer bubble).
  if (!requestFilename) {
    await streamAnswer(text, domainRequest);
    loading.value = false;
    generatingAnswer.value = false;
    scrollToBottom();
    return;
  }

  try {
    const res = await makeRequest('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: text,
        limit: 4,
        filename: requestFilename || undefined,
        use_remote: useRemote.value,
        ...activeSourceFields(),
        ...domainRequestFields(domainRequest),
      }),
    });

    const data = await res.json();

    // Phase 1: Orchestration started — poll for progress
    if (data.phase === 'orchestrating' && data.job_id) {
      const jobId = data.job_id;
      const queryId = data.query_id;

      resetPollTimer();

      let stopped = false;

      const poll = async () => {
        if (stopped) return;
        try {
          const rRes = await makeRequest('/api/query/result', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              job_id: jobId,
              user_query: text,
              use_remote: useRemote.value,
              ...activeSourceFields(),
              ...domainRequestFields(domainRequest),
            }),
          });

          const rData = await rRes.json();

          // Update step progress
          if (rData.steps && rData.steps.length > 0) {
            orchestrationSteps.value = mapSteps(rData.steps);
          }

          // Done — show answer
          if (rData.phase === 'done' && rData.answer) {
            stopped = true;
            resetPollTimer();
            await ensureMinLoading();
            messages.value.push({
              id: `assistant_${Date.now()}`,
              role: 'assistant',
              content: rData.answer,
              timestamp: new Date().toLocaleTimeString(),
              matches: rData.matches,
              llmUsed: rData.llmUsed || 'Gemini 3.5',
              durationMs: rData.durationMs,
              usage: rData.usage || null,
              context_usage: rData.context_usage || null,
              contextPreview: rData.context_preview,
              orchestration: rData,
              query: text,
              queryId,
              routeName: rData.route_name || null,
            });
            loading.value = false;
            orchestrationSteps.value = [];
            generatingAnswer.value = false;
            scrollToBottom();
            return;
          } else if (rData.phase === 'done' && !rData.answer) {
            // Orchestration finished — web server is generating the final answer.
            generatingAnswer.value = true;
          } else if (rData.phase === 'error') {
            stopped = true;
            resetPollTimer();
            messages.value.push({
              id: `assistant_${Date.now()}`,
              role: 'assistant',
              content: `❌ **Error:** ${rData.error || 'Processing failed.'}`,
              timestamp: new Date().toLocaleTimeString(),
            });
            loading.value = false;
            orchestrationSteps.value = [];
            generatingAnswer.value = false;
            scrollToBottom();
            return;
          }
        } catch {
          // Polling error — keep trying
        }
        if (!stopped) {
          pollTimerId = setTimeout(poll, 1000);
        }
      };

      pollTimerId = setTimeout(poll, 1000);

      // Safety timeout after 5 minutes
      setTimeout(() => {
        stopped = true;
        resetPollTimer();
        if (loading.value) {
          loading.value = false;
          orchestrationSteps.value = [];
          generatingAnswer.value = false;
          triggerToast('Processing timed out.', false);
        }
      }, 300000);
    } else if (data.success || data.answer) {
      // No orchestration — direct answer
      await ensureMinLoading();
      messages.value.push({
        id: `assistant_${Date.now()}`,
        role: 'assistant',
        content: data.answer,
        timestamp: new Date().toLocaleTimeString(),
        matches: data.matches,
        llmUsed: data.llmUsed || 'Gemini 3.5',
        durationMs: data.durationMs,
        usage: data.usage || null,
        context_usage: data.context_usage || null,
        contextPreview: data.context_preview,
        query: text,
        queryId: data.query_id,
      });
      loading.value = false;
    } else {
      messages.value.push({
        id: `assistant_${Date.now()}`,
        role: 'assistant',
        content: `❌ **Error:** ${data.error || 'Unknown error occurred.'}`,
        timestamp: new Date().toLocaleTimeString(),
      });
      loading.value = false;
    }
  } catch (err: any) {
    messages.value.push({
      id: `assistant_${Date.now()}`,
      role: 'assistant',
      content: `❌ **Connection failure:** Failed to contact the backend service.

Details: ${err.message}`,
      timestamp: new Date().toLocaleTimeString(),
    });
    loading.value = false;
  } finally {
    scrollToBottom();
  }
};

const copyToClipboard = (text: string) => {
  navigator.clipboard.writeText(text);
  triggerToast('Copied content to clipboard!');
};

const submitFeedback = async (query: string, route: string) => {
  try {
    const res = await makeRequest('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, route }),
    });
    const data = await res.json();
    if (data.success) {
      triggerToast('Feedback saved — routing will improve.');
    } else {
      triggerToast(data.error || 'Feedback failed.', false);
    }
  } catch (err: any) {
    triggerToast(`Error: ${err.message}`, false);
  }
};

// Choosing a revision detaches any uploaded file, and choosing a file leaves revision mode.
const selectSource = (value: string) => {
  const parsed = parseRevisionOption(value);
  if (parsed) {
    activeRevision.value = parsed;
    clearAttach();
  } else {
    activeRevision.value = null;
    selectAttachment(value);
  }
};
watch(activeFileId, (id) => { if (id) activeRevision.value = null; });
watch(files, () => { void fetchChatRevisions(); });

onMounted(async () => {
  window.addEventListener('popstate', syncTabFromHistory);
  try {
    const authResponse = await makeRequest('/api/auth/me');
    if (authResponse.ok) {
      const identity = await authResponse.json();
      authUser.value = identity.user;
      developmentAuth.value = identity.developmentMode === true;
      authProjects.value = identity.projects ?? [];
    }
  } catch { /* the sign-in surface below handles an unavailable API */ }
  authChecked.value = true;
  if (!authUser.value) return;
  sidebarCollapsed.value = localStorage.getItem('dev-assistant-sidebar-collapsed') === 'true';
  fetchConfig();
  await fetchFiles();
  if (files.value.some((f) => f.status === 'processing')) {
    pollWhileProcessing();
  }
});

onUnmounted(() => window.removeEventListener('popstate', syncTabFromHistory));
</script>

<template>
  <div v-if="!authChecked" class="flex h-screen items-center justify-center bg-slate-950 text-sm text-slate-400">Checking sign-in…</div>
  <div v-else-if="!authUser" class="flex h-screen items-center justify-center bg-slate-950 p-6 text-center">
    <div class="max-w-md rounded-2xl border border-slate-800 bg-slate-900/60 p-8">
      <BookOpen class="mx-auto h-8 w-8 text-indigo-400" />
      <h1 class="mt-4 text-lg font-semibold text-white">Sign in to Developer Assistant</h1>
      <p class="mt-2 text-sm text-slate-400">Use your configured organization identity provider to continue.</p>
      <a href="/api/auth/login" class="mt-6 inline-flex rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500">Continue with SSO</a>
      <p class="mt-4 text-xs text-slate-600">If sign-in is unavailable, the server administrator must configure OIDC and the bootstrap admin subject.</p>
    </div>
  </div>
  <div v-else class="app-shell flex h-screen w-screen overflow-hidden font-sans"
    :data-theme="appearance.resolvedTheme"
    :data-palette="appearance.preferences.palette"
    :data-accent="appearance.preferences.accent"
    :data-neon="appearance.preferences.neonBorder"
    :data-ui-font="appearance.preferences.uiFontFamily"
    :data-code-font="appearance.preferences.codeFontFamily"
    :data-low-performance="appearance.preferences.lowPerformanceMode"
  >
    
    <!-- Sidebar navigation & Quick Config -->
    <aside
      class="app-sidebar flex flex-col shrink-0 select-none transition-[width] duration-300 ease-in-out"
      :class="sidebarCollapsed ? 'w-[5.25rem]' : 'w-80'"
    >
      <div
        class="relative border-b border-slate-800 flex items-center"
        :class="sidebarCollapsed ? 'justify-center p-4' : 'gap-3 p-5'"
      >
        <div class="brand-logo shrink-0" aria-hidden="true">
          <svg class="brand-logo-svg" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <linearGradient id="brandLogoGradient" x1="8" y1="7" x2="40" y2="42" gradientUnits="userSpaceOnUse">
                <stop stop-color="#67E8F9" />
                <stop offset="0.48" stop-color="#818CF8" />
                <stop offset="1" stop-color="#C084FC" />
              </linearGradient>
            </defs>
            <path class="brand-logo-ring brand-logo-ring-a" d="M24 4.5C35.3 4.5 44.5 13.2 44.5 24S35.3 43.5 24 43.5 3.5 34.8 3.5 24 12.7 4.5 24 4.5Z" stroke="url(#brandLogoGradient)" stroke-opacity=".28" />
            <path class="brand-logo-ring brand-logo-ring-b" d="M24 9.5 37 17v14L24 38.5 11 31V17L24 9.5Z" stroke="url(#brandLogoGradient)" stroke-opacity=".55" />
            <path class="brand-logo-network" d="m14 17 10-5 10 5v14l-10 5-10-5V17Z M14 17l10 7 10-7 M24 24v12" stroke="url(#brandLogoGradient)" stroke-width="1.35" stroke-linejoin="round" />
            <circle class="brand-logo-node brand-logo-node-a" cx="14" cy="17" r="2.3" fill="#67E8F9" />
            <circle class="brand-logo-node brand-logo-node-b" cx="34" cy="17" r="2.3" fill="#A5B4FC" />
            <circle class="brand-logo-node brand-logo-node-c" cx="24" cy="36" r="2.3" fill="#C084FC" />
            <circle class="brand-logo-core" cx="24" cy="24" r="3.2" fill="#F8FAFC" />
          </svg>
        </div>
        <div v-if="!sidebarCollapsed" class="min-w-0">
          <h1 class="brand-title text-base font-bold tracking-tight leading-tight text-left">DevAssistant RAG</h1>
          <span class="brand-subtitle text-[10px] text-slate-400 font-mono block text-left mt-0.5">Codebase Intelligence Workspace</span>
        </div>
      </div>

      <!-- Quick stats/status dashboard -->
      <div v-if="!sidebarCollapsed" class="px-5 py-4 bg-[#11162e] border-b border-slate-800">
        <div class="flex items-center justify-between text-xs">
          <span class="text-slate-400">Indexed Files</span>
          <span class="font-semibold text-slate-300 font-mono">{{ files.length }} document(s)</span>
        </div>
      </div>

      <!-- Sidebar Tabs -->
      <nav class="flex-1 p-4 space-y-1 overflow-y-auto">
        <button 
          @click="activeTab = 'chat'" 
          class="w-full flex items-center gap-3 py-3 rounded-xl text-sm font-medium transition-all"
          :class="[sidebarCollapsed ? 'justify-center px-2' : 'px-4', activeTab === 'chat' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50']"
          title="Interactive Chat"
        >
          <Terminal class="w-4 h-4" />
          <span v-if="!sidebarCollapsed">Interactive Chat</span>
        </button>

        <button 
          @click="activeTab = 'files'" 
          class="w-full flex items-center gap-3 py-3 rounded-xl text-sm font-medium transition-all"
          :class="[sidebarCollapsed ? 'justify-center px-2' : 'px-4', activeTab === 'files' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50']"
          title="Knowledge Base"
        >
          <FileText class="w-4 h-4" />
          <span v-if="!sidebarCollapsed">Knowledge Base</span>
          <span v-if="!sidebarCollapsed && files.length > 0" class="ml-auto bg-slate-900/60 text-slate-300 text-[10px] px-2 py-0.5 rounded-full font-mono">{{ files.length }}</span>
        </button>

        <button 
          @click="activeTab = 'config'" 
          class="w-full flex items-center gap-3 py-3 rounded-xl text-sm font-medium transition-all"
          :class="[sidebarCollapsed ? 'justify-center px-2' : 'px-4', activeTab === 'config' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50']"
          title="Connection & Settings"
        >
          <Settings class="w-4 h-4" />
          <span v-if="!sidebarCollapsed">Connection & Settings</span>
        </button>

        <button
          @click="activeTab = 'logs'"
          class="w-full flex items-center gap-3 py-3 rounded-xl text-sm font-medium transition-all"
          :class="[sidebarCollapsed ? 'justify-center px-2' : 'px-4', activeTab === 'logs' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50']"
          title="Developer Logs"
        >
          <ScrollText class="w-4 h-4" />
          <span v-if="!sidebarCollapsed">Developer Logs</span>
        </button>

        <button
          @click="activeTab = 'schema'"
          class="w-full flex items-center gap-3 py-3 rounded-xl text-sm font-medium transition-all"
          :class="[sidebarCollapsed ? 'justify-center px-2' : 'px-4', activeTab === 'schema' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50']"
          title="Database Schema"
        >
          <Database class="w-4 h-4" />
          <span v-if="!sidebarCollapsed">Database Schema</span>
        </button>

        <button
          @click="activeTab = 'domain'"
          class="w-full flex items-center gap-3 py-3 rounded-xl text-sm font-medium transition-all"
          :class="[sidebarCollapsed ? 'justify-center px-2' : 'px-4', activeTab === 'domain' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50']"
          title="Domain Knowledge"
        >
          <Network class="w-4 h-4" />
          <span v-if="!sidebarCollapsed">Domain Knowledge</span>
        </button>

        <button
          @click="activeTab = 'runtime'"
          class="w-full flex items-center gap-3 py-2.5 text-sm font-medium rounded-xl transition-all duration-200"
          :class="[sidebarCollapsed ? 'justify-center px-2' : 'px-4', activeTab === 'runtime' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50']"
          title="Runtime telemetry"
        >
          <Activity class="w-4 h-4" />
          <span v-if="!sidebarCollapsed">Runtime</span>
        </button>
        <button
          @click="activeTab = 'code-quality'"
          class="flex w-full items-center gap-3 rounded-xl py-2.5 text-sm font-medium transition-all"
          :class="[sidebarCollapsed ? 'justify-center px-2' : 'px-4', activeTab === 'code-quality' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50']"
          :title="sidebarCollapsed ? 'Code quality' : undefined"
          :aria-label="sidebarCollapsed ? 'Code quality' : undefined"
        >
          <ShieldCheck :size="18" />
          <span v-if="!sidebarCollapsed">Code quality</span>
        </button>

      </nav>

      <!-- Footer developer banner -->
      <div
        class="p-4 border-t border-slate-800 text-[10px] text-slate-500 font-mono flex items-center"
        :class="sidebarCollapsed ? 'justify-center' : 'justify-between gap-2'"
      >
        <span v-if="!sidebarCollapsed">Dev RAG Front-End Client</span>
        <button
          @click="toggleSidebar"
          class="text-slate-400 hover:text-cyan-300 hover:bg-slate-800/60 rounded-lg p-1.5 transition-colors"
          :title="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
          :aria-label="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        >
          <PanelLeftOpen v-if="sidebarCollapsed" class="w-4 h-4" />
          <PanelLeftClose v-else class="w-4 h-4" />
        </button>
      </div>
    </aside>

    <!-- Main Content Panel -->
    <main class="app-main flex-1 flex flex-col min-w-0 relative">
      
      <!-- Top navbar -->
      <div class="flex flex-wrap items-center gap-2 border-b border-slate-800/70 bg-slate-950/40 px-4 py-2">
        <label v-if="activeTab !== 'files'" class="text-[10px] text-slate-500" for="upload-project-id">Upload identity</label>
        <input v-if="activeTab !== 'files'" id="upload-project-id" v-model="uploadProjectId" class="w-36 rounded-md border border-slate-800 bg-slate-900 px-2 py-1 text-xs text-slate-200" placeholder="project-id" />
        <input v-if="activeTab !== 'files'" v-model="uploadCommitSha" aria-label="Upload commit SHA (optional)" class="min-w-64 flex-1 rounded-md border border-slate-800 bg-slate-900 px-2 py-1 font-mono text-[10px] text-slate-200" placeholder="Optional Git commit SHA" />
        <button v-if="authProjects.some((project) => project.project_id === uploadProjectId && project.role === 'admin' || project.project_id === '*' && project.role === 'admin')" @click="memberPanelOpen = !memberPanelOpen; if (memberPanelOpen) loadProjectMembers()" class="rounded-md border border-slate-700 px-2 py-1 text-[10px] text-slate-300">Project access</button>
        <span class="text-[10px] text-slate-500">{{ authUser.email || authUser.name || authUser.sub }}</span>
        <span v-if="developmentAuth" class="text-[10px] text-amber-400">Authentication disabled in development</span>
        <button v-else @click="logout" class="rounded-md px-2 py-1 text-[10px] text-slate-400 hover:text-white">Sign out</button>
      </div>
      <div v-if="memberPanelOpen" class="flex flex-wrap items-center gap-2 border-b border-slate-800 bg-slate-900/60 px-4 py-2">
        <span class="text-[10px] text-slate-400">Project members</span>
        <input v-model="memberSubject" class="w-48 rounded-md border border-slate-800 bg-slate-950 px-2 py-1 text-xs text-slate-200" placeholder="OIDC subject" />
        <select v-model="memberRole" class="rounded-md border border-slate-800 bg-slate-950 px-2 py-1 text-xs text-slate-200"><option value="viewer">viewer</option><option value="operator">operator</option><option value="admin">admin</option></select>
        <button @click="saveProjectMember" class="rounded-md bg-indigo-600 px-2 py-1 text-[10px] text-white">Add / update</button>
        <span v-for="member in members" :key="member.subject" class="flex items-center gap-1 rounded border border-slate-800 px-2 py-1 text-[10px] text-slate-300">{{ member.subject }} · {{ member.role }}<button @click="deleteProjectMember(member.subject)" class="ml-1 text-rose-400">×</button></span>
        <label
          class="ml-auto flex items-center gap-2 rounded border border-slate-800 px-2 py-1 text-[10px] text-slate-300"
          :title="'Retrieved code and code-tool results for ' + (uploadProjectId || 'this project') + ' are sent to the external model only when this is on.'"
        >
          <input
            type="checkbox"
            class="h-3.5 w-3.5 accent-indigo-400"
            :checked="remoteSourceAllowed === true"
            :disabled="remoteSourceAllowed === null || remoteSourceSaving"
            @change="toggleRemoteSource"
          />
          Allow source to go to the external model
          <span v-if="remoteSourceAllowed === null" class="text-slate-500">· loading</span>
          <span v-else-if="!remoteSourceAllowed" class="text-amber-300/80">· chat is blocked for this project</span>
        </label>
        <div class="flex w-full flex-wrap items-center gap-2 text-[10px] text-slate-300">
          <label
            class="flex items-center gap-2 rounded border border-slate-800 px-2 py-1"
            title="Older ready revisions are removed automatically. The latest revision, pinned ones, ones with Code Quality reports, a running deployment, or Git's last sync are always kept, so more may remain."
          >
            Keep latest
            <input
              type="number" min="1" max="100"
              class="w-12 rounded border border-slate-800 bg-slate-950 px-1 py-0.5 text-xs text-slate-200"
              :value="retention?.keep_latest ?? ''"
              :disabled="!retention || retentionSaving"
              @change="changeKeepLatest"
            />
            revisions
            <span class="text-slate-500">· a minimum: protected revisions also stay</span>
            <span v-if="!retention" class="text-slate-500">· loading</span>
          </label>
          <span v-if="retention && retentionRevisions.length" class="text-slate-500">Keep always:</span>
          <label
            v-for="revision in retentionRevisions" :key="revision.revision_id"
            class="flex items-center gap-1 rounded border border-slate-800 px-2 py-1"
          >
            <input
              type="checkbox" class="h-3 w-3 accent-indigo-400"
              :checked="retention?.pinned.includes(revision.revision_id)"
              :disabled="!retention || retentionSaving"
              @change="toggleRevisionPin(revision.revision_id)"
            />
            {{ describeRevision(revision) }}
            <span class="text-slate-500">{{ revision.revision_id.slice(4, 10) }}</span>
          </label>
        </div>
        <div class="flex w-full flex-wrap items-center gap-2 text-[10px] text-slate-300">
          <button
            type="button"
            class="rounded border border-slate-700 px-2 py-1 hover:border-indigo-400 disabled:opacity-50"
            :disabled="workingTreeBusy || !uploadProjectId.trim()"
            title="The desktop helper asks you to choose a folder and approve it, then indexes your uncommitted edits a minute after they settle. Chat can use them; Code Quality can't."
            @click="syncWorkingTree"
          >{{ workingTreeBusy ? 'Waiting for the desktop helper…' : 'Sync a working-tree folder…' }}</button>
          <span v-if="!workingTreeGrants.length" class="text-slate-500">No folder syncs to this project.</span>
          <span
            v-for="grant in workingTreeGrants" :key="grant.grant_id"
            class="flex items-center gap-1 rounded border border-slate-800 px-2 py-1"
          >
            Folder sync by {{ grant.user.email || grant.user.name || grant.user.sub }}
            <span class="text-slate-500">· {{ grant.last_upload_at ? `last upload ${relativeAge(grant.last_upload_at)} (${grant.last_status})` : 'no upload yet' }}</span>
            <button type="button" class="ml-1 text-rose-400" title="Revoke: the helper stops uploading this folder" @click="revokeWorkingTreeGrant(uploadProjectId.trim(), grant.grant_id)">Revoke</button>
          </span>
        </div>
      </div>
      <header class="app-header h-16 px-6 flex items-center justify-between select-none backdrop-blur">
        <div class="flex items-center gap-2">
          <BookOpen class="w-4 h-4 text-indigo-400" />
          <h2 class="text-sm font-semibold text-white">
            <span v-if="activeTab === 'chat'">Codebase Chat</span>
            <span v-else-if="activeTab === 'files'">Knowledge Base</span>
            <span v-else-if="activeTab === 'config'">RAG Pipeline Configurations</span>
            <span v-else-if="activeTab === 'logs'">Developer Logs</span>
            <span v-else-if="activeTab === 'schema'">Database Schema</span>
            <span v-else-if="activeTab === 'domain'">Domain Knowledge</span>
            <span v-else-if="activeTab === 'runtime'">Runtime telemetry</span>
            <span v-else-if="activeTab === 'code-quality'">Code quality assessment</span>
          </h2>
        </div>
        
        <!-- Status indicator -->
        <div class="flex items-center gap-4">
          <div class="flex items-center gap-2 text-xs text-slate-400 bg-slate-900 px-3 py-1.5 rounded-lg border border-slate-800">
            <span class="w-2 h-2 rounded-full bg-emerald-500" :class="appearance.preferences.lowPerformanceMode ? '' : 'animate-pulse'"></span>
            <span>RAG System:</span>
            <span class="font-mono text-slate-200 uppercase">Ready</span>
          </div>
        </div>
      </header>

      <!-- View components rendering -->
      <Transition name="app-page" mode="out-in">
      <div :key="activeTab" class="flex min-h-0 flex-1 flex-col">
      <RAGChat
        v-if="activeTab === 'chat'"
        :messages="messages"
        :loading="loading"
        :filesCount="files.length"
        :files="files"
        :activeFileName="activeFileName"
        :activeFileId="activeFileId"
        :chatRevisions="chatRevisions"
        :activeSourceValue="activeSourceValue()"
        :orchestrationSteps="orchestrationSteps"
        :generatingAnswer="generatingAnswer"
        :showSystemMonitor="appearance.preferences.showSystemMonitor"
        :showWelcomeBanner="appearance.preferences.showWelcomeBanner"
        :lowPerformanceMode="appearance.preferences.lowPerformanceMode"
        @sendMessage="sendMessage"
        @attachZip="attachZip"
        @selectAttachment="selectSource"
        @clearAttach="clearAttach"
        @feedback="submitFeedback"
        :useRemote="useRemote"
        @update:useRemote="useRemote = $event"
        :previewMode="config.contextPreviewMode"
        :gatePreview="gatePreview"
        @confirmSend="confirmGateSend"
        @cancelSend="cancelGateSend"
        @openCitation="openCitation"
        @decideApproval="decideApproval"
      />
      <CodeCitationModal
        v-if="activeTab === 'chat' && citationPreview"
        :preview="citationPreview"
        @close="citationPreview = null"
      />

      <KnowledgeBaseView
        v-else-if="activeTab === 'files'"
        :files="files"
        :uploading="uploading"
        :uploadProgress="uploadProgress"
        :parsingFileId="parsingFileId"
        :indexingProgress="indexingProgress"
        :indexedFileIds="indexedFileIds"
        :embeddingProvider="config.embeddingProvider"
        v-model:project-id="uploadProjectId"
        v-model:commit-sha="uploadCommitSha"
        @uploadFiles="processFiles"
        @deleteFile="deleteFile"
        @indexFile="indexFile"
        @rebuildCohereEmbeddings="rebuildCohereEmbeddings"
      />

      <RAGConfigComponent
        v-else-if="activeTab === 'config'"
        :config="config"
        :appearance="appearance.preferences"
        @saveConfig="saveConfig"
        @updateAppearance="appearance.updatePreference"
      />

      <LogsView v-else-if="activeTab === 'logs'" />

      <DatabaseSchemaView v-else-if="activeTab === 'schema'" />

      <DomainKnowledgeView v-else-if="activeTab === 'domain'" @ask-about-concept="askAboutDomainConcept" />

      <RuntimeView v-else-if="activeTab === 'runtime'" :use-remote="useRemote" />
      <CodeQualityView v-else-if="activeTab === 'code-quality'" :files="files" @open-knowledge-base="activeTab = 'files'" @revision-removed="fetchFiles" />
      </div>
      </Transition>

    </main>

    <!-- Success Toast Notification -->
    <Transition name="app-toast">
      <div v-if="showSuccessToast" class="fixed bottom-6 right-6 z-50 max-w-sm w-full bg-slate-900 border border-emerald-500/30 rounded-2xl shadow-xl shadow-slate-950/40 p-4 pointer-events-auto flex items-start gap-3">
        <CheckCircle class="w-5 h-5 text-emerald-400 shrink-0" />
        <div class="flex-1 text-left">
          <p class="text-xs font-semibold text-white">Action Completed</p>
          <p class="text-[11px] text-slate-400 mt-1 leading-normal">{{ successMessage }}</p>
        </div>
        <button @click="showSuccessToast = false" class="text-slate-500 hover:text-slate-300">
          <X class="w-4 h-4" />
        </button>
      </div>
    </Transition>

    <!-- Error Toast Notification -->
    <Transition name="app-toast">
      <div v-if="showErrorToast" class="fixed bottom-6 right-6 z-50 max-w-sm w-full bg-slate-900 border border-rose-500/30 rounded-2xl shadow-xl shadow-slate-950/40 p-4 pointer-events-auto flex items-start gap-3">
        <AlertTriangle class="w-5 h-5 text-rose-400 shrink-0" />
        <div class="flex-1 text-left">
          <p class="text-xs font-semibold text-white">Pipeline Error</p>
          <p class="text-[11px] text-slate-400 mt-1 leading-normal">{{ errorMessage }}</p>
        </div>
        <button @click="showErrorToast = false" class="text-slate-500 hover:text-slate-300">
          <X class="w-4 h-4" />
        </button>
      </div>
    </Transition>

  </div>
</template>
