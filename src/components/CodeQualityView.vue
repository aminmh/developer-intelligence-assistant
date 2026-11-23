<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import {
  AlertCircle, ArrowDownRight, ArrowRight, ArrowUpRight, Boxes, Bug, Calendar, ChevronDown, ChevronsUpDown, CircleCheck, Code, Cpu,
  Eye, Files, FileSearch, FlaskConical, Gauge, GitBranch, GitCommitHorizontal, GitCompareArrows, Library, LifeBuoy, ListChecks,
  LoaderCircle, Play, Plug, Radar, RefreshCw, Save, SearchCode, ShieldAlert, SlidersHorizontal, Sparkles, Trash2, TriangleAlert,
  Waypoints, Wrench, X, Zap, Workflow, BookOpen, Languages, Accessibility, ShieldCheck,
} from 'lucide-vue-next';
import ConfirmModal from './ConfirmModal.vue';
import { makeRequest } from '../ui/makeRequest';
import PerformanceSetup from './code-quality/PerformanceSetup.vue';
import RegressionCheck from './regression/RegressionCheck.vue';
import RuleManager from './code-quality/RuleManager.vue';
import SourcePreview from './code-quality/SourcePreview.vue';
import DirectoryExclusionTree from './code-quality/DirectoryExclusionTree.vue';
import PublicRouteSettings from './code-quality/PublicRouteSettings.vue';
import BusinessRuleReviews from './code-quality/BusinessRuleReviews.vue';
import ExternalAnalyzerSetup from './code-quality/ExternalAnalyzerSetup.vue';
import { useUrlTab } from '../composables/useUrlTab';
import { useProjectScope } from '../composables/useProjectScope';
import type { UploadedFile } from '../types';
import { assessmentRevisions, isWorkingTreeRevision, revisionKey, type ProjectRevision } from '../code-quality/projectRevisions';

const props = defineProps<{ files: UploadedFile[] }>();
const emit = defineEmits<{ openKnowledgeBase: []; revisionRemoved: [] }>();

const categories = [
  { id: 'security', label: 'Security' },
  { id: 'clean_code', label: 'Architecture and design' },
  { id: 'maintainability', label: 'Maintainability' },
  { id: 'reliability', label: 'Reliability' },
  { id: 'performance', label: 'Performance' },
  // Scored only from their own non-source evidence; unassessed with a reason when there is none.
  { id: 'process', label: 'Process' },
  { id: 'testing', label: 'Testing' },
  { id: 'documentation', label: 'Documentation' },
  { id: 'localization', label: 'Localization' },
  { id: 'accessibility', label: 'Accessibility' },
] as const;
type CategoryId = typeof categories[number]['id'];
type AiOpinion = { assessed: boolean; score: number | null; reason: string; evidence_ids: string[] };
type CategoryScore = { assessed: boolean; score: number | null; reason: string; evidence_ids: string[]; score_method?: string; metric_schema?: string; rubric_version?: string; assessment_status?: 'scored' | 'provisional' | 'unavailable'; confidence?: 'high' | 'medium' | 'low'; confidence_reasons?: string[]; finding_count?: number; deterministic_evidence_ids?: string[]; ai_opinion?: AiOpinion };
type FlowTrace = { nodes: Array<{ symbol: string; file: string; start_line: number; end_line: number }>; edges: Array<{ type: string; from: string; to: string; file: string; line: number }> };
type PhpDesignSignals = { status: 'complete' | 'partial' | 'unavailable'; revision_id: string; analyzer_version: string; languages: string[]; principles_with_static_signals: string[]; principles_requiring_semantic_evidence: string[]; chunk_count: number; class_count: number; method_count: number; signal_count: number; signals_truncated: boolean; limits?: Record<string, number>; reason: string };
type AssessmentEvidence = { evidence_id: string; kind?: 'finding' | 'coverage' | 'semantic_trace' | 'semantic_context' | 'architectural_signal' | 'runtime_measurement'; category: CategoryId | 'business_rule'; rule: string; file: string | null; line: number | null; message: string; trace?: FlowTrace };
// Budgeted per-category model reviews (assessment plan Phase 4): findings are kept only with verified citations.
type ReviewTraceRow = { tool: string; arguments: Record<string, unknown>; ok: boolean; error?: string | null; result_bytes: number; truncated: boolean; ranges_returned: number };
type CategoryReview = { category: string; status: 'complete' | 'partial' | 'unavailable'; reason: string | null; summary: string | null;
  budget: { max_tool_calls: number; max_tokens: number; max_seconds: number };
  usage: { tool_calls: number; total_tokens: number; estimated: boolean; seconds: number };
  trace: ReviewTraceRow[]; accepted_findings: number; rejected_citations: number };
type CategoryReviews = { status: 'complete' | 'partial' | 'disabled' | 'unavailable'; version: string; reason?: string; model?: string;
  config: { max_tool_calls: number; max_tokens: number; max_seconds: number; global_max_tokens: number; parallel: number; tools: string[] };
  categories: Record<string, CategoryReview>; total_tokens?: number; seconds?: number };
type BusinessRuleSnapshot = { rule_id: string; version: number; title: string; statement: string; category: string; source?: 'outline'; collection_id?: string; updated_at?: string; content_hash?: string };
type BusinessRuleReview = { rule_id: string; version: number; status: 'supported' | 'concern' | 'unknown'; reason: string; evidence_ids: string[] };
type TokenUsage = { prompt_tokens: number | null; completion_tokens: number | null; total_tokens: number; estimated: boolean; source: 'provider' | 'estimate'; batch_count?: number; summarized_evidence_count?: number };
type Report = {
  report_id: string; project_id: string; revision_id: string; commit_sha: string | null; created_at: string;
  overall_score: number | null; categories: Record<CategoryId, CategoryScore>; weights: Record<CategoryId, number>; shadow_scoring?: { version: string; status: string; categories: Record<CategoryId, CategoryScore> };
  coverage: { files: number; scanned_files: number; skipped_files: number; excluded_files?: number; user_excluded_files?: number; user_exclusion_patterns?: string[]; scanned_bytes: number; languages: Record<string, number>; unsupported_extensions: Record<string, number>; analyzer_failures: Array<{ name: string; reason: string }>; categories: Record<CategoryId, { supported: boolean; status?: 'covered' | 'partial' | 'unavailable'; evidence_count: number; reason?: string | null }>; analyzers: Array<{ name: string; version: string; rules_version?: string; languages: string[]; categories: string[]; status?: string; server_version?: string; server_edition?: string; scanned_files?: number; skipped_files?: number; finding_count?: number; reason?: string | null; unresolved?: string[] }>; external_analyzers?: { sonarqube?: { status: string; version?: string; server_version?: string; server_edition?: string; scanned_files?: number; skipped_files?: number; finding_count?: number; reason?: string | null }; joern?: { status: string; version?: string; languages?: string[]; scanned_files?: number; skipped_files?: number; finding_count?: number; reason?: string | null; unresolved?: string[] } }; business_rule_review?: { status: string; reason?: string | null; collection_id?: string | null; document_count?: number; imported_count?: number; omitted_count?: number; oversized_count?: number; limit_exceeded?: boolean; synchronized_at?: string | null }; semantic_context?: { status: 'available' | 'available_no_matches' | 'unavailable'; revision_id: string; retriever_version: string; query_count: number; business_rule_query_count?: number; omitted_business_rule_ids?: string[]; evidence_count: number; failure_code?: string | null; diagnostic_detail?: string | null; searched_collection_count?: number; candidate_count?: number; rejected_revision_count?: number; rejected_source_count?: number; category_search?: Partial<Record<CategoryId, { attempted_queries: number; candidate_count: number; accepted_count: number; rejected_revision_count: number; rejected_source_count: number }>>; reason?: string | null }; category_review_context?: { status: 'available' | 'partial' | 'unavailable' | 'not_needed'; source: string | null; excerpt_count: number; categories: Record<CategoryId, number>; scanned_files: number; scanned_bytes: number; reason: string }; semantic_flow_tracing?: { status: 'complete' | 'partial' | 'unavailable'; revision_id: string; tracer_version: string; languages?: string[]; flow_types: string[]; node_count: number; edge_count: number; trace_count: number; unresolved_edge_count: number; unresolved_edges?: Array<{ source_symbol: string; file: string; line: number; target_symbol: string | null; reason: string; count: number }>; limits_reached?: string[]; reason: string }; php_design_signals?: PhpDesignSignals };
  token_usage?: TokenUsage | null;
  category_reviews?: CategoryReviews;
  custom_rules?: Array<{ rule_id: string; title: string; content_hash: string; yaml: string }>;
  findings: Array<{ evidence_id: string; category: CategoryId; rule: string; file: string; line: number; message: string; severity?: string; tool?: string; tool_version?: string }>;
  evidence: AssessmentEvidence[];
  business_rules?: BusinessRuleSnapshot[];
  business_rule_reviews?: BusinessRuleReview[];
  rubric_version: string; scoring_parameters?: { temperature: number; top_p: number }; model: string; provider: string;
};
type ReportFinding = Report['findings'][number];
type ReportEvidenceRow = { evidenceId: string; evidence: AssessmentEvidence | null; finding: ReportFinding | null };
type HistoryRow = Pick<Report, 'report_id' | 'project_id' | 'revision_id' | 'commit_sha' | 'created_at' | 'overall_score' | 'categories' | 'weights' | 'rubric_version' | 'scoring_parameters' | 'model' | 'provider' | 'token_usage'> & { coverage: number; languages: Record<string, number> };

const registeredRevisions = ref<ProjectRevision[]>([]);
const projects = computed(() => assessmentRevisions(props.files, registeredRevisions.value));
type QualityTab = 'assessment' | 'compare' | 'regression' | 'performance' | 'rules' | 'external';
const activeTab = useUrlTab<QualityTab>({ values: ['assessment', 'compare', 'regression', 'performance', 'rules', 'external'], fallback: 'assessment' });
const selectedRevision = ref('');
// The header switcher picks the project; this page lists that project's revisions.
const scope = useProjectScope();
const scopedProjects = computed(() => scope.projectId.value
  ? projects.value.filter((project) => project.project_id === scope.projectId.value)
  : projects.value);
const newestAssessable = (list: ProjectRevision[]) =>
  list.find((project) => !isWorkingTreeRevision(project) && project.state !== 'not_indexed')
  ?? list.find((project) => !isWorkingTreeRevision(project)) ?? list[0];
const preferredRevisionKey = () => {
  const list = scopedProjects.value;
  if (!list.length) return '';
  if (scope.revision.value !== 'latest') {
    const pinned = list.find((project) => project.revision_id === scope.revision.value);
    if (pinned) return revisionKey(pinned);
  }
  const first = newestAssessable(list);
  return first ? revisionKey(first) : '';
};
let syncingFromScope = false;
const applyScope = (force: boolean) => {
  const inScope = scopedProjects.value.some((project) => revisionKey(project) === selectedRevision.value);
  if (!force && inScope) return;
  const next = preferredRevisionKey();
  if (next === selectedRevision.value) return;
  syncingFromScope = true;
  selectedRevision.value = next;
  void nextTick(() => { syncingFromScope = false; });
};
let keepLocalPick = false;
watch(() => [scope.projectId.value, scope.revision.value] as const, () => {
  if (keepLocalPick) { keepLocalPick = false; return; }
  applyScope(true);
});
// Picking a revision here becomes the app-wide choice; the newest one stays "latest".
watch(selectedRevision, (key) => {
  if (syncingFromScope || !key) return;
  const picked = projects.value.find((project) => revisionKey(project) === key);
  if (!picked) return;
  const isNewest = newestAssessable(projects.value.filter((project) => project.project_id === picked.project_id)) === picked;
  const nextRevision = isNewest || !picked.revision_id ? 'latest' : picked.revision_id;
  // Keep this exact pick (e.g. an upload not indexed yet) rather than snapping to the newest revision.
  if (picked.project_id !== scope.projectId.value || nextRevision !== scope.revision.value) keepLocalPick = true;
  scope.select(picked.project_id, nextRevision);
});
const equalWeights = () => Object.fromEntries(categories.map((item) => [item.id, 100 / categories.length])) as Record<CategoryId, number>;
const weights = ref<Record<CategoryId, number>>(equalWeights());
const savedWeights = ref<Record<CategoryId, number>>(equalWeights());
const history = ref<HistoryRow[]>([]);
const selectedReportId = ref('');
const leftCompareId = ref('');
const rightCompareId = ref('');
const currentReport = ref<Report | null>(null);
const selectedSource = ref<{ file: string; line: number } | null>(null);
const selectedEvidenceId = ref<string | null>(null);
const compareReports = ref<[Report | null, Report | null]>([null, null]);
const loadingProjects = ref(true);
const loadingHistory = ref(false);
const loadingReport = ref(false);
const loadingExclusions = ref(false);
const exclusionSettingsUnavailable = ref(false);
const savingExclusions = ref(false);
const exclusionPatterns = ref<string[]>([]);
const savedExclusionPatterns = ref<string[]>([]);
const exclusionMessage = ref('');
const sourceDirectories = ref<string[]>([]);
const loadingSourceDirectories = ref(false);
const sourceDirectoryError = ref('');
const customExclusionInput = ref('');
const assessing = ref(false);
const checkingIndex = ref(false);
const indexReadiness = ref<{ ready: boolean; reason: string } | null>(null);
let indexCheckRequest = 0;
const savingWeights = ref(false);
const deletingReportId = ref('');
const deleteTarget = ref<HistoryRow | null>(null);
const deleteReportError = ref('');
const revisionDeleteTarget = ref<ProjectRevision | null>(null);
const deletingRevision = ref(false);
const revisionDeleteError = ref('');
const error = ref('');
const preferenceMessage = ref('');
const recoveryAction = ref<'projects' | 'history' | 'report' | 'comparison' | 'weights' | 'exclusions' | 'saveExclusions' | 'assessment'>('projects');
let historyRequest = 0;
let reportRequest = 0;
let exclusionsRequest = 0;

const selectedProject = computed(() => projects.value.find((item) => revisionKey(item) === selectedRevision.value) || null);
// Reports saved before the ten-category rubric hold only the five source categories.
const reportCategories = computed(() => categories.filter((item) => currentReport.value?.categories?.[item.id]));
const totalWeight = computed(() => categories.reduce((total, item) => total + Number(weights.value[item.id] || 0), 0));
const weightsChanged = computed(() => categories.some((item) => Number(weights.value[item.id]) !== savedWeights.value[item.id]));
const parsedExclusionPatterns = computed(() => [...new Set(exclusionPatterns.value.map((item) => item.trim()).filter(Boolean))].sort());
const exclusionsChanged = computed(() => JSON.stringify(parsedExclusionPatterns.value) !== JSON.stringify([...savedExclusionPatterns.value].sort()));
const unassessedCategories = computed(() => categories
  .filter((item) => currentReport.value?.categories?.[item.id]?.assessed !== true)
  .map((item) => item.label));
const compareIsEquivalent = computed(() => {
  const [left, right] = compareReports.value;
  if (!left || !right) return true;
  const analyzer = (report: Report) => report.coverage.analyzers.map((item) => `${item.name}:${item.version}:${item.rules_version || ''}`).sort();
  // Different review models, budgets or tool sets make scores non-equivalent (review spec clause 12).
  const review = (report: Report) => {
    const reviews = report.category_reviews;
    return reviews && reviews.status !== 'disabled' ? { model: reviews.model, config: reviews.config } : null;
  };
  return left.rubric_version === right.rubric_version
    && JSON.stringify(review(left)) === JSON.stringify(review(right))
    && left.categories.clean_code?.score_method === right.categories.clean_code?.score_method
    && left.categories.clean_code?.metric_schema === right.categories.clean_code?.metric_schema
    && left.categories.clean_code?.rubric_version === right.categories.clean_code?.rubric_version
    && left.model === right.model
    && left.provider === right.provider
    && JSON.stringify(left.scoring_parameters || null) === JSON.stringify(right.scoring_parameters || null)
    && JSON.stringify(analyzer(left)) === JSON.stringify(analyzer(right))
    && JSON.stringify((left.custom_rules || []).map((rule) => rule.content_hash).sort()) === JSON.stringify((right.custom_rules || []).map((rule) => rule.content_hash).sort())
    && JSON.stringify((left.business_rules || []).map((rule) => `${rule.rule_id}:${rule.version}:${rule.title}:${rule.statement}:${rule.category}`).sort())
      === JSON.stringify((right.business_rules || []).map((rule) => `${rule.rule_id}:${rule.version}:${rule.title}:${rule.statement}:${rule.category}`).sort())
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
const visibleEvidence = computed(() => {
  const report = currentReport.value;
  if (!report) return [];
  const findingsById = new Set(report.findings.map((finding) => finding.evidence_id));
  return report.evidence.filter((item) => item.kind !== 'coverage'
    && (item.kind !== 'semantic_context' || findingsById.has(item.evidence_id)));
});
const cleanCodeReviewWithoutFinding = computed(() => {
  const report = currentReport.value;
  if (!report?.categories.clean_code.assessed) return false;
  const citedEvidence = new Set(report.categories.clean_code.evidence_ids);
  const reviewedContext = report.evidence.some((item) => item.kind === 'semantic_context'
    && item.category === 'clean_code' && citedEvidence.has(item.evidence_id));
  const hasCleanCodeFinding = report.findings.some((item) => item.category === 'clean_code');
  return reviewedContext && !hasCleanCodeFinding;
});
const reportEvidenceRows = computed<ReportEvidenceRow[]>(() => {
  const report = currentReport.value;
  if (!report) return [];
  const findingsById = new Map(report.findings.map((finding) => [finding.evidence_id, finding]));
  const rows = visibleEvidence.value.map((evidence): ReportEvidenceRow => ({
    evidenceId: evidence.evidence_id,
    evidence,
    finding: findingsById.get(evidence.evidence_id) || null,
  }));
  const includedIds = new Set(rows.map((row) => row.evidenceId));
  for (const finding of report.findings) {
    if (!includedIds.has(finding.evidence_id)) {
      rows.push({ evidenceId: finding.evidence_id, evidence: null, finding });
    }
  }
  return rows;
});

const categoryLabel = (category: CategoryId | 'business_rule') => category === 'business_rule'
  ? 'Business rule'
  : categories.find((item) => item.id === category)?.label || 'Assessment';
const confidenceLabel = (category: CategoryScore) => category.confidence ? `${category.confidence} confidence` : 'confidence unavailable';
const ruleLabel = (rule: string) => {
  const known: Record<string, string> = {
    'large-file': 'Large file',
    'long-lines': 'Long lines',
    'builtin-dynamic-code-execution': 'Dynamic code execution',
    'ai-clean-code-review': 'AI Clean Code review',
  };
  return known[rule] || rule.replace(/^(builtin-|custom-)/, '').replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase()) || 'Code evidence';
};
const findingSummary = (finding: Report['findings'][number]) => {
  const lineCount = finding.message.match(/\b(\d+)\s+lines?\b/i)?.[1];
  if (finding.rule === 'ai-clean-code-review') {
    return { title: 'AI Clean Code observation', detail: finding.message };
  }
  if (finding.rule === 'large-file') {
    const count = lineCount ? Number(lineCount).toLocaleString() : 'many';
    return { title: `Large file · ${count} lines`, detail: 'Review whether this file has distinct responsibilities that would be clearer as separate modules.' };
  }
  if (finding.rule === 'long-lines') {
    return { title: 'Several unusually long lines', detail: 'More than ten lines exceed 160 characters, which can make code harder to scan.' };
  }
  if (finding.rule === 'builtin-dynamic-code-execution') {
    return { title: 'Dynamic code execution needs review', detail: 'Check whether executable input can come from an untrusted source and whether a safer alternative is available.' };
  }
  return { title: finding.message.replace(/[.!?]+$/, ''), detail: finding.message };
};
const evidenceLabel = (evidenceId: string, index: number) => {
  const item = currentReport.value?.evidence.find((evidence) => evidence.evidence_id === evidenceId);
  if (!item) return `Assessment evidence ${index + 1}`;
  const category = categoryLabel(item.category);
  if (!item.kind || item.kind === 'finding') return `${category} · ${ruleLabel(item.rule)} · ${index + 1}`;
  if (item.kind === 'coverage') return `${category} · Analyzer coverage · ${index + 1}`;
  if (item.kind === 'semantic_trace') return `${category} · Source flow trace · ${index + 1}`;
  return `${category} · Retrieved code context · ${index + 1}`;
};
const openFindingSource = (file: string, line: number | null | undefined, evidenceId?: string) => {
  selectedSource.value = { file, line: line && line > 0 ? line : 1 };
  selectedEvidenceId.value = evidenceId && currentReport.value?.evidence.some((item) => item.evidence_id === evidenceId)
    ? evidenceId : null;
  if (selectedEvidenceId.value) {
    void nextTick(() => document.getElementById(selectedEvidenceId.value || '')?.scrollIntoView({
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
      block: 'center',
    }));
  }
};
const closeSourcePreview = () => {
  selectedSource.value = null;
  selectedEvidenceId.value = null;
};

const readError = async (response: Response, fallback: string) => {
  const body = await response.json().catch(() => ({}));
  return body.detail || body.error || fallback;
};

const removeRevision = async () => {
  const target = revisionDeleteTarget.value;
  if (!target?.revision_id || deletingRevision.value) return;
  deletingRevision.value = true;
  revisionDeleteError.value = '';
  try {
    const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(target.project_id)}/revisions/${encodeURIComponent(target.revision_id)}`, { method: 'DELETE' });
    if (!response.ok) throw new Error(await readError(response, 'Revision could not be removed.'));
    revisionDeleteTarget.value = null;
    registeredRevisions.value = registeredRevisions.value.filter(item => item.revision_id !== target.revision_id);
    currentReport.value = null;
    history.value = [];
    emit('revisionRemoved');
    await loadProjectsAndPreferences();
  } catch (cause: any) {
    revisionDeleteError.value = cause?.message || 'Revision could not be removed.';
  } finally {
    deletingRevision.value = false;
  }
};

const loadProjectsAndPreferences = async () => {
  loadingProjects.value = true;
  error.value = '';
  try {
    const [projectResponse, preferenceResponse] = await Promise.all([
      makeRequest('/api/code-quality/projects'), makeRequest('/api/code-quality/preferences'),
    ]);
    if (!projectResponse.ok) throw new Error(await readError(projectResponse, 'Indexed projects are unavailable.'));
    registeredRevisions.value = (await projectResponse.json()).projects || [];
    if (!preferenceResponse.ok) throw new Error(await readError(preferenceResponse, 'Assessment preferences are unavailable.'));
    const preference = await preferenceResponse.json();
    if (preference.weights) { weights.value = preference.weights; savedWeights.value = { ...preference.weights }; }
    applyScope(false);
  } catch (cause: any) {
    recoveryAction.value = 'projects';
    error.value = cause?.message || 'Code quality assessment is unavailable.';
  } finally {
    loadingProjects.value = false;
  }
};

const loadProjectExclusions = async (projectId = selectedProject.value?.project_id) => {
  const requestId = ++exclusionsRequest;
  exclusionMessage.value = '';
  exclusionSettingsUnavailable.value = false;
  if (!projectId) {
    exclusionPatterns.value = [];
    savedExclusionPatterns.value = [];
    loadingExclusions.value = false;
    return;
  }
  loadingExclusions.value = true;
  error.value = '';
  try {
    const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(projectId)}/exclusions`);
    if (!response.ok) throw new Error(await readError(response, 'Project assessment exclusions could not be loaded.'));
    const result = await response.json();
    if (requestId !== exclusionsRequest) return;
    const patterns = Array.isArray(result.patterns) ? result.patterns.filter((item: unknown): item is string => typeof item === 'string') : [];
    savedExclusionPatterns.value = patterns;
    exclusionPatterns.value = patterns;
  } catch (cause: any) {
    if (requestId === exclusionsRequest) {
      exclusionSettingsUnavailable.value = true;
      recoveryAction.value = 'exclusions';
      error.value = cause?.message || 'Project assessment exclusions could not be loaded.';
    }
  } finally {
    if (requestId === exclusionsRequest) loadingExclusions.value = false;
  }
};

let sourceDirectoriesRequest = 0;
const loadSourceDirectories = async () => {
  const project = selectedProject.value;
  const requestId = ++sourceDirectoriesRequest;
  sourceDirectoryError.value = '';
  if (!project?.revision_id || project.state !== 'ready') {
    sourceDirectories.value = [];
    loadingSourceDirectories.value = false;
    return;
  }
  loadingSourceDirectories.value = true;
  try {
    const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(project.project_id)}/revisions/${encodeURIComponent(project.revision_id)}/directories`);
    if (!response.ok) throw new Error(await readError(response, 'Project folders could not be loaded.'));
    const result = await response.json();
    if (requestId !== sourceDirectoriesRequest) return;
    sourceDirectories.value = Array.isArray(result.directories)
      ? result.directories.filter((item: unknown): item is string => typeof item === 'string') : [];
  } catch (cause: any) {
    if (requestId === sourceDirectoriesRequest) {
      sourceDirectoryError.value = cause?.message || 'Project folders could not be loaded.';
      sourceDirectories.value = [];
    }
  } finally {
    if (requestId === sourceDirectoriesRequest) loadingSourceDirectories.value = false;
  }
};

const toggleFolderExclusion = (value: { path: string; excluded: boolean }) => {
  const folderPattern = `${value.path}/**`;
  if (value.excluded) {
    if (!exclusionPatterns.value.includes(folderPattern)) exclusionPatterns.value = [...exclusionPatterns.value, folderPattern];
    return;
  }
  exclusionPatterns.value = exclusionPatterns.value.filter((pattern) =>
    pattern !== folderPattern && pattern !== value.path && !pattern.startsWith(`${value.path}/`));
};

const addCustomExclusion = () => {
  const pattern = customExclusionInput.value.trim();
  if (!pattern || exclusionPatterns.value.includes(pattern)) return;
  exclusionPatterns.value = [...exclusionPatterns.value, pattern];
  customExclusionInput.value = '';
};

const saveProjectExclusions = async (): Promise<boolean> => {
  const projectId = selectedProject.value?.project_id;
  if (!projectId || savingExclusions.value) return false;
  const patterns = parsedExclusionPatterns.value;
  savingExclusions.value = true;
  exclusionMessage.value = '';
  error.value = '';
  try {
    const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(projectId)}/exclusions`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patterns }),
    });
    if (!response.ok) throw new Error(await readError(response, 'Project assessment exclusions could not be saved.'));
    const result = await response.json();
    if (selectedProject.value?.project_id !== projectId) return false;
    savedExclusionPatterns.value = result.patterns || [];
    exclusionPatterns.value = savedExclusionPatterns.value;
    exclusionMessage.value = 'Exclusions saved for this project.';
    return true;
  } catch (cause: any) {
    recoveryAction.value = 'saveExclusions';
    error.value = cause?.message || 'Project assessment exclusions could not be saved.';
    return false;
  } finally {
    savingExclusions.value = false;
  }
};

const loadHistory = async (options: { preserveReport?: boolean; preferredReportId?: string } = {}) => {
  const requestId = ++historyRequest;
  const project = selectedProject.value;
  history.value = [];
  if (!options.preserveReport) {
    reportRequest += 1;
    loadingReport.value = false;
    selectedReportId.value = '';
    currentReport.value = null;
    compareReports.value = [null, null];
    leftCompareId.value = '';
    rightCompareId.value = '';
  }
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
      const preferred = options.preferredReportId && history.value.some((item) => item.report_id === options.preferredReportId)
        ? options.preferredReportId
        : history.value[0].report_id;
      selectedReportId.value = preferred;
      if (!options.preserveReport) {
        leftCompareId.value = history.value[0].report_id;
        if (history.value.length > 1) rightCompareId.value = history.value[1].report_id;
      }
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
  const requestId = ++reportRequest;
  selectedReportId.value = reportId;
  selectedSource.value = null;
  selectedEvidenceId.value = null;
  currentReport.value = null;
  if (!reportId) return;
  loadingReport.value = true;
  error.value = '';
  try {
    const report = await loadFullReport(reportId);
    if (requestId === reportRequest && selectedReportId.value === reportId) currentReport.value = report;
  } catch (cause: any) {
    if (requestId === reportRequest) {
      recoveryAction.value = 'report';
      error.value = cause?.message || 'Assessment report could not be loaded. Retry.';
    }
  } finally {
    if (requestId === reportRequest) loadingReport.value = false;
  }
};

const removeReport = async () => {
  const project = selectedProject.value;
  const report = deleteTarget.value;
  if (!project || !report || deletingReportId.value) return;
  deletingReportId.value = report.report_id;
  deleteReportError.value = '';
  try {
    const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(project.project_id)}/reports/${encodeURIComponent(report.report_id)}`, { method: 'DELETE' });
    if (!response.ok) throw new Error(await readError(response, 'Assessment report could not be removed.'));
    deleteTarget.value = null;
    await loadHistory();
  } catch (cause: any) {
    recoveryAction.value = 'history';
    deleteReportError.value = cause?.message || 'Assessment report could not be removed.';
  } finally {
    deletingReportId.value = '';
  }
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
  if (!project?.revision_id || checkingIndex.value || exclusionSettingsUnavailable.value || loadingExclusions.value) return;
  if (!(await checkIndex())) return;
  assessing.value = true;
  error.value = '';
  try {
    if (totalWeight.value !== 100) throw new Error('Category weights must total 100% before assessment.');
    if (exclusionsChanged.value && !(await saveProjectExclusions())) return;
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
    reportRequest += 1;
    loadingReport.value = false;
    selectedReportId.value = report.report_id;
    currentReport.value = report;
    assessing.value = false;
    void loadHistory({ preserveReport: true, preferredReportId: report.report_id });
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
  if (recoveryAction.value === 'exclusions') return loadProjectExclusions();
  if (recoveryAction.value === 'saveExclusions') return saveProjectExclusions();
  return runAssessment();
};

const formatDate = (value: string) => new Date(value).toLocaleString();
const formatScore = (value: number | null | undefined) => value == null ? 'Not assessed' : `${Math.round(value)}`;
const formatTokenUsage = (usage?: TokenUsage | null, provider?: string) => usage
  ? `${usage.total_tokens.toLocaleString()} tokens${usage.estimated ? ' (estimate)' : ''}`
  : provider === 'not-run' ? 'No model call' : 'Unavailable';
const reportCategoryScore = (report: Report | null, category: CategoryId) => report?.categories?.[category]?.assessed ? report.categories[category].score : null;
const scoreDelta = (category: CategoryId) => {
  const left = reportCategoryScore(compareReports.value[0], category);
  const right = reportCategoryScore(compareReports.value[1], category);
  return left == null || right == null ? null : right - left;
};

const checkIndex = async () => {
  const requestId = ++indexCheckRequest;
  const project = selectedProject.value;
  indexReadiness.value = null;
  checkingIndex.value = true;
  try {
    if (!project?.revision_id) {
      indexReadiness.value = { ready: false, reason: 'Index this project revision in Knowledge Base → Files & indexing before running an assessment.' };
      return false;
    }
    const response = await makeRequest(`/api/code-quality/projects/${encodeURIComponent(project.project_id)}/revisions/${encodeURIComponent(project.revision_id)}/precheck`);
    if (!response.ok) throw new Error(await readError(response, 'The project index could not be checked.'));
    const result = await response.json();
    if (requestId !== indexCheckRequest) return false;
    indexReadiness.value = { ready: result.ready === true, reason: result.reason || 'Index this revision in Knowledge Base before assessment.' };
    return indexReadiness.value.ready;
  } catch (cause: any) {
    if (requestId === indexCheckRequest) indexReadiness.value = { ready: false, reason: cause?.message || 'The project index could not be checked. Retry.' };
    return false;
  } finally {
    if (requestId === indexCheckRequest) checkingIndex.value = false;
  }
};

watch(selectedProject, () => {
  void loadHistory();
  void loadSourceDirectories();
  void checkIndex();
});
watch(projects, () => applyScope(false));
watch(() => selectedProject.value?.project_id, (projectId) => loadProjectExclusions(projectId), { immediate: true });
watch([leftCompareId, rightCompareId], loadComparison);
onMounted(loadProjectsAndPreferences);

// ---- Page presentation helpers ----------------------------------------------
const categoryMeta: Record<CategoryId, { color: string; icon: unknown }> = {
  security: { color: '#fb7185', icon: ShieldAlert },
  clean_code: { color: '#818cf8', icon: Sparkles },
  maintainability: { color: '#22d3ee', icon: Wrench },
  reliability: { color: '#34d399', icon: LifeBuoy },
  performance: { color: '#fbbf24', icon: Zap },
  process: { color: '#a78bfa', icon: Workflow },
  testing: { color: '#2dd4bf', icon: FlaskConical },
  documentation: { color: '#60a5fa', icon: BookOpen },
  localization: { color: '#f472b6', icon: Languages },
  accessibility: { color: '#a3e635', icon: Accessibility },
};
const qualityTabs = [
  { id: 'assessment' as const, label: 'Assessment', icon: Gauge, domId: 'quality-assessment-tab', panel: 'quality-assessment-panel' },
  { id: 'compare' as const, label: 'Compare', icon: GitCompareArrows, domId: 'quality-compare-tab', panel: 'quality-compare-panel' },
  { id: 'regression' as const, label: 'Regression', icon: ShieldCheck, domId: 'quality-regression-tab', panel: 'quality-regression-panel' },
  { id: 'performance' as const, label: 'Performance', icon: Zap, domId: 'quality-performance-tab', panel: 'quality-performance-panel' },
  { id: 'rules' as const, label: 'Custom rules', icon: ListChecks, domId: 'quality-rules-tab', panel: 'quality-rules-panel' },
  { id: 'external' as const, label: 'External analyzers', icon: Plug, domId: 'quality-external-analyzers-tab', panel: 'quality-external-analyzers-panel' },
];

const revisionLabel = (project: ProjectRevision) => {
  const identity = project.commit_sha ? project.commit_sha.slice(0, 12) : project.revision_id || 'upload revision';
  return `${project.project_id} · ${identity}${isWorkingTreeRevision(project) ? ' · working tree (can’t assess uncommitted edits)' : ''}`;
};

const indexState = computed<'checking' | 'ready' | 'blocked' | 'loading'>(() => {
  if (loadingProjects.value) return 'loading';
  if (checkingIndex.value) return 'checking';
  if (!selectedProject.value || indexReadiness.value?.ready === false) return 'blocked';
  return indexReadiness.value?.ready === true ? 'ready' : 'checking';
});
const canAssess = computed(() => !(assessing.value || checkingIndex.value || indexReadiness.value?.ready !== true || savingWeights.value
  || savingExclusions.value || loadingExclusions.value || exclusionSettingsUnavailable.value || !selectedProject.value || totalWeight.value !== 100));
const startAssessment = () => {
  activeTab.value = 'assessment';
  void runAssessment();
};

const setupOpen = ref(false);
const setupSummary = computed(() => {
  const parts = categories.map((item) => Number(weights.value[item.id] || 0)).join(' · ');
  const count = parsedExclusionPatterns.value.length;
  return `weights ${parts} · ${count} ${count === 1 ? 'exclusion' : 'exclusions'}`;
});

// Score ring: circumference of r=50 is 2πr.
const RING = 2 * Math.PI * 50;
const ringOffset = computed(() => RING * (1 - Math.max(0, Math.min(100, currentReport.value?.overall_score ?? 0)) / 100));

// Findings filter: by category, or only AI review findings.
type FindingFilter = 'all' | 'ai' | CategoryId;
const findingFilter = ref<FindingFilter>('all');
const rowCategory = (row: ReportEvidenceRow) => (row.finding?.category || row.evidence?.category || 'security') as CategoryId | 'business_rule';
const rowIsAi = (row: ReportEvidenceRow) => row.finding?.tool === 'ai-semantic-review';
const findingFilters = computed(() => {
  const rows = reportEvidenceRows.value;
  const items: Array<{ id: FindingFilter; label: string; count: number }> = [{ id: 'all', label: 'All', count: rows.length }];
  for (const category of categories) {
    const count = rows.filter((row) => rowCategory(row) === category.id).length;
    if (count) items.push({ id: category.id, label: category.label, count });
  }
  const ai = rows.filter(rowIsAi).length;
  if (ai) items.push({ id: 'ai', label: 'AI review', count: ai });
  return items;
});
const visibleEvidenceRows = computed(() => reportEvidenceRows.value.filter((row) => findingFilter.value === 'all'
  || (findingFilter.value === 'ai' ? rowIsAi(row) : rowCategory(row) === findingFilter.value)));
watch(() => currentReport.value?.report_id, () => { findingFilter.value = 'all'; });
const severityColor = (row: ReportEvidenceRow) => {
  const severity = (row.finding?.severity || '').toLowerCase();
  if (['critical', 'high', 'error', 'blocker'].includes(severity)) return '#fb7185';
  if (['medium', 'major', 'warning'].includes(severity)) return '#fbbf24';
  if (rowIsAi(row)) return '#818cf8';
  const category = rowCategory(row);
  return category in categoryMeta ? categoryMeta[category as CategoryId].color : '#8591a8';
};
const severityTone = (severity?: string) => {
  const value = (severity || '').toLowerCase();
  if (['critical', 'high', 'error', 'blocker'].includes(value)) return 'bad';
  if (['medium', 'major', 'warning'].includes(value)) return 'warn';
  return undefined;
};

// History trend: oldest → newest, scored reports only.
const trend = computed(() => [...history.value]
  .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at))
  .slice(-12)
  .map((report) => ({ id: report.report_id, score: report.overall_score, date: report.created_at })));
const shortDate = (value: string) => new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

const statusTone = (status?: string) => (status === 'complete' || status === 'available' || status === 'synchronized'
  ? 'ok' : status === 'partial' || status === 'available_no_matches' ? 'warn' : status === 'unavailable' ? 'warn' : undefined);
</script>

<template>
  <section role="region" aria-label="Code quality assessment" tabindex="0" class="cq-page">
    <div class="cq-wrap">
      <!-- Control bar: revision, index status, sections, actions -->
      <div class="cq-bar">
        <label class="cq-rev" for="quality-revision">
          <GitBranch aria-hidden="true" />
          <span class="sr-only">Project revision</span>
          <select id="quality-revision" v-model="selectedRevision" :disabled="loadingProjects || assessing || checkingIndex || !scopedProjects.length">
            <option v-if="loadingProjects" value="">Loading project revisions…</option>
            <option v-else-if="!scopedProjects.length" value="">{{ scope.projectId.value ? `No ${scope.projectId.value} revisions in Knowledge Base` : 'No project revisions in Knowledge Base' }}</option>
            <option v-for="project in scopedProjects" :key="revisionKey(project)" :value="revisionKey(project)" :title="project.revision_id || project.commit_sha || 'Upload revision'" :disabled="isWorkingTreeRevision(project)">
              {{ revisionLabel(project) }}
            </option>
          </select>
          <ChevronsUpDown aria-hidden="true" />
        </label>
        <span class="cq-state" :data-tone="indexState === 'ready' ? 'ok' : indexState === 'blocked' ? 'warn' : undefined" role="status">
          <LoaderCircle v-if="indexState === 'checking' || indexState === 'loading'" class="cq-spin" aria-hidden="true" />
          <CircleCheck v-else-if="indexState === 'ready'" aria-hidden="true" />
          <TriangleAlert v-else aria-hidden="true" />
          {{ indexState === 'ready' ? 'Index ready' : indexState === 'blocked' ? 'Not ready' : indexState === 'loading' ? 'Loading…' : 'Checking index…' }}
        </span>

        <nav class="cq-tabs" role="tablist" aria-label="Code Quality sections">
          <button
            v-for="tab in qualityTabs"
            :id="tab.domId"
            :key="tab.id"
            type="button"
            role="tab"
            :aria-selected="activeTab === tab.id"
            :aria-controls="tab.panel"
            @click="activeTab = tab.id"
          ><component :is="tab.icon" aria-hidden="true" />{{ tab.label }}</button>
        </nav>

        <div class="cq-bar-actions">
          <button v-if="selectedProject?.revision_id" type="button" class="cq-btn cq-btn-sm" data-variant="danger" :disabled="assessing || deletingRevision" @click="revisionDeleteTarget = selectedProject; revisionDeleteError = ''">
            <Trash2 aria-hidden="true" />Remove revision
          </button>
          <button type="button" class="cq-btn cq-btn-lg" data-variant="primary" :disabled="!canAssess" @click="startAssessment">
            <LoaderCircle v-if="assessing" class="cq-spin" aria-hidden="true" /><Play v-else aria-hidden="true" />
            {{ assessing ? 'Assessing revision…' : weightsChanged ? 'Save weights and assess' : 'Assess this revision' }}
          </button>
        </div>
      </div>

      <!-- Precheck: blocked revision -->
      <div v-if="!loadingProjects && !checkingIndex && (!selectedProject || indexReadiness?.ready === false)" role="status" class="cq-banner" data-tone="warn">
        <TriangleAlert aria-hidden="true" />
        <span class="cq-banner-text">{{ indexReadiness?.reason || 'Add a project revision to Knowledge Base and index it before running an assessment.' }}</span>
        <button type="button" class="cq-btn" @click="emit('openKnowledgeBase')"><Library aria-hidden="true" />Open Knowledge Base</button>
        <button v-if="selectedProject" type="button" class="cq-btn" @click="loadProjectsAndPreferences().then(checkIndex)"><RefreshCw aria-hidden="true" />Check again</button>
      </div>

      <!-- ================= Assessment ================= -->
      <div v-if="activeTab === 'assessment'" id="quality-assessment-panel" role="tabpanel" aria-labelledby="quality-assessment-tab" class="cq-layout">
        <div class="cq-main">
          <div v-if="error" role="alert" class="cq-banner" data-tone="bad">
            <AlertCircle aria-hidden="true" /><span class="cq-banner-text">{{ error }}</span>
            <button type="button" class="cq-btn" @click="retryLastAction"><RefreshCw aria-hidden="true" />Retry</button>
          </div>

          <!-- Setup: weights and exclusions -->
          <details class="cq-setup" :open="setupOpen || totalWeight !== 100 || weightsChanged || exclusionsChanged" @toggle="setupOpen = ($event.target as HTMLDetailsElement).open">
            <summary>
              <SlidersHorizontal aria-hidden="true" /><b>Assessment setup</b>
              <span class="cq-setup-sum">{{ setupSummary }}</span>
              <span v-if="totalWeight !== 100" class="cq-tag" data-tone="warn">Weights total {{ totalWeight }}%</span>
              <span v-else-if="weightsChanged || exclusionsChanged" class="cq-tag" data-tone="warn">Unsaved changes</span>
              <ChevronDown class="cq-chev" aria-hidden="true" />
            </summary>
            <div class="cq-setup-body">
              <div>
                <h3 class="cq-h4">Category weights</h3>
                <p class="cq-hint">The weighted total appears only when all ten categories are assessed. Scoring uses a fixed rubric, temperature 0 and top_p 1. Providers can still change behaviour; comparisons flag known differences in model, rubric, analyzers, coverage, weights and scoring settings.</p>
                <div class="cq-weights">
                  <label v-for="category in categories" :key="category.id" class="cq-weight" :style="{ '--c': categoryMeta[category.id].color }">
                    <span>{{ category.label }}</span>
                    <span class="cq-weight-input"><input v-model.number="weights[category.id]" type="number" min="0" max="100" :aria-label="`${category.label} weight`" /><i>%</i></span>
                  </label>
                </div>
                <div class="cq-total">
                  <span class="cq-total-bar" aria-hidden="true">
                    <i v-for="category in categories" :key="category.id" :style="{ width: `${Math.max(0, Number(weights[category.id] || 0)) / Math.max(totalWeight, 100) * 100}%`, background: categoryMeta[category.id].color }"></i>
                  </span>
                  <span :data-tone="totalWeight === 100 ? 'ok' : 'warn'">Total <strong>{{ totalWeight }}%</strong><template v-if="totalWeight !== 100"> · weights must total 100%</template></span>
                  <button type="button" class="cq-btn cq-btn-sm" :disabled="totalWeight !== 100 || savingWeights" @click="saveWeights"><Save aria-hidden="true" />{{ savingWeights ? 'Saving…' : 'Save weights' }}</button>
                </div>
                <p v-if="weightsChanged && totalWeight === 100" class="cq-hint">Changes apply when you save or start an assessment.</p>
                <p v-if="preferenceMessage" class="cq-hint" data-tone="ok">{{ preferenceMessage }}</p>
              </div>

              <div>
                <h3 class="cq-h4">Excluded files and folders</h3>
                <p class="cq-hint">Saved for {{ selectedProject?.project_id || 'this project' }} and applied to future assessments. Built-in artifact exclusions still apply.</p>
                <div v-if="loadingSourceDirectories" class="cq-hint cq-inline"><LoaderCircle class="cq-spin" aria-hidden="true" />Loading revision folders…</div>
                <div v-else-if="sourceDirectoryError" class="cq-hint cq-inline" data-tone="bad" role="alert">
                  {{ sourceDirectoryError }}
                  <button type="button" class="cq-link" @click="loadSourceDirectories">Retry folder loading</button>
                </div>
                <DirectoryExclusionTree v-else :directories="sourceDirectories" :patterns="parsedExclusionPatterns" :disabled="loadingExclusions || exclusionSettingsUnavailable || savingExclusions || assessing" class="cq-tree" @toggle="toggleFolderExclusion" />
                <form class="cq-addrow" @submit.prevent="addCustomExclusion">
                  <label for="quality-custom-exclusion" class="sr-only">Add a file path or exclusion glob</label>
                  <input id="quality-custom-exclusion" v-model="customExclusionInput" class="cq-input" :disabled="loadingExclusions || exclusionSettingsUnavailable || savingExclusions || assessing" type="text" maxlength="300" placeholder="Add a file path or glob, e.g. src/LegacyReport.php" />
                  <button type="submit" class="cq-btn cq-btn-sm" :disabled="!customExclusionInput.trim() || loadingExclusions || exclusionSettingsUnavailable || savingExclusions || assessing">Add</button>
                </form>
                <ul v-if="parsedExclusionPatterns.length" class="cq-pats" :aria-label="`Excluded patterns (${parsedExclusionPatterns.length})`">
                  <li v-for="pattern in parsedExclusionPatterns" :key="pattern" class="cq-pat">
                    <span>{{ pattern }}</span>
                    <button type="button" :disabled="loadingExclusions || exclusionSettingsUnavailable || savingExclusions || assessing" :aria-label="`Remove exclusion ${pattern}`" @click="exclusionPatterns = exclusionPatterns.filter((item) => item !== pattern)"><X aria-hidden="true" /></button>
                  </li>
                </ul>
                <p class="cq-hint">The tree shows folders only; selecting one excludes everything inside it. Patterns use `/`; `*` and `?` match within a path segment, and `**` can match across folders.</p>
                <div class="cq-inline">
                  <button type="button" class="cq-btn cq-btn-sm" :disabled="!selectedProject || loadingExclusions || exclusionSettingsUnavailable || savingExclusions || !exclusionsChanged" @click="saveProjectExclusions">
                    <LoaderCircle v-if="savingExclusions" class="cq-spin" aria-hidden="true" /><Save v-else aria-hidden="true" />{{ savingExclusions ? 'Saving…' : 'Save exclusions' }}
                  </button>
                  <span v-if="exclusionMessage" class="cq-hint" data-tone="ok" role="status">{{ exclusionMessage }}</span>
                </div>
              </div>
              <PublicRouteSettings :project-id="selectedProject?.project_id" :disabled="assessing" />
            </div>
          </details>

          <!-- Running -->
          <div v-if="assessing" class="cq-running" role="status" aria-live="polite">
            <div class="cq-running-top"><LoaderCircle class="cq-spin" aria-hidden="true" />Assessing {{ selectedProject ? revisionLabel(selectedProject) : '' }}<span class="cq-tag">in progress</span></div>
            <div role="progressbar" aria-label="Project assessment progress" aria-valuetext="Assessment is running" class="cq-track" data-indeterminate="true"></div>
            <p>Running source checks and evidence-based scoring. The report appears here when it's done; you can keep using the app.</p>
          </div>

          <!-- Report -->
          <template v-if="currentReport">
            <section class="cq-card cq-hero" aria-labelledby="cq-report-title">
              <div class="cq-ring" :aria-label="currentReport.overall_score == null ? 'Overall score unavailable' : `Overall score ${currentReport.overall_score} out of 100`" role="img">
                <svg viewBox="0 0 120 120" aria-hidden="true">
                  <defs><linearGradient id="cq-ring-gradient" x1="0" y1="0" x2="1" y2="1"><stop style="stop-color: rgb(var(--shell-grad-a-rgb))" /><stop offset="1" style="stop-color: var(--accent-primary)" /></linearGradient></defs>
                  <circle cx="60" cy="60" r="50" class="cq-ring-track" />
                  <circle v-if="currentReport.overall_score != null" cx="60" cy="60" r="50" class="cq-ring-value" :stroke-dasharray="RING" :stroke-dashoffset="ringOffset" />
                </svg>
                <div class="cq-ring-label"><b>{{ currentReport.overall_score ?? '—' }}</b><span>{{ currentReport.overall_score == null ? 'no overall score' : 'out of 100' }}</span></div>
              </div>
              <div class="min-w-0">
                <p class="cq-eyebrow">Assessment report</p>
                <h2 id="cq-report-title" class="cq-hero-title">{{ currentReport.overall_score == null ? 'Overall score unavailable' : `${currentReport.overall_score} / 100` }}</h2>
                <p v-if="currentReport.overall_score == null && unassessedCategories.length" class="cq-hint" data-tone="warn">Not assessed: {{ unassessedCategories.join(', ') }}. Each category card explains why.</p>
                <div class="cq-meta">
                  <span class="cq-tag"><Calendar aria-hidden="true" />{{ formatDate(currentReport.created_at) }}</span>
                  <span class="cq-tag"><GitCommitHorizontal aria-hidden="true" />{{ currentReport.commit_sha?.slice(0, 12) || currentReport.revision_id }}</span>
                  <span class="cq-tag"><Files aria-hidden="true" />{{ currentReport.coverage.scanned_files }} / {{ currentReport.coverage.files }} files scanned</span>
                  <span class="cq-tag"><Code aria-hidden="true" />{{ Object.keys(currentReport.coverage.languages).join(', ') || 'No detected languages' }}</span>
                  <span class="cq-tag" :data-tone="currentReport.token_usage?.estimated ? 'warn' : undefined"><Cpu aria-hidden="true" />
                    <template v-if="currentReport.token_usage">{{ formatTokenUsage(currentReport.token_usage) }} · {{ currentReport.token_usage.estimated ? 'estimated' : 'provider-reported' }}</template>
                    <template v-else>{{ formatTokenUsage(null, currentReport.provider) }}</template>
                  </span>
                </div>
                <p v-if="currentReport.token_usage" class="cq-hint">Evaluator input {{ currentReport.token_usage.prompt_tokens?.toLocaleString() ?? 'unavailable' }} · output {{ currentReport.token_usage.completion_tokens?.toLocaleString() ?? 'unavailable' }}<template v-if="currentReport.token_usage.batch_count"> · {{ currentReport.token_usage.summarized_evidence_count }} evidence items summarized across {{ currentReport.token_usage.batch_count }} requests before scoring; summaries may omit details</template>.</p>
                <p class="cq-hint">
                  <template v-if="currentReport.coverage.skipped_files">{{ currentReport.coverage.skipped_files }} over scan limits · </template>
                  <template v-if="currentReport.coverage.excluded_files">{{ currentReport.coverage.excluded_files }} artifacts excluded · </template>
                  <template v-if="currentReport.coverage.user_excluded_files">{{ currentReport.coverage.user_excluded_files }} excluded by project settings · </template>
                  {{ currentReport.model }} · rubric {{ currentReport.rubric_version }}
                </p>
              </div>
            </section>

            <div class="cq-cats">
              <article v-for="category in reportCategories" :key="category.id" class="cq-cat" :style="{ '--c': categoryMeta[category.id].color }" :data-assessed="currentReport.categories[category.id].assessed">
                <div class="cq-cat-top"><component :is="categoryMeta[category.id].icon" aria-hidden="true" />{{ category.label }}</div>
                <div class="cq-cat-score">{{ formatScore(currentReport.categories[category.id].score) }}<small v-if="currentReport.categories[category.id].assessed"> /100</small></div>
                <div class="cq-cat-bar" aria-hidden="true"><i :style="{ transform: `scaleX(${(currentReport.categories[category.id].score ?? 0) / 100})` }"></i></div>
                <p>{{ currentReport.categories[category.id].reason }}</p>
                <p class="cq-cat-foot">{{ currentReport.coverage.categories[category.id].status || (currentReport.coverage.categories[category.id].supported ? 'covered' : 'unavailable') }} · {{ currentReport.coverage.categories[category.id].evidence_count }} signals · weight {{ currentReport.weights[category.id] }}%</p>
                <p v-if="currentReport.shadow_scoring" class="cq-cat-foot" data-tone="ai">Experimental: {{ formatScore(currentReport.shadow_scoring.categories[category.id].score) }} · {{ confidenceLabel(currentReport.shadow_scoring.categories[category.id]) }} · {{ currentReport.shadow_scoring.categories[category.id].finding_count ?? 0 }} findings</p>
              </article>
            </div>

            <BusinessRuleReviews v-if="currentReport.business_rules?.length" :rules="currentReport.business_rules" :reviews="currentReport.business_rule_reviews || []" :evidence="currentReport.evidence" @view-source="openFindingSource" />
            <div v-if="currentReport.coverage.business_rule_review && !currentReport.business_rules?.length" class="cq-banner" :data-tone="currentReport.coverage.business_rule_review.status === 'unavailable' ? 'warn' : undefined">
              <Radar aria-hidden="true" />
              <span v-if="currentReport.coverage.business_rule_review.status === 'not_configured'" class="cq-banner-text">Business rule review isn't configured for this project. Connect an Outline collection in Knowledge Base → Business rules.</span>
              <span v-else-if="currentReport.coverage.business_rule_review.status === 'unavailable'" class="cq-banner-text">Business rule review couldn't sync Outline: {{ currentReport.coverage.business_rule_review.reason }}</span>
              <span v-else class="cq-banner-text">Outline synced, but the selected collection has no usable business rule documents.</span>
            </div>
            <p v-else-if="currentReport.coverage.business_rule_review && currentReport.business_rules?.length" class="cq-hint">Outline snapshot: {{ currentReport.coverage.business_rule_review.imported_count }} documents reviewed from collection {{ currentReport.coverage.business_rule_review.collection_id }}.</p>

            <!-- Coverage and diagnostics -->
            <section class="cq-card" aria-labelledby="cq-diag-title">
              <div class="cq-sec-head"><h3 id="cq-diag-title" class="cq-sec-title"><Radar aria-hidden="true" />Coverage and diagnostics</h3><span class="cq-hint cq-push">How the scores were produced</span></div>

              <details v-if="currentReport.coverage.user_exclusion_patterns?.length" class="cq-diag">
                <summary><Files aria-hidden="true" />Project exclusions used<span class="cq-tag">{{ currentReport.coverage.user_exclusion_patterns.length }}</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <ul class="cq-diag-body cq-mono"><li v-for="pattern in currentReport.coverage.user_exclusion_patterns" :key="pattern">{{ pattern }}</li></ul>
              </details>

              <details v-if="currentReport.coverage.semantic_context" class="cq-diag">
                <summary><SearchCode aria-hidden="true" />Revision-matched code context<span class="cq-tag" :data-tone="statusTone(currentReport.coverage.semantic_context.status)">{{ currentReport.coverage.semantic_context.status === 'available' ? 'Retrieved' : currentReport.coverage.semantic_context.status === 'available_no_matches' ? 'No matches' : 'Unavailable' }}</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <div class="cq-diag-body" aria-live="polite">
                  <p>{{ currentReport.coverage.semantic_context.evidence_count }} code excerpts from {{ currentReport.coverage.semantic_context.query_count }} bounded searches for this revision<span v-if="currentReport.coverage.semantic_context.business_rule_query_count">, including {{ currentReport.coverage.semantic_context.business_rule_query_count }} business rule searches</span>. {{ currentReport.coverage.semantic_context.reason || "Retrieved snippets provide context; they don't prove complete control-flow coverage." }}</p>
                  <p v-if="currentReport.coverage.semantic_context.failure_code" class="cq-hint">Diagnostic: {{ currentReport.coverage.semantic_context.failure_code.replaceAll('_', ' ') }}<span v-if="currentReport.coverage.semantic_context.diagnostic_detail"> · {{ currentReport.coverage.semantic_context.diagnostic_detail }}</span> · {{ currentReport.coverage.semantic_context.candidate_count ?? 0 }} candidates · {{ currentReport.coverage.semantic_context.searched_collection_count ?? 0 }} collections searched<span v-if="currentReport.coverage.semantic_context.rejected_revision_count"> · {{ currentReport.coverage.semantic_context.rejected_revision_count }} revision-mismatched candidates discarded</span><span v-if="currentReport.coverage.semantic_context.rejected_source_count"> · {{ currentReport.coverage.semantic_context.rejected_source_count }} out-of-scope candidates discarded</span></p>
                  <div v-if="currentReport.coverage.semantic_context.category_search" class="cq-mini-grid" aria-label="Semantic retrieval by assessment category">
                    <div v-for="category in categories" :key="category.id" class="cq-mini">
                      <b>{{ category.label }}</b>
                      <span>{{ currentReport.coverage.semantic_context.category_search[category.id]?.attempted_queries ?? 0 }} queries · {{ currentReport.coverage.semantic_context.category_search[category.id]?.accepted_count ?? 0 }} accepted</span>
                      <span v-if="(currentReport.coverage.semantic_context.category_search[category.id]?.rejected_revision_count ?? 0) + (currentReport.coverage.semantic_context.category_search[category.id]?.rejected_source_count ?? 0) > 0" data-tone="warn">{{ currentReport.coverage.semantic_context.category_search[category.id]?.rejected_revision_count ?? 0 }} revision mismatch · {{ currentReport.coverage.semantic_context.category_search[category.id]?.rejected_source_count ?? 0 }} out of scope</span>
                    </div>
                  </div>
                  <p v-if="currentReport.coverage.semantic_context.omitted_business_rule_ids?.length" class="cq-hint" data-tone="warn">{{ currentReport.coverage.semantic_context.omitted_business_rule_ids.length }} business rules exceeded the retrieval limit; their reviews remain unknown.</p>
                </div>
              </details>

              <details v-if="currentReport.coverage.category_review_context && currentReport.coverage.category_review_context.status !== 'not_needed'" class="cq-diag">
                <summary><FileSearch aria-hidden="true" />Source samples used for category scores<span class="cq-tag">{{ currentReport.coverage.category_review_context.excerpt_count }} excerpts</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <div class="cq-diag-body">
                  <p>{{ currentReport.coverage.category_review_context.excerpt_count }} bounded source excerpts from {{ currentReport.coverage.category_review_context.scanned_files }} files were taken from this revision to fill categories without other evidence. They're review context, not findings. {{ currentReport.coverage.category_review_context.reason }}</p>
                  <div class="cq-mini-grid">
                    <div v-for="category in categories" :key="category.id" class="cq-mini"><b>{{ category.label }}</b><span>{{ currentReport.coverage.category_review_context.categories[category.id] || 0 }} excerpts</span></div>
                  </div>
                </div>
              </details>

              <details v-if="currentReport.category_reviews" class="cq-diag">
                <summary><Radar aria-hidden="true" />Category reviews<span class="cq-tag" :data-tone="statusTone(currentReport.category_reviews.status)">{{ currentReport.category_reviews.status === 'disabled' ? 'Off' : currentReport.category_reviews.status }}</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <div class="cq-diag-body" aria-live="polite">
                  <p v-if="currentReport.category_reviews.status === 'disabled' || !Object.keys(currentReport.category_reviews.categories).length">{{ currentReport.category_reviews.reason }}</p>
                  <template v-else>
                    <p>{{ currentReport.category_reviews.model }} explored this revision with read-only tools, one budgeted review per category ({{ currentReport.category_reviews.config.max_tool_calls }} tool calls, {{ currentReport.category_reviews.config.max_tokens.toLocaleString() }} tokens and {{ currentReport.category_reviews.config.max_seconds }} s each; {{ (currentReport.category_reviews.total_tokens || 0).toLocaleString() }} of {{ currentReport.category_reviews.config.global_max_tokens.toLocaleString() }} tokens used in {{ currentReport.category_reviews.seconds }} s). Findings are kept only when a tool showed the cited lines.</p>
                    <div v-for="(review, key) in currentReport.category_reviews.categories" :key="key" class="cq-review">
                      <p class="cq-review-head"><b>{{ categoryLabel(key as CategoryId) }}</b>
                        <span class="cq-tag" :data-tone="statusTone(review.status)">{{ review.status }}</span>
                        <span>{{ review.usage.tool_calls }}/{{ review.budget.max_tool_calls }} tool calls · {{ review.usage.total_tokens.toLocaleString() }}{{ review.usage.estimated ? ' (estimated)' : '' }} tokens · {{ review.usage.seconds }} s · {{ review.accepted_findings }} kept</span>
                        <span v-if="review.rejected_citations" data-tone="warn">· {{ review.rejected_citations }} rejected citations</span></p>
                      <p v-if="review.reason" class="cq-hint" data-tone="warn">{{ review.reason }}</p>
                      <p v-if="review.summary" class="cq-hint">{{ review.summary }}</p>
                      <ul v-if="review.trace.length" class="cq-mono">
                        <li v-for="(step, index) in review.trace" :key="index">{{ step.ok ? '✓' : '✗' }} {{ step.tool }} {{ Object.values(step.arguments).map((value) => String(value)).join(' ').slice(0, 120) }} · {{ step.result_bytes.toLocaleString() }} bytes<span v-if="step.ranges_returned"> · {{ step.ranges_returned }} citable ranges</span><span v-if="step.error"> · {{ step.error }}</span></li>
                      </ul>
                    </div>
                  </template>
                </div>
              </details>

              <details v-if="currentReport.coverage.semantic_flow_tracing" class="cq-diag">
                <summary><Waypoints aria-hidden="true" />Semantic flow tracing<span class="cq-tag" :data-tone="statusTone(currentReport.coverage.semantic_flow_tracing.status)">{{ currentReport.coverage.semantic_flow_tracing.status === 'complete' ? 'Complete within scope' : currentReport.coverage.semantic_flow_tracing.status === 'partial' ? 'Partial' : 'Unavailable' }}</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <div class="cq-diag-body" aria-live="polite">
                  <p>{{ currentReport.coverage.semantic_flow_tracing.trace_count }} request-to-sensitive-operation paths · {{ currentReport.coverage.semantic_flow_tracing.node_count }} symbols · {{ currentReport.coverage.semantic_flow_tracing.edge_count }} resolved calls · {{ currentReport.coverage.semantic_flow_tracing.unresolved_edge_count }} unresolved. {{ currentReport.coverage.semantic_flow_tracing.reason }}</p>
                  <ul v-if="currentReport.coverage.semantic_flow_tracing.unresolved_edges?.length" class="cq-mono" data-tone="warn">
                    <li v-for="(edge, index) in currentReport.coverage.semantic_flow_tracing.unresolved_edges.slice(0, 8)" :key="`${edge.file}:${edge.line}:${edge.source_symbol}:${index}`">{{ edge.file }}:{{ edge.line }} · {{ edge.source_symbol }} → {{ edge.target_symbol || 'unresolved reference' }} ({{ edge.reason }})</li>
                  </ul>
                  <p v-if="currentReport.coverage.semantic_flow_tracing.limits_reached?.length" class="cq-hint" data-tone="warn">Limits reached: {{ currentReport.coverage.semantic_flow_tracing.limits_reached.join(', ') }}</p>
                </div>
              </details>

              <details v-if="currentReport.coverage.php_design_signals" class="cq-diag">
                <summary><Boxes aria-hidden="true" />PHP design review signals<span class="cq-tag" :data-tone="statusTone(currentReport.coverage.php_design_signals.status)">{{ currentReport.coverage.php_design_signals.status === 'complete' ? 'Indexed scope complete' : currentReport.coverage.php_design_signals.status === 'partial' ? 'Partial' : 'Unavailable' }}</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <div class="cq-diag-body" aria-live="polite">
                  <p v-if="currentReport.coverage.php_design_signals.status !== 'unavailable'">{{ currentReport.coverage.php_design_signals.class_count }} classes · {{ currentReport.coverage.php_design_signals.method_count }} methods · {{ currentReport.coverage.php_design_signals.signal_count }} review signals. Static checks cover {{ currentReport.coverage.php_design_signals.principles_with_static_signals.join(', ') }} within the indexed PHP scope. {{ currentReport.coverage.php_design_signals.reason }}</p>
                  <p v-else>{{ currentReport.coverage.php_design_signals.reason }}</p>
                  <p v-if="currentReport.coverage.php_design_signals.signals_truncated" class="cq-hint" data-tone="warn">The signal limit was reached; more candidates may be omitted.</p>
                </div>
              </details>

              <details v-if="currentReport.coverage.analyzer_failures.length || Object.keys(currentReport.coverage.unsupported_extensions).length || currentReport.coverage.skipped_files || currentReport.coverage.excluded_files" class="cq-diag">
                <summary><TriangleAlert aria-hidden="true" />Analyzer warnings<span class="cq-tag" :data-tone="currentReport.coverage.analyzer_failures.length ? 'warn' : undefined">{{ currentReport.coverage.analyzer_failures.length ? `${currentReport.coverage.analyzer_failures.length} ${currentReport.coverage.analyzer_failures.length === 1 ? 'pass' : 'passes'} incomplete` : 'Coverage notes' }}</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <div class="cq-diag-body">
                  <p v-if="currentReport.coverage.analyzer_failures.length" data-tone="warn">Some analyzer passes couldn't complete: {{ currentReport.coverage.analyzer_failures.map((item) => `${item.name}: ${item.reason}`).join(' · ') }}</p>
                  <p v-if="Object.keys(currentReport.coverage.unsupported_extensions).length">Source-like file types without an analyzer: {{ Object.entries(currentReport.coverage.unsupported_extensions).map(([extension, count]) => `${extension} (${count})`).join(', ') }}</p>
                  <p v-if="currentReport.coverage.skipped_files">{{ currentReport.coverage.skipped_files }} files exceeded source scan limits and weren't analyzed.</p>
                  <p v-if="currentReport.coverage.excluded_files">{{ currentReport.coverage.excluded_files }} source-like files in dependency, build, cache, log, generated or temporary paths were excluded.</p>
                </div>
              </details>

              <details v-if="currentReport.custom_rules?.length" class="cq-diag">
                <summary><ListChecks aria-hidden="true" />Custom rule snapshots used<span class="cq-tag">{{ currentReport.custom_rules.length }}</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <ul class="cq-diag-body cq-mono"><li v-for="rule in currentReport.custom_rules" :key="rule.rule_id">{{ rule.title }} · {{ rule.content_hash }}</li></ul>
              </details>

              <details v-if="currentReport.shadow_scoring" class="cq-diag">
                <summary><FlaskConical aria-hidden="true" />Experimental analyzer baseline<span class="cq-tag" data-tone="ai">Not used for scores</span><ChevronDown class="cq-chev" aria-hidden="true" /></summary>
                <div class="cq-diag-body"><p>These experimental scores are being evaluated and don't determine the report's official scores. Coverage and rule weights still need calibration. Each category card shows its experimental value.</p></div>
              </details>
            </section>

            <SourcePreview v-if="selectedSource && !selectedEvidenceId" :key="`${currentReport.report_id}:${selectedSource.file}:${selectedSource.line}`" :project-id="currentReport.project_id" :revision-id="currentReport.revision_id" :commit-sha="currentReport.commit_sha" :file-path="selectedSource.file" :line="selectedSource.line" @close="closeSourcePreview" />

            <!-- Findings -->
            <section id="assessment-evidence" class="cq-card" aria-labelledby="cq-findings-title">
              <div class="cq-sec-head">
                <h3 id="cq-findings-title" class="cq-sec-title"><Bug aria-hidden="true" />Findings and evidence</h3>
                <span class="cq-tag">{{ reportEvidenceRows.length }}</span>
                <div v-if="findingFilters.length > 2" class="cq-filters cq-push" role="group" aria-label="Filter findings">
                  <button v-for="item in findingFilters" :key="item.id" type="button" class="cq-fchip" :aria-pressed="findingFilter === item.id" @click="findingFilter = item.id">{{ item.label }} <span>{{ item.count }}</span></button>
                </div>
              </div>
              <p v-if="reportEvidenceRows.length" class="cq-hint">Each finding appears once with its source evidence and any available trace.</p>
              <p v-if="cleanCodeReviewWithoutFinding" class="cq-note">The AI reviewed revision-matched Clean Code excerpts but didn't substantiate a specific Clean Code problem. The excerpts are review context, not findings.</p>
              <p v-if="!reportEvidenceRows.length" class="cq-note">No findings or source-level evidence were emitted. This doesn't establish that the code has no issues.</p>

              <article v-for="(row, evidenceIndex) in visibleEvidenceRows" :id="row.evidenceId" :key="row.evidenceId" class="cq-finding" :style="{ '--s': severityColor(row) }">
                <span class="cq-finding-bar" aria-hidden="true"></span>
                <div class="min-w-0">
                  <div class="cq-finding-top">
                    <b>{{ row.finding ? findingSummary(row.finding).title : evidenceLabel(row.evidenceId, evidenceIndex) }}</b>
                    <span v-if="row.finding?.tool === 'ai-semantic-review'" class="cq-tag" data-tone="ai">AI review</span>
                    <span v-else class="cq-tag" :data-tone="severityTone(row.finding?.severity)">{{ row.finding?.severity || row.evidence?.kind || 'evidence' }}</span>
                    <span class="cq-tag">{{ categoryLabel(row.finding?.category || row.evidence?.category || 'security') }}</span>
                  </div>
                  <p v-if="row.finding" class="cq-finding-text">{{ findingSummary(row.finding).detail }}</p>
                  <p v-else-if="row.evidence" class="cq-finding-text">{{ row.evidence.message }}</p>
                  <details v-if="row.finding && row.evidence" class="cq-finding-more">
                    <summary>{{ row.finding.tool === 'ai-semantic-review' ? 'Cited code context' : 'Analyzer evidence' }} · {{ ruleLabel(row.evidence.rule) }}</summary>
                    <p>{{ row.evidence.message }}</p>
                  </details>
                  <p v-else-if="row.finding" class="cq-finding-text">{{ row.finding.message }}</p>
                  <div v-if="row.finding?.file || row.evidence?.file" class="cq-finding-loc">
                    <span class="cq-file">{{ row.finding?.file || row.evidence?.file }}<template v-if="row.finding?.line || row.evidence?.line">:{{ row.finding?.line || row.evidence?.line }}</template></span>
                    <span v-if="row.finding?.line === 1">· file-level anchor</span>
                    <button v-if="(row.finding?.file || row.evidence?.file) && (row.finding?.line || row.evidence?.line)" type="button" class="cq-btn cq-btn-sm" @click="openFindingSource(row.finding?.file || row.evidence?.file || '', row.finding?.line || row.evidence?.line, row.evidenceId)"><Eye aria-hidden="true" />View source</button>
                  </div>
                  <ol v-if="row.evidence?.trace?.nodes?.length" class="cq-trace">
                    <li v-for="(node, index) in row.evidence.trace.nodes" :key="`${node.file}:${node.start_line}:${node.symbol}`">
                      <span class="cq-trace-sym">{{ node.symbol }}</span>
                      <span> · {{ node.file }}:{{ node.start_line }}–{{ node.end_line }}</span>
                      <span v-if="index < (row.evidence?.trace?.edges.length || 0)"> · calls →</span>
                    </li>
                  </ol>
                  <p v-if="row.evidence?.trace?.nodes?.length" class="cq-hint">Call reachability only; this trace doesn't prove that a specific input value reaches the operation.</p>
                  <SourcePreview v-if="selectedSource && selectedEvidenceId === row.evidenceId" :key="`${currentReport.report_id}:${selectedSource.file}:${selectedSource.line}`" class="cq-finding-source" :project-id="currentReport.project_id" :revision-id="currentReport.revision_id" :commit-sha="currentReport.commit_sha" :file-path="selectedSource.file" :line="selectedSource.line" @close="closeSourcePreview" />
                </div>
              </article>
              <p v-if="reportEvidenceRows.length && !visibleEvidenceRows.length" class="cq-note">No findings match this filter.</p>
            </section>
          </template>

          <div v-else-if="loadingReport" class="cq-card cq-inline" role="status"><LoaderCircle class="cq-spin" aria-hidden="true" />Loading report…</div>
          <div v-else-if="!loadingHistory && !history.length" class="cq-empty">
            <span class="cq-empty-icon"><Gauge aria-hidden="true" /></span>
            <h2>No assessment reports yet</h2>
            <p>Pick weights in Assessment setup, or keep the equal-weight default, then assess this indexed revision.</p>
            <button type="button" class="cq-btn cq-btn-lg" data-variant="primary" :disabled="!canAssess" @click="startAssessment"><Play aria-hidden="true" />Assess this revision</button>
          </div>
        </div>

        <!-- History rail -->
        <aside class="cq-rail" aria-labelledby="cq-history-title">
          <div class="cq-card">
            <div class="cq-sec-head">
              <h2 id="cq-history-title" class="cq-sec-title">Report history</h2>
              <LoaderCircle v-if="loadingHistory" class="cq-spin cq-push" aria-hidden="true" />
            </div>
            <p class="cq-hint">{{ history.length }} saved for this project</p>
            <div v-if="trend.length > 1" class="cq-spark" role="img" :aria-label="`Overall score trend across ${trend.length} reports`">
              <i v-for="item in trend" :key="item.id" :class="{ 'is-on': item.id === selectedReportId, 'is-empty': item.score == null }" :style="{ height: `${Math.max(6, item.score ?? 6)}%` }" :title="`${shortDate(item.date)} · ${item.score ?? 'no overall score'}`"></i>
            </div>
            <div v-if="trend.length > 1" class="cq-spark-labels"><span>{{ shortDate(trend[0].date) }}</span><span>{{ shortDate(trend[trend.length - 1].date) }}</span></div>

            <div v-if="history.length" class="cq-history">
              <div v-for="report in history" :key="report.report_id" class="cq-hist" :aria-current="selectedReportId === report.report_id ? 'true' : undefined">
                <button type="button" class="cq-hist-main" :aria-label="`Show assessment report from ${formatDate(report.created_at)}`" :aria-pressed="selectedReportId === report.report_id" :aria-busy="selectedReportId === report.report_id && loadingReport" @click="chooseReport(report.report_id)">
                  <span class="cq-hist-score">{{ report.overall_score ?? '—' }}</span>
                  <span class="cq-hist-meta">
                    <b>{{ formatDate(report.created_at) }}</b>
                    <span>{{ report.commit_sha?.slice(0, 12) || report.revision_id }} · {{ report.coverage }} files</span>
                    <span>{{ formatTokenUsage(report.token_usage, report.provider) }}</span>
                  </span>
                </button>
                <button type="button" class="cq-hist-del" :aria-label="`Delete assessment from ${formatDate(report.created_at)}`" :title="deletingReportId === report.report_id ? 'Deleting report' : 'Delete report'" :disabled="!!deletingReportId" @click="deleteTarget = report; deleteReportError = ''">
                  <LoaderCircle v-if="deletingReportId === report.report_id" class="cq-spin" aria-hidden="true" /><Trash2 v-else aria-hidden="true" />
                </button>
              </div>
            </div>
            <p v-else-if="!loadingHistory" class="cq-hint">Reports from every indexed revision in this project will appear here.</p>
          </div>
        </aside>
      </div>

      <!-- ================= Compare ================= -->
      <div v-else-if="activeTab === 'compare'" id="quality-compare-panel" role="tabpanel" aria-labelledby="quality-compare-tab">
        <section class="cq-card">
          <div class="cq-sec-head"><h2 class="cq-sec-title"><GitCompareArrows aria-hidden="true" />Compare saved reports</h2></div>
          <p class="cq-hint">Choose two reports from {{ selectedProject?.project_id || 'this project' }} to compare their scores and findings.</p>
          <div class="cq-cmp-pick">
            <label class="cq-field">Earlier report
              <select v-model="leftCompareId" class="cq-input"><option value="">Select a report</option><option v-for="report in history" :key="report.report_id" :value="report.report_id">{{ formatDate(report.created_at) }} · {{ report.overall_score ?? 'Unscored' }}</option></select>
            </label>
            <span class="cq-cmp-arrow" aria-hidden="true"><ArrowRight /></span>
            <label class="cq-field">Later report
              <select v-model="rightCompareId" class="cq-input"><option value="">Select a report</option><option v-for="report in history" :key="report.report_id" :value="report.report_id">{{ formatDate(report.created_at) }} · {{ report.overall_score ?? 'Unscored' }}</option></select>
            </label>
          </div>
          <template v-if="compareReports[0] && compareReports[1]">
            <div v-if="!compareIsEquivalent" class="cq-banner" data-tone="warn"><AlertCircle aria-hidden="true" /><span class="cq-banner-text">Scoring inputs, coverage, weights or provenance differ. Treat score deltas as non-equivalent.</span></div>
            <div class="cq-cmp-rows">
              <div v-for="category in categories" :key="category.id" class="cq-cmp-row" :style="{ '--c': categoryMeta[category.id].color }">
                <span>{{ category.label }}</span>
                <div class="cq-cmp-bars">
                  <div><span class="cq-cmp-val">{{ formatScore(reportCategoryScore(compareReports[0], category.id)) }}</span><span class="cq-cmp-bar" data-old><i :style="{ transform: `scaleX(${(reportCategoryScore(compareReports[0], category.id) ?? 0) / 100})` }"></i></span></div>
                  <div><span class="cq-cmp-val">{{ formatScore(reportCategoryScore(compareReports[1], category.id)) }}</span><span class="cq-cmp-bar"><i :style="{ transform: `scaleX(${(reportCategoryScore(compareReports[1], category.id) ?? 0) / 100})` }"></i></span></div>
                </div>
                <span v-if="scoreDelta(category.id) != null" class="cq-delta" :data-dir="(scoreDelta(category.id) || 0) >= 0 ? 'up' : 'down'">
                  <ArrowUpRight v-if="(scoreDelta(category.id) || 0) >= 0" aria-hidden="true" /><ArrowDownRight v-else aria-hidden="true" />{{ Math.abs(scoreDelta(category.id) || 0) }}
                </span>
                <span v-else class="cq-delta">—</span>
              </div>
            </div>
            <p class="cq-hint">Evaluator tokens: <strong>{{ formatTokenUsage(compareReports[0].token_usage, compareReports[0].provider) }} → {{ formatTokenUsage(compareReports[1].token_usage, compareReports[1].provider) }}</strong></p>
            <div class="cq-diff">
              <div class="cq-diff-col"><h3 data-tone="ok">New findings · {{ findingChanges.added.length }}</h3><p v-for="item in findingChanges.added.slice(0, 12)" :key="item.evidence_id"><span class="cq-file">{{ item.file }}:{{ item.line }}</span> · {{ item.message }}</p></div>
              <div class="cq-diff-col"><h3>No longer present · {{ findingChanges.removed.length }}</h3><p v-for="item in findingChanges.removed.slice(0, 12)" :key="item.evidence_id"><span class="cq-file">{{ item.file }}:{{ item.line }}</span> · {{ item.message }}</p></div>
            </div>
          </template>
          <p v-else-if="history.length < 2" class="cq-note">You need at least two saved reports to compare results.</p>
          <p v-else class="cq-note">Choose two saved reports to compare their category scores and findings.</p>
        </section>
      </div>

      <div v-else-if="activeTab === 'regression'" id="quality-regression-panel" role="tabpanel" aria-labelledby="quality-regression-tab" class="cq-legacy">
        <RegressionCheck :project-id="selectedProject?.project_id || ''" :candidate-revision-id="selectedProject?.revision_id || ''"
                         :revisions="projects.filter((item) => item.project_id === selectedProject?.project_id)" />
      </div>
      <div v-else-if="activeTab === 'performance'" id="quality-performance-panel" role="tabpanel" aria-labelledby="quality-performance-tab" class="cq-legacy">
        <PerformanceSetup :project-id="selectedProject?.project_id || ''" :revision-id="selectedProject?.revision_id || ''" />
      </div>
      <div v-else-if="activeTab === 'rules'" id="quality-rules-panel" role="tabpanel" aria-labelledby="quality-rules-tab" class="cq-legacy">
        <RuleManager :project-id="selectedProject?.project_id || ''" :revision-id="selectedProject?.revision_id || ''" />
      </div>
      <div v-else-if="activeTab === 'external'" id="quality-external-analyzers-panel" role="tabpanel" aria-labelledby="quality-external-analyzers-tab" class="cq-legacy">
        <ExternalAnalyzerSetup :project-id="selectedProject?.project_id || ''" />
      </div>
    </div>
  </section>
  <ConfirmModal :show="!!deleteTarget" title="Delete assessment report?" :message="deleteTarget ? `This permanently removes the report from ${formatDate(deleteTarget.created_at)} and comparison history. This cannot be undone.` : ''" confirm-label="Delete report" :busy="!!deletingReportId" :error="deleteReportError" @confirm="removeReport" @cancel="deleteTarget = null; deleteReportError = ''" />
  <ConfirmModal :show="!!revisionDeleteTarget" title="Remove project revision?" :message="revisionDeleteTarget ? `Permanently remove ${revisionDeleteTarget.project_id} (${revisionDeleteTarget.revision_id}), its uploaded source, graph, vectors, assessment reports and configured runtime telemetry. Other revisions and shared project settings remain. This cannot be undone.` : ''" confirm-label="Remove revision" :busy="deletingRevision" :error="revisionDeleteError" @confirm="removeRevision" @cancel="revisionDeleteTarget = null; revisionDeleteError = ''" />
</template>
