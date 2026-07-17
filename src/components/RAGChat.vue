<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { 
  Send, 
  Cpu, 
  Database, 
  Info, 
  ChevronDown, 
  ChevronUp 
} from 'lucide-vue-next';
import { SearchMatch } from '../types';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  matches?: SearchMatch[];
  llmUsed?: string;
  durationMs?: number;
}

const props = defineProps<{
  messages: Message[];
  loading: boolean;
  filesCount: number;
}>();

const emit = defineEmits<{
  (e: 'sendMessage', text: string): void;
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

      <!-- Loader while computing answer -->
      <div v-if="loading" class="flex flex-col items-start max-w-4xl mx-auto">
        <div class="rounded-2xl p-5 bg-slate-900/60 text-slate-300 border border-slate-800 rounded-tl-none max-w-3xl">
          <div class="flex items-center gap-3 font-mono text-xs text-indigo-400">
            <div class="w-2.5 h-2.5 rounded-full bg-indigo-500 animate-ping"></div>
            <span>RAG Pipeline running: Querying nearest vectors and synthesizing response...</span>
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

        <!-- Input area -->
        <form @submit.prevent="onSubmit" class="relative">
          <input 
            v-model="chatInput" 
            type="text" 
            placeholder="Ask me about APIs, code snippets, or configurations of your indexed files..." 
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
        <div class="mt-2 text-[10px] text-slate-500 text-center font-mono">
          <span>Press Enter to query.</span>
        </div>
      </div>
    </div>
  </div>
</template>
