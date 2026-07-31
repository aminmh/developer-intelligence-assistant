<script setup lang="ts">
import { 
  Sliders, 
  CpuIcon, 
  Database,
  SlidersHorizontal
} from 'lucide-vue-next';
import { RAGConfig } from '../types';

const props = defineProps<{
  config: RAGConfig;
}>();

const emit = defineEmits<{
  (e: 'saveConfig'): void;
}>();

const updateField = <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => {
  props.config[key] = value;
};
</script>

<template>
  <div class="flex-1 overflow-y-auto p-6 space-y-6 text-left">
    <div class="max-w-4xl mx-auto space-y-6">
      
      <div class="bg-slate-900/40 rounded-2xl border border-slate-800/80 p-6">
        <h3 class="text-lg font-bold text-white flex items-center gap-2">
          <Sliders class="w-5 h-5 text-indigo-400" />
          RAG Pipeline Configuration
        </h3>
        <p class="text-xs text-slate-400 mt-1">Configure your local or cloud LLMs and adjust Qdrant VectorDB connection details.</p>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        
        <!-- Column 1: Model Selections -->
        <div class="space-y-6">
          
          <!-- Card 1: Large Language Model Configuration -->
          <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
            <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
              <CpuIcon class="w-4 h-4 text-indigo-400" />
              Large Language Model (LLM)
            </h4>

            <div class="space-y-4">
              <!-- Mode Select -->
              <div class="space-y-1.5">
                <label class="text-xs text-slate-400 font-medium">Model Connection Mode</label>
                <div class="grid grid-cols-2 gap-2">
                  <button 
                    type="button" 
                    @click="updateField('llmMode', 'gemini')"
                    class="px-4 py-2.5 rounded-xl border text-xs font-semibold transition-all flex flex-col items-center justify-center gap-1"
                    :class="config.llmMode === 'gemini' ? 'border-indigo-500 bg-indigo-500/10 text-white' : 'border-slate-800 bg-[#0c101f] text-slate-400 hover:text-slate-300'"
                  >
                    <span class="text-xs">Gemini (Cloud)</span>
                    <span class="text-[9px] text-slate-500 font-normal">Active out-of-the-box</span>
                  </button>
                  <button 
                    type="button" 
                    @click="updateField('llmMode', 'local')"
                    class="px-4 py-2.5 rounded-xl border text-xs font-semibold transition-all flex flex-col items-center justify-center gap-1"
                    :class="config.llmMode === 'local' ? 'border-emerald-500 bg-emerald-500/10 text-white' : 'border-slate-800 bg-[#0c101f] text-slate-400 hover:text-slate-300'"
                  >
                    <span class="text-xs">Ollama (Local)</span>
                    <span class="text-[9px] text-slate-500 font-normal">Requires local model URL</span>
                  </button>
                </div>
              </div>

              <!-- Local LLM Configurations -->
              <div v-if="config.llmMode === 'local'" class="space-y-3 pt-2">
                <div class="space-y-1.5">
                  <label class="text-xs text-slate-400 font-mono">Local LLM Host URL</label>
                  <input 
                    :value="config.localLlmUrl" 
                    @input="updateField('localLlmUrl', ($event.target as HTMLInputElement).value)"
                    type="text" 
                    class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
                    placeholder="e.g. http://localhost:11434"
                  />
                </div>
                <div class="space-y-1.5">
                  <label class="text-xs text-slate-400 font-mono">Local Model Name</label>
                  <input 
                    :value="config.localLlmModel" 
                    @input="updateField('localLlmModel', ($event.target as HTMLInputElement).value)"
                    type="text" 
                    class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
                    placeholder="e.g. llama3, mistral, codegemma"
                  />
                </div>
              </div>

              <div v-else class="p-3 bg-[#11162d] border border-slate-800 rounded-xl text-xs text-indigo-300 font-mono leading-relaxed">
                🌟 Running **Gemini 3.5 Flash** for rapid reasoning and responses. Ensure your **GEMINI_API_KEY** is configured.
              </div>
            </div>
          </div>
        </div>

        <!-- Column 2: Chunking & Vector DB Params -->
        <div class="space-y-6">
          
          <!-- Card 3: Chunking Specifications -->
          <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-5">
            <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
              <SlidersHorizontal class="w-4 h-4 text-indigo-400" />
              Document Chunking Specifications
            </h4>

            <div class="space-y-4">
              <!-- Chunk Size -->
              <div class="space-y-2">
                <div class="flex items-center justify-between text-xs">
                  <span class="text-slate-400">Chunk Size (Characters)</span>
                  <span class="font-mono text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded font-bold">{{ config.chunkSize }}</span>
                </div>
                <input 
                  type="range" 
                  min="100" 
                  max="2000" 
                  step="50" 
                  :value="config.chunkSize"
                  @input="updateField('chunkSize', parseInt(($event.target as HTMLInputElement).value))"
                  class="w-full h-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500" 
                />
                <p class="text-[10px] text-slate-500 leading-normal">Defines the maximum size of each semantic segment sliced during text processing.</p>
              </div>

              <!-- Chunk Overlap -->
              <div class="space-y-2">
                <div class="flex items-center justify-between text-xs">
                  <span class="text-slate-400">Chunk Overlap</span>
                  <span class="font-mono text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded font-bold">{{ config.chunkOverlap }}</span>
                </div>
                <input 
                  type="range" 
                  min="0" 
                  max="400" 
                  step="10" 
                  :value="config.chunkOverlap"
                  @input="updateField('chunkOverlap', parseInt(($event.target as HTMLInputElement).value))"
                  class="w-full h-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500" 
                />
                <p class="text-[10px] text-slate-500 leading-normal">Maintains continuity and prevents loss of context at chunk borders by overlapping sequential tokens.</p>
              </div>
            </div>
          </div>

          <!-- Card 4: Qdrant Vector DB specifications -->
          <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
            <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
              <Database class="w-4 h-4 text-indigo-400" />
              Qdrant Vector Database
            </h4>

            <div class="space-y-3">
              <div class="space-y-1.5">
                <div class="flex items-center justify-between">
                  <label class="text-xs text-slate-400 font-mono">Qdrant Server URL</label>
                  <span class="text-[9px] text-slate-500">Optional</span>
                </div>
                <input 
                  :value="config.qdrantUrl" 
                  @input="updateField('qdrantUrl', ($event.target as HTMLInputElement).value)"
                  type="text" 
                  class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
                  placeholder="e.g. https://your-cluster.qdrant.tech"
                />
              </div>

              <div class="space-y-1.5">
                <div class="flex items-center justify-between">
                  <label class="text-xs text-slate-400 font-mono">Qdrant API Key</label>
                  <span class="text-[9px] text-slate-500">Optional</span>
                </div>
                <input 
                  :value="config.qdrantApiKey" 
                  @input="updateField('qdrantApiKey', ($event.target as HTMLInputElement).value)"
                  type="password" 
                  class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
                  placeholder="Enter Qdrant cloud token"
                />
              </div>

              <p class="text-[10px] text-slate-500 leading-relaxed">
                If left blank, the integrated sandbox automatically provisions an in-memory database index for seamless, instant testing.
              </p>
            </div>
          </div>

          <!-- Card 5: Smart Model Parameters (Temperature / TopP) -->
          <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
            <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
              <SlidersHorizontal class="w-4 h-4 text-indigo-400" />
              Model Temperature and Randomness
            </h4>

            <div class="space-y-4">
              <!-- Temperature -->
              <div class="space-y-2">
                <div class="flex items-center justify-between text-xs">
                  <span class="text-slate-400">LLM Temperature</span>
                  <span class="font-mono text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded font-bold">{{ config.temperature }}</span>
                </div>
                <input 
                  type="range" 
                  min="0" 
                  max="1.2" 
                  step="0.05" 
                  :value="config.temperature"
                  @input="updateField('temperature', parseFloat(($event.target as HTMLInputElement).value))"
                  class="w-full h-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500" 
                />
              </div>

              <!-- Top P -->
              <div class="space-y-2">
                <div class="flex items-center justify-between text-xs">
                  <span class="text-slate-400">Top-P Sampling</span>
                  <span class="font-mono text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded font-bold">{{ config.topP }}</span>
                </div>
                <input 
                  type="range" 
                  min="0" 
                  max="1" 
                  step="0.05" 
                  :value="config.topP"
                  @input="updateField('topP', parseFloat(($event.target as HTMLInputElement).value))"
                  class="w-full h-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500" 
                />
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Action Footer -->
      <div class="pt-4 border-t border-slate-800 flex justify-end">
        <button 
          type="button" 
          @click="emit('saveConfig')"
          class="bg-indigo-600 hover:bg-indigo-700 text-white font-semibold text-xs px-6 py-3 rounded-xl transition-all shadow-md shadow-indigo-600/10"
        >
          Apply & Save Configurations
        </button>
      </div>
    </div>
  </div>
</template>
