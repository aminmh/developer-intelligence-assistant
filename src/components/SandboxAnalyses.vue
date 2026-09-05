<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import {
  FlaskConical,
  Play,
  Save,
  Trash2,
  FolderArchive,
  Cpu,
  CheckCircle2,
  AlertTriangle,
  Loader2,
  ChevronDown,
  ChevronRight,
  FileCode2,
  Terminal,
  History,
  Copy,
  Check,
} from 'lucide-vue-next';
import { UploadedFile, AnalysisSession, SandboxProbe, SandboxResult } from '../types';
import PipelineProgress, { PipelineStep } from './PipelineProgress.vue';

const props = defineProps<{ files: UploadedFile[] }>();

const apiBaseUrl = import.meta.env.VITE_API_URL || '';
const makeRequest = async (path: string, options: RequestInit = {}) => {
  const base = apiBaseUrl.replace(/\/+$/, '');
  return fetch(base ? `${base}${path}` : path, options);
};

// --- Sandbox runner status chip -------------------------------------------

const sandboxStatus = ref<{ available: boolean; backend: string; runsc: boolean; detail: string; dockerHost: string; jobsRoot: string } | null>(null);

const fetchSandboxStatus = async () => {
  try {
    const res = await makeRequest('/api/sandbox/status');
    const data = await res.json();
    sandboxStatus.value = res.ok && data.success ? data : null;
  } catch {
    sandboxStatus.value = null;
  }
};

// --- Selected project + run state ------------------------------------------

const projectFiles = computed(() => props.files.filter((f) => f.name.toLowerCase().endsWith('.zip')));
const selectedFile = ref<string>('');

const running = ref(false);
const jobId = ref<string | null>(null);
const steps = ref<PipelineStep[]>([]);
const sandboxResult = ref<SandboxResult | null>(null);
const profileSummary = ref<string>('');
const lastFramework = ref<string>('php');
const runError = ref<string>('');
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let stopped = false;

const resetRun = () => {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
  stopped = true;
  running.value = false;
};

const mapSteps = (raw: any[]): PipelineStep[] =>
  (raw ?? []).map((s: any) => ({
    id: s.key || s.id,
    label: s.label || '',
    status: s.status || 'pending',
    detail: s.detail ?? null,
  }));

const probeTitle = (p: SandboxProbe) => `${p.name}: ${p.status}${p.exit_code != null ? ` (exit ${p.exit_code})` : ''}`;

const startAnalyze = async () => {
  if (!selectedFile.value || running.value) return;
  resetRun();
  runError.value = '';
  sandboxResult.value = null;
  profileSummary.value = '';
  stopped = false;
  running.value = true;
  steps.value = [];
  jobId.value = null;
  try {
    const res = await makeRequest('/api/project/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: selectedFile.value }),
    });
    const data = await res.json();
    if (!res.ok || !data.job_id) {
      throw new Error(data.error || data.detail || 'Failed to start analysis.');
    }
    jobId.value = data.job_id;
    pollJob(data.job_id);
  } catch (err: any) {
    runError.value = String(err.message || err);
    running.value = false;
  }
};

const pollJob = async (id: string) => {
  for (let attempt = 0; attempt < 240 && !stopped; attempt++) {
    try {
      const res = await makeRequest(`/api/project/analyze/${id}/status`);
      const status = await res.json();
      if (status.steps && status.steps.length) {
        steps.value = mapSteps(status.steps);
      }
      if (status.status === 'done' || status.status === 'error' || status.status === 'failed') {
        if (status.status === 'done') {
          sandboxResult.value = status.sandbox ?? null;
          const profile = status.project_profile;
          lastFramework.value = profile?.framework || 'php';
          profileSummary.value = profile
            ? `${(profile.framework || 'unknown')}${profile.version ? ` ${profile.version}` : ''} — ${(profile.summary || '').slice(0, 220)}`
            : '';
        } else {
          runError.value = status.error || `Job ended with status ${status.status}.`;
        }
        running.value = false;
        return;
      }
    } catch (err: any) {
      // transient — keep polling until timeout
      console.warn('poll error', err.message);
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  if (!stopped) {
    runError.value = 'Analysis timed out after ~6 minutes.';
    running.value = false;
  }
};

// --- Save / list / delete analyses -----------------------------------------

const analyses = ref<AnalysisSession[]>([]);
const label = ref('');
const saving = ref(false);
const notice = ref<{ ok: boolean; text: string } | null>(null);
const expandedId = ref<string | null>(null);

const listAnalyses = async () => {
  try {
    const res = await makeRequest('/api/analysis?limit=50');
    const data = await res.json();
    analyses.value = res.ok && (data.success || data.analyses) ? (data.analyses ?? []) : [];
  } catch {
    analyses.value = [];
  }
};

const saveAnalysis = async () => {
  if (!jobId.value || saving.value) return;
  saving.value = true;
  notice.value = null;
  try {
    const res = await makeRequest('/api/analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: jobId.value, label: label.value.trim() || undefined }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.error || data.detail || 'Save failed.');
    }
    notice.value = { ok: true, text: `Saved — ${data.analysis?.ingest?.ingested ?? 0} knowledge chunks indexed, ${data.analysis?.link?.linked ?? 0} graph links.` };
    label.value = '';
    await listAnalyses();
  } catch (err: any) {
    notice.value = { ok: false, text: String(err.message || err) };
  } finally {
    saving.value = false;
  }
};

const toggleExpand = (id: string) => {
  expandedId.value = expandedId.value === id ? null : id;
};

const deleteAnalysis = async (id: string) => {
  try {
    const res = await makeRequest(`/api/analysis/${id}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.error || data.detail || 'Delete failed.');
    }
    if (expandedId.value === id) expandedId.value = null;
    await listAnalyses();
  } catch (err: any) {
    notice.value = { ok: false, text: String(err.message || err) };
  }
};

onMounted(() => {
  fetchSandboxStatus();
  listAnalyses();
});

// --- Shell: run any command in the staged project ---------------------------

type ShellOutcome = {
  at?: string;
  success: boolean;
  status?: string;
  exit_code?: number | null;
  stdout?: string;
  command?: string;
  image?: string;
  working_dir?: string;
  reason?: string;
  error?: string;
};

const shellCommand = ref<string>('');
const shellRunning = ref(false);
const shellOut = ref<ShellOutcome | null>(null);
const shellHistory = ref<ShellOutcome[]>([]);

const shellEnabled = computed(
  () => !!selectedFile.value && !!sandboxResult.value?.staged,
);

const runShell = async () => {
  const cmd = shellCommand.value.trim();
  if (!cmd || !shellEnabled.value || shellRunning.value) return;
  shellRunning.value = true;
  shellOut.value = null;
  try {
    const res = await makeRequest('/api/sandbox/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: selectedFile.value,
        command: cmd,
        framework: lastFramework.value,
      }),
    });
    const data = await res.json();
    const outcome: ShellOutcome = data ?? { success: false, error: 'no response' };
    shellOut.value = outcome;
    shellHistory.value.unshift({ at: new Date().toISOString(), ...outcome });
    if (shellHistory.value.length > 20) shellHistory.value.pop();
  } catch (err: any) {
    shellOut.value = { success: false, error: String(err.message || err) };
  } finally {
    shellRunning.value = false;
  }
};

const runFromHistory = (outcome: ShellOutcome) => {
  if (outcome.command) {
    shellCommand.value = outcome.command;
    runShell();
  }
};

// --- Copy shell result -----------------------------------------------------

const shellCopied = ref(false);
let shellCopyTimer: ReturnType<typeof setTimeout> | null = null;

const shellOutputText = (): string =>
  shellOut.value?.stdout || shellOut.value?.reason || shellOut.value?.error || '';

const copyShellOutput = async () => {
  const text = shellOutputText();
  if (!text) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // Non-secure-context fallback.
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    shellCopied.value = true;
    if (shellCopyTimer) clearTimeout(shellCopyTimer);
    shellCopyTimer = setTimeout(() => (shellCopied.value = false), 1600);
  } catch {
    /* clipboard unavailable — ignore */
  }
};
</script>

<template>
  <div class="flex-1 overflow-y-auto p-6 space-y-6 text-left">
    <div class="max-w-4xl mx-auto space-y-6">

      <!-- Sandbox status chip -->
      <div class="bg-slate-900/30 border border-slate-800 rounded-2xl px-5 py-3 flex items-center justify-between gap-3">
        <div class="flex items-center gap-2.5">
          <Cpu class="w-4 h-4 text-indigo-400" />
          <span class="text-xs font-semibold text-slate-300">Sandbox runner</span>
          <span
            v-if="sandboxStatus"
            class="text-[10px] font-mono px-2 py-0.5 rounded-full"
            :class="sandboxStatus.available ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'"
          >
            {{ sandboxStatus.available ? 'available' : 'unavailable' }}
          </span>
          <span v-else class="text-[10px] text-slate-500 font-mono">checking…</span>
        </div>
        <div v-if="sandboxStatus?.available" class="text-[10px] font-mono text-slate-500 truncate">
          {{ sandboxStatus.backend }} · runsc:{{ sandboxStatus.runsc ? 'yes' : 'no' }}
        </div>
      </div>

      <!-- Run card -->
      <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
        <h4 class="text-xs font-bold tracking-wider uppercase text-slate-400 flex items-center gap-2">
          <FlaskConical class="w-4 h-4 text-indigo-400" />
          Run project in sandbox
        </h4>

        <div class="flex items-end gap-3 flex-wrap">
          <div class="flex-1 min-w-[240px]">
            <label class="block text-[10px] text-slate-500 font-mono mb-1.5 uppercase tracking-wider">Uploaded project (ZIP)</label>
            <select
              v-model="selectedFile"
              class="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-indigo-500/60"
            >
              <option value="" disabled>Select a project archive…</option>
              <option v-for="f in projectFiles" :key="f.id" :value="f.name">{{ f.name }}</option>
            </select>
            <p v-if="projectFiles.length === 0" class="text-[10px] text-amber-400/80 mt-1">
              No project ZIPs uploaded yet — add one under Knowledge Base (Files).
            </p>
          </div>
          <button
            @click="startAnalyze"
            :disabled="running || !selectedFile"
            class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold transition-all disabled:opacity-40"
            :class="running ? 'bg-slate-800 text-slate-400 cursor-wait' : 'bg-indigo-600 text-white hover:bg-indigo-500 shadow-md shadow-indigo-600/20'"
          >
            <Loader2 v-if="running" class="w-3.5 h-3.5 animate-spin" />
            <Play v-else class="w-3.5 h-3.5" />
            {{ running ? 'Running…' : 'Analyze in Sandbox' }}
          </button>
        </div>

        <!-- Probe templates hint -->
        <p class="text-[10px] text-slate-600 leading-relaxed">
          Runs <span class="font-mono text-slate-500">composer install</span> (networked DinD) then isolated
          <span class="font-mono text-slate-500">runsc</span> probes:
          <span class="font-mono text-slate-500">php artisan route:list --json</span>,
          <span class="font-mono text-slate-500">php artisan schedule:list</span>,
          <span class="font-mono text-slate-500">./vendor/bin/phpunit --no-coverage</span>.
        </p>

        <div v-if="runError" class="rounded-xl bg-rose-500/10 border border-rose-500/30 px-4 py-3 text-xs text-rose-300 flex items-start gap-2">
          <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
          <span>{{ runError }}</span>
        </div>

        <!-- Live pipeline -->
        <PipelineProgress
          v-if="running && steps.length"
          :fileName="selectedFile"
          :steps="steps"
        />

        <!-- Result -->
        <div v-if="sandboxResult" class="rounded-2xl bg-slate-950/60 border border-slate-800 p-4 space-y-3">
          <div class="flex items-center gap-2">
            <CheckCircle2 class="w-4 h-4 text-emerald-400" />
            <span class="text-xs font-semibold text-slate-200">Sandbox run complete</span>
            <span class="ml-auto text-[10px] font-mono text-slate-500">{{ sandboxResult.runtime_image }}</span>
          </div>

          <p v-if="profileSummary" class="text-[11px] text-slate-400 leading-relaxed">{{ profileSummary }}</p>

          <div class="grid grid-cols-2 gap-2 text-[10px] font-mono">
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">install</div>
              <div v-if="sandboxResult.install">
                <span :class="sandboxResult.install.status === 'done' ? 'text-emerald-400' : 'text-rose-400'">{{ sandboxResult.install.status }}</span>
                <span v-if="sandboxResult.install.exit_code != null" class="text-slate-500"> · exit {{ sandboxResult.install.exit_code }}</span>
                <div v-if="sandboxResult.install.error || sandboxResult.install.reason" class="text-rose-300/80 mt-1">{{ sandboxResult.install.error || sandboxResult.install.reason }}</div>
              </div>
              <div v-else class="text-slate-600">n/a</div>
            </div>
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">probes</div>
              <div class="space-y-0.5">
                <div v-for="p in (sandboxResult.probes || [])" :key="p.name">
                  <span
                    class="mr-1.5"
                    :class="p.status === 'done' ? 'text-emerald-400' : p.status === 'skipped' ? 'text-slate-500' : 'text-rose-400'"
                  >{{ p.status }}</span>
                  <span class="text-slate-300">{{ p.name }}</span>
                  <span v-if="p.exit_code != null" class="text-slate-600"> exit {{ p.exit_code }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Save row -->
        <div v-if="jobId && !running" class="flex items-center gap-3 pt-1 flex-wrap">
          <input
            v-model="label"
            placeholder="Optional label (e.g. checkout flow analysis)"
            class="flex-1 min-w-[200px] bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-indigo-500/60 placeholder:text-slate-600"
          />
          <button
            @click="saveAnalysis"
            :disabled="saving"
            class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50 transition-all"
          >
            <Loader2 v-if="saving" class="w-3.5 h-3.5 animate-spin" />
            <Save v-else class="w-3.5 h-3.5" />
            {{ saving ? 'Saving…' : 'Save to Knowledge' }}
          </button>
        </div>
        <p v-if="notice" class="text-[11px]" :class="notice.ok ? 'text-emerald-400' : 'text-rose-400'">{{ notice.text }}</p>
      </div>

      <!-- Shell: run any command -->
      <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-3">
        <h4 class="text-xs font-bold tracking-wider uppercase text-slate-400 flex items-center gap-2">
          <Terminal class="w-4 h-4 text-emerald-400" />
          Shell — run anything in the project
        </h4>

        <div class="flex items-center gap-2">
          <input
            v-model="shellCommand"
            @keydown.enter="runShell"
            placeholder="php artisan app:code-parse --option=value"
            spellcheck="false"
            class="flex-1 font-mono bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-emerald-200 focus:outline-none focus:border-emerald-500/60 placeholder:text-slate-600"
          />
          <button
            @click="runShell"
            :disabled="!shellEnabled || shellRunning"
            class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold transition-all disabled:opacity-40"
            :class="shellRunning ? 'bg-slate-800 text-slate-400 cursor-wait' : 'bg-emerald-600 text-white hover:bg-emerald-500 shadow-md shadow-emerald-600/20'"
          >
            <Loader2 v-if="shellRunning" class="w-3.5 h-3.5 animate-spin" />
            <Play v-else class="w-3.5 h-3.5" />
            {{ shellRunning ? 'Running…' : 'Run' }}
          </button>
        </div>

        <p class="text-[10px] text-slate-600 leading-relaxed">
          Runs <span class="font-mono text-slate-500">sh -c "…"</span> in a networked container with the
          project writable at <span class="font-mono text-slate-500">/app</span> — file changes persist for later
          commands (composer require, artisan make:*, logs). Requires a successful
          <span class="text-slate-500">Analyze in Sandbox</span> run first. Non-interactive: no TTY, so
          long-running apps like <span class="font-mono text-slate-500">artisan serve</span> aren't supported.
        </p>

        <p v-if="!shellEnabled && selectedFile" class="text-[10px] text-amber-400/80">
          Run an analysis first — the project must be staged (and dependencies installed) on the sandbox daemon.
        </p>

        <!-- Output -->
        <div v-if="shellOut" class="rounded-xl bg-slate-950 border border-slate-800 overflow-hidden">
          <div class="flex items-center gap-2 px-3 py-2 border-b border-slate-800/70 text-[10px] font-mono">
            <span
              class="px-1.5 py-0.5 rounded"
              :class="shellOut.success ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'"
            >{{ shellOut.status || (shellOut.success ? 'done' : 'failed') }}</span>
            <span v-if="shellOut.exit_code != null" class="text-slate-500">exit {{ shellOut.exit_code }}</span>
            <span v-if="shellOut.image" class="text-slate-600">{{ shellOut.image }}</span>
            <span v-if="shellOut.working_dir" class="text-slate-600 truncate">{{ shellOut.working_dir }}</span>
            <button
              v-if="shellOutputText()"
              @click="copyShellOutput"
              title="Copy output"
              class="ml-auto inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-slate-800/60 border border-slate-700/60 hover:border-slate-500/70 hover:bg-slate-700/60 transition-colors"
            >
              <Check v-if="shellCopied" class="w-3 h-3 text-emerald-400" />
              <Copy v-else class="w-3 h-3 text-slate-300" />
              <span :class="shellCopied ? 'text-emerald-400' : 'text-slate-400'">
                {{ shellCopied ? 'Copied' : 'Copy' }}
              </span>
            </button>
          </div>
          <pre v-if="shellOut.stdout" class="p-3 text-[10px] font-mono text-emerald-100/90 leading-relaxed max-h-80 overflow-auto whitespace-pre-wrap">{{ shellOut.stdout }}</pre>
          <div v-else-if="shellOut.reason || shellOut.error" class="p-3 text-[10px] font-mono text-rose-300/90">{{ shellOut.reason || shellOut.error }}</div>
          <div v-else class="p-3 text-[10px] font-mono text-slate-600">(no output)</div>
        </div>

        <!-- History -->
        <div v-if="shellHistory.length" class="space-y-1">
          <div class="text-[10px] text-slate-500 font-mono uppercase tracking-wider flex items-center gap-1.5">
            <History class="w-3 h-3" /> Recent commands
          </div>
          <button
            v-for="(h, i) in shellHistory.slice(0, 8)"
            :key="`${h.command}-${i}`"
            @click="runFromHistory(h)"
            class="w-full text-left flex items-center gap-2 px-2.5 py-1.5 rounded-md bg-slate-950/50 border border-slate-800/60 hover:border-emerald-500/40 transition-colors"
          >
            <span class="flex-1 font-mono text-[10px] text-slate-400 truncate">{{ h.command }}</span>
            <span
              class="text-[9px] font-mono px-1.5 py-0.5 rounded shrink-0"
              :class="h.success ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'"
            >{{ h.exit_code != null ? `exit ${h.exit_code}` : h.status || (h.success ? 'ok' : 'err') }}</span>
          </button>
        </div>
      </div>

      <!-- Saved analyses -->
      <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-3">
        <h4 class="text-xs font-bold tracking-wider uppercase text-slate-400 flex items-center gap-2">
          <FileCode2 class="w-4 h-4 text-indigo-400" />
          Saved analyses ({{ analyses.length }})
        </h4>

        <div v-if="analyses.length === 0" class="text-[11px] text-slate-600 py-6 text-center">
          No analyses saved yet — run one above and press <span class="text-emerald-500">Save to Knowledge</span>.
        </div>

        <div
          v-for="a in analyses"
          :key="a.id"
          class="rounded-xl bg-slate-950/60 border border-slate-800 overflow-hidden"
        >
          <button
            @click="toggleExpand(a.id)"
            class="w-full flex items-center gap-2.5 px-4 py-3 text-left hover:bg-slate-900/40 transition-colors"
          >
            <ChevronDown v-if="expandedId === a.id" class="w-4 h-4 text-slate-500 shrink-0" />
            <ChevronRight v-else class="w-4 h-4 text-slate-500 shrink-0" />
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2">
                <span class="text-xs font-semibold text-slate-200 truncate">{{ a.filename }}</span>
                <span v-if="a.label" class="text-[10px] text-indigo-300 bg-indigo-500/10 px-1.5 py-0.5 rounded-full truncate">{{ a.label }}</span>
              </div>
              <p class="text-[10px] text-slate-500 font-mono truncate mt-0.5">{{ a.summary }}</p>
            </div>
            <div class="text-right shrink-0">
              <div class="text-[10px] font-mono text-slate-500">{{ a.created_at }}</div>
              <div class="text-[10px] font-mono text-emerald-500/80 mt-0.5">
                {{ a.counts?.payment_gateways?.length ? 'payment: ' + a.counts.payment_gateways.join(',') : 'no payment SDK' }}
              </div>
            </div>
          </button>

          <div v-if="expandedId === a.id" class="border-t border-slate-800 px-4 py-3 space-y-3">
            <div class="flex flex-wrap gap-2 text-[10px] font-mono text-slate-400">
              <span class="bg-slate-900 px-2 py-1 rounded-md">chunks: {{ a.ingest?.ingested ?? '–' }}</span>
              <span class="bg-slate-900 px-2 py-1 rounded-md">graph links: {{ a.link?.linked ?? '–' }}</span>
              <span class="bg-slate-900 px-2 py-1 rounded-md">collection: {{ a.collection_name ?? '–' }}</span>
            </div>
            <pre v-if="a.grounding_md" class="bg-slate-950 border border-slate-800/70 rounded-lg p-3 text-[10px] text-slate-400 leading-relaxed max-h-64 overflow-y-auto whitespace-pre-wrap">{{ a.grounding_md }}</pre>
            <div class="flex items-center justify-between">
              <span class="text-[10px] text-slate-600">persisted to Qdrant + Neo4j on save</span>
              <button
                @click="deleteAnalysis(a.id)"
                class="inline-flex items-center gap-1.5 text-[10px] text-rose-400 hover:text-rose-300 px-2 py-1 rounded-md hover:bg-rose-500/10"
              >
                <Trash2 class="w-3 h-3" /> Delete
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
