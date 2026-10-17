<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { AlertCircle, ArrowDownRight, ArrowUpRight, CheckCircle2, GitCompareArrows, LoaderCircle, Play, Save, ShieldCheck } from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';
import PerformanceSetup from './code-quality/PerformanceSetup.vue';

const categories = [
  { id: 'security', label: 'Security' },
  { id: 'clean_code', label: 'Clean code' },
  { id: 'maintainability', label: 'Maintainability' },
  { id: 'reliability', label: 'Reliability' },
  { id: 'performance', label: 'Performance' },
] as const;
type CategoryId = typeof categories[number]['id'];
type ProjectRevision = { project_id: string; revision_id: string; commit_sha: string; updated_at?: string };
type CategoryScore = { assessed: boolean; score: number | null; reason: string; evidence_ids: string[] };
type AssessmentEvidence = { evidence_id: string; kind: 'finding' | 'coverage'; category: CategoryId; rule: string; file: string | null; line: number | null; message: string };
type Report = {
  report_id: string; project_id: string; revision_id: string; commit_sha: string; created_at: string;
  overall_score: number | null; categories: Record<CategoryId, CategoryScore>; weights: Record<CategoryId, number>;
  coverage: { files: number; scanned_files: number; skipped_files: number; scanned_bytes: number; languages: Record<string, number>; unsupported_extensions: Record<string, number>; analyzer_failures: Array<{ name: string; reason: string }>; categories: Record<CategoryId, { supported: boolean; evidence_count: number; reason?: string | null }>; analyzers: Array<{ name: string; version: string; languages: string[]; categories: string[] }> };
  findings: Array<{ evidence_id: string; category: CategoryId; rule: string; file: string; line: number; message: string }>;
  evidence: AssessmentEvidence[];
  rubric_version: string; scoring_parameters?: { temperature: number; top_p: number }; model: string; provider: string;
};
type HistoryRow = Pick<Report, 'report_id' | 'project_id' | 'revision_id' | 'commit_sha' | 'created_at' | 'overall_score' | 'categories' | 'weights' | 'rubric_version' | 'scoring_parameters' | 'model' | 'provider'> & { coverage: number; languages: Record<string, number> };

const projects = ref<ProjectRevision[]>([]);
const activeTab = ref<'assessment' | 'performance'>('assessment');
const selectedRevision = ref('');
const weights = ref<Record<CategoryId, number>>({ security: 20, clean_code: 20, maintainability: 20, reliability: 20, performance: 20 });
const savedWeights = ref<Record<CategoryId, number>>({ security: 20, clean_code: 20, maintainability: 20, reliability: 20, performance: 20 });
const history = ref<HistoryRow[]>([]);
const selectedReportId = ref('');
const leftCompareId = ref('');
const rightCompareId = ref('');
const currentReport = ref<Report | null>(null);
const compareReports = ref<[Report | null, Report | null]>([null, null]);
const loadingProjects = ref(true);
const loadingHistory = ref(false);
const loadingReport = ref(false);
const assessing = ref(false);
const savingWeights = ref(false);
const error = ref('');
const preferenceMessage = ref('');
const recoveryAction = ref<'projects' | 'history' | 'report' | 'comparison' | 'weights' | 'assessment'>('projects');
let historyRequest = 0;

const selectedProject = computed(() => projects.value.find((item) => `${item.project_id}:${item.revision_id}` === selectedRevision.value) || null);
const totalWeight = computed(() => categories.reduce((total, item) => total + Number(weights.value[item.id] || 0), 0));
const weightsChanged = computed(() => categories.some((item) => Number(weights.value[item.id]) !== savedWeights.value[item.id]));
const compareIsEquivalent = computed(() => {
  const [left, right] = compareReports.value;
  if (!left || !right) return true;
  const analyzer = (report: Report) => report.coverage.analyzers.map((item) => `${item.name}:${item.version}`).sort();
  return left.rubric_version === right.rubric_version
    && left.model === right.model
    && left.provider === right.provider
    && JSON.stringify(left.scoring_parameters || null) === JSON.stringify(right.scoring_parameters || null)
    && JSON.stringify(analyzer(left)) === JSON.stringify(analyzer(right))
    && JSON.stringify(left.coverage) === JSON.stringify(right.coverage)
    && JSON.stringify(left.weights) === JSON.stringify(right.weights);
});
const findingChanges = computed(() => {
  const [left, right] = compareReports.value;
  if (!left || !right) return { added: [], removed: [] };
  const leftIds = new Set(left.findings.map((item) => item.evidence_id));
  const rightIds = new Set(right.findings.map((item) => item.evidence_id));
  return {
    added: right.findings.filter((item) => !leftIds.has(item.evidence_id)),
    removed: left.findings.filter((item) => !rightIds.has(item.evidence_id)),
  };
});

const readError = async (response: Response, fallback: string) => {
  const body = await response.json().catch(() => ({}));
  return body.detail || body.error || fallback;
};

const loadProjectsAndPreferences = async () => {
  loadingProjects.value = true;
  error.value = '';
  try {
    const [projectResponse, preferenceResponse] = await Promise.all([
      makeRequest('/api/code-quality/projects'), makeRequest('/api/code-quality/preferences'),
    ]);
    if (!projectResponse.ok) throw new Error(await readError(projectResponse, 'Indexed projects are unavailable.'));
    projects.value = (await projectResponse.json()).projects || [];
    if (!preferenceResponse.ok) throw new Error(await readError(preferenceResponse, 'Assessment preferences are unavailable.'));
    const preference = await preferenceResponse.json();
    if (preference.weights) { weights.value = preference.weights; savedWeights.value = { ...preference.weights }; }
    if (projects.value.length && !selectedRevision.value) {
      const first = projects.value[0];
      selectedRevision.value = `${first.project_id}:${first.revision_id}`;
    }
  } catch (cause: any) {
    recoveryAction.value = 'projects';
    error.value = cause?.message || 'Code quality assessment is unavailable.';
  } finally {
    loadingProjects.value = false;
  }
};

const loadHistory = async () => {
  const requestId = ++historyRequest;
  const project = selectedProject.value;
  history.value = [];
  selectedReportId.value = '';
  currentReport.value = null;
  compareReports.value = [null, null];
  leftCompareId.value = '';
  rightCompareId.value = '';
  if (!project) return;
  loadingHistory.value = true;
  error.value = '';
  try {
    const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(project.project_id)}/reports`);
    if (!response.ok) throw new Error(await readError(response, 'Assessment history could not be loaded.'));
    const result = await response.json();
    if (requestId !== historyRequest) return;
    history.value = result.reports || [];
    if (history.value.length) {
      selectedReportId.value = history.value[0].report_id;
      leftCompareId.value = history.value[0].report_id;
      if (history.value.length > 1) rightCompareId.value = history.value[1].report_id;
    }
  } catch (cause: any) {
    if (requestId === historyRequest) { recoveryAction.value = 'history'; error.value = cause?.message || 'Assessment history could not be loaded.'; }
  } finally {
    if (requestId === historyRequest) loadingHistory.value = false;
  }
};

const loadFullReport = async (reportId: string): Promise<Report | null> => {
  const project = selectedProject.value;
  if (!project || !reportId) return null;
  const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(project.project_id)}/reports/${encodeURIComponent(reportId)}`);
  if (!response.ok) throw new Error(await readError(response, 'Assessment report could not be loaded.'));
  return response.json();
};

const chooseReport = async (reportId: string) => {
  selectedReportId.value = reportId;
  if (!reportId) { currentReport.value = null; return; }
  loadingReport.value = true;
  error.value = '';
  try { currentReport.value = await loadFullReport(reportId); }
  catch (cause: any) { recoveryAction.value = 'report'; error.value = cause?.message || 'Assessment report could not be loaded. Retry.'; }
  finally { loadingReport.value = false; }
};

const loadComparison = async () => {
  compareReports.value = [null, null];
  if (!leftCompareId.value || !rightCompareId.value || leftCompareId.value === rightCompareId.value) return;
  try {
    const [left, right] = await Promise.all([loadFullReport(leftCompareId.value), loadFullReport(rightCompareId.value)]);
    compareReports.value = [left, right];
  } catch (cause: any) {
    recoveryAction.value = 'comparison';
    error.value = cause?.message || 'Selected reports could not be compared. Retry.';
  }
};

const saveWeights = async () => {
  if (totalWeight.value !== 100) return;
  savingWeights.value = true;
  preferenceMessage.value = '';
  error.value = '';
  try {
    const response = await makeRequest('/api/code-quality/preferences', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ weights: weights.value }),
    });
    if (!response.ok) throw new Error(await readError(response, 'Weights could not be saved.'));
    const result = await response.json();
    weights.value = result.weights;
    savedWeights.value = { ...result.weights };
    preferenceMessage.value = 'Weights saved for your next assessment.';
  } catch (cause: any) { recoveryAction.value = 'weights'; error.value = cause?.message || 'Weights could not be saved.'; }
  finally { savingWeights.value = false; }
};

const runAssessment = async () => {
  const project = selectedProject.value;
  if (!project) return;
  assessing.value = true;
  error.value = '';
  try {
    if (totalWeight.value !== 100) throw new Error('Category weights must total 100% before assessment.');
    if (weightsChanged.value) {
      const preferenceResponse = await makeRequest('/api/code-quality/preferences', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ weights: weights.value }),
      });
      if (!preferenceResponse.ok) throw new Error(await readError(preferenceResponse, 'Weights could not be saved.'));
      const preference = await preferenceResponse.json();
      weights.value = preference.weights;
      savedWeights.value = { ...preference.weights };
    }
    const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(project.project_id)}/revisions/${encodeURIComponent(project.revision_id)}/assess`, { method: 'POST' });
    if (!response.ok) throw new Error(await readError(response, 'Assessment failed. Check the configured model and retry.'));
    const report = await response.json() as Report;
    await loadHistory();
    selectedReportId.value = report.report_id;
    currentReport.value = report;
  } catch (cause: any) { recoveryAction.value = 'assessment'; error.value = cause?.message || 'Assessment failed. Retry the request.'; }
  finally { assessing.value = false; }
};

const retryLastAction = () => {
  error.value = '';
  if (recoveryAction.value === 'projects') return loadProjectsAndPreferences();
  if (recoveryAction.value === 'history') return loadHistory();
  if (recoveryAction.value === 'report') return chooseReport(selectedReportId.value);
  if (recoveryAction.value === 'comparison') return loadComparison();
  if (recoveryAction.value === 'weights') return saveWeights();
  return runAssessment();
};

const formatDate = (value: string) => new Date(value).toLocaleString();
const formatScore = (value: number | null | undefined) => value == null ? 'Not assessed' : `${Math.round(value)}`;
const reportCategoryScore = (report: Report | null, category: CategoryId) => report?.categories?.[category]?.assessed ? report.categories[category].score : null;
const scoreDelta = (category: CategoryId) => {
  const left = reportCategoryScore(compareReports.value[0], category);
  const right = reportCategoryScore(compareReports.value[1], category);
  return left == null || right == null ? null : right - left;
};

watch(selectedRevision, loadHistory);
watch([leftCompareId, rightCompareId], loadComparison);
onMounted(loadProjectsAndPreferences);
</script>

<template>
  <section role="region" aria-label="Code quality assessment" tabindex="0" class="mx-auto min-h-0 w-full max-w-7xl flex-1 space-y-6 overflow-y-auto overscroll-contain p-4 text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-400 sm:p-6">
    <header class="flex flex-col gap-3 border-b border-slate-800 pb-5 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <div class="mb-2 flex items-center gap-2 text-indigo-300"><ShieldCheck :size="18" /><span class="text-xs font-semibold uppercase tracking-[0.18em]">Codebase health</span></div>
        <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">Quality assessment</h1>
        <p class="mt-1 text-sm text-slate-400">Review evidence-based scores for an indexed revision and compare saved reports.</p>
      </div>
      <div class="flex flex-col gap-2 sm:min-w-[24rem]">
        <label for="quality-revision" class="text-xs font-medium text-slate-400">Project revision</label>
        <select id="quality-revision" v-model="selectedRevision" :disabled="loadingProjects || !projects.length" class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2.5 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-400/20">
          <option v-if="loadingProjects" value="">Loading indexed revisions…</option>
          <option v-else-if="!projects.length" value="">No indexed revisions available</option>
          <option v-for="project in projects" :key="`${project.project_id}:${project.revision_id}`" :value="`${project.project_id}:${project.revision_id}`">
            {{ project.project_id }} · {{ project.commit_sha?.slice(0, 10) }}
          </option>
        </select>
      </div>
    </header>

    <nav role="tablist" aria-label="Code Quality sections" class="flex flex-wrap gap-2 border-b border-slate-800 pb-3">
      <button id="quality-assessment-tab" role="tab" :aria-selected="activeTab === 'assessment'" aria-controls="quality-assessment-panel" class="min-h-10 rounded-lg px-4 text-sm font-medium transition" :class="activeTab === 'assessment' ? 'bg-indigo-500/15 text-indigo-100 ring-1 ring-indigo-400/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'" @click="activeTab = 'assessment'">Static assessment</button>
      <button id="quality-performance-tab" role="tab" :aria-selected="activeTab === 'performance'" aria-controls="quality-performance-panel" class="min-h-10 rounded-lg px-4 text-sm font-medium transition" :class="activeTab === 'performance' ? 'bg-indigo-500/15 text-indigo-100 ring-1 ring-indigo-400/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'" @click="activeTab = 'performance'">Setup Performance</button>
    </nav>

    <div v-if="activeTab === 'assessment'" id="quality-assessment-panel" role="tabpanel" aria-labelledby="quality-assessment-tab" class="space-y-6">
    <div v-if="error" role="alert" class="flex items-start gap-3 rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">
      <AlertCircle class="mt-0.5 shrink-0" :size="18" /><span class="min-w-0 flex-1">{{ error }}</span>
      <button class="shrink-0 rounded-md px-2 py-1 font-medium hover:bg-rose-400/10" @click="retryLastAction">Retry</button>
    </div>

    <section class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_20rem]">
      <div class="space-y-5">
        <div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
          <div class="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div><h2 class="text-lg font-semibold">Category weights</h2><p class="mt-1 text-sm text-slate-400">The weighted total appears only when all five categories are assessed.</p></div>
            <button :disabled="assessing || savingWeights || !selectedProject || totalWeight !== 100" class="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-indigo-500 px-4 text-sm font-semibold text-white transition hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50" @click="runAssessment">
              <LoaderCircle v-if="assessing" class="motion-safe:animate-spin" :size="16" /><Play v-else :size="16" />
              {{ assessing ? 'Assessing revision…' : weightsChanged ? 'Save weights and assess' : 'Assess this revision' }}
            </button>
          </div>
          <p class="mt-3 text-xs leading-relaxed text-slate-500">Scoring uses a fixed rubric, temperature 0, and top_p 1. Model providers can still change behavior; report comparisons flag known differences in model, rubric, analyzers, coverage, weights, and scoring settings.</p>
          <div v-if="assessing" class="mt-5 rounded-xl border border-indigo-400/20 bg-indigo-400/5 p-4" role="status" aria-live="polite">
            <div class="flex items-center justify-between gap-3">
              <p class="text-sm font-medium text-indigo-100">Assessing {{ selectedProject?.project_id }}…</p>
              <span class="text-xs text-slate-400">In progress</span>
            </div>
            <div role="progressbar" aria-label="Project assessment progress" aria-valuetext="Assessment is running" class="mt-3 h-2 overflow-hidden rounded-full bg-slate-800">
              <div class="quality-assessment-progress h-full w-1/3 rounded-full bg-indigo-400"></div>
            </div>
            <p class="mt-2 text-xs text-slate-400">Running source checks and evidence-based scoring. The result will appear here when complete.</p>
          </div>
          <div class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <label v-for="category in categories" :key="category.id" class="rounded-xl border border-slate-800 bg-slate-950/60 p-3">
              <span class="block text-xs font-medium text-slate-400">{{ category.label }}</span>
              <span class="mt-2 flex items-center gap-2"><input v-model.number="weights[category.id]" type="number" min="0" max="100" class="w-full bg-transparent text-xl font-semibold outline-none" :aria-label="`${category.label} weight`" /><span class="text-sm text-slate-500">%</span></span>
            </label>
          </div>
          <div class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-800 pt-4">
            <p class="text-sm" :class="totalWeight === 100 ? 'text-slate-400' : 'text-amber-300'">Total <strong>{{ totalWeight }}%</strong><span v-if="totalWeight !== 100"> · Weights must total 100%.</span><span v-else-if="weightsChanged"> · Changes apply when you save or start an assessment.</span></p>
            <div class="flex items-center gap-3"><span v-if="preferenceMessage" class="text-xs text-emerald-300">{{ preferenceMessage }}</span>
              <button :disabled="totalWeight !== 100 || savingWeights" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 px-3 text-sm font-medium text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40" @click="saveWeights"><Save :size="15" />{{ savingWeights ? 'Saving…' : 'Save weights' }}</button>
            </div>
          </div>
        </div>

        <section v-if="currentReport" class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div><div class="text-xs font-medium uppercase tracking-wider text-slate-500">Assessment report</div><h2 class="mt-1 text-xl font-semibold">{{ currentReport.overall_score == null ? 'Overall score unavailable' : `${currentReport.overall_score} / 100` }}</h2><p class="mt-1 text-xs text-slate-400">{{ formatDate(currentReport.created_at) }} · {{ currentReport.commit_sha.slice(0, 12) }}</p></div>
            <span class="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-400">{{ currentReport.coverage.scanned_files }} / {{ currentReport.coverage.files }} scanned<span v-if="currentReport.coverage.skipped_files"> · {{ currentReport.coverage.skipped_files }} skipped</span> · {{ Object.keys(currentReport.coverage.languages).join(', ') || 'No detected languages' }}</span>
          </div>
          <div class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <article v-for="category in categories" :key="category.id" class="rounded-xl border border-slate-800 bg-slate-950/60 p-4">
              <div class="text-xs font-medium text-slate-400">{{ category.label }}</div>
              <div class="mt-2 text-2xl font-semibold" :class="currentReport.categories[category.id].assessed ? 'text-slate-100' : 'text-slate-500'">{{ formatScore(currentReport.categories[category.id].score) }}<span v-if="currentReport.categories[category.id].assessed" class="ml-1 text-xs font-normal text-slate-500">/ 100</span></div>
              <p class="mt-2 text-xs leading-relaxed text-slate-400">{{ currentReport.categories[category.id].reason }}</p>
              <p class="mt-3 text-[11px] text-slate-500">{{ currentReport.coverage.categories[category.id].evidence_count }} evidence signals · weight {{ currentReport.weights[category.id] }}%</p>
              <div v-if="currentReport.categories[category.id].evidence_ids.length" class="mt-2 flex flex-wrap gap-1.5">
                <a v-for="evidenceId in currentReport.categories[category.id].evidence_ids" :key="evidenceId" :href="`#${evidenceId}`" class="rounded bg-indigo-400/10 px-1.5 py-1 font-mono text-[10px] text-indigo-200 hover:bg-indigo-400/20">{{ evidenceId }}</a>
              </div>
            </article>
          </div>
          <div v-if="currentReport.coverage.analyzer_failures.length || Object.keys(currentReport.coverage.unsupported_extensions).length || currentReport.coverage.skipped_files" class="mt-4 rounded-lg border border-amber-500/25 bg-amber-500/5 p-3 text-xs text-amber-200">
            <p v-if="currentReport.coverage.analyzer_failures.length" class="font-medium">Some analyzer passes could not complete: {{ currentReport.coverage.analyzer_failures.map((item) => `${item.name}: ${item.reason}`).join(' · ') }}</p>
            <p v-if="Object.keys(currentReport.coverage.unsupported_extensions).length" :class="currentReport.coverage.analyzer_failures.length ? 'mt-1' : ''">Source-like file types without a configured analyzer: {{ Object.entries(currentReport.coverage.unsupported_extensions).map(([extension, count]) => `${extension} (${count})`).join(', ') }}</p>
            <p v-if="currentReport.coverage.skipped_files" class="mt-1">{{ currentReport.coverage.skipped_files }} files exceeded source scan limits and were not analyzed.</p>
          </div>
          <div class="mt-5 grid gap-4 border-t border-slate-800 pt-4 sm:grid-cols-2">
            <div><div class="text-xs font-semibold uppercase tracking-wider text-slate-500">Analyzer coverage</div><p class="mt-2 text-sm text-slate-300">{{ currentReport.coverage.analyzers.map((item) => `${item.name} ${item.version}`).join(' · ') }}</p><p class="mt-1 text-xs text-slate-500">Rubric {{ currentReport.rubric_version }} · {{ currentReport.provider }} / {{ currentReport.model }}<span v-if="currentReport.scoring_parameters"> · temp {{ currentReport.scoring_parameters.temperature }} · top_p {{ currentReport.scoring_parameters.top_p }}</span></p></div>
            <div><div class="text-xs font-semibold uppercase tracking-wider text-slate-500">Evidence findings</div><p class="mt-2 text-sm text-slate-300">{{ currentReport.findings.length }} normalized findings</p></div>
          </div>
          <div v-if="currentReport.findings.length" class="mt-4 space-y-2">
            <article v-for="finding in currentReport.findings" :key="finding.evidence_id" class="flex flex-col gap-1 rounded-lg border border-slate-800/80 bg-slate-950/50 px-3 py-2.5 sm:flex-row sm:items-start sm:justify-between">
              <div><p class="text-sm font-medium text-slate-200">{{ finding.message }}</p><p class="mt-1 font-mono text-xs text-slate-500">{{ finding.file }}:{{ finding.line }}</p></div><span class="shrink-0 rounded-full bg-slate-800 px-2 py-1 text-[11px] text-slate-300">{{ categories.find((item) => item.id === finding.category)?.label }}</span>
            </article>
          </div>
          <div v-else class="mt-4 rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-sm text-slate-400">No findings were emitted by the enabled source checks. This does not establish that the code has no issues.</div>
          <div id="assessment-evidence" class="mt-5 space-y-2">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-500">Evidence references</h3>
            <article v-for="item in currentReport.evidence" :id="item.evidence_id" :key="item.evidence_id" class="scroll-mt-24 rounded-lg border border-slate-800/80 bg-slate-950/40 px-3 py-2.5">
              <div class="flex flex-wrap items-center justify-between gap-2"><span class="font-mono text-[10px] text-indigo-200">{{ item.evidence_id }}</span><span class="text-[10px] uppercase tracking-wider text-slate-500">{{ item.kind }} · {{ categories.find((category) => category.id === item.category)?.label }}</span></div>
              <p class="mt-1.5 text-xs leading-relaxed text-slate-300">{{ item.message }}</p>
              <p v-if="item.file" class="mt-1 font-mono text-[10px] text-slate-500">{{ item.file }}<span v-if="item.line">:{{ item.line }}</span></p>
            </article>
          </div>
        </section>
        <div v-else-if="loadingReport" class="flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-900/50 p-5 text-sm text-slate-400"><LoaderCircle class="animate-spin" :size="16" />Loading report…</div>
        <div v-else-if="!loadingHistory && !history.length" class="rounded-2xl border border-dashed border-slate-700 bg-slate-900/30 p-8 text-center">
          <CheckCircle2 class="mx-auto text-slate-500" :size="26" /><h2 class="mt-3 text-lg font-semibold">No assessment reports yet</h2><p class="mx-auto mt-1 max-w-lg text-sm text-slate-400">Choose a saved weight preset or use the equal-weight default, then assess this indexed revision.</p>
        </div>

        <section class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
          <div class="flex items-center gap-2"><GitCompareArrows class="text-indigo-300" :size="18" /><h2 class="text-lg font-semibold">Compare reports</h2></div>
          <div class="mt-4 grid gap-3 sm:grid-cols-2">
            <label class="text-xs font-medium text-slate-400">Earlier report
              <select v-model="leftCompareId" class="mt-1.5 block w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2.5 text-sm text-slate-200"><option value="">Select a report</option><option v-for="report in history" :key="report.report_id" :value="report.report_id">{{ formatDate(report.created_at) }} · {{ report.overall_score ?? 'Unscored' }}</option></select>
            </label>
            <label class="text-xs font-medium text-slate-400">Later report
              <select v-model="rightCompareId" class="mt-1.5 block w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2.5 text-sm text-slate-200"><option value="">Select a report</option><option v-for="report in history" :key="report.report_id" :value="report.report_id">{{ formatDate(report.created_at) }} · {{ report.overall_score ?? 'Unscored' }}</option></select>
            </label>
          </div>
          <template v-if="compareReports[0] && compareReports[1]">
            <div v-if="!compareIsEquivalent" class="mt-4 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200"><AlertCircle class="mt-0.5 shrink-0" :size="16" /><span>Scoring inputs, coverage, weights, or provenance differ. Treat score deltas as non-equivalent.</span></div>
            <div class="mt-4 divide-y divide-slate-800 rounded-xl border border-slate-800">
              <div v-for="category in categories" :key="category.id" class="flex items-center justify-between gap-4 px-4 py-3 text-sm"><span class="text-slate-300">{{ category.label }}</span><span class="flex items-center gap-3 font-medium"><span class="text-slate-500">{{ formatScore(reportCategoryScore(compareReports[0], category.id)) }}</span><span class="text-slate-600">→</span><span>{{ formatScore(reportCategoryScore(compareReports[1], category.id)) }}</span><span v-if="scoreDelta(category.id) != null" class="inline-flex items-center gap-1 text-xs" :class="(scoreDelta(category.id) || 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'"><ArrowUpRight v-if="(scoreDelta(category.id) || 0) >= 0" :size="13" /><ArrowDownRight v-else :size="13" />{{ Math.abs(scoreDelta(category.id) || 0) }}</span></span></div>
            </div>
            <div class="mt-4 grid gap-3 sm:grid-cols-2">
              <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3"><div class="text-xs font-semibold text-emerald-300">New findings · {{ findingChanges.added.length }}</div><p v-for="item in findingChanges.added.slice(0, 12)" :key="item.evidence_id" class="mt-2 text-xs text-slate-300">{{ item.file }}:{{ item.line }} · {{ item.message }}</p></div>
              <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3"><div class="text-xs font-semibold text-slate-400">Findings no longer present · {{ findingChanges.removed.length }}</div><p v-for="item in findingChanges.removed.slice(0, 12)" :key="item.evidence_id" class="mt-2 text-xs text-slate-400">{{ item.file }}:{{ item.line }} · {{ item.message }}</p></div>
            </div>
          </template>
          <p v-else class="mt-4 text-sm text-slate-500">Choose two saved reports to compare their category scores and findings.</p>
        </section>
      </div>

      <aside class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5">
          <div class="flex items-center justify-between gap-2"><div><h2 class="text-lg font-semibold">Report history</h2><p class="mt-1 text-xs text-slate-500">{{ history.length }} saved for this project</p></div><LoaderCircle v-if="loadingHistory" class="motion-safe:animate-spin text-slate-500" :size="16" /></div>
        <div v-if="history.length" class="mt-4 space-y-2">
          <button v-for="report in history" :key="report.report_id" class="w-full rounded-xl border p-3 text-left transition" :class="selectedReportId === report.report_id ? 'border-indigo-400/60 bg-indigo-500/10' : 'border-slate-800 bg-slate-950/40 hover:border-slate-700'" @click="chooseReport(report.report_id)">
            <span class="flex items-center justify-between gap-2"><span class="text-sm font-semibold">{{ report.overall_score == null ? 'No overall score' : `${report.overall_score} / 100` }}</span><span class="text-[11px] text-slate-500">{{ report.coverage }} files</span></span>
            <span class="mt-1.5 block text-xs text-slate-400">{{ formatDate(report.created_at) }}</span>
            <span class="mt-1 block truncate font-mono text-[10px] text-slate-600">{{ report.commit_sha }}</span>
          </button>
        </div>
        <p v-else-if="!loadingHistory" class="mt-5 text-sm leading-relaxed text-slate-500">Reports from every indexed revision in the selected project will appear here.</p>
      </aside>
    </section>
    </div>
    <div v-else id="quality-performance-panel" role="tabpanel" aria-labelledby="quality-performance-tab">
      <PerformanceSetup :project-id="selectedProject?.project_id || ''" :revision-id="selectedProject?.revision_id || ''" />
    </div>
  </section>
</template>

<style scoped>
.quality-assessment-progress {
  animation: quality-assessment-progress-slide 1.2s ease-in-out infinite;
}

@keyframes quality-assessment-progress-slide {
  from { transform: translateX(-110%); }
  to { transform: translateX(310%); }
}

@media (prefers-reduced-motion: reduce) {
  .quality-assessment-progress {
    animation: none;
    transform: translateX(0);
    opacity: 0.6;
  }
}
</style>
