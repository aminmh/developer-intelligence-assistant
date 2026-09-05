<script setup lang="ts">
import { ref } from 'vue';
import { Shield, RefreshCw } from 'lucide-vue-next';
import { RAGConfig } from '../../types';
import { makeRequest } from '../../ui/makeRequest';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();

const sandboxStatus = ref<{
  available: boolean;
  backend: string;
  runsc: boolean;
  detail: string;
  dockerHost: string;
  jobsRoot: string;
} | null>(null);
const loadingSandbox = ref(false);

const fetchSandboxStatus = async () => {
  loadingSandbox.value = true;
  try {
    const res = await makeRequest('/api/sandbox/status');
    const data = await res.json();
    if (res.ok && data.success) {
      sandboxStatus.value = data;
    } else {
      sandboxStatus.value = null;
    }
  } catch {
    sandboxStatus.value = null;
  } finally {
    loadingSandbox.value = false;
  }
};
</script>

<template>
  <!-- ============ Sandbox Runner ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-rose-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Sandbox Runner</h4>
    </div>

    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-2xl">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <span class="flex items-center gap-2">
          <Shield class="w-4 h-4 text-rose-400" />
          gVisor Sandbox (runsc)
        </span>
        <button
          type="button"
          @click="fetchSandboxStatus"
          class="text-[10px] text-slate-400 hover:text-indigo-300 flex items-center gap-1 font-mono"
          :disabled="loadingSandbox"
        >
          <RefreshCw class="w-3 h-3" :class="loadingSandbox ? 'animate-spin' : ''" />
          refresh
        </button>
      </h4>

      <div v-if="loadingSandbox" class="flex items-center gap-2 text-[10px] text-slate-400 font-mono">
        <RefreshCw class="w-3.5 h-3.5 animate-spin" />
        probing runner daemon…
      </div>
      <div v-else-if="!sandboxStatus" class="text-[10px] text-slate-500 font-mono">
        Status endpoint unavailable.
      </div>
      <div v-else class="space-y-3">
        <div class="flex items-center justify-between gap-3">
          <span class="text-xs text-slate-400 font-mono flex items-center gap-1.5">
            <Shield class="w-3.5 h-3.5 text-rose-400" />
            Sandbox backend
          </span>
          <span
            class="px-2 py-0.5 rounded-full border text-[10px] font-mono"
            :class="sandboxStatus.available
              ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
              : 'bg-rose-500/10 text-rose-400 border-rose-500/30'"
          >
            {{ sandboxStatus.available ? `ready · ${sandboxStatus.backend}` : sandboxStatus.runsc ? 'runsc not registered' : 'unavailable' }}
          </span>
        </div>
        <div class="flex items-center justify-between gap-3">
          <span class="text-xs text-slate-400 font-mono">Docker host</span>
          <span class="text-[10px] font-mono text-slate-500 max-w-[60%] truncate" :title="sandboxStatus.dockerHost">{{ sandboxStatus.dockerHost }}</span>
        </div>
        <div class="flex items-center justify-between gap-3">
          <span class="text-xs text-slate-400 font-mono">Jobs root</span>
          <span class="text-[10px] font-mono text-slate-500">{{ sandboxStatus.jobsRoot }}</span>
        </div>
        <div class="text-[10px] font-mono text-slate-500 bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-2 break-all">
          {{ sandboxStatus.detail }}
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-1">
        <div class="space-y-1.5">
          <label class="text-xs text-slate-400 font-mono">Docker Host</label>
          <input
            :value="config.sandboxDockerHost"
            @input="updateField('sandboxDockerHost', ($event.target as HTMLInputElement).value)"
            type="text"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-rose-500 text-slate-200"
            placeholder="tcp://sandbox-runner:2375"
          />
          <p class="text-[9px] text-slate-600">Empty = host daemon socket. In the dev stack point this at the nested sandbox-runner daemon.</p>
        </div>

        <div class="space-y-1.5">
          <label class="text-xs text-slate-400 font-mono">Jobs Root</label>
          <input
            :value="config.sandboxJobsRoot"
            @input="updateField('sandboxJobsRoot', ($event.target as HTMLInputElement).value)"
            type="text"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-rose-500 text-slate-200"
            placeholder="/jobs"
          />
          <p class="text-[9px] text-slate-600">Directory on the runner where untrusted project code lives.</p>
        </div>
      </div>
    </div>
  </div>
</template>
