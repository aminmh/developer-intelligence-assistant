<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import MarkdownIt from 'markdown-it';
import {
  Send,
  Cpu,
  Database,
  Info,
  ChevronDown,
  ChevronUp,
  Paperclip,
  FolderOpen,
  Gauge,
  Eye,
  ShieldCheck,
  X,
} from 'lucide-vue-next';
import { SearchMatch, UploadedFile, LLMUsage, ContextUsage } from '../types';
import PipelineProgress from './PipelineProgress.vue';
import ChatLoading from './ChatLoading.vue';
import EvaluationModal from './EvaluationModal.vue';
import RetrievalInsights from './RetrievalInsights.vue';
import SysMonitor from './SysMonitor.vue';
import WelcomeTerminal from './WelcomeTerminal.vue';
import type { PipelineStep } from './PipelineProgress.vue';

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
  useRemote: boolean;
  previewMode: 'off' | 'inline' | 'gate';
  gatePreview: { text: string; markdown: string } | null;
}>();

const emit = defineEmits<{
  (e: 'sendMessage', text: string): void;
  (e: 'attachZip', file: File): void;
  (e: 'selectAttachment', fileId: string): void;
  (e: 'clearAttach'): void;
  (e: 'feedback', query: string, route: string): void;
  (e: 'update:useRemote', value: boolean): void;
  (e: 'confirmSend'): void;
  (e: 'cancelSend'): void;
}>();

const ROUTES = ['code_explain', 'code_refactor', 'debug', 'search', 'knowledge', 'chat'];

const chatInput = ref('');

// Render assistant answers as GitHub-flavoured markdown. `html: false` keeps
// raw HTML in model output inert (XSS-safe); linkify turns bare URLs clickable.
const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
});

const renderMarkdown = (content: string): string => md.render(content || '');

// Feedback UI state: which message's route picker is open / already submitted
const feedbackOpenId = ref<string | null>(null);
const feedbackDoneId = ref<string | null>(null);

// Evaluation modal state
const showEvaluation = ref(false);
const evalQueryId = ref<string | null>(null);

const openEvaluation = (queryId: string | undefined) => {
  if (!queryId) return;
  evalQueryId.value = queryId;
  showEvaluation.value = true;
};

const toggleFeedback = (id: string) => {
  feedbackOpenId.value = feedbackOpenId.value === id ? null : id;
};

const onFeedback = (query: string | undefined, route: string, id: string) => {
  if (!query) return;
  emit('feedback', query, route);
  feedbackOpenId.value = null;
  feedbackDoneId.value = id;
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

// Steps shown during loading: orchestration steps + a trailing "Generating answer"
// step while the web server runs the final LLM call.
const displaySteps = computed<PipelineStep[]>(() => {
  const steps = [...props.orchestrationSteps];
  if (props.generatingAnswer && !steps.some((s) => s.id === 'generating_answer')) {
    steps.push({ id: 'generating_answer', label: 'Generating answer', status: 'running' });
  }
  return steps;
});

const messagesContainer = ref<HTMLElement | null>(null);

const scrollToBottom = () => {
  setTimeout(() => {
    const el = document.getElementById('chat-scroll-container');
    if (el) el.scrollTop = el.scrollHeight;
  }, 50);
};

onMounted(() => {
  scrollToBottom();
});

const onSubmit = () => {
  if (chatInput.value.trim() && !chatLocked.value) {
    emit('sendMessage', chatInput.value.trim());
    chatInput.value = '';
    scrollToBottom();
  }
};

const onAttachZip = (event: Event) => {
  const target = event.target as HTMLInputElement;
  if (target.files && target.files.length > 0) {
    emit('attachZip', target.files[0]);
    target.value = '';
  }
};

const onSelectFile = (event: Event) => {
  const value = (event.target as HTMLSelectElement).value;
  if (value) {
    emit('selectAttachment', value);
  }
};

// Toggle for showing match details
const expandedMessageId = ref<string | null>(null);
const toggleMatchExpansion = (id: string) => {
  expandedMessageId.value = expandedMessageId.value === id ? null : id;
};

// Toggle for showing the "context window sent to model" disclosure on answers.
const expandedPreviewId = ref<string | null>(null);
const togglePreviewExpansion = (id: string) => {
  expandedPreviewId.value = expandedPreviewId.value === id ? null : id;
};

const expandedUsageId = ref<string | null>(null);
const toggleUsageExpansion = (id: string) => {
  expandedUsageId.value = expandedUsageId.value === id ? null : id;
};

const formatTokens = (value: number | null | undefined, estimated = false): string => {
  if (value === null || value === undefined) return '—';
  return `${estimated ? '~' : ''}${value.toLocaleString()}`;
};

// While a gate preview awaits confirmation the model must NOT run, so further
// sends are locked (same visual state as loading).
const chatLocked = computed(() => props.loading || !!props.gatePreview);
</script>

<template>
  <div class="flex-1 flex flex-col min-h-0">
    <!-- Messages area -->
    <div id="chat-scroll-container" class="flex-1 overflow-y-auto p-6 space-y-4 scroll-smooth">
      
      <!-- Messages list -->
      <div class="max-w-4xl mx-auto w-full space-y-4">
        <WelcomeTerminal v-if="showWelcomeBanner" :generatingAnswer="generatingAnswer" />
        <SysMonitor v-if="showSystemMonitor" />
        <div 
          v-for="msg in messages" 
          :key="msg.id" 
          class="flex flex-col"
          :class="msg.role === 'user' ? 'items-end' : 'items-start'"
        >
          <!-- Message bubble -->
          <div 
            class="rounded-2xl px-5 py-4 max-w-3xl"
            :class="msg.role === 'user' 
              ? 'bg-indigo-600/10 border border-indigo-500/20 text-slate-100 rounded-br-none'
              : 'bg-slate-900/60 border border-slate-800 text-slate-300 rounded-tl-none'"
          >
            <div class="prose prose-invert prose-sm max-w-none text-left" v-if="msg.role === 'assistant'" v-html="renderMarkdown(msg.content)"></div>
            <span v-if="msg.role === 'assistant' && msg.streaming" class="inline-block w-2 h-4 bg-indigo-400 animate-pulse ml-0.5 align-text-bottom"></span>
            <div class="text-sm text-left font-mono leading-relaxed text-slate-100 whitespace-pre-wrap break-words" v-else>{{ msg.content }}</div>
            
            <!-- Assistant message metadata -->
            <div v-if="msg.role === 'assistant'" class="mt-3 pt-3 border-t border-slate-800 flex items-center justify-between text-[10px] text-slate-500 font-mono">
              <span>{{ msg.timestamp }}</span>
              <span v-if="msg.llmUsed" class="flex items-center gap-1">
                <Cpu class="w-3 h-3" />
                {{ msg.llmUsed }}
              </span>
              <span v-if="msg.durationMs">{{ msg.durationMs }}ms</span>
            </div>

            <button
              v-if="msg.role === 'assistant' && (msg.usage || msg.context_usage)"
              @click="toggleUsageExpansion(msg.id)"
              class="mt-2 text-[10px] text-cyan-400/80 hover:text-cyan-300 flex items-center gap-1 font-mono transition-colors"
            >
              <Gauge class="w-3 h-3" />
              <span>{{ expandedUsageId === msg.id ? 'Hide' : 'Show' }} token usage</span>
              <span v-if="msg.usage">· {{ formatTokens(msg.usage.total_tokens, msg.usage.estimated) }} total</span>
              <ChevronDown v-if="expandedUsageId !== msg.id" class="w-3 h-3" />
              <ChevronUp v-else class="w-3 h-3" />
            </button>

            <div
              v-if="expandedUsageId === msg.id && (msg.usage || msg.context_usage)"
              class="mt-2 rounded-lg border border-cyan-500/15 bg-cyan-950/10 px-3 py-2 text-[10px] font-mono text-slate-400"
            >
              <div v-if="msg.usage" class="grid grid-cols-3 gap-2">
                <span>Input {{ formatTokens(msg.usage.prompt_tokens, msg.usage.estimated) }}</span>
                <span>Output {{ formatTokens(msg.usage.completion_tokens, msg.usage.estimated) }}</span>
                <span>Total {{ formatTokens(msg.usage.total_tokens, msg.usage.estimated) }}</span>
              </div>
              <div v-if="msg.context_usage" class="mt-1 text-cyan-300/80">
                Context {{ msg.context_usage.percent.toLocaleString() }}%
                ({{ formatTokens(msg.context_usage.input_tokens, msg.context_usage.estimated) }} /
                {{ msg.context_usage.limit_tokens.toLocaleString() }})
              </div>
              <div v-if="msg.usage?.estimated || msg.context_usage?.estimated" class="mt-1 text-slate-500">
                ~ estimated; provider usage was unavailable
              </div>
            </div>

            <!-- Orchestration info -->
            <div v-if="msg.orchestration" class="mt-3 pt-3 border-t border-slate-800 text-left">
              <div class="text-[10px] text-emerald-400 font-mono flex items-center gap-1 mb-1">
                <Database class="w-3 h-3" />
                Orchestration — {{ msg.orchestration.route || 'tools' }}
              </div>
              <div v-if="profileSummary(msg.orchestration)" class="text-[10px] font-mono mt-1 inline-flex items-center gap-1.5 flex-wrap">
                <span class="text-amber-300/90 bg-amber-400/5 border border-amber-400/20 rounded px-1.5 py-0.5">
                  {{ profileSummary(msg.orchestration) }}
                </span>
              </div>
              <div v-for="(out, tool) in msg.orchestration.outputs" :key="tool"
                class="text-[10px] text-slate-500 font-mono mt-1">
                <span class="text-slate-400">{{ tool }}:</span>
                <span v-if="out && out.exit_code !== undefined"> exit {{ out.exit_code }}</span>
              </div>
            </div>

            <!-- Routing feedback -->
            <div v-if="msg.role === 'assistant' && msg.routeName && msg.query" class="mt-3 pt-3 border-t border-slate-800 text-left">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-[10px] text-slate-500 font-mono">
                  Routed as <span class="text-indigo-400 font-semibold">{{ msg.routeName }}</span>
                </span>
                <button
                  @click="toggleFeedback(msg.id)"
                  class="text-[10px] text-slate-400 hover:text-indigo-300 underline underline-offset-2 font-mono transition-colors"
                >
                  wrong route?
                </button>
                <span v-if="feedbackDoneId === msg.id" class="text-[10px] text-emerald-400 font-mono">Feedback saved ✓</span>
              </div>
              <div v-if="feedbackOpenId === msg.id" class="mt-2 flex flex-wrap gap-1.5">
                <button
                  v-for="r in ROUTES"
                  :key="r"
                  @click="onFeedback(msg.query, r, msg.id)"
                  class="px-2.5 py-1 rounded-lg border text-[10px] font-mono transition-all"
                  :class="r === msg.routeName
                    ? 'border-indigo-500 bg-indigo-500/10 text-indigo-300'
                    : 'border-slate-800 bg-[#11162d] text-slate-400 hover:text-white hover:border-indigo-500'"
                >
                  {{ r }}
                </button>
              </div>
            </div>

            <!-- Evaluate response -->
            <div v-if="msg.role === 'assistant' && msg.queryId" class="mt-2 pt-2 border-t border-slate-800 text-left">
              <button
                @click="openEvaluation(msg.queryId)"
                class="text-[10px] text-slate-400 hover:text-indigo-300 flex items-center gap-1 font-mono transition-colors"
              >
                <Gauge class="w-3 h-3" />
                View evaluation
              </button>
            </div>
          </div>

          <!-- Context window disclosure (inline/gate: what the model saw) -->
          <button
            v-if="msg.role === 'assistant' && msg.contextPreview"
            @click="togglePreviewExpansion(msg.id)"
            class="mt-1 text-[10px] text-violet-400 hover:text-violet-300 flex items-center gap-1 font-mono transition-all"
          >
            <Eye class="w-3 h-3" />
            <span>{{ expandedPreviewId === msg.id ? 'Hide' : 'Show' }} Context Window (sent to model)</span>
            <ChevronDown v-if="expandedPreviewId !== msg.id" class="w-3 h-3" />
            <ChevronUp v-else class="w-3 h-3" />
          </button>

          <div
            v-if="expandedPreviewId === msg.id && msg.contextPreview"
            class="mt-2 max-w-3xl w-full"
          >
            <div class="bg-[#0a0e1a] border border-violet-500/20 rounded-xl max-h-[60vh] overflow-y-auto">
              <div class="prose prose-invert prose-sm max-w-none text-left p-3" v-html="renderMarkdown(msg.contextPreview)"></div>
            </div>
          </div>

          <RetrievalInsights
            v-if="msg.role === 'assistant' && msg.queryId"
            :queryId="msg.queryId"
          />

          <!-- Expand matches toggle -->
          <button 
            v-if="msg.role === 'assistant' && msg.matches && msg.matches.length > 0"
            @click="toggleMatchExpansion(msg.id)"
            class="mt-1 text-[10px] text-indigo-400 hover:text-indigo-300 flex items-center gap-1 font-mono transition-all"
          >
            <Database class="w-3 h-3" />
            <span>{{ expandedMessageId === msg.id ? 'Hide' : 'Show' }} Context Matches ({{ msg.matches.length }})</span>
            <ChevronDown v-if="expandedMessageId !== msg.id" class="w-3 h-3" />
            <ChevronUp v-else class="w-3 h-3" />
          </button>

          <!-- Expandable match details -->
          <div 
            v-if="expandedMessageId === msg.id && msg.matches" 
            class="mt-2 space-y-2 max-w-3xl w-full"
          >
            <div 
              v-for="(match, i) in msg.matches" 
              :key="i"
              class="bg-[#11162d] border border-slate-800 rounded-xl p-3 text-left"
            >
              <div class="flex items-center justify-between text-[10px] font-mono text-slate-400 mb-2">
                <span class="flex items-center gap-1">
                  <Database class="w-3 h-3" />
                  Chunk #{{ match.chunk.index }}
                </span>
                <span class="bg-indigo-600/20 text-indigo-400 px-2 py-0.5 rounded-full">
                  Score: {{ (match.score * 100).toFixed(0) }}%
                </span>
              </div>
              <p class="text-[11px] text-slate-400 line-clamp-4 leading-relaxed font-mono">
                {{ match.chunk.text }}
              </p>
            </div>
          </div>
        </div>
      </div>

      <!-- Gate preview awaiting confirmation: the model has NOT been called -->
      <div v-if="gatePreview" class="flex flex-col items-start max-w-4xl mx-auto">
        <div class="rounded-2xl px-5 py-4 max-w-3xl bg-slate-900/60 border border-amber-500/25 text-slate-300 rounded-tl-none w-full">
          <div class="flex items-center gap-2 text-[10px] text-amber-300 font-mono uppercase tracking-wider border-b border-slate-800 pb-2 mb-3">
            <ShieldCheck class="w-3.5 h-3.5" />
            Context window preview
            <span class="ml-auto normal-case tracking-normal text-[9px] bg-amber-500/10 border border-amber-500/25 rounded px-1.5 py-0.5">
              model not called yet
            </span>
          </div>

          <div class="bg-[#0a0e1a] border border-slate-800 rounded-xl max-h-[60vh] overflow-y-auto">
            <div class="prose prose-invert prose-sm max-w-none text-left p-3" v-html="renderMarkdown(gatePreview.markdown)"></div>
          </div>

          <div class="mt-3 pt-3 border-t border-slate-800 flex items-center justify-end gap-2">
            <button
              type="button"
              @click="emit('cancelSend')"
              class="px-3.5 py-1.5 rounded-lg border border-slate-700 text-[11px] font-mono text-slate-400 hover:text-white hover:border-slate-500 transition-all"
            >
              Cancel
            </button>
            <button
              type="button"
              @click="emit('confirmSend')"
              class="px-3.5 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-[11px] font-mono inline-flex items-center gap-1.5 transition-all shadow-md shadow-indigo-600/20"
            >
              <Send class="w-3 h-3" />
              Send to model
            </button>
          </div>
          <p class="text-[9px] text-slate-500 mt-2">
            This is the exact markdown that will be sent to the model. Nothing has been generated yet.
          </p>
        </div>
      </div>

      <!-- Pipeline progress (orchestration with steps) -->
      <div
        v-if="loading && displaySteps.length > 0"
        class="flex flex-col items-start max-w-4xl mx-auto"
      >
        <PipelineProgress
          :fileName="activeFileName || 'project'"
          :steps="displaySteps"
        />
      </div>

      <!-- Loader while computing answer (simple RAG) -->
      <div
        v-else-if="loading"
        class="flex flex-col items-start max-w-4xl mx-auto"
      >
        <ChatLoading :label="generatingAnswer ? 'Generating answer' : 'Thinking'" />
      </div>
    </div>

    <!-- Chat Input Bar -->
    <div class="p-6 border-t border-slate-800 bg-[#0c101f]/30">
      <div class="max-w-4xl mx-auto">
        
        <!-- File warning if knowledge base is empty -->
        <div v-if="filesCount === 0" class="mb-4 p-3 bg-indigo-950/20 border border-indigo-500/20 rounded-xl text-xs flex items-center gap-3 text-indigo-300 text-left">
          <Info class="w-4 h-4 shrink-0 text-indigo-400" />
          <span>Your active knowledge base has no document references indexed yet. Upload reference material under the **Files** tab first!</span>
        </div>

        <!-- Active project file -->
        <div v-if="activeFileName" class="mb-3 text-left">
          <span class="text-[10px] font-mono px-2 py-1 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 inline-flex items-center gap-1">
            <Database class="w-3 h-3" />
            Project: {{ activeFileName }}
            <button @click="emit('clearAttach')" class="ml-1 text-emerald-500 hover:text-emerald-300">
              <X class="w-3 h-3" />
            </button>
          </span>
        </div>

        <!-- Input area -->
        <form @submit.prevent="onSubmit" class="relative">
          <input
            v-model="chatInput"
            type="text"
            :placeholder="activeFileName ? 'Ask about your project code...' : 'Ask me about APIs, code snippets, or configurations of your indexed files...'"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl py-4 pl-5 pr-14 text-sm focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-all font-mono placeholder-slate-500 text-slate-100"
            :disabled="chatLocked"
          />
          <button
            type="submit"
            class="absolute right-3.5 top-3.5 p-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            :disabled="!chatInput.trim() || chatLocked"
          >
            <Send class="w-4 h-4" />
          </button>
        </form>
        <div class="mt-2 flex items-center justify-between gap-3 text-[10px] text-slate-500">
          <div class="flex items-center gap-3 flex-wrap">
            <button
              type="button"
              @click="emit('update:useRemote', !useRemote)"
              :class="useRemote ? 'bg-indigo-600/20 border-indigo-500/40 text-indigo-300' : 'bg-[#11162d] border-slate-800 text-slate-400 hover:text-white'"
              class="px-2.5 py-1 rounded-lg border text-[10px] font-mono inline-flex items-center gap-1 transition-all"
              :disabled="chatLocked"
              title="Route this answer to the external OpenAI-compatible model"
              aria-label="Toggle external model"
            >
              <Cpu class="w-3.5 h-3.5" />
              {{ useRemote ? 'External Model' : 'Local Model' }}
            </button>
            <label class="cursor-pointer hover:text-indigo-400 transition-colors flex items-center gap-1">
              <input type="file" accept=".zip" hidden @change="onAttachZip" />
              <Paperclip class="w-3.5 h-3.5" />
              <span class="font-mono">Attach .zip</span>
            </label>
            <div class="flex items-center gap-1.5">
              <FolderOpen class="w-3.5 h-3.5 text-slate-500" />
              <select
                :value="activeFileId || ''"
                @change="onSelectFile"
                :disabled="files.length === 0"
                class="bg-[#11162d] border border-slate-800 rounded-lg px-2 py-1 text-[10px] font-mono text-slate-300 focus:outline-none focus:border-indigo-500 disabled:opacity-50 max-w-[200px]"
              >
                <option value="" disabled>Choose from knowledge base…</option>
                <option v-for="f in files" :key="f.id" :value="f.id">{{ f.name }}<template v-if="f.projectId"> · {{ f.projectId }}</template></option>
              </select>
            </div>
          </div>
          <span v-if="gatePreview" class="font-mono shrink-0 text-amber-400/90">Confirm or cancel the preview above.</span>
          <span v-else-if="previewMode === 'gate'" class="font-mono shrink-0 text-amber-400/70">Gate mode — you review the context window before the model runs.</span>
          <span v-else-if="previewMode === 'inline'" class="font-mono shrink-0 text-violet-400/70">Inline — each answer carries a “Context Window” disclosure.</span>
          <span v-else class="font-mono shrink-0">Press Enter to send.</span>
        </div>
      </div>
    </div>

    <EvaluationModal
      :show="showEvaluation"
      :queryId="evalQueryId"
      @close="showEvaluation = false"
    />
  </div>
</template>
