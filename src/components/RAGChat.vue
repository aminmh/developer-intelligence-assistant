<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { 
  Send, 
  Cpu, 
  Database, 
  Info, 
  ChevronDown, 
  ChevronUp,
  Paperclip,
  X,
} from 'lucide-vue-next';
import { SearchMatch } from '../types';
import PipelineProgress from './PipelineProgress.vue';
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
}

const props = defineProps<{
  messages: Message[];
  loading: boolean;
  filesCount: number;
  activeFileName: string | null;
  orchestrationSteps: PipelineStep[];
}>();

const emit = defineEmits<{
  (e: 'sendMessage', text: string): void;
  (e: 'attachZip', file: File): void;
  (e: 'clearAttach'): void;
}>();

const chatInput = ref('');

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
          </div>

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
        v-if="loading && orchestrationSteps.length > 0"
        class="flex flex-col items-start max-w-4xl mx-auto"
      >
        <PipelineProgress
          :fileName="activeFileName || 'project'"
          :steps="orchestrationSteps"
        />
      </div>

      <!-- Loader while computing answer (simple RAG) -->
      <div
        v-else-if="loading"
        class="flex flex-col items-start max-w-4xl mx-auto"
      >
        <div class="rounded-2xl p-5 bg-slate-900/60 text-slate-300 border border-slate-800 rounded-tl-none max-w-3xl">
          <div class="flex items-center gap-3 font-mono text-xs text-indigo-400">
            <div class="w-2.5 h-2.5 rounded-full bg-indigo-500 animate-ping"></div>
            <span>Thinking...</span>
          </div>
        </div>
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
        <div class="mt-2 flex items-center justify-between text-[10px] text-slate-500">
          <label class="cursor-pointer hover:text-indigo-400 transition-colors flex items-center gap-1">
            <input type="file" accept=".zip" hidden @change="onAttachZip" />
            <Paperclip class="w-3.5 h-3.5" />
            <span class="font-mono">Attach project (.zip)</span>
          </label>
          <span class="font-mono">Press Enter to send.</span>
        </div>
      </div>
    </div>
  </div>
</template>
