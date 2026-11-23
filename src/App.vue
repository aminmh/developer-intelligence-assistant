<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch, onMounted, onUnmounted } from 'vue';
import {
  Settings,
  X,
  ScrollText,
  Database,
  Network, LayoutPanelLeft,
  Activity,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  MessageSquareText,
  Library,
  LogOut,
  Users,
  FolderGit2,
  GitCommitHorizontal,
  FolderSync,
} from 'lucide-vue-next';
import BrandLogo from './components/BrandLogo.vue';
import ProjectSwitcher from './components/ProjectSwitcher.vue';
import ToastStack from './components/ToastStack.vue';
import { toast } from './composables/useToasts';
import { useProjectScope } from './composables/useProjectScope';
import ToggleCard from './components/ToggleCard.vue';
import type { RAGConfig, SearchMatch, LLMUsage, ContextUsage, ChatBusinessRuleSource, ToolCallView, ToolAvailability, CitationPreview, ApprovalView } from './types';
import { mergeToolCalls } from './utils/agentTools';
import { describeRevision, relativeAge, type ChatProjectRevisions, type RevisionView } from './utils/chatRevisions';
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
const UiMapView = defineAsyncComponent(() => import('./components/UiMapView.vue'));

type AppTab = 'chat' | 'files' | 'config' | 'logs' | 'schema' | 'domain' | 'runtime' | 'code-quality' | 'ui-map';
const VALID_TABS: AppTab[] = ['chat', 'files', 'config', 'logs', 'schema', 'domain', 'runtime', 'code-quality', 'ui-map'];
const tabFromUrl = (): AppTab => {
  const value = new URLSearchParams(window.location.search).get('tab');
  return VALID_TABS.includes(value as AppTab) ? value as AppTab : 'chat';
};
const activeTab = ref<AppTab>(tabFromUrl());
// The app-wide project selection (header switcher); every page follows it.
const scope = useProjectScope();
const uploadProjectId = computed({ get: () => scope.projectId.value, set: (value: string) => scope.select(value) });
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
// Chat answers from the selected project unless a ZIP is attached in the composer.
const activeRevision = computed<{ projectId: string; revision: string } | null>(() =>
  activeFileId.value || !scope.projectId.value || !scope.currentProject.value?.indexed
    ? null
    : { projectId: scope.projectId.value, revision: scope.revision.value });
const fetchChatRevisions = async () => {
  try {
    const res = await makeRequest('/api/chat/revisions');
    const data = await res.json().catch(() => ({}));
    if (res.ok && Array.isArray(data.projects)) chatRevisions.value = data.projects;
  } catch { /* the picker keeps uploaded files only */ }
};
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

// Every success and error message in the app goes to the shared toast stack.
const triggerToast = (msg: string, isSuccess = true) => {
  if (isSuccess) toast.success(msg); else toast.error(msg);
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

watch(files, () => { void fetchChatRevisions(); void scope.load(); });
// Switching project drops a ZIP attached for another project and refreshes the access panel.
watch(() => scope.projectId.value, (projectId) => {
  const attached = files.value.find((item) => item.id === activeFileId.value);
  if (attached && attached.projectId && attached.projectId !== projectId) clearAttach();
  members.value = [];
  if (memberPanelOpen.value) void loadProjectMembers();
});
const openProjectAccess = () => {
  memberPanelOpen.value = true;
  void loadProjectMembers();
};

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

// ---- App shell: navigation, page headings, sidebar sizing ----
const navGroups: Array<{ label: string; items: Array<{ id: AppTab; label: string; icon: unknown }> }> = [
  { label: 'workspace', items: [
    { id: 'chat', label: 'Chat', icon: MessageSquareText },
    { id: 'files', label: 'Knowledge base', icon: Library },
  ] },
  { label: 'analyze', items: [
    { id: 'code-quality', label: 'Code quality', icon: ShieldCheck },
    { id: 'runtime', label: 'Runtime', icon: Activity },
    { id: 'schema', label: 'Database schema', icon: Database },
    { id: 'domain', label: 'Domain knowledge', icon: Network },
    { id: 'ui-map', label: 'UI map', icon: LayoutPanelLeft },
  ] },
  { label: 'system', items: [
    { id: 'logs', label: 'Developer logs', icon: ScrollText },
    { id: 'config', label: 'Settings', icon: Settings },
  ] },
];
const pageMeta: Record<AppTab, { title: string; description: string }> = {
  chat: { title: 'Codebase chat', description: 'Ask questions about the indexed code' },
  files: { title: 'Knowledge base', description: 'Upload, version and index projects' },
  'code-quality': { title: 'Code quality', description: 'Scores, analyzers and rule reviews' },
  'ui-map': { title: 'UI map', description: 'Routes, components and requests of a front end' },
  runtime: { title: 'Runtime', description: 'Traces and host telemetry' },
  schema: { title: 'Database schema', description: 'Explore and edit the data model' },
  domain: { title: 'Domain knowledge', description: 'Business entities and rules' },
  logs: { title: 'Developer logs', description: 'Prompts and pipeline output' },
  config: { title: 'Settings', description: 'Models, retrieval and appearance' },
};
const currentPage = computed(() => pageMeta[activeTab.value]);
const currentProjectId = computed(() => uploadProjectId.value.trim());
const projectCardMeta = computed(() => {
  const revisions = scope.currentProject.value?.revisions ?? [];
  if (!currentProjectId.value) return `${scope.projects.value.length} ${scope.projects.value.length === 1 ? 'project' : 'projects'} available`;
  if (!revisions.length) return 'No revisions yet';
  const indexed = revisions.filter((rev) => rev.indexed).length;
  return `${indexed} of ${revisions.length} ${revisions.length === 1 ? 'revision' : 'revisions'} indexed`;
});
const isProjectAdmin = computed(() => authProjects.value.some(
  (project) => (project.project_id === uploadProjectId.value || project.project_id === '*') && project.role === 'admin'));
const userLabel = computed(() => authUser.value?.email || authUser.value?.name || authUser.value?.sub || '');
const userInitials = computed(() => {
  const source = (authUser.value?.name || authUser.value?.email || authUser.value?.sub || '?').replace(/@.*/, '');
  const parts = source.split(/[\s._-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? '?') + (parts[1]?.[0] ?? parts[0]?.[1] ?? '')).toUpperCase();
});
const toggleMemberPanel = () => {
  memberPanelOpen.value = !memberPanelOpen.value;
  if (memberPanelOpen.value) void loadProjectMembers();
};

// Narrow windows always show the compact sidebar.
const isNarrow = ref(false);
let narrowQuery: MediaQueryList | null = null;
const syncNarrow = () => { isNarrow.value = narrowQuery?.matches === true; };
const sidebarIsCollapsed = computed(() => sidebarCollapsed.value || isNarrow.value);
onMounted(() => {
  narrowQuery = window.matchMedia('(max-width: 900px)');
  syncNarrow();
  narrowQuery.addEventListener('change', syncNarrow);
});
onUnmounted(() => narrowQuery?.removeEventListener('change', syncNarrow));
</script>

<template>
  <div v-if="!authChecked" class="app-shell app-gate" :data-theme="appearance.resolvedTheme.value">
    <p class="app-panel-hint">Checking sign-in…</p>
  </div>
  <div v-else-if="!authUser" class="app-shell app-gate" :data-theme="appearance.resolvedTheme.value">
    <div class="app-gate-card app-glass">
      <span class="brand-logo mx-auto block"><BrandLogo /></span>
      <h1 class="mt-5 text-lg font-semibold" style="color: var(--shell-text)">Sign in to <span class="font-mono">dev<span class="brand-title-dot">.</span>assistant</span></h1>
      <p class="mt-2 text-sm" style="color: var(--shell-text-2)">Use your organization's identity provider to continue.</p>
      <a href="/api/auth/login" class="app-btn-primary mt-6 h-10 px-5 text-sm">Continue with SSO</a>
      <p class="mt-4 text-xs" style="color: var(--shell-text-3)">If sign-in isn't available, the server administrator needs to configure OIDC and the bootstrap admin subject.</p>
    </div>
  </div>
  <div v-else class="app-shell flex h-screen w-screen gap-3 overflow-hidden p-3 font-sans"
    :data-theme="appearance.resolvedTheme.value"
    :data-palette="appearance.preferences.palette"
    :data-accent="appearance.preferences.accent"
    :data-neon="appearance.preferences.neonBorder"
    :data-ui-font="appearance.preferences.uiFontFamily"
    :data-code-font="appearance.preferences.codeFontFamily"
    :data-low-performance="appearance.preferences.lowPerformanceMode"
  >
    <div class="app-orbs" aria-hidden="true">
      <div class="app-orb app-orb-a"></div>
      <div class="app-orb app-orb-b"></div>
      <div class="app-orb app-orb-c"></div>
    </div>

    <!-- Sidebar: floating glass panel -->
    <aside class="app-sidebar app-glass select-none" :data-collapsed="sidebarIsCollapsed" aria-label="Sidebar">
      <div class="app-sidebar-edge" aria-hidden="true"></div>

      <div class="app-brand">
        <span class="brand-logo"><BrandLogo /></span>
        <div v-if="!sidebarIsCollapsed" class="min-w-0">
          <p class="brand-title">dev<span class="brand-title-dot">.</span>assistant</p>
          <p class="brand-subtitle">codebase intelligence</p>
        </div>
      </div>

      <div class="app-project-card" :title="currentProjectId ? `Project ${currentProjectId}` : 'No project selected'">
        <FolderGit2 class="h-[1.125rem] w-[1.125rem]" aria-hidden="true" />
        <div v-if="!sidebarIsCollapsed" class="min-w-0 flex-1">
          <span class="app-project-name">{{ currentProjectId || 'No project selected' }}</span>
          <span class="app-project-meta">{{ projectCardMeta }}</span>
        </div>
      </div>

      <nav class="app-nav" aria-label="Main">
        <template v-for="group in navGroups" :key="group.label">
          <p v-if="!sidebarIsCollapsed" class="app-nav-group">{{ group.label }}</p>
          <div v-else class="app-nav-divider" aria-hidden="true"></div>
          <button
            v-for="item in group.items"
            :key="item.id"
            type="button"
            class="app-nav-item"
            :aria-current="activeTab === item.id ? 'page' : undefined"
            :title="sidebarIsCollapsed ? item.label : undefined"
            :aria-label="sidebarIsCollapsed ? item.label : undefined"
            @click="activeTab = item.id"
          >
            <span v-if="activeTab === item.id" class="app-nav-indicator" aria-hidden="true"></span>
            <component :is="item.icon" aria-hidden="true" />
            <span v-if="!sidebarIsCollapsed">{{ item.label }}</span>
            <span v-if="!sidebarIsCollapsed && item.id === 'files' && files.length > 0" class="app-nav-count">{{ files.length }}</span>
          </button>
        </template>
      </nav>

      <div class="app-sidebar-footer">
        <span class="app-avatar" :title="userLabel" aria-hidden="true">{{ userInitials }}</span>
        <p v-if="!sidebarIsCollapsed" class="app-user" :title="userLabel">
          {{ userLabel }}
          <small v-if="developmentAuth">Sign-in off (development)</small>
        </p>
        <button v-if="!developmentAuth" type="button" class="app-icon-btn" title="Sign out" aria-label="Sign out" @click="logout">
          <LogOut aria-hidden="true" />
        </button>
        <button
          v-if="!isNarrow"
          type="button"
          class="app-icon-btn"
          :title="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
          :aria-label="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
          :aria-expanded="!sidebarCollapsed"
          @click="toggleSidebar"
        >
          <PanelLeftOpen v-if="sidebarCollapsed" aria-hidden="true" />
          <PanelLeftClose v-else aria-hidden="true" />
        </button>
      </div>
    </aside>

    <!-- Main area -->
    <main class="app-main flex min-w-0 flex-1 flex-col gap-3">
      <header class="app-header app-glass">
        <div class="min-w-[9rem] flex-1">
          <h1 class="app-page-title truncate">
            <span v-if="currentProjectId" class="app-page-crumb">{{ currentProjectId }} /</span>{{ currentPage.title }}
          </h1>
          <p class="app-page-desc hidden truncate md:block">{{ currentPage.description }}</p>
        </div>
        <div class="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
          <ProjectSwitcher :can-manage-access="isProjectAdmin" @open-access="openProjectAccess" />
          <button
            v-if="isProjectAdmin"
            type="button"
            class="app-chip"
            :aria-expanded="memberPanelOpen"
            aria-controls="project-access-panel"
            @click="toggleMemberPanel"
          >
            <Users aria-hidden="true" />
            <span class="hidden md:inline">Project access</span>
          </button>
          <span class="app-chip app-chip-status" title="Assistant status">
            <span class="app-status-dot" aria-hidden="true"></span>Ready
          </span>
        </div>
      </header>

      <!-- Project access: members, external model consent, retention, folder sync -->
      <Transition name="app-panel">
        <section
          v-if="memberPanelOpen"
          id="project-access-panel"
          class="app-panel app-glass grid max-h-[45vh] shrink-0 gap-5 overflow-y-auto md:grid-cols-2 xl:grid-cols-4"
          aria-label="Project access"
        >
          <div class="app-panel-section">
            <div>
              <h2 class="app-panel-title">Members</h2>
              <p class="app-panel-hint">Who can open {{ currentProjectId || 'this project' }}.</p>
            </div>
            <div class="flex flex-wrap gap-2">
              <div class="app-field-group w-full">
                <input v-model="memberSubject" aria-label="OIDC subject" class="w-full" placeholder="OIDC subject" />
              </div>
              <select v-model="memberRole" class="app-select" aria-label="Role">
                <option value="viewer">viewer</option>
                <option value="operator">operator</option>
                <option value="admin">admin</option>
              </select>
              <button type="button" class="app-btn-primary" @click="saveProjectMember">Add or update</button>
            </div>
            <div class="flex flex-wrap gap-1.5">
              <span v-for="member in members" :key="member.subject" class="app-tag">
                {{ member.subject }} · {{ member.role }}
                <button type="button" class="app-icon-btn" :aria-label="`Remove ${member.subject}`" :title="`Remove ${member.subject}`" @click="deleteProjectMember(member.subject)">
                  <X aria-hidden="true" />
                </button>
              </span>
            </div>
          </div>

          <div class="app-panel-section">
            <div>
              <h2 class="app-panel-title">External model</h2>
              <p class="app-panel-hint">Retrieved code and code-tool results are sent to the remote model only when this is on.</p>
            </div>
            <ToggleCard
              :model-value="remoteSourceAllowed === true"
              label="Send source to the external model"
              :description="remoteSourceAllowed === null ? 'Loading…' : remoteSourceAllowed ? 'Chat can use this project.' : 'Chat is blocked for this project.'"
              :disabled="remoteSourceAllowed === null || remoteSourceSaving"
              @update:model-value="toggleRemoteSource"
            />
          </div>

          <div class="app-panel-section">
            <div>
              <h2 class="app-panel-title">Revision retention</h2>
              <p class="app-panel-hint">Older ready revisions are removed automatically. The latest, pinned ones, ones with Code Quality reports, a running deployment, and Git's last sync are always kept.</p>
            </div>
            <label class="app-field-group w-fit">
              <span class="pl-2.5 text-xs" style="color: var(--shell-text-3)">Keep latest</span>
              <input
                type="number" min="1" max="100"
                class="w-14"
                :value="retention?.keep_latest ?? ''"
                :disabled="!retention || retentionSaving"
                @change="changeKeepLatest"
              />
              <span class="pr-2.5 text-xs" style="color: var(--shell-text-3)">{{ retention ? 'revisions' : 'loading…' }}</span>
            </label>
            <div v-if="retention && retentionRevisions.length" class="flex flex-wrap gap-1.5">
              <span class="app-panel-hint w-full">Always keep:</span>
              <label v-for="revision in retentionRevisions" :key="revision.revision_id" class="app-check">
                <input
                  type="checkbox"
                  :checked="retention?.pinned.includes(revision.revision_id)"
                  :disabled="!retention || retentionSaving"
                  @change="toggleRevisionPin(revision.revision_id)"
                />
                {{ describeRevision(revision) }}
                <span class="font-mono" style="color: var(--shell-text-3)">{{ revision.revision_id.slice(4, 10) }}</span>
              </label>
            </div>
          </div>

          <div class="app-panel-section">
            <div>
              <h2 class="app-panel-title">Working-tree sync</h2>
              <p class="app-panel-hint">The desktop helper asks you to choose and approve a folder, then indexes your uncommitted edits a minute after they settle. Chat can use them; Code Quality can't.</p>
            </div>
            <button
              type="button"
              class="app-chip w-fit disabled:cursor-not-allowed disabled:opacity-50"
              :disabled="workingTreeBusy || !currentProjectId"
              @click="syncWorkingTree"
            >
              <FolderSync aria-hidden="true" />
              {{ workingTreeBusy ? 'Waiting for the desktop helper…' : 'Sync a working-tree folder…' }}
            </button>
            <p v-if="!workingTreeGrants.length" class="app-panel-hint">No folder syncs to this project.</p>
            <div v-for="grant in workingTreeGrants" :key="grant.grant_id" class="app-tag h-auto py-1">
              <span class="min-w-0">
                Folder sync by {{ grant.user.email || grant.user.name || grant.user.sub }}
                <span class="block" style="color: var(--shell-text-3)">{{ grant.last_upload_at ? `Last upload ${relativeAge(grant.last_upload_at)} (${grant.last_status})` : 'No upload yet' }}</span>
              </span>
              <button
                type="button"
                class="app-icon-btn ml-auto"
                aria-label="Revoke folder sync"
                title="Revoke: the helper stops uploading this folder"
                @click="revokeWorkingTreeGrant(currentProjectId, grant.grant_id)"
              >
                <X aria-hidden="true" />
              </button>
            </div>
          </div>
        </section>
      </Transition>

      <!-- View components rendering -->
      <div class="relative flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl">
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
        :orchestrationSteps="orchestrationSteps"
        :generatingAnswer="generatingAnswer"
        :showSystemMonitor="appearance.preferences.showSystemMonitor"
        :showWelcomeBanner="appearance.preferences.showWelcomeBanner"
        :lowPerformanceMode="appearance.preferences.lowPerformanceMode"
        @sendMessage="sendMessage"
        @attachZip="attachZip"
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
        @openKnowledgeBase="activeTab = 'files'"
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

      <UiMapView v-else-if="activeTab === 'ui-map'" />

      <RuntimeView v-else-if="activeTab === 'runtime'" :use-remote="useRemote" />
      <CodeQualityView v-else-if="activeTab === 'code-quality'" :files="files" @open-knowledge-base="activeTab = 'files'" @revision-removed="fetchFiles" />
      </div>
      </Transition>
      </div>
    </main>

    <ToastStack />

  </div>
</template>
