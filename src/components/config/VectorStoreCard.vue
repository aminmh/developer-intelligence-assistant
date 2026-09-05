<script setup lang="ts">
import { Database } from 'lucide-vue-next';
import { RAGConfig } from '../../types';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();
</script>

<template>
  <!-- ============ Vector Store ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-emerald-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Vector Store</h4>
    </div>

    <!-- Qdrant -->
    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
        <Database class="w-4 h-4 text-indigo-400" />
        Qdrant Vector Database
      </h4>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div class="space-y-1.5">
          <div class="flex items-center justify-between">
            <label class="text-xs text-slate-400 font-mono">Server URL</label>
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
            <label class="text-xs text-slate-400 font-mono">API Key</label>
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
      </div>
    </div>
  </div>
</template>
