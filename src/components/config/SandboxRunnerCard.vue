<script setup lang="ts">
import { ref } from 'vue';
import { Shield, RefreshCw, PlugZap, Wrench } from 'lucide-vue-next';
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
  runner?: 'local' | 'remote';
  remoteAlias?: string;
  tunnelState?: string;
} | null>(null);
const loadingSandbox = ref(false);
const testingRemote = ref(false);
const initializingRemote = ref(false);
const remoteResult = ref<{ success: boolean; detail: string; jobsRootWritable?: boolean } | null>(null);
const initJob = ref<{ jobId: string; status: string; detail: string; log: string[] } | null>(null);
let initTimer: ReturnType<typeof setTimeout> | null = null;

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

const testRemote = async () => {
  testingRemote.value = true;
  remoteResult.value = null;
  try {
    const res = await makeRequest('/api/sandbox/remote/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ alias: config.sandboxRemoteSshAlias, jobsRoot: config.sandboxJobsRoot }),
    });
    remoteResult.value = await res.json();
  } catch {
    remoteResult.value = { success: false, detail: 'Could not reach the sandbox service.' };
  } finally { testingRemote.value = false; }
};

const pollInitialization = async (jobId: string) => {
  try {
    const res = await makeRequest(`/api/sandbox/remote/initialize/${encodeURIComponent(jobId)}`);
    const data = await res.json();
    if (data.success) initJob.value = data.job;
    if (data.job?.status === 'running') initTimer = setTimeout(() => pollInitialization(jobId), 1500);
    else { initializingRemote.value = false; fetchSandboxStatus(); }
  } catch {
    initializingRemote.value = false;
  }
};

const initializeRemote = async () => {
  initializingRemote.value = true;
  try {
    const res = await makeRequest('/api/sandbox/remote/initialize', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ alias: config.sandboxRemoteSshAlias, jobsRoot: config.sandboxJobsRoot }),
    });
    const data = await res.json();
    if (data.success) { initJob.value = data.job; pollInitialization(data.job.jobId); }
    else { initJob.value = { jobId: '', status: 'failed', detail: data.error || 'Initialization could not start.', log: [] }; initializingRemote.value = false; }
  } catch {
    initJob.value = { jobId: '', status: 'failed', detail: 'Could not reach the sandbox service.', log: [] };
    initializingRemote.value = false;
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
        <div v-if="sandboxStatus.runner === 'remote'" class="flex items-center justify-between gap-3">
          <span class="text-xs text-slate-400 font-mono">SSH tunnel</span>
          <span class="text-[10px] font-mono text-slate-500">{{ sandboxStatus.remoteAlias || 'no alias' }} · {{ sandboxStatus.tunnelState }}</span>
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
          <label class="text-xs text-slate-400 font-mono">Execution Mode</label>
          <select
            :value="config.sandboxExecutionMode"
            @change="updateField('sandboxExecutionMode', ($event.target as HTMLSelectElement).value as 'local' | 'remote')"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-rose-500 text-slate-200"
          >
            <option value="local">Local Docker runner</option>
            <option value="remote">Remote SSH runner</option>
          </select>
          <p class="text-[9px] text-slate-600">Remote mode only runs after a successful connection test.</p>
        </div>

        <div v-if="config.sandboxExecutionMode === 'remote'" class="space-y-1.5">
          <label class="text-xs text-slate-400 font-mono">SSH Alias</label>
          <input
            :value="config.sandboxRemoteSshAlias"
            @input="updateField('sandboxRemoteSshAlias', ($event.target as HTMLInputElement).value)"
            type="text" class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-rose-500 text-slate-200"
            placeholder="linuxbox"
          />
          <p class="text-[9px] text-slate-600">An alias from the mounted SSH config; no keys or passwords are stored here.</p>
        </div>

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

        <div class="space-y-1.5" :class="config.sandboxExecutionMode === 'remote' ? '' : 'md:col-span-1'">
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

      <div v-if="config.sandboxExecutionMode === 'remote'" class="space-y-3 border-t border-slate-800 pt-4">
        <p class="text-[10px] text-amber-300/80 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
          Enabling remote mode stages projects and runs commands on the selected SSH host. Docker is reached only through a loopback SSH tunnel.
        </p>
        <div class="flex flex-wrap gap-2">
          <button type="button" @click="testRemote" :disabled="testingRemote || initializingRemote" class="inline-flex items-center gap-1.5 bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-100 px-3 py-2 rounded-lg text-[10px] font-mono">
            <PlugZap class="w-3.5 h-3.5" :class="testingRemote ? 'animate-pulse' : ''" /> Test connection
          </button>
          <button type="button" @click="initializeRemote" :disabled="initializingRemote" class="inline-flex items-center gap-1.5 bg-rose-600 hover:bg-rose-500 disabled:opacity-50 text-white px-3 py-2 rounded-lg text-[10px] font-mono">
            <Wrench class="w-3.5 h-3.5" :class="initializingRemote ? 'animate-spin' : ''" /> Initialize remote sandbox
          </button>
        </div>
        <div v-if="remoteResult" class="text-[10px] font-mono rounded-lg px-3 py-2 border" :class="remoteResult.success ? 'text-emerald-300 border-emerald-500/30 bg-emerald-500/5' : 'text-amber-200 border-amber-500/30 bg-amber-500/5'">
          {{ remoteResult.detail }}
        </div>
        <div v-if="initJob" class="text-[10px] font-mono rounded-lg px-3 py-2 border border-slate-700 bg-slate-900/60 text-slate-300 space-y-1">
          <div>{{ initJob.status }} · {{ initJob.detail }}</div>
          <div v-for="line in initJob.log" :key="line" class="text-slate-500">{{ line }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
