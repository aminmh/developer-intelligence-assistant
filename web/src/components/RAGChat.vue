<script setup lang="ts">
import { ref, computed, onMounted, nextTick } from 'vue';
import MarkdownIt from 'markdown-it';
import {
  SendHorizontal,
  Info,
  Paperclip,
  FolderGit2,
  GitCommitHorizontal,
  Gauge,
  Eye,
  ShieldCheck,
  X,
  Copy,
  Check,
  Route,
  GitBranch,
  FileCode2,
  Scale,
  FlaskConical,
  ChartNoAxesColumn,
  HardDrive,
  Cloud,
  BookOpenText,
  SearchCode,
  Bug,
  WandSparkles,
  Boxes,
} from 'lucide-vue-next';
import { SearchMatch, UploadedFile, LLMUsage, ContextUsage, type ChatBusinessRuleSource, type ToolCallView, type ToolAvailability, type ApprovalView } from '../types';
import { linkCitations } from '../utils/agentTools';
import ToolActivity from './ToolActivity.vue';
import ToolApprovalCard from './ToolApprovalCard.vue';
import { outlineHref, syncedAgo, symbolLabel } from './code-quality/ruleKnowledge';
import PipelineProgress from './PipelineProgress.vue';
import ChatLoading from './ChatLoading.vue';
import EvaluationModal from './EvaluationModal.vue';
import RetrievalInsights from './RetrievalInsights.vue';
import SysMonitor from './SysMonitor.vue';
import BrandLogo from './BrandLogo.vue';
import { useProjectScope } from '../composables/useProjectScope';
import type { PipelineStep } from './PipelineProgress.vue';
import { describeRevision, type RevisionView } from '../utils/chatRevisions';

interface CodeValidation {
  language?: string;
  index?: number;
  status?: string;
  errors?: unknown[];
  warnings?: unknown[];
}

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
  code_validation?: CodeValidation[];
  contextPreview?: string;
  toolCalls?: ToolCallView[];
  tools?: ToolAvailability | null;
  approval?: ApprovalView | null;
  revision?: RevisionView | null;
}

const props = defineProps<{
  messages: Message[];
  loading: boolean;
  filesCount: number;
  files: UploadedFile[];
  activeFileName: string | null;
  activeFileId: string | null;
  orchestrationSteps: PipelineStep[];
  generatingAnswer: boolean;
  showSystemMonitor: boolean;
  showWelcomeBanner: boolean;
  lowPerformanceMode: boolean;
  useRemote: boolean;
  previewMode: 'off' | 'inline' | 'gate';
  gatePreview: { text: string; markdown: string } | null;
}>();

const emit = defineEmits<{
  (e: 'sendMessage', text: string): void;
  (e: 'attachZip', file: File): void;
  (e: 'clearAttach'): void;
  (e: 'feedback', query: string, route: string): void;
  (e: 'update:useRemote', value: boolean): void;
  (e: 'confirmSend'): void;
  (e: 'cancelSend'): void;
  (e: 'openCitation', path: string, line: number, messageId?: string): void;
  (e: 'decideApproval', messageId: string, decision: 'approve' | 'deny'): void;
  (e: 'openKnowledgeBase'): void;
}>();

const ROUTES = ['code_explain', 'code_refactor', 'debug', 'search', 'knowledge', 'chat'];

const STARTERS = [
  { label: 'Explain', icon: BookOpenText, text: 'Explain how the main request flow works, step by step.' },
  { label: 'Find', icon: SearchCode, text: 'Where is the payment or checkout logic handled?' },
  { label: 'Debug', icon: Bug, text: 'What could cause this error, and where should I look first?' },
  { label: 'Refactor', icon: WandSparkles, text: 'Which class is doing too much, and how would you split it?' },
];

const chatInput = ref('');
const inputEl = ref<HTMLTextAreaElement | null>(null);

// ---- Markdown -------------------------------------------------------------
// `html: false` keeps raw HTML in model output inert (XSS-safe); linkify turns bare URLs clickable.
const md = new MarkdownIt({ html: false, linkify: true, breaks: true });

const escapeHtml = (value: string) => value
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const validationTone = (status: string | undefined) => {
  const s = (status || '').toLowerCase();
  if (s.includes('fail') || s.includes('error') || s.includes('invalid')) return 'bad';
  if (s.includes('warn')) return 'warn';
  return 'ok';
};

// Code blocks get a header: language, the validator's verdict for that block, and Copy.
// The validator numbers blocks per language in order, so the renderer counts the same way.
const defaultFence = md.renderer.rules.fence!;
md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const token = tokens[idx];
  const lang = (token.info || '').trim().split(/\s+/)[0].toLowerCase();
  const inner = defaultFence(tokens, idx, options, env, self);
  const counters: Record<string, number> = (env.fenceCounters ??= {});
  const ordinal = counters[lang] ?? 0;
  counters[lang] = ordinal + 1;
  const result = (env.validations as CodeValidation[] | undefined)?.find(
    (v) => (v.language || '').toLowerCase() === lang && v.index === ordinal,
  );
  const badge = result?.status
    ? `<span class="chat-code-badge" data-tone="${validationTone(result.status)}">${escapeHtml(result.status)}</span>`
    : '';
  return `<div class="chat-code"><div class="chat-code-head"><span>${escapeHtml(lang || 'text')}</span>${badge}`
    + `<button type="button" class="chat-code-copy" data-copy-code aria-label="Copy code">Copy</button></div>${inner}</div>`;
};

// `path:line` code spans become citation links that open the cited source.
const renderMarkdown = (content: string, validations?: CodeValidation[]): string =>
  linkCitations(md.render(content || '', { validations }));

// ---- Clipboard ------------------------------------------------------------
const copiedKey = ref<string | null>(null);
let copiedTimer: ReturnType<typeof setTimeout> | null = null;
const copyText = async (text: string, key: string) => {
  try {
    await navigator.clipboard.writeText(text);
    copiedKey.value = key;
    if (copiedTimer) clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => { copiedKey.value = null; }, 1600);
  } catch { /* clipboard blocked: nothing to confirm */ }
};

const onAnswerClick = (event: MouseEvent, messageId?: string) => {
  const target = event.target as HTMLElement | null;
  const copyButton = target?.closest<HTMLElement>('[data-copy-code]');
  if (copyButton) {
    const code = copyButton.closest('.chat-code')?.querySelector('pre')?.textContent ?? '';
    void copyText(code, `code-${messageId}`);
    copyButton.textContent = 'Copied';
    setTimeout(() => { copyButton.textContent = 'Copy'; }, 1600);
    return;
  }
  const link = target?.closest<HTMLElement>('[data-cite-path]');
  if (!link) return;
  event.preventDefault();
  const line = Number(link.dataset.citeLine);
  // The message tells the app which revision answered, so the citation opens that revision's source.
  if (link.dataset.citePath && Number.isFinite(line)) emit('openCitation', link.dataset.citePath, line, messageId);
};

// ---- Per-answer detail panels (one open at a time per answer) -------------
type PanelName = 'sources' | 'rules' | 'usage' | 'context' | 'route' | 'insights';
const openPanels = ref<Record<string, PanelName | null>>({});
const panelOf = (id: string) => openPanels.value[id] ?? null;
const togglePanel = (id: string, panel: PanelName) => {
  const opening = panelOf(id) !== panel;
  openPanels.value = { ...openPanels.value, [id]: opening ? panel : null };
  // An answer near the bottom would open its panel out of sight, so bring it into view.
  if (opening) {
    void nextTick(() => {
      const panel = document.getElementById(`chat-panel-${id}`);
      const thread = document.getElementById('chat-scroll-container');
      if (!panel || !thread) return;
      const overflow = panel.getBoundingClientRect().bottom - thread.getBoundingClientRect().bottom;
      if (overflow > 0) thread.scrollTop += overflow + 12;
    });
  }
};

// Routing feedback
const feedbackDoneId = ref<string | null>(null);
const onFeedback = (query: string | undefined, route: string, id: string) => {
  if (!query) return;
  emit('feedback', query, route);
  feedbackDoneId.value = id;
  openPanels.value = { ...openPanels.value, [id]: null };
};

// Evaluation modal state
const showEvaluation = ref(false);
const evalQueryId = ref<string | null>(null);
const openEvaluation = (queryId: string | undefined) => {
  if (!queryId) return;
  evalQueryId.value = queryId;
  showEvaluation.value = true;
};

// One-line project knowledge chip from an orchestration status payload.
const profileSummary = (orch: any): string | null => {
  const p = orch?.project_profile;
  if (!p || typeof p !== 'object') return null;
  const bits: string[] = [];
  if (p.framework) bits.push(String(p.framework) + (p.version ? ` ${p.version}` : ''));
  const routes = Array.isArray(p.routes) ? p.routes.length : 0;
  const models = Array.isArray(p.models) ? p.models.length : 0;
  if (routes) bits.push(`${routes} routes`);
  if (models) bits.push(`${models} models`);
  const gateways = p.packages?.payment_gateways;
  if (Array.isArray(gateways) && gateways.length) {
    bits.push('payment: ' + gateways.map((g: any) => g.gateway || '?').join(', '));
  } else if (p.packages?.payment_absent?.length) {
    bits.push('no payment SDK');
  }
  return bits.length ? bits.join(' · ') : null;
};

const orchestrationOutputs = (orch: any): Array<{ tool: string; exit?: number }> =>
  Object.entries(orch?.outputs ?? {}).map(([tool, out]: [string, any]) => ({ tool, exit: out?.exit_code }));

// Steps shown during loading: orchestration steps + a trailing "Generating answer"
// step while the web server runs the final LLM call.
const displaySteps = computed<PipelineStep[]>(() => {
  const steps = [...props.orchestrationSteps];
  if (props.generatingAnswer && !steps.some((s) => s.id === 'generating_answer')) {
    steps.push({ id: 'generating_answer', label: 'Generating answer', status: 'running' });
  }
  return steps;
});

const formatTokens = (value: number | null | undefined, estimated = false): string => {
  if (value === null || value === undefined) return '—';
  return `${estimated ? '~' : ''}${value.toLocaleString()}`;
};

const formatDuration = (ms?: number) => {
  if (!ms) return '';
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
};

const projectName = computed(() => props.activeFileName || scope.projectId.value || 'your codebase');
const hasConversation = computed(() => props.messages.length > 0 || !!props.gatePreview || props.loading);

// ---- Scrolling and composer ----------------------------------------------
const scrollToBottom = () => {
  setTimeout(() => {
    const el = document.getElementById('chat-scroll-container');
    if (el) el.scrollTop = el.scrollHeight;
  }, 50);
};

onMounted(() => {
  scrollToBottom();
});

// While a gate preview awaits confirmation the model must NOT run, so further
// sends are locked (same visual state as loading).
const chatLocked = computed(() => props.loading || !!props.gatePreview);

const autoGrow = () => {
  const el = inputEl.value;
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
};

const onSubmit = () => {
  if (chatInput.value.trim() && !chatLocked.value) {
    emit('sendMessage', chatInput.value.trim());
    chatInput.value = '';
    void nextTick(autoGrow);
    scrollToBottom();
  }
};

const onComposerKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    onSubmit();
  }
};

const useStarter = (text: string) => {
  chatInput.value = text;
  void nextTick(() => {
    autoGrow();
    inputEl.value?.focus();
  });
};

const onAttachZip = (event: Event) => {
  const target = event.target as HTMLInputElement;
  if (target.files && target.files.length > 0) {
    emit('attachZip', target.files[0]);
    target.value = '';
  }
};

const scope = useProjectScope();
// Where answers come from: an attached ZIP, or the project chosen in the header.
const answerSource = computed(() => {
  if (props.activeFileName) return null;
  const project = scope.projectId.value;
  if (!project) return { project: 'No project', detail: 'choose one in the header', tone: 'warn' };
  const resolved = scope.resolvedRevision.value;
  if (!scope.usable.value || !resolved) return { project, detail: 'not indexed', tone: 'warn' };
  return { project, detail: scope.revision.value === 'latest' ? 'latest' : describeRevision({ ...resolved, created_at: null }), tone: scope.revision.value === 'latest' ? 'latest' : 'pinned' };
});
</script>

<template>
  <section class="chat-page" aria-label="Chat">
    <div id="chat-scroll-container" class="chat-thread">
      <div class="chat-column">
        <!-- New chat -->
        <div v-if="!hasConversation && showWelcomeBanner" class="chat-hero">
          <span class="chat-hero-logo"><BrandLogo /></span>
          <h2>Ask about <span class="chat-hero-name">{{ projectName }}</span></h2>
          <p>Answers come from the indexed code, its call graph and your business rules, with links to the exact lines they used.</p>
          <div class="chat-starters">
            <button
              v-for="(starter, i) in STARTERS"
              :key="starter.label"
              type="button"
              class="chat-starter"
              :style="{ animationDelay: `${40 * (i + 1)}ms` }"
              @click="useStarter(starter.text)"
            >
              <b><component :is="starter.icon" aria-hidden="true" />{{ starter.label }}</b>
              <span>{{ starter.text }}</span>
            </button>
          </div>
        </div>

        <div v-if="!activeFileName && !scope.usable.value && scope.loaded.value && !hasConversation" class="chat-notice">
          <Info aria-hidden="true" />
          <span v-if="!scope.projectId.value">Choose a project in the header to ask about its code.</span>
          <span v-else><b>{{ scope.projectId.value }}</b> has no indexed revision yet. Index it before asking about its code.</span>
          <button type="button" class="chat-btn" @click="emit('openKnowledgeBase')">Open knowledge base</button>
        </div>

        <!-- Conversation -->
        <template v-for="msg in messages" :key="msg.id">
          <template v-if="msg.role !== 'assistant'">
            <div class="chat-user">{{ msg.content }}</div>
            <div class="chat-user-meta">{{ msg.timestamp }}</div>
          </template>

          <article v-else class="chat-ai">
            <span class="chat-ai-avatar"><BrandLogo /></span>
            <div class="min-w-0">
              <div class="chat-ai-head">
                <b>dev.assistant</b>
                <span v-if="msg.routeName" class="chat-tag" data-tone="route"><Route aria-hidden="true" />{{ msg.routeName }}</span>
                <span v-if="msg.revision" class="chat-tag" :title="msg.revision.revision_id">
                  <GitBranch aria-hidden="true" />{{ describeRevision(msg.revision) }}<template v-if="msg.revision.requested === 'latest'"> · latest</template>
                </span>
                <span v-if="profileSummary(msg.orchestration)" class="chat-tag" data-tone="warn"><Boxes aria-hidden="true" />{{ profileSummary(msg.orchestration) }}</span>
                <span class="chat-ai-meta">
                  <template v-if="msg.llmUsed">{{ msg.llmUsed }} · </template>
                  <template v-if="msg.durationMs">{{ formatDuration(msg.durationMs) }} · </template>
                  {{ msg.timestamp }}
                </span>
              </div>

              <ToolActivity :calls="msg.toolCalls" :tools="msg.tools" :streaming="msg.streaming" />
              <ToolApprovalCard
                v-if="msg.approval"
                :approval="msg.approval"
                @decide="(decision) => emit('decideApproval', msg.id, decision)"
              />
              <div v-if="msg.orchestration && orchestrationOutputs(msg.orchestration).length" class="chat-outputs">
                <span v-for="out in orchestrationOutputs(msg.orchestration)" :key="out.tool" class="chat-tag" :data-tone="out.exit && out.exit !== 0 ? 'bad' : undefined">
                  {{ out.tool }}<template v-if="out.exit !== undefined"> · exit {{ out.exit }}</template>
                </span>
              </div>

              <div class="chat-answer" :class="{ 'is-streaming': msg.streaming }">
                <div class="prose prose-invert prose-sm max-w-none text-left" v-html="renderMarkdown(msg.content, msg.code_validation)" @click="onAnswerClick($event, msg.id)"></div>
                <span v-if="msg.streaming" class="chat-caret" aria-hidden="true"></span>
              </div>

              <!-- One toolbar replaces the separate disclosure links -->
              <div v-if="!msg.streaming" class="chat-toolbar" role="toolbar" :aria-label="`Details for the answer at ${msg.timestamp}`">
                <button v-if="msg.matches?.length" type="button" class="chat-tb" :aria-expanded="panelOf(msg.id) === 'sources'" @click="togglePanel(msg.id, 'sources')">
                  <FileCode2 aria-hidden="true" />Sources<span class="chat-tb-count">{{ msg.matches.length }}</span>
                </button>
                <button v-if="msg.businessRules?.length" type="button" class="chat-tb" :aria-expanded="panelOf(msg.id) === 'rules'" @click="togglePanel(msg.id, 'rules')">
                  <Scale aria-hidden="true" />Rules<span class="chat-tb-count">{{ msg.businessRules.length }}</span>
                </button>
                <button v-if="msg.usage || msg.context_usage" type="button" class="chat-tb" :aria-expanded="panelOf(msg.id) === 'usage'" @click="togglePanel(msg.id, 'usage')">
                  <Gauge aria-hidden="true" />
                  <template v-if="msg.usage">{{ formatTokens(msg.usage.total_tokens, msg.usage.estimated) }} tokens</template>
                  <template v-else>Usage</template>
                </button>
                <button v-if="msg.contextPreview" type="button" class="chat-tb" :aria-expanded="panelOf(msg.id) === 'context'" @click="togglePanel(msg.id, 'context')">
                  <Eye aria-hidden="true" />Context
                </button>
                <template v-if="msg.routeName && msg.query">
                  <span class="chat-tb-sep" aria-hidden="true"></span>
                  <button type="button" class="chat-tb" :aria-expanded="panelOf(msg.id) === 'route'" @click="togglePanel(msg.id, 'route')">
                    <Route aria-hidden="true" />{{ feedbackDoneId === msg.id ? 'Route feedback saved' : 'Wrong route?' }}
                  </button>
                </template>
                <span class="chat-tb-end">
                  <button type="button" class="chat-tb chat-tb-icon" :title="copiedKey === `answer-${msg.id}` ? 'Copied' : 'Copy answer'" aria-label="Copy answer" @click="copyText(msg.content, `answer-${msg.id}`)">
                    <Check v-if="copiedKey === `answer-${msg.id}`" aria-hidden="true" />
                    <Copy v-else aria-hidden="true" />
                  </button>
                  <template v-if="msg.queryId">
                    <button type="button" class="chat-tb chat-tb-icon" title="View evaluation" aria-label="View evaluation" @click="openEvaluation(msg.queryId)">
                      <FlaskConical aria-hidden="true" />
                    </button>
                    <button type="button" class="chat-tb chat-tb-icon" title="Retrieval insights" aria-label="Retrieval insights" :aria-expanded="panelOf(msg.id) === 'insights'" @click="togglePanel(msg.id, 'insights')">
                      <ChartNoAxesColumn aria-hidden="true" />
                    </button>
                  </template>
                </span>
              </div>

              <div :id="`chat-panel-${msg.id}`">
              <Transition name="app-panel">
                <div v-if="panelOf(msg.id) === 'sources' && msg.matches" class="chat-panel">
                  <div class="chat-sources">
                    <div v-for="(match, i) in msg.matches" :key="i" class="chat-source">
                      <div class="chat-source-top">
                        <FileCode2 aria-hidden="true" />
                        <span>Chunk #{{ match.chunk.index }}</span>
                        <span class="chat-score" :title="`Match score ${(match.score * 100).toFixed(0)}%`">
                          <span class="chat-bar"><i :style="{ transform: `scaleX(${Math.max(0, Math.min(1, match.score))})` }"></i></span>
                          {{ (match.score * 100).toFixed(0) }}%
                        </span>
                      </div>
                      <p>{{ match.chunk.text }}</p>
                      <p v-if="match.runtime_usage" class="chat-runtime">
                        Runtime: {{ Number(match.runtime_usage.runs).toLocaleString() }} calls · p95 {{ Number(match.runtime_usage.p95_ms).toFixed(1) }} ms · {{ Number(match.runtime_usage.failures).toLocaleString() }} failures · last seen {{ match.runtime_usage.last_seen ? new Date(match.runtime_usage.last_seen).toLocaleString() : 'unknown' }}
                      </p>
                    </div>
                  </div>
                </div>

                <div v-else-if="panelOf(msg.id) === 'rules' && msg.businessRules" class="chat-panel" aria-label="Business rules used for this answer">
                  <ul class="chat-rules">
                    <li v-for="rule in msg.businessRules" :key="rule.doc_id">
                      <span class="font-mono">[{{ rule.label }}]</span>
                      <a :href="outlineHref(rule.instance_url, rule.url)" target="_blank" rel="noopener noreferrer">{{ rule.title }}<template v-if="rule.heading"> · {{ rule.heading }}</template></a>
                      <span class="chat-muted"> · {{ syncedAgo(rule.synced_at) }}</span>
                      <span v-if="rule.symbol_ids.length" class="chat-muted"> · implemented by <code>{{ rule.symbol_ids.slice(0, 3).map(symbolLabel).join(', ') }}</code><template v-if="rule.symbol_ids.length > 3"> +{{ rule.symbol_ids.length - 3 }}</template></span>
                    </li>
                  </ul>
                </div>

                <div v-else-if="panelOf(msg.id) === 'usage'" class="chat-panel">
                  <div class="chat-usage">
                    <template v-if="msg.usage">
                      <div class="chat-stat"><span>Input</span><b>{{ formatTokens(msg.usage.prompt_tokens, msg.usage.estimated) }}</b></div>
                      <div class="chat-stat"><span>Output</span><b>{{ formatTokens(msg.usage.completion_tokens, msg.usage.estimated) }}</b></div>
                      <div class="chat-stat"><span>Total</span><b>{{ formatTokens(msg.usage.total_tokens, msg.usage.estimated) }}</b></div>
                    </template>
                    <div v-if="msg.context_usage" class="chat-context-use">
                      Context
                      <span class="chat-bar"><i :style="{ transform: `scaleX(${Math.min(1, msg.context_usage.percent / 100)})` }"></i></span>
                      {{ msg.context_usage.percent.toLocaleString() }}% · {{ formatTokens(msg.context_usage.input_tokens, msg.context_usage.estimated) }} of {{ msg.context_usage.limit_tokens.toLocaleString() }}
                    </div>
                    <p v-if="msg.usage?.estimated || msg.context_usage?.estimated" class="chat-muted chat-usage-note">~ estimated; the provider didn't report usage.</p>
                  </div>
                </div>

                <div v-else-if="panelOf(msg.id) === 'context' && msg.contextPreview" class="chat-panel">
                  <p class="chat-muted mb-2 text-xs">What the model received for this answer.</p>
                  <div class="chat-context-box prose prose-invert prose-sm max-w-none text-left" v-html="renderMarkdown(msg.contextPreview)"></div>
                </div>

                <div v-else-if="panelOf(msg.id) === 'route'" class="chat-panel">
                  <p class="chat-muted mb-2 text-xs">Pick how this question should have been routed. The router learns from it.</p>
                  <div class="chat-routes">
                    <button
                      v-for="r in ROUTES"
                      :key="r"
                      type="button"
                      class="chat-tag"
                      :data-tone="r === msg.routeName ? 'route' : undefined"
                      :aria-pressed="r === msg.routeName"
                      @click="onFeedback(msg.query, r, msg.id)"
                    >{{ r }}</button>
                  </div>
                </div>

                <div v-else-if="panelOf(msg.id) === 'insights' && msg.queryId" class="chat-panel">
                  <RetrievalInsights :queryId="msg.queryId" open />
                </div>
              </Transition>
              </div>
            </div>
          </article>
        </template>

        <!-- Gate preview awaiting confirmation: the model has NOT been called -->
        <article v-if="gatePreview" class="chat-ai">
          <span class="chat-ai-avatar"><BrandLogo /></span>
          <div class="chat-gate min-w-0">
            <h3><ShieldCheck aria-hidden="true" />Review the context before it's sent<span class="chat-tag" data-tone="warn">model not called yet</span></h3>
            <div class="chat-context-box prose prose-invert prose-sm max-w-none text-left" v-html="renderMarkdown(gatePreview.markdown)"></div>
            <p class="chat-muted text-xs">This is the exact markdown the model will receive. Nothing has been generated yet.</p>
            <div class="chat-gate-actions">
              <button type="button" class="chat-btn" @click="emit('cancelSend')"><X aria-hidden="true" />Cancel</button>
              <button type="button" class="chat-btn" data-variant="primary" @click="emit('confirmSend')"><SendHorizontal aria-hidden="true" />Send to model</button>
            </div>
          </div>
        </article>

        <!-- Working -->
        <article v-if="loading" class="chat-ai">
          <span class="chat-ai-avatar"><BrandLogo /></span>
          <div class="min-w-0">
            <PipelineProgress v-if="displaySteps.length > 0" :fileName="activeFileName || 'project'" :steps="displaySteps" />
            <ChatLoading v-else :label="generatingAnswer ? 'Generating answer' : 'Thinking'" />
          </div>
        </article>
      </div>
    </div>

    <!-- Composer -->
    <div class="chat-dock">
      <div v-if="showSystemMonitor" class="chat-monitor-row">
        <SysMonitor :lowPerformanceMode="lowPerformanceMode" />
      </div>
      <form class="chat-composer" @submit.prevent="onSubmit">
        <div v-if="activeFileName" class="chat-attached">
          <span class="chat-tag" data-tone="ok">
            <FolderGit2 aria-hidden="true" />{{ activeFileName }}
            <button type="button" class="chat-tag-x" aria-label="Stop using this project" title="Stop using this project" @click="emit('clearAttach')"><X aria-hidden="true" /></button>
          </span>
        </div>
        <textarea
          ref="inputEl"
          v-model="chatInput"
          rows="1"
          aria-label="Message"
          :placeholder="activeFileName ? `Ask about ${activeFileName}…` : scope.projectId.value ? `Ask about APIs, code or configuration in ${scope.projectId.value}…` : 'Ask about APIs, code or configuration in your indexed projects…'"
          :disabled="chatLocked"
          @input="autoGrow"
          @keydown="onComposerKeydown"
        ></textarea>
        <div class="chat-composer-bar">
          <span v-if="answerSource" class="chat-from" :data-tone="answerSource.tone" title="Answers come from this project. Change it in the header.">
            <GitCommitHorizontal aria-hidden="true" /><b>{{ answerSource.project }}</b><span>· {{ answerSource.detail }}</span>
          </span>
          <div class="chat-seg" role="group" aria-label="Model">
            <button type="button" :aria-pressed="!useRemote" :disabled="chatLocked" title="Answer with the local model" @click="emit('update:useRemote', false)">
              <HardDrive aria-hidden="true" />Local
            </button>
            <button type="button" :aria-pressed="useRemote" :disabled="chatLocked" title="Answer with the external OpenAI-compatible model" @click="emit('update:useRemote', true)">
              <Cloud aria-hidden="true" />External
            </button>
          </div>
          <label class="chat-pick chat-pick-icon" title="Attach a .zip" aria-label="Attach a .zip">
            <input type="file" accept=".zip" hidden @change="onAttachZip" />
            <Paperclip aria-hidden="true" />
          </label>
          <button type="submit" class="chat-send" :disabled="!chatInput.trim() || chatLocked">
            <SendHorizontal aria-hidden="true" />Send
          </button>
        </div>
      </form>
      <div class="chat-hint">
        <span><kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line</span>
        <span v-if="gatePreview" data-tone="warn">Confirm or cancel the preview above.</span>
        <span v-else-if="previewMode === 'gate'" data-tone="warn">Gate mode: you review the context before the model runs.</span>
        <span v-else-if="previewMode === 'inline'">Inline: each answer includes the context it used.</span>
      </div>
    </div>

    <EvaluationModal :show="showEvaluation" :queryId="evalQueryId" @close="showEvaluation = false" />
  </section>
</template>
