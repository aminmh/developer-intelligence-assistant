<script setup lang="ts">
import { ref, watch, onMounted } from 'vue';
import { 
  FileText, 
  Settings, 
  Terminal, 
  Sparkles, 
  BookOpen, 
  FlaskConical,
  X,
  CheckCircle,
  AlertTriangle,
  ScrollText,
  Code2,
  Database,
  Network
} from 'lucide-vue-next';
import { RAGConfig, SearchMatch, LLMUsage, ContextUsage } from './types';
import { makeRequest } from './ui/makeRequest';
import { useProjectFiles } from './composables/useProjectFiles';

import RAGChat from './components/RAGChat.vue';
import RAGFiles from './components/RAGFiles.vue';
import RAGConfigComponent from './components/RAGConfig.vue';
import SandboxAnalyses from './components/SandboxAnalyses.vue';
import LogsView from './components/LogsView.vue';
import DefinitionsView from './components/DefinitionsView.vue';
import DatabaseSchemaView from './components/DatabaseSchemaView.vue';
import DomainKnowledgeView from './components/DomainKnowledgeView.vue';
import type { DomainConcept, AnalysisSession } from './types';
import type { PipelineStep } from './components/PipelineProgress.vue';

const activeTab = ref<'chat' | 'files' | 'sandbox' | 'config' | 'logs' | 'definitions' | 'schema' | 'domain'>('chat');

const config = ref<RAGConfig>({
  ollamaBaseUrl: 'http://host.docker.internal:11434',
  ollamaEmbedModel: 'BGE-M3',
  embeddingBaseUrl: '',
  ollamaLlmModel: 'gemma3-4b',
  ollamaEvalModel: 'gemma3-4b',
  qdrantUrl: '',
  qdrantApiKey: '',
  sandboxDockerHost: '',
  sandboxJobsRoot: '/jobs',
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
}

const ROUTES = ['code_explain', 'code_refactor', 'debug', 'search', 'knowledge', 'chat'];

const messages = ref<Message[]>([
  {
    id: 'welcome',
    role: 'assistant',
    content: `👋 **Welcome to the Developer Assistant RAG System!**

This application runs with an integrated Node/TS sandbox backend. Upload documents, generate embeddings, and query your knowledge base using semantic search.

Configure your LLM and embedding settings under the **Connection & Settings** tab.`,
    timestamp: new Date().toLocaleTimeString(),
  }
]);

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
  fetchFiles,
  pollWhileProcessing,
  processFiles,
  deleteFile,
  indexFile,
  attachZip,
  clearAttach,
  selectAttachment,
} = useProjectFiles({ triggerToast });

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

  let llmUsed = '';
  let deltas = 0;
  try {
    const res = await makeRequest('/api/query/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: text, use_remote: useRemote.value, ...domainRequestFields(domainRequest) }),
    });
    if (!res.ok || !res.body) {
      throw new Error(`Stream unavailable (${res.status})`);
    }

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
        } else if (evt.type === 'context') {
          if (typeof evt.preview === 'string') m.contextPreview = evt.preview;
        } else if (evt.type === 'delta') {
          m.content += evt.text;
          if (++deltas % 8 === 0) scrollToBottom();
        } else if (evt.type === 'validation') {
          m.code_validation = evt.code_validation || [];
        } else if (evt.type === 'error') {
          if (!m.content) m.content = `❌ **Error:** ${evt.detail}`;
        } else if (evt.type === 'done') {
          m.content = evt.answer || m.content;
          m.llmUsed = llmUsed;
          m.durationMs = evt.duration_ms;
          m.usage = evt.usage || null;
          m.context_usage = evt.context_usage || null;
          m.streaming = false;
        }
      }
    }
    const m = messages.value.find((x) => x.id === msgId);
    if (m) {
      m.streaming = false;
      if (!m.content) m.content = '_(empty response)_';
    }
  } catch (err: any) {
    const m = messages.value.find((x) => x.id === msgId);
    if (m) {
      m.content = m.content || `❌ **Stream error:** ${err.message}`;
      m.streaming = false;
    }
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
  });
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
  const requestFilename = domainRequest?.filename || activeFileName.value;

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

onMounted(async () => {
  fetchConfig();
  await fetchFiles();
  if (files.value.some((f) => f.status === 'processing')) {
    pollWhileProcessing();
  }
});
</script>

<template>
  <div class="flex h-screen w-screen bg-[#070a13] text-slate-100 overflow-hidden font-sans">
    
    <!-- Sidebar navigation & Quick Config -->
    <aside class="w-80 border-r border-slate-800 bg-[#0c101f] flex flex-col shrink-0 select-none">
      <div class="p-5 border-b border-slate-800 flex items-center gap-3">
        <div class="p-2 rounded-lg bg-indigo-600/20 text-indigo-400 border border-indigo-500/30">
          <Sparkles class="w-6 h-6 animate-pulse" />
        </div>
        <div>
          <h1 class="text-base font-bold tracking-tight text-white leading-tight text-left">DevAssistant RAG</h1>
          <span class="text-[10px] text-slate-400 font-mono block text-left mt-0.5">Node/TS Sandbox Backend</span>
        </div>
      </div>

      <!-- Quick stats/status dashboard -->
      <div class="px-5 py-4 bg-[#11162e] border-b border-slate-800">
        <div class="flex items-center justify-between text-xs">
          <span class="text-slate-400">Indexed Files</span>
          <span class="font-semibold text-slate-300 font-mono">{{ files.length }} document(s)</span>
        </div>
      </div>

      <!-- Sidebar Tabs -->
      <nav class="flex-1 p-4 space-y-1 overflow-y-auto">
        <button 
          @click="activeTab = 'chat'" 
          class="w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all"
          :class="activeTab === 'chat' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50'"
        >
          <Terminal class="w-4 h-4" />
          <span>Interactive Chat</span>
        </button>

        <button 
          @click="activeTab = 'files'" 
          class="w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all"
          :class="activeTab === 'files' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50'"
        >
          <FileText class="w-4 h-4" />
          <span>Knowledge Base (Files)</span>
          <span v-if="files.length > 0" class="ml-auto bg-slate-900/60 text-slate-300 text-[10px] px-2 py-0.5 rounded-full font-mono">{{ files.length }}</span>
        </button>

        <button 
          @click="activeTab = 'sandbox'" 
          class="w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all"
          :class="activeTab === 'sandbox' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50'"
        >
          <FlaskConical class="w-4 h-4" />
          <span>Sandbox Analyzer</span>
        </button>

        <button 
          @click="activeTab = 'config'" 
          class="w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all"
          :class="activeTab === 'config' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50'"
        >
          <Settings class="w-4 h-4" />
          <span>Connection & Settings</span>
        </button>

        <button
          @click="activeTab = 'logs'"
          class="w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all"
          :class="activeTab === 'logs' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50'"
        >
          <ScrollText class="w-4 h-4" />
          <span>Developer Logs</span>
        </button>

        <button
          @click="activeTab = 'definitions'"
          class="w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all"
          :class="activeTab === 'definitions' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50'"
        >
          <Code2 class="w-4 h-4" />
          <span>Extracted Definitions</span>
        </button>

        <button
          @click="activeTab = 'schema'"
          class="w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all"
          :class="activeTab === 'schema' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50'"
        >
          <Database class="w-4 h-4" />
          <span>Database Schema</span>
        </button>

        <button
          @click="activeTab = 'domain'"
          class="w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all"
          :class="activeTab === 'domain' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:text-white hover:bg-slate-800/50'"
        >
          <Network class="w-4 h-4" />
          <span>Domain Knowledge</span>
        </button>
      </nav>

      <!-- Footer developer banner -->
      <div class="p-4 border-t border-slate-800 text-[10px] text-slate-500 font-mono text-center">
        <span>Dev RAG Front-End Client</span>
      </div>
    </aside>

    <!-- Main Content Panel -->
    <main class="flex-1 flex flex-col min-w-0 bg-[#090c17] relative">
      
      <!-- Top navbar -->
      <header class="h-16 border-b border-slate-800 px-6 flex items-center justify-between select-none bg-[#0c101f]/40 backdrop-blur">
        <div class="flex items-center gap-2">
          <BookOpen class="w-4 h-4 text-indigo-400" />
          <h2 class="text-sm font-semibold text-white">
            <span v-if="activeTab === 'chat'">Developer Playground</span>
            <span v-else-if="activeTab === 'files'">Document Chunker & Vector Indexer</span>
            <span v-else-if="activeTab === 'sandbox'">Sandbox Project Analysis</span>
            <span v-else-if="activeTab === 'config'">RAG Pipeline Configurations</span>
            <span v-else-if="activeTab === 'logs'">Developer Logs</span>
            <span v-else-if="activeTab === 'definitions'">Extracted Definitions</span>
            <span v-else-if="activeTab === 'schema'">Database Schema</span>
            <span v-else-if="activeTab === 'domain'">Domain Knowledge</span>
          </h2>
        </div>
        
        <!-- Status indicator -->
        <div class="flex items-center gap-4">
          <div class="flex items-center gap-2 text-xs text-slate-400 bg-slate-900 px-3 py-1.5 rounded-lg border border-slate-800">
            <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
            <span>RAG System:</span>
            <span class="font-mono text-slate-200 uppercase">Sandbox Mode</span>
          </div>
        </div>
      </header>

      <!-- View components rendering -->
      <RAGChat
        v-if="activeTab === 'chat'"
        :messages="messages"
        :loading="loading"
        :filesCount="files.length"
        :files="files"
        :activeFileName="activeFileName"
        :orchestrationSteps="orchestrationSteps"
        :generatingAnswer="generatingAnswer"
        @sendMessage="sendMessage"
        @attachZip="attachZip"
        @selectAttachment="selectAttachment"
        @clearAttach="clearAttach"
        @feedback="submitFeedback"
        :useRemote="useRemote"
        @update:useRemote="useRemote = $event"
        :previewMode="config.contextPreviewMode"
        :gatePreview="gatePreview"
        @confirmSend="confirmGateSend"
        @cancelSend="cancelGateSend"
      />

      <RAGFiles
        v-else-if="activeTab === 'files'"
        :files="files"
        :uploading="uploading"
        :uploadProgress="uploadProgress"
        :parsingFileId="parsingFileId"
        :indexingProgress="indexingProgress"
        :indexedFileIds="indexedFileIds"
        @uploadFiles="processFiles"
        @deleteFile="deleteFile"
        @indexFile="indexFile"
      />

      <SandboxAnalyses
        v-else-if="activeTab === 'sandbox'"
        :files="files"
      />

      <RAGConfigComponent
        v-else-if="activeTab === 'config'"
        :config="config"
        @saveConfig="saveConfig"
      />

      <LogsView v-else-if="activeTab === 'logs'" />

      <DefinitionsView v-else-if="activeTab === 'definitions'" />

      <DatabaseSchemaView v-else-if="activeTab === 'schema'" />

      <DomainKnowledgeView v-else-if="activeTab === 'domain'" @ask-about-concept="askAboutDomainConcept" />

    </main>

    <!-- Success Toast Notification -->
    <Transition
      enter-active-class="transform ease-out duration-300 transition"
      enter-from-class="translate-y-2 opacity-0 sm:translate-y-0 sm:translate-x-2"
      enter-to-class="translate-y-0 opacity-100 sm:translate-x-0"
      leave-active-class="transition ease-in duration-200"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
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
    <Transition
      enter-active-class="transform ease-out duration-300 transition"
      enter-from-class="translate-y-2 opacity-0 sm:translate-y-0 sm:translate-x-2"
      enter-to-class="translate-y-0 opacity-100 sm:translate-x-0"
      leave-active-class="transition ease-in duration-200"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
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
