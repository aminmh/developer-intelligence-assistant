<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import MarkdownIt from 'markdown-it';
import { Shield, RefreshCw, PlugZap, Wrench, BookOpen, ChevronDown } from 'lucide-vue-next';
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
const showRemoteGuide = ref(false);
const remoteMonitor = ref<{
  success: boolean;
  detail?: string;
  cpuPercent?: number;
  ramTotalBytes?: number;
  ramFreeBytes?: number;
  ramUsedBytes?: number;
  networkDownloadBytesPerSec?: number;
  networkUploadBytesPerSec?: number;
} | null>(null);
const loadingRemoteMonitor = ref(false);
let initTimer: ReturnType<typeof setTimeout> | null = null;
let monitorTimer: ReturnType<typeof setInterval> | null = null;

const remoteGuide = new MarkdownIt({ html: false, linkify: true, breaks: true }).render(`
## Remote sandbox developer guide

### 1. Mount SSH configuration

Set an **absolute** local path in \`api/.env\`, then recreate \`python-api\`:

\`\`\`env
SANDBOX_SSH_DIR=/Users/you/.ssh
\`\`\`

The directory must contain the SSH alias configuration, its private key, and \`known_hosts\`. The container mounts it read-only; the UI never stores keys or passwords.

### 2. Configure the remote host

#### Create the local SSH alias

On the developer machine, add an alias to \`~/.ssh/config\`. The alias—not a hostname, username, or password—is what goes in the Sandbox **SSH Alias** field:

\`\`\`sshconfig
Host linuxbox
  HostName 192.0.2.10
  User amin
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking yes
\`\`\`

Use the remote host's actual DNS name or IP for \`HostName\`. Ensure \`known_hosts\` is populated before using the UI:

\`\`\`sh
ssh linuxbox 'echo connected'
\`\`\`

#### Authorize the developer machine's public key

Copy the public half of the key from the developer machine to the remote SSH account:

\`\`\`sh
ssh-copy-id -i ~/.ssh/id_ed25519.pub linuxbox
\`\`\`

Or append the contents of \`~/.ssh/id_ed25519.pub\` to the remote user's \`~/.ssh/authorized_keys\`, then apply:

\`\`\`sh
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
\`\`\`

#### Require passwordless public-key SSH

The sandbox uses \`BatchMode=yes\`, so it cannot answer a password or key-passphrase prompt. Use a dedicated, unencrypted deployment key or make an SSH agent available to the container. On the remote machine, an administrator can enforce public-key-only login in \`/etc/ssh/sshd_config.d/dev-assistant.conf\`:

\`\`\`text
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
\`\`\`

Validate and reload the SSH daemon:

\`\`\`sh
sudo sshd -t && sudo systemctl reload ssh
\`\`\`

Use an SSH-config alias such as \`linuxbox\`, select **Remote SSH runner**, and set the jobs root to:

\`\`\`text
/var/lib/dev-assistant-sandbox/jobs
\`\`\`

The SSH account needs non-interactive SSH, non-interactive sudo for bootstrap, and Docker-group membership:

\`\`\`sh
ssh linuxbox 'sudo -n true && docker info'
\`\`\`

The jobs root must be owned by \`root:docker\` with mode \`0770\` so Docker-group users can stage disposable projects.

### 3. Validate and enable

Click **Test connection**. A ready result requires SSH, Docker, \`runsc\`, and a writable jobs root. Remote mode never silently falls back to the local runner.

### Troubleshooting

- **Permission denied (publickey):** add the mounted key's public half to the remote user's \`~/.ssh/authorized_keys\`.
- **sudo: a password is required:** configure passwordless sudo for the bootstrap, or install/configure Docker and gVisor manually.
- **runsc not registered:** install gVisor on the remote host and register \`runsc\` with Docker, then restart Docker.
- **Jobs root not writable:** create \`/var/lib/dev-assistant-sandbox/jobs\` as \`root:docker\` mode \`0770\`.
- **gVisor download errors:** allow the host to fetch gVisor through your approved package or artifact source; the initializer reports external download failures directly.
`);

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

const fetchRemoteMonitor = async () => {
  if (config.sandboxExecutionMode !== 'remote' || loadingRemoteMonitor.value) return;
  loadingRemoteMonitor.value = true;
  try {
    const res = await makeRequest('/api/sandbox/remote/monitor');
    remoteMonitor.value = await res.json();
  } catch {
    remoteMonitor.value = { success: false, detail: 'Remote monitor is unavailable.' };
  } finally {
    loadingRemoteMonitor.value = false;
  }
};

const refreshRunner = () => {
  fetchSandboxStatus();
  fetchRemoteMonitor();
};

const formatBytes = (bytes = 0) => {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length);
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index - 1]}`;
};

onMounted(() => {
  refreshRunner();
  monitorTimer = setInterval(fetchRemoteMonitor, 5000);
});

onUnmounted(() => {
  if (monitorTimer) clearInterval(monitorTimer);
  if (initTimer) clearTimeout(initTimer);
});

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

    <div class="grid grid-cols-1 xl:grid-cols-[minmax(0,2fr)_minmax(280px,1fr)] gap-4 items-start max-w-6xl">
    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <span class="flex items-center gap-2">
          <Shield class="w-4 h-4 text-rose-400" />
          gVisor Sandbox (runsc)
        </span>
        <button
          type="button"
          @click="refreshRunner"
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

        <div class="space-y-1.5 md:col-span-2">
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

        <div class="space-y-1.5 md:col-span-2">
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

        <div class="border border-slate-800 rounded-xl overflow-hidden">
          <button
            type="button"
            class="w-full flex items-center justify-between gap-3 px-3 py-2.5 text-left text-[10px] font-mono text-slate-300 hover:bg-slate-800/50"
            :aria-expanded="showRemoteGuide"
            @click="showRemoteGuide = !showRemoteGuide"
          >
            <span class="inline-flex items-center gap-1.5"><BookOpen class="w-3.5 h-3.5 text-indigo-300" /> Developer setup guide</span>
            <ChevronDown class="w-3.5 h-3.5 transition-transform" :class="showRemoteGuide ? 'rotate-180' : ''" />
          </button>
          <div v-if="showRemoteGuide" class="prose prose-invert prose-sm max-w-none border-t border-slate-800 px-4 py-3 text-slate-400 [&_h2]:text-sm [&_h2]:text-slate-200 [&_h3]:text-xs [&_h3]:text-slate-300 [&_code]:text-indigo-200 [&_pre]:bg-[#11162d] [&_pre]:border [&_pre]:border-slate-800 [&_pre]:text-[10px]" v-html="remoteGuide"></div>
        </div>
      </div>
    </div>

    <aside v-if="config.sandboxExecutionMode === 'remote'" class="bg-slate-900/30 border border-slate-800 rounded-2xl p-5 space-y-4">
      <div class="flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <div>
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider">Remote host monitor</h4>
          <p class="mt-0.5 text-[9px] font-mono text-slate-600">Refreshes every 5 seconds</p>
        </div>
        <button type="button" @click="fetchRemoteMonitor" :disabled="loadingRemoteMonitor" class="text-slate-400 hover:text-indigo-300 disabled:opacity-50">
          <RefreshCw class="w-3.5 h-3.5" :class="loadingRemoteMonitor ? 'animate-spin' : ''" />
          <span class="sr-only">Refresh remote monitor</span>
        </button>
      </div>

      <div v-if="loadingRemoteMonitor && !remoteMonitor" class="flex items-center gap-2 text-[10px] font-mono text-slate-500">
        <RefreshCw class="w-3.5 h-3.5 animate-spin" /> Sampling remote host…
      </div>
      <div v-else-if="!remoteMonitor || !remoteMonitor.success" class="text-[10px] font-mono text-amber-200 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
        {{ remoteMonitor?.detail || 'Remote monitor is waiting for a successful connection.' }}
      </div>
      <div v-else class="space-y-4">
        <div class="space-y-1.5">
          <div class="flex items-center justify-between text-[10px] font-mono">
            <span class="text-slate-400">CPU usage</span>
            <span class="text-emerald-300">{{ remoteMonitor.cpuPercent?.toFixed(1) }}%</span>
          </div>
          <div class="h-1.5 rounded-full bg-slate-800 overflow-hidden"><div class="h-full rounded-full bg-emerald-400" :style="{ width: `${Math.min(remoteMonitor.cpuPercent || 0, 100)}%` }"></div></div>
        </div>
        <div class="space-y-1.5">
          <div class="flex items-center justify-between text-[10px] font-mono"><span class="text-slate-400">RAM allocated</span><span class="text-slate-200">{{ formatBytes(remoteMonitor.ramUsedBytes) }} / {{ formatBytes(remoteMonitor.ramTotalBytes) }}</span></div>
          <div class="flex items-center justify-between text-[10px] font-mono"><span class="text-slate-400">RAM free</span><span class="text-sky-300">{{ formatBytes(remoteMonitor.ramFreeBytes) }}</span></div>
        </div>
        <div class="grid grid-cols-2 gap-2 border-t border-slate-800 pt-3 text-[10px] font-mono">
          <div><div class="text-slate-500">UPLOAD</div><div class="mt-1 text-violet-300">{{ formatBytes(remoteMonitor.networkUploadBytesPerSec) }}/s</div></div>
          <div><div class="text-slate-500">DOWNLOAD</div><div class="mt-1 text-cyan-300">{{ formatBytes(remoteMonitor.networkDownloadBytesPerSec) }}/s</div></div>
        </div>
      </div>
    </aside>
    </div>
  </div>
</template>
