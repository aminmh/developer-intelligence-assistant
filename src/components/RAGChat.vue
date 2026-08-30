<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
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
  X,
} from 'lucide-vue-next';
import { SearchMatch, UploadedFile } from '../types';
import PipelineProgress from './PipelineProgress.vue';
import ChatLoading from './ChatLoading.vue';
import EvaluationModal from './EvaluationModal.vue';
import RetrievalInsights from './RetrievalInsights.vue';
import SysMonitor from './SysMonitor.vue';
import type { PipelineStep } from './PipelineProgress.vue';

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'orchestration';
  content: string;
  timestamp: string;
  matches?: SearchMatch[];
  llmUsed?: string;
  durationMs?: number;
  orchestration?: any;
  query?: string;
  queryId?: string;
  routeName?: string;
}

const props = defineProps<{
  messages: Message[];
  loading: boolean;
  filesCount: number;
  files: UploadedFile[];
  activeFileName: string | null;
  orchestrationSteps: PipelineStep[];
  generatingAnswer: boolean;
  useRemote: boolean;
}>();

const emit = defineEmits<{
  (e: 'sendMessage', text: string): void;
  (e: 'attachZip', file: File): void;
  (e: 'selectAttachment', fileName: string): void;
  (e: 'clearAttach'): void;
  (e: 'feedback', query: string, route: string): void;
  (e: 'update:useRemote', value: boolean): void;
}>();

const ROUTES = ['code_explain', 'code_refactor', 'debug', 'search', 'knowledge', 'chat'];

const chatInput = ref('');

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
  if (chatInput.value.trim() && !props.loading) {
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
</script>

<template>
  <div class="flex-1 flex flex-col min-h-0">
    <!-- Messages area -->
    <div id="chat-scroll-container" class="flex-1 overflow-y-auto p-6 space-y-4 scroll-smooth">
      
      <!-- Messages list -->
      <div class="max-w-4xl mx-auto space-y-4">
        <SysMonitor />
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
            <div class="prose prose-invert prose-sm max-w-none text-left" v-if="msg.role === 'assistant'" v-html="msg.content.replace(/\n/g, '<br>')"></div>
            <div class="text-sm text-left font-mono leading-relaxed text-slate-100" v-else>{{ msg.content }}</div>
            
            <!-- Assistant message metadata -->
            <div v-if="msg.role === 'assistant'" class="mt-3 pt-3 border-t border-slate-800 flex items-center justify-between text-[10px] text-slate-500 font-mono">
              <span>{{ msg.timestamp }}</span>
              <span v-if="msg.llmUsed" class="flex items-center gap-1">
                <Cpu class="w-3 h-3" />
                {{ msg.llmUsed }}
              </span>
              <span v-if="msg.durationMs">{{ msg.durationMs }}ms</span>
            </div>

            <!-- Orchestration info -->
            <div v-if="msg.orchestration" class="mt-3 pt-3 border-t border-slate-800 text-left">
              <div class="text-[10px] text-emerald-400 font-mono flex items-center gap-1 mb-1">
                <Database class="w-3 h-3" />
                Orchestration — {{ msg.orchestration.route || 'tools' }}
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
            :disabled="loading"
          />
          <button
            type="submit"
            class="absolute right-3.5 top-3.5 p-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            :disabled="!chatInput.trim() || loading"
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
              :disabled="loading"
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
                :value="activeFileName || ''"
                @change="onSelectFile"
                :disabled="files.length === 0"
                class="bg-[#11162d] border border-slate-800 rounded-lg px-2 py-1 text-[10px] font-mono text-slate-300 focus:outline-none focus:border-indigo-500 disabled:opacity-50 max-w-[200px]"
              >
                <option value="" disabled>Choose from knowledge base…</option>
                <option v-for="f in files" :key="f.id" :value="f.name">{{ f.name }}</option>
              </select>
            </div>
          </div>
          <span class="font-mono shrink-0">Press Enter to send.</span>
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
