<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
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
  Database,
  Braces,
} from 'lucide-vue-next';
import { UploadedFile, AnalysisSession, SandboxProbe, SandboxResult, EvaluatorReport, EvaluatorRecommendation } from '../types';
import PipelineProgress, { PipelineStep } from './PipelineProgress.vue';
import { makeRequest } from '../ui/makeRequest';

const props = defineProps<{ files: UploadedFile[] }>();

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

// Sandbox (full run) flow — legacy fields kept for shell + save integration.
const running = ref(false);
const jobId = ref<string | null>(null);
const steps = ref<PipelineStep[]>([]);
const sandboxResult = ref<SandboxResult | null>(null);
const profileSummary = ref<string>('');
const lastFramework = ref<string>('php');
const runError = ref<string>('');
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let stopped = false;

// Structure census flow (/api/structure/analyze) — deep:true delegates to the
// sandbox route, in which case the preview shows the sandbox result instead.
const runningStructure = ref(false);
const structureDeep = ref(false);
const structureJobId = ref<string | null>(null);
const structureSteps = ref<PipelineStep[]>([]);
const structureProfile = ref<StructureProfile | null>(null);
const structureError = ref<string>('');

// Code-index preview flow (/api/code/index).
const runningIndex = ref(false);
const indexJobId = ref<string | null>(null);
const indexSteps = ref<PipelineStep[]>([]);
const codeIndexResult = ref<CodeIndexSummary | null>(null);
const indexError = ref<string>('');

// Live per-step progress from the job status payload (steps_progress map).
const sandboxProgress = ref<StepProgressMap>({});
const structureProgress = ref<StepProgressMap>({});
const indexProgress = ref<StepProgressMap>({});

// Guard: only one analysis job may run at a time.
const anyRunning = computed(() => running.value || runningStructure.value || runningIndex.value);

// Cancel any in-flight poll timer and clear every flow's state so a fresh run
// never inherits a stale result/error/notice/job from a previous (different)
// flow — the merged error banner only ever reflects the active run.
const resetRun = () => {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
  stopped = true;
  running.value = false;
  runningStructure.value = false;
  runningIndex.value = false;
  sandboxResult.value = null;
  structureProfile.value = null;
  codeIndexResult.value = null;
  runError.value = '';
  structureError.value = '';
  indexError.value = '';
  jobId.value = null;
  structureJobId.value = null;
  indexJobId.value = null;
  profileSummary.value = '';
  steps.value = [];
  structureSteps.value = [];
  indexSteps.value = [];
  sandboxProgress.value = {};
  structureProgress.value = {};
  indexProgress.value = {};
  notice.value = null;
  structureNotice.value = null;
  indexNotice.value = null;
};

const mapSteps = (raw: any[]): PipelineStep[] =>
  (raw ?? []).map((s: any) => ({
    id: s.key || s.id,
    label: s.label || '',
    status: s.status || 'pending',
    detail: s.detail ?? null,
  }));

const probeTitle = (p: SandboxProbe) => `${p.name}: ${p.status}${p.exit_code != null ? ` (exit ${p.exit_code})` : ''}`;

type StepProgressMap = Record<string, { percent: number; label?: string }>;

interface StructureProfile {
  framework?: string | null;
  version?: string | null;
  confidence?: number | null;
  language?: string | null;
  database?: string | null;
  database_confidence?: string | null;
  evidence?: string[] | string | null;
  languages?: { language: string; files: number }[];
  total_files?: number | null;
  top_level_dirs?: string[];
  entrypoints?: string[];
  manifests?: string[];
  dependency_count?: number | null;
  summary?: string | null;
  structure_source?: string | null;
  error?: string | null;
}

interface CodeIndexSummary {
  language?: string | null;
  chunks?: number | null;
  files?: number | null;
  producer?: string | null;
  error?: string | null;
}

const normalizeProgress = (raw: any): StepProgressMap => {
  const out: StepProgressMap = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const [key, value] of Object.entries(raw)) {
    const entry = value as any;
    const num = typeof entry?.percent === 'number' ? entry.percent : typeof entry?.percent === 'string' ? Number(entry.percent) : NaN;
    if (Number.isFinite(num)) {
      out[key] = {
        percent: Math.max(0, Math.min(100, num)),
        label: entry?.label ? String(entry.label) : undefined,
      };
    }
  }
  return out;
};

type FlowHandlers = {
  setSteps: (steps: PipelineStep[]) => void;
  setProgress: (progress: StepProgressMap) => void;
  setRunning: (value: boolean) => void;
  onDone: (status: any) => void;
  onError: (message: string) => void;
};

const pollJob = async (id: string, h: FlowHandlers) => {
  for (let attempt = 0; attempt < 240 && !stopped; attempt++) {
    try {
      const res = await makeRequest(`/api/project/analyze/${id}/status`);
      if (stopped) return;
      const status = await res.json();
      if (Array.isArray(status.steps) && status.steps.length) {
        h.setSteps(mapSteps(status.steps));
      }
      h.setProgress(normalizeProgress(status.steps_progress));
      if (status.status === 'done') {
        h.setRunning(false);
        h.onDone(status);
        return;
      }
      if (status.status === 'error' || status.status === 'failed') {
        h.setRunning(false);
        h.onError(status.error || `Job ended with status ${status.status}.`);
        return;
      }
    } catch (err: any) {
      // transient — keep polling until timeout
      console.warn('poll error', err.message);
    }
    if (stopped) return;
    // Track the sleep timer so unmount/resetRun can cancel it immediately — a
    // cleared timer never resolves, so the loop cannot resume after a stop.
    await new Promise<void>((resolve) => {
      pollTimer = setTimeout(resolve, 1500);
    });
  }
  if (stopped) return;
  h.setRunning(false);
  h.onError('Job timed out after ~6 minutes.');
};

const summarizeProfile = (profile: any): string =>
  profile
    ? `${(profile.framework || 'unknown')}${profile.version ? ` ${profile.version}` : ''} — ${(profile.summary || '').slice(0, 220)}`
    : '';

// --- Sandbox flow (full analyze) -------------------------------------------

const startAnalyze = async () => {
  if (!selectedFile.value || anyRunning.value) return;
  resetRun();
  stopped = false;
  running.value = true;
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
    pollJob(data.job_id, {
      setSteps: (s) => (steps.value = s),
      setProgress: (p) => (sandboxProgress.value = p),
      setRunning: (v) => (running.value = v),
      onDone: (status) => {
        sandboxResult.value = status.sandbox ?? null;
        const profile = status.project_profile;
        lastFramework.value = profile?.framework || 'php';
        profileSummary.value = summarizeProfile(profile);
      },
      onError: (message) => (runError.value = message),
    });
  } catch (err: any) {
    runError.value = String(err.message || err);
    running.value = false;
  }
};

// --- Structure census flow --------------------------------------------------

const startStructureAnalyze = async () => {
  if (!selectedFile.value || anyRunning.value) return;
  resetRun();
  stopped = false;
  runningStructure.value = true;
  try {
    const res = await makeRequest('/api/structure/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: selectedFile.value, deep: structureDeep.value }),
    });
    const data = await res.json();
    if (!res.ok || !data.job_id) {
      throw new Error(data.error || data.detail || 'Failed to start structure analysis.');
    }
    structureJobId.value = data.job_id;
    pollJob(data.job_id, {
      setSteps: (s) => (structureSteps.value = s),
      setProgress: (p) => (structureProgress.value = p),
      setRunning: (v) => (runningStructure.value = v),
      onDone: (status) => {
        // Branch on the requested intent (structureDeep) rather than sniffing
        // the payload: a shallow run stays in the structure card and never
        // falls into the deep/sandbox result path.
        if (!structureDeep.value) {
          if (status.structure_profile && typeof status.structure_profile === 'object') {
            structureProfile.value = status.structure_profile as StructureProfile;
            structureError.value = status.structure_profile.error ? String(status.structure_profile.error) : '';
            return;
          }
          // Done without a profile (e.g. missing workspace) — honest card-local
          // warning. The job id is retained so the save row stays consistent
          // with what the shallow census actually produced.
          structureProfile.value = {
            error: 'This job finished without producing a structure profile — the workspace may be missing or contain no analysable project.',
          } as StructureProfile;
          return;
        }
        // deep:true delegates to the sandbox route → project_profile + sandbox.
        // Surface it through the sandbox result card so the install/probes
        // summary and Save flow are exactly the existing sandbox ones.
        sandboxResult.value = status.sandbox ?? null;
        const profile = status.project_profile;
        lastFramework.value = profile?.framework || 'php';
        profileSummary.value = summarizeProfile(profile);
        jobId.value = status.job_id || structureJobId.value;
      },
      onError: (message) => (structureError.value = message),
    });
  } catch (err: any) {
    structureError.value = String(err.message || err);
    runningStructure.value = false;
  }
};

// --- Code-index preview flow ------------------------------------------------

const startCodeIndex = async () => {
  if (!selectedFile.value || anyRunning.value) return;
  resetRun();
  stopped = false;
  runningIndex.value = true;
  try {
    const res = await makeRequest('/api/code/index', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: selectedFile.value }),
    });
    const data = await res.json();
    if (!res.ok || !data.job_id) {
      throw new Error(data.error || data.detail || 'Failed to start code index.');
    }
    indexJobId.value = data.job_id;
    pollJob(data.job_id, {
      setSteps: (s) => (indexSteps.value = s),
      setProgress: (p) => (indexProgress.value = p),
      setRunning: (v) => (runningIndex.value = v),
      onDone: (status) => {
        codeIndexResult.value = status.code_index && typeof status.code_index === 'object'
          ? (status.code_index as CodeIndexSummary)
          : null;
        // Card-local only: a finished job that reports "no parser available" /
        // unsupported language (code_index.error or falsy producer) stays inside
        // the amber code-index preview card. The global indexError banner is
        // reserved for real transport/job failures, which arrive via onError.
      },
      onError: (message) => (indexError.value = message),
    });
  } catch (err: any) {
    indexError.value = String(err.message || err);
    runningIndex.value = false;
  }
};

// Live pipeline display — only one flow runs at a time (guard above).
const liveSteps = computed<PipelineStep[]>(() => {
  if (runningStructure.value) return structureSteps.value;
  if (runningIndex.value) return indexSteps.value;
  return steps.value;
});
const liveProgress = computed<StepProgressMap>(() => {
  if (runningStructure.value) return structureProgress.value;
  if (runningIndex.value) return indexProgress.value;
  return sandboxProgress.value;
});

// --- Save / list / delete analyses -----------------------------------------

const analyses = ref<AnalysisSession[]>([]);
const label = ref('');
const saving = ref(false);
const notice = ref<{ ok: boolean; text: string } | null>(null);
const structureLabel = ref('');
const structureSaving = ref(false);
const structureNotice = ref<{ ok: boolean; text: string } | null>(null);
const indexLabel = ref('');
const indexSaving = ref(false);
const indexNotice = ref<{ ok: boolean; text: string } | null>(null);
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

// Persist a finished job preview. Never auto-called — always an explicit save.
const persistRun = async (jobIdToSave: string, labelText: string): Promise<string> => {
  const res = await makeRequest('/api/analysis', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_id: jobIdToSave, label: labelText.trim() || undefined }),
  });
  const data = await res.json();
  if (!res.ok || !data.success) {
    throw new Error(data.error || data.detail || 'Save failed.');
  }
  const a = data.analysis || {};
  const parts: string[] = [];
  if (typeof a.ingest?.ingested === 'number') parts.push(`${a.ingest.ingested} chunk(s)`);
  if (typeof a.link?.linked === 'number') parts.push(`${a.link.linked} graph link(s)`);
  if (typeof a.counts?.structure_facts === 'number') parts.push(`${a.counts.structure_facts} structure fact(s)`);
  if (typeof a.knowledge_chunks === 'number') parts.push(`${a.knowledge_chunks} fact chunk(s)`);
  return `Saved ${a.kind || 'analysis'} — ${parts.length ? parts.join(' · ') : 'row persisted'}.`;
};

const saveAnalysis = async () => {
  if (!jobId.value || saving.value) return;
  saving.value = true;
  notice.value = null;
  try {
    notice.value = { ok: true, text: await persistRun(jobId.value, label.value) };
    label.value = '';
    await listAnalyses();
  } catch (err: any) {
    notice.value = { ok: false, text: String(err.message || err) };
  } finally {
    saving.value = false;
  }
};

const saveStructure = async () => {
  if (!structureJobId.value || structureSaving.value) return;
  structureSaving.value = true;
  structureNotice.value = null;
  try {
    structureNotice.value = { ok: true, text: await persistRun(structureJobId.value, structureLabel.value) };
    structureLabel.value = '';
    await listAnalyses();
  } catch (err: any) {
    structureNotice.value = { ok: false, text: String(err.message || err) };
  } finally {
    structureSaving.value = false;
  }
};

const saveCodeIndex = async () => {
  if (!indexJobId.value || indexSaving.value) return;
  indexSaving.value = true;
  indexNotice.value = null;
  try {
    indexNotice.value = { ok: true, text: await persistRun(indexJobId.value, indexLabel.value) };
    indexLabel.value = '';
    await listAnalyses();
  } catch (err: any) {
    indexNotice.value = { ok: false, text: String(err.message || err) };
  } finally {
    indexSaving.value = false;
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

// Kind chip metadata for saved-analysis rows.
const kindLabel = (kind?: AnalysisSession['kind']): string =>
  kind === 'structure' ? 'Structure' : kind === 'code_index' ? 'Code Index' : 'Sandbox';
const kindClass = (kind?: AnalysisSession['kind']): string =>
  kind === 'structure'
    ? 'bg-violet-500/10 text-violet-300'
    : kind === 'code_index'
      ? 'bg-cyan-500/10 text-cyan-300'
      : 'bg-indigo-500/10 text-indigo-300';

onMounted(() => {
  fetchSandboxStatus();
  listAnalyses();
});

onUnmounted(() => {
  // Stop any in-flight poll loop: cancels the pending sleep timer and marks
  // stopped so no handler writes into an unmounted component.
  resetRun();
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

// --- Postman-style route runner + opt-in database evaluation --------------

type ApiOutcome = {
  success: boolean;
  status?: string;
  exit_code?: number | null;
  stdout?: string;
  reason?: string;
  error?: string;
  request?: { method: string; path: string; headers: string[] };
  database_evaluation?: { enabled: boolean; run_id?: string; hook?: { status: string; reason?: string }; cleanup?: { status: string; reason?: string } };
};

const apiMethod = ref('GET');
const apiPath = ref('/');
const apiHeaders = ref('');
const apiBody = ref('');
const apiDatabase = ref<'mysql' | 'postgresql'>('mysql');
const apiEvaluateDatabase = ref(false);
const apiRunning = ref(false);
const apiOut = ref<ApiOutcome | null>(null);
const apiReports = ref<EvaluatorReport[]>([]);
const apiRecommendation = ref<EvaluatorRecommendation | null>(null);
const apiRunId = ref<string | null>(null);
const apiLlmRunning = ref(false);
const apiLlmError = ref('');
const apiSystemPrompt = ref('You are a senior database performance engineer. Use only observed evaluator evidence to explain the root cause and propose a safe application/index fix. Return a review-only unified diff and never claim changes were applied.');

const parseApiHeaders = (): Record<string, string> => {
  const result: Record<string, string> = {};
  for (const line of apiHeaders.value.split(/\r?\n/)) {
    const index = line.indexOf(':');
    if (index <= 0) continue;
    result[line.slice(0, index).trim()] = line.slice(index + 1).trim();
  }
  return result;
};

const waitForEvaluator = async (runId: string) => {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const response = await makeRequest(`/api/evaluator/runs/${encodeURIComponent(runId)}`);
    const status = await response.json();
    if (status.status === 'completed' || status.status === 'error') {
      apiReports.value = (status.reports || []) as EvaluatorReport[];
      if (status.status === 'error') apiLlmError.value = status.error || 'Database evaluation failed.';
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  apiLlmError.value = 'Database evaluation timed out while waiting for telemetry.';
};

const runApiRequest = async () => {
  if (!selectedFile.value || !shellEnabled.value || apiRunning.value) return;
  if (!apiPath.value.trim().startsWith('/')) { apiLlmError.value = 'Route path must start with /.'; return; }
  apiRunning.value = true;
  apiOut.value = null;
  apiReports.value = [];
  apiRecommendation.value = null;
  apiRunId.value = null;
  apiLlmError.value = '';
  try {
    const response = await makeRequest('/api/sandbox/request', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: selectedFile.value,
        method: apiMethod.value,
        path: apiPath.value.trim(),
        headers: parseApiHeaders(),
        body: apiBody.value || undefined,
        framework: lastFramework.value,
        database: apiDatabase.value,
        evaluate_database: apiEvaluateDatabase.value,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || data.detail || 'API request failed.');
    apiOut.value = data as ApiOutcome;
    const runId = data.database_evaluation?.run_id;
    if (runId) { apiRunId.value = runId; await waitForEvaluator(runId); }
  } catch (err: any) {
    apiOut.value = { success: false, error: String(err.message || err) };
  } finally {
    apiRunning.value = false;
  }
};

const sendApiReportToLlm = async () => {
  if (!apiRunId.value || apiLlmRunning.value) return;
  apiLlmRunning.value = true;
  apiLlmError.value = '';
  try {
    const response = await makeRequest(`/api/evaluator/runs/${encodeURIComponent(apiRunId.value)}/recommendation`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_llm: true, system_prompt: apiSystemPrompt.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'LLM request failed.');
    apiRecommendation.value = data.recommendation as EvaluatorRecommendation;
  } catch (err: any) {
    apiLlmError.value = String(err.message || err);
  } finally {
    apiLlmRunning.value = false;
  }
};

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
          Analyze project
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

          <div class="flex flex-col items-stretch gap-1.5">
            <button
              @click="startAnalyze"
              :disabled="anyRunning || !selectedFile"
              class="inline-flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold transition-all disabled:opacity-40"
              :class="running ? 'bg-slate-800 text-slate-400 cursor-wait' : 'bg-indigo-600 text-white hover:bg-indigo-500 shadow-md shadow-indigo-600/20'"
            >
              <Loader2 v-if="running" class="w-3.5 h-3.5 animate-spin" />
              <Play v-else class="w-3.5 h-3.5" />
              {{ running ? 'Running…' : 'Analyze in Sandbox' }}
            </button>

            <div class="flex items-stretch gap-1.5">
              <button
                @click="startStructureAnalyze"
                :disabled="anyRunning || !selectedFile"
                :title="structureDeep ? 'Deep run — delegates to the sandbox (install + probes)' : 'Static multi-language structure census (fast, no sandbox)'"
                class="inline-flex items-center justify-center gap-2 px-3 py-1.5 rounded-lg text-[11px] font-semibold border transition-all disabled:opacity-40"
                :class="runningStructure
                  ? 'bg-slate-800 text-slate-400 border-slate-700 cursor-wait'
                  : 'border-violet-500/30 bg-violet-500/10 text-violet-300 hover:bg-violet-500/20'"
              >
                <Loader2 v-if="runningStructure" class="w-3.5 h-3.5 animate-spin" />
                <Database v-else class="w-3.5 h-3.5" />
                {{ runningStructure ? 'Running…' : structureDeep ? 'Structure (deep)' : 'Analyze Structure' }}
              </button>
              <button
                @click="startCodeIndex"
                :disabled="anyRunning || !selectedFile"
                title="Parse-only code index preview (no store writes)"
                class="inline-flex items-center justify-center gap-2 px-3 py-1.5 rounded-lg text-[11px] font-semibold border transition-all disabled:opacity-40"
                :class="runningIndex
                  ? 'bg-slate-800 text-slate-400 border-slate-700 cursor-wait'
                  : 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300 hover:bg-cyan-500/20'"
              >
                <Loader2 v-if="runningIndex" class="w-3.5 h-3.5 animate-spin" />
                <Braces v-else class="w-3.5 h-3.5" />
                {{ runningIndex ? 'Running…' : 'Index Codebase' }}
              </button>
            </div>

            <label class="flex items-center gap-1.5 text-[10px] text-slate-500 font-mono cursor-pointer select-none">
              <input v-model="structureDeep" type="checkbox" :disabled="anyRunning || !selectedFile" class="accent-violet-500" />
              deep (run sandbox install + probes)
            </label>
          </div>
        </div>

        <!-- Probe templates hint -->
        <p class="text-[10px] text-slate-600 leading-relaxed">
          <span class="font-mono text-slate-500">Analyze in Sandbox</span> runs
          <span class="font-mono text-slate-500">composer install</span> (networked DinD) then isolated
          <span class="font-mono text-slate-500">runsc</span> probes:
          <span class="font-mono text-slate-500">php artisan route:list --json</span>,
          <span class="font-mono text-slate-500">php artisan schedule:list</span>,
          <span class="font-mono text-slate-500">./vendor/bin/phpunit --no-coverage</span>.
          <span class="font-mono text-slate-500">Analyze Structure</span> is a deterministic offline census;
          <span class="font-mono text-slate-500">Index Codebase</span> parses code chunks (preview only).
        </p>

        <div
          v-if="runError || structureError || indexError"
          class="rounded-xl bg-rose-500/10 border border-rose-500/30 px-4 py-3 text-xs text-rose-300 flex items-start gap-2"
        >
          <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
          <span>{{ runError || structureError || indexError }}</span>
        </div>

        <!-- Live pipeline -->
        <PipelineProgress
          v-if="anyRunning && liveSteps.length"
          :fileName="selectedFile"
          :steps="liveSteps"
          :progress="liveProgress"
        />

        <!-- Result: sandbox (full / deep-structure) run -->
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

        <!-- Result: structure profile (shallow census) -->
        <div v-if="structureProfile" class="rounded-2xl bg-slate-950/60 border border-violet-500/20 p-4 space-y-3">
          <div class="flex items-center gap-2">
            <Database class="w-4 h-4 text-violet-300" />
            <span class="text-xs font-semibold text-slate-200">Structure profile</span>
            <span
              v-if="structureProfile.structure_source"
              class="ml-auto text-[10px] font-mono text-slate-500"
            >{{ structureProfile.structure_source }}</span>
          </div>

          <p v-if="structureProfile.summary" class="text-[11px] text-slate-400 leading-relaxed">{{ structureProfile.summary }}</p>

          <div
            v-if="structureProfile.error"
            class="rounded-lg bg-rose-500/10 border border-rose-500/30 px-3 py-2 text-[10px] font-mono text-rose-300"
          >{{ structureProfile.error }}</div>

          <div class="grid grid-cols-2 gap-2 text-[10px] font-mono">
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">framework</div>
              <div class="text-violet-200">{{ structureProfile.framework || '—' }}<span v-if="structureProfile.version" class="text-slate-500"> {{ structureProfile.version }}</span></div>
              <div v-if="structureProfile.confidence != null" class="text-slate-600 mt-0.5">confidence {{ structureProfile.confidence }}</div>
            </div>
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">language</div>
              <div class="text-slate-300">{{ structureProfile.language || '—' }}</div>
              <div class="text-slate-600 mt-0.5">{{ structureProfile.total_files ?? '–' }} file(s)</div>
            </div>
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">database</div>
              <div class="text-slate-300">{{ structureProfile.database || 'not determined' }}</div>
              <div v-if="structureProfile.database" class="text-slate-600 mt-0.5">
                conf {{ structureProfile.database_confidence || 'low' }}<span v-if="structureProfile.evidence"> · {{ String(structureProfile.evidence).slice(0, 80) }}</span>
              </div>
            </div>
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">dependencies</div>
              <div class="text-slate-300">{{ structureProfile.dependency_count ?? '–' }} <span class="text-slate-600">manifest(s): {{ (structureProfile.manifests || []).length }}</span></div>
            </div>
          </div>

          <div class="text-[10px] font-mono space-y-1">
            <div v-if="(structureProfile.languages || []).length" class="flex flex-wrap gap-1.5">
              <span class="text-slate-500 uppercase tracking-wider mt-0.5">langs</span>
              <span
                v-for="l in structureProfile.languages"
                :key="l.language"
                class="bg-slate-900 border border-slate-800 px-2 py-0.5 rounded text-slate-300"
              >{{ l.language }} · {{ l.files }}</span>
            </div>
            <div class="flex flex-wrap gap-x-4 gap-y-0.5">
              <span v-if="structureProfile.top_level_dirs?.length"><span class="text-slate-500">dirs</span> {{ structureProfile.top_level_dirs.join(', ') }}</span>
              <span v-if="structureProfile.entrypoints?.length"><span class="text-slate-500">entry</span> {{ structureProfile.entrypoints.join(', ') }}</span>
            </div>
          </div>

          <div v-if="structureJobId" class="border-t border-slate-800/70 pt-3 flex items-center gap-3 flex-wrap">
            <input
              v-model="structureLabel"
              placeholder="Optional label (e.g. structure facts)"
              class="flex-1 min-w-[200px] bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-violet-500/60 placeholder:text-slate-600"
            />
            <button
              @click="saveStructure"
              :disabled="structureSaving || anyRunning"
              class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50 transition-all"
            >
              <Loader2 v-if="structureSaving" class="w-3.5 h-3.5 animate-spin" />
              <Save v-else class="w-3.5 h-3.5" />
              {{ structureSaving ? 'Saving…' : 'Save structure knowledge' }}
            </button>
          </div>
          <p v-if="structureNotice" class="text-[11px]" :class="structureNotice.ok ? 'text-emerald-400' : 'text-rose-400'">{{ structureNotice.text }}</p>
        </div>

        <!-- Result: code index preview -->
        <div v-if="codeIndexResult" class="rounded-2xl bg-slate-950/60 border border-cyan-500/20 p-4 space-y-3">
          <div class="flex items-center gap-2">
            <Braces class="w-4 h-4 text-cyan-300" />
            <span class="text-xs font-semibold text-slate-200">Code index preview</span>
            <span
              v-if="codeIndexResult.producer"
              class="ml-auto text-[10px] font-mono text-cyan-300/80 bg-cyan-500/10 px-2 py-0.5 rounded-full"
            >{{ codeIndexResult.producer }}</span>
          </div>

          <div v-if="codeIndexResult.error || !codeIndexResult.producer" class="rounded-lg bg-amber-500/10 border border-amber-500/30 px-3 py-2 text-[10px] font-mono text-amber-200/90">
            <span v-if="codeIndexResult.error">{{ codeIndexResult.error }}</span>
            <span v-else>No {{ codeIndexResult.language || 'recognised' }} parser available — this language is not supported yet, so no chunks were produced. Nothing will be written on save.</span>
          </div>

          <div class="grid grid-cols-3 gap-2 text-[10px] font-mono">
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">language</div>
              <div class="text-cyan-200">{{ codeIndexResult.language || '—' }}</div>
            </div>
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">chunks</div>
              <div class="text-cyan-200">{{ codeIndexResult.chunks ?? 0 }}</div>
            </div>
            <div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2">
              <div class="text-slate-500 uppercase tracking-wider mb-1">files</div>
              <div class="text-cyan-200">{{ codeIndexResult.files ?? '–' }}</div>
            </div>
          </div>

          <div v-if="indexSteps.length" class="text-[10px] font-mono space-y-0.5">
            <div class="text-slate-500 uppercase tracking-wider mb-1">steps</div>
            <div v-for="s in indexSteps" :key="s.id" class="flex items-center gap-1.5">
              <CheckCircle2 v-if="s.status === 'done'" class="w-3 h-3 text-emerald-400 shrink-0" />
              <span v-else class="w-[9px] h-[9px] rounded-full bg-slate-600 shrink-0"></span>
              <span class="text-slate-400">{{ s.label }}</span>
              <span v-if="s.detail" class="text-slate-600 truncate">— {{ s.detail }}</span>
            </div>
          </div>

          <div v-if="indexJobId" class="border-t border-slate-800/70 pt-3 flex items-center gap-3 flex-wrap">
            <input
              v-model="indexLabel"
              placeholder="Optional label (e.g. code index)"
              class="flex-1 min-w-[200px] bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-cyan-500/60 placeholder:text-slate-600"
            />
            <button
              @click="saveCodeIndex"
              :disabled="indexSaving || anyRunning"
              class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50 transition-all"
            >
              <Loader2 v-if="indexSaving" class="w-3.5 h-3.5 animate-spin" />
              <Save v-else class="w-3.5 h-3.5" />
              {{ indexSaving ? 'Saving…' : 'Save code index' }}
            </button>
          </div>
          <p v-if="indexNotice" class="text-[11px]" :class="indexNotice.ok ? 'text-emerald-400' : 'text-rose-400'">{{ indexNotice.text }}</p>
        </div>

        <!-- Save row (sandbox flow) -->
        <div v-if="jobId && !running" class="flex items-center gap-3 pt-1 flex-wrap">
          <input
            v-model="label"
            placeholder="Optional label (e.g. checkout flow analysis)"
            class="flex-1 min-w-[200px] bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-indigo-500/60 placeholder:text-slate-600"
          />
          <button
            @click="saveAnalysis"
            :disabled="saving || anyRunning"
            class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50 transition-all"
          >
            <Loader2 v-if="saving" class="w-3.5 h-3.5 animate-spin" />
            <Save v-else class="w-3.5 h-3.5" />
            {{ saving ? 'Saving…' : 'Save to Knowledge' }}
          </button>
        </div>
        <p v-if="notice" class="text-[11px]" :class="notice.ok ? 'text-emerald-400' : 'text-rose-400'">{{ notice.text }}</p>
      </div>

      <!-- Route request runner: Postman-like request against staged app -->
      <div class="bg-slate-900/30 border border-cyan-500/20 rounded-2xl p-6 space-y-4">
        <h4 class="text-xs font-bold tracking-wider uppercase text-slate-400 flex items-center gap-2">
          <Braces class="w-4 h-4 text-cyan-400" />
          Run API route
        </h4>
        <div class="grid grid-cols-[110px_1fr] gap-2">
          <select v-model="apiMethod" class="bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-cyan-200">
            <option v-for="method in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']" :key="method" :value="method">{{ method }}</option>
          </select>
          <input v-model="apiPath" placeholder="/api/orders" class="font-mono bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200" />
        </div>
        <div class="grid md:grid-cols-2 gap-3">
          <textarea v-model="apiHeaders" rows="4" placeholder="Headers (one per line)\nAuthorization: Bearer …\nAccept: application/json" class="font-mono bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-[10px] text-slate-300" />
          <textarea v-model="apiBody" rows="4" placeholder="Optional request body" class="font-mono bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-[10px] text-slate-300" />
        </div>
        <div class="flex flex-wrap items-center gap-3">
          <button
            @click="runApiRequest"
            :disabled="!shellEnabled || apiRunning"
            class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold bg-cyan-600 text-white hover:bg-cyan-500 disabled:opacity-40"
          >
            <Loader2 v-if="apiRunning" class="w-3.5 h-3.5 animate-spin" />
            <Play v-else class="w-3.5 h-3.5" />
            {{ apiRunning ? 'Requesting…' : 'Send request' }}
          </button>
          <label class="inline-flex items-center gap-2 text-[10px] text-slate-400 font-mono cursor-pointer">
            <input v-model="apiEvaluateDatabase" type="checkbox" class="accent-amber-500" :disabled="apiRunning" />
            evaluate database performance
          </label>
          <select v-if="apiEvaluateDatabase" v-model="apiDatabase" class="bg-slate-950 border border-slate-800 rounded px-2 py-1 text-[10px] text-amber-200">
            <option value="mysql">MySQL</option><option value="postgresql">PostgreSQL</option>
          </select>
        </div>
        <p class="text-[10px] text-slate-600">The route is resolved inside the staged project sandbox. Database evaluation is opt-in and never applies a patch automatically.</p>

        <div v-if="apiOut" class="rounded-xl bg-slate-950 border border-slate-800 overflow-hidden">
          <div class="px-3 py-2 border-b border-slate-800 text-[10px] font-mono text-slate-400">{{ apiOut.request?.method }} {{ apiOut.request?.path }} · {{ apiOut.status || (apiOut.success ? 'done' : 'failed') }}</div>
          <pre v-if="apiOut.stdout" class="p-3 text-[10px] font-mono text-cyan-100/90 max-h-64 overflow-auto whitespace-pre-wrap">{{ apiOut.stdout }}</pre>
          <div v-else-if="apiOut.error || apiOut.reason" class="p-3 text-[10px] font-mono text-rose-300">{{ apiOut.error || apiOut.reason }}</div>
        </div>

        <div v-if="apiEvaluateDatabase && apiOut?.database_evaluation" class="rounded-xl border border-amber-500/20 bg-amber-950/10 p-4 space-y-3">
          <div class="flex items-center gap-2"><Database class="w-4 h-4 text-amber-300" /><span class="text-xs font-semibold text-amber-100">Database evaluation report</span><span class="ml-auto text-[10px] text-slate-500">{{ apiReports.length }} report(s)</span></div>
          <div v-if="apiReports.length === 0" class="text-[10px] text-amber-200/80">No normalized telemetry was available for this request. The sandbox hook was {{ apiOut.database_evaluation.hook?.status || 'not available' }}.</div>
          <div v-for="report in apiReports" :key="report.query_fingerprint" class="rounded-lg bg-slate-950/70 border border-slate-800 p-3">
            <div class="flex items-center justify-between gap-2"><code class="text-[10px] text-cyan-200">{{ report.query_fingerprint }}</code><span class="text-[9px] uppercase text-slate-500">{{ report.availability }}</span></div>
            <div class="mt-1 text-[10px] text-amber-200">{{ (report.issues || []).join(', ') || 'No evidence-backed issue detected' }}</div>
            <div v-if="report.source_trace?.file" class="mt-1 text-[10px] text-slate-500">{{ report.source_trace.file }}:{{ report.source_trace.line }}</div>
          </div>
          <textarea v-model="apiSystemPrompt" rows="3" class="w-full font-mono bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-[10px] text-slate-300" />
          <button @click="sendApiReportToLlm" :disabled="!apiRunId || apiLlmRunning" class="inline-flex items-center gap-2 px-3 py-2 rounded-lg text-[11px] font-semibold bg-amber-600 text-white hover:bg-amber-500 disabled:opacity-40">
            <Loader2 v-if="apiLlmRunning" class="w-3.5 h-3.5 animate-spin" />
            <Database v-else class="w-3.5 h-3.5" />
            {{ apiLlmRunning ? 'Asking LLM…' : 'Send report to LLM' }}
          </button>
          <div v-if="apiLlmError" class="text-[10px] text-rose-300">{{ apiLlmError }}</div>
          <article v-if="apiRecommendation?.llm_response" class="rounded-lg bg-slate-950 border border-emerald-500/20 p-3">
            <div class="text-[10px] uppercase tracking-wider text-emerald-300 mb-2">LLM fix proposal · review only</div>
            <pre class="whitespace-pre-wrap text-[10px] leading-relaxed text-slate-200">{{ apiRecommendation.llm_response }}</pre>
          </article>
        </div>
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
          No analyses saved yet — run one above and save its preview.
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
                <span
                  class="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded-full shrink-0"
                  :class="kindClass(a.kind)"
                >{{ kindLabel(a.kind) }}</span>
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
