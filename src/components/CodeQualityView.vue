<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { AlertCircle, ArrowDownRight, ArrowUpRight, CheckCircle2, GitCompareArrows, LoaderCircle, Play, Save, ShieldCheck, Trash2 } from 'lucide-vue-next';
import ConfirmModal from './ConfirmModal.vue';
import { makeRequest } from '../ui/makeRequest';
import PerformanceSetup from './code-quality/PerformanceSetup.vue';
import RuleManager from './code-quality/RuleManager.vue';
import SourcePreview from './code-quality/SourcePreview.vue';
import DirectoryExclusionTree from './code-quality/DirectoryExclusionTree.vue';
import BusinessRuleReviews from './code-quality/BusinessRuleReviews.vue';
import ExternalAnalyzerSetup from './code-quality/ExternalAnalyzerSetup.vue';
import { useUrlTab } from '../composables/useUrlTab';
import type { UploadedFile } from '../types';
import { assessmentRevisions, revisionKey, type ProjectRevision } from '../code-quality/projectRevisions';

const props = defineProps<{ files: UploadedFile[] }>();
const emit = defineEmits<{ openKnowledgeBase: []; revisionRemoved: [] }>();

const categories = [
  { id: 'security', label: 'Security' },
  { id: 'clean_code', label: 'Clean code' },
  { id: 'maintainability', label: 'Maintainability' },
  { id: 'reliability', label: 'Reliability' },
  { id: 'performance', label: 'Performance' },
] as const;
type CategoryId = typeof categories[number]['id'];
type AiOpinion = { assessed: boolean; score: number | null; reason: string; evidence_ids: string[] };
type CategoryScore = { assessed: boolean; score: number | null; reason: string; evidence_ids: string[]; score_method?: string; metric_schema?: string; rubric_version?: string; assessment_status?: 'scored' | 'provisional' | 'unavailable'; confidence?: 'high' | 'medium' | 'low'; confidence_reasons?: string[]; finding_count?: number; deterministic_evidence_ids?: string[]; ai_opinion?: AiOpinion };
type FlowTrace = { nodes: Array<{ symbol: string; file: string; start_line: number; end_line: number }>; edges: Array<{ type: string; from: string; to: string; file: string; line: number }> };
type PhpDesignSignals = { status: 'complete' | 'partial' | 'unavailable'; revision_id: string; analyzer_version: string; languages: string[]; principles_with_static_signals: string[]; principles_requiring_semantic_evidence: string[]; chunk_count: number; class_count: number; method_count: number; signal_count: number; signals_truncated: boolean; limits?: Record<string, number>; reason: string };
type AssessmentEvidence = { evidence_id: string; kind?: 'finding' | 'coverage' | 'semantic_trace' | 'semantic_context' | 'architectural_signal' | 'runtime_measurement'; category: CategoryId | 'business_rule'; rule: string; file: string | null; line: number | null; message: string; trace?: FlowTrace };
type BusinessRuleSnapshot = { rule_id: string; version: number; title: string; statement: string; category: string; source?: 'outline'; collection_id?: string; updated_at?: string; content_hash?: string };
type BusinessRuleReview = { rule_id: string; version: number; status: 'supported' | 'concern' | 'unknown'; reason: string; evidence_ids: string[] };
type TokenUsage = { prompt_tokens: number | null; completion_tokens: number | null; total_tokens: number; estimated: boolean; source: 'provider' | 'estimate'; batch_count?: number; summarized_evidence_count?: number };
type Report = {
  report_id: string; project_id: string; revision_id: string; commit_sha: string | null; created_at: string;
  overall_score: number | null; categories: Record<CategoryId, CategoryScore>; weights: Record<CategoryId, number>; shadow_scoring?: { version: string; status: string; categories: Record<CategoryId, CategoryScore> };
  coverage: { files: number; scanned_files: number; skipped_files: number; excluded_files?: number; user_excluded_files?: number; user_exclusion_patterns?: string[]; scanned_bytes: number; languages: Record<string, number>; unsupported_extensions: Record<string, number>; analyzer_failures: Array<{ name: string; reason: string }>; categories: Record<CategoryId, { supported: boolean; status?: 'covered' | 'partial' | 'unavailable'; evidence_count: number; reason?: string | null }>; analyzers: Array<{ name: string; version: string; rules_version?: string; languages: string[]; categories: string[]; status?: string; server_version?: string; server_edition?: string; scanned_files?: number; skipped_files?: number; finding_count?: number; reason?: string | null; unresolved?: string[] }>; external_analyzers?: { sonarqube?: { status: string; version?: string; server_version?: string; server_edition?: string; scanned_files?: number; skipped_files?: number; finding_count?: number; reason?: string | null }; joern?: { status: string; version?: string; languages?: string[]; scanned_files?: number; skipped_files?: number; finding_count?: number; reason?: string | null; unresolved?: string[] } }; business_rule_review?: { status: string; reason?: string | null; collection_id?: string | null; document_count?: number; imported_count?: number; omitted_count?: number; oversized_count?: number; limit_exceeded?: boolean; synchronized_at?: string | null }; semantic_context?: { status: 'available' | 'available_no_matches' | 'unavailable'; revision_id: string; retriever_version: string; query_count: number; business_rule_query_count?: number; omitted_business_rule_ids?: string[]; evidence_count: number; failure_code?: string | null; diagnostic_detail?: string | null; searched_collection_count?: number; candidate_count?: number; rejected_revision_count?: number; rejected_source_count?: number; category_search?: Partial<Record<CategoryId, { attempted_queries: number; candidate_count: number; accepted_count: number; rejected_revision_count: number; rejected_source_count: number }>>; reason?: string | null }; category_review_context?: { status: 'available' | 'partial' | 'unavailable' | 'not_needed'; source: string | null; excerpt_count: number; categories: Record<CategoryId, number>; scanned_files: number; scanned_bytes: number; reason: string }; semantic_flow_tracing?: { status: 'complete' | 'partial' | 'unavailable'; revision_id: string; tracer_version: string; languages?: string[]; flow_types: string[]; node_count: number; edge_count: number; trace_count: number; unresolved_edge_count: number; unresolved_edges?: Array<{ source_symbol: string; file: string; line: number; target_symbol: string | null; reason: string; count: number }>; limits_reached?: string[]; reason: string }; php_design_signals?: PhpDesignSignals };
  token_usage?: TokenUsage | null;
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
type QualityTab = 'assessment' | 'compare' | 'performance' | 'rules' | 'external';
const activeTab = useUrlTab<QualityTab>({ values: ['assessment', 'compare', 'performance', 'rules', 'external'], fallback: 'assessment' });
const selectedRevision = ref('');
const weights = ref<Record<CategoryId, number>>({ security: 20, clean_code: 20, maintainability: 20, reliability: 20, performance: 20 });
const savedWeights = ref<Record<CategoryId, number>>({ security: 20, clean_code: 20, maintainability: 20, reliability: 20, performance: 20 });
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
  return left.rubric_version === right.rubric_version
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
    if (projects.value.length && !projects.value.some((project) => revisionKey(project) === selectedRevision.value)) {
      const first = projects.value[0];
      selectedRevision.value = revisionKey(first);
    }
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
watch(projects, (revisions) => {
  if (!revisions.some((project) => revisionKey(project) === selectedRevision.value)) {
    selectedRevision.value = revisions.length ? revisionKey(revisions[0]) : '';
  }
});
watch(() => selectedProject.value?.project_id, (projectId) => loadProjectExclusions(projectId), { immediate: true });
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
        <div class="flex flex-wrap items-center gap-2">
          <select id="quality-revision" v-model="selectedRevision" :disabled="loadingProjects || assessing || checkingIndex || !projects.length" class="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2.5 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-400/20">
            <option v-if="loadingProjects" value="">Loading project revisions…</option>
            <option v-else-if="!projects.length" value="">No project revisions in Knowledge Base</option>
            <option v-for="project in projects" :key="revisionKey(project)" :value="revisionKey(project)" :title="project.revision_id || project.commit_sha || 'Upload revision'">
              {{ project.project_id }}
            </option>
          </select>
          <button v-if="selectedProject?.revision_id" type="button" class="shrink-0 rounded-lg border border-rose-500/40 px-3 py-2 text-sm text-rose-300 hover:bg-rose-500/10 disabled:opacity-50" :disabled="assessing || deletingRevision" @click="revisionDeleteTarget = selectedProject; revisionDeleteError = ''">Remove revision</button>
        </div>
      </div>
    </header>

    <div v-if="checkingIndex" role="status" class="flex items-center gap-2 rounded-xl border border-slate-700 p-4 text-sm text-slate-300"><LoaderCircle :size="16" class="motion-safe:animate-spin" />Checking the selected revision's index…</div>
    <div v-else-if="!loadingProjects && (!selectedProject || indexReadiness?.ready === false)" role="status" class="rounded-xl border border-amber-400/30 bg-amber-400/10 p-4 text-sm text-amber-100">
      <p>{{ indexReadiness?.reason || 'Add a project revision to Knowledge Base and index it before running an assessment.' }}</p>
      <div class="mt-3 flex flex-wrap gap-3">
        <button type="button" class="min-h-9 rounded-lg border border-amber-300/30 px-3 font-medium hover:bg-amber-300/10" @click="emit('openKnowledgeBase')">Open Knowledge Base</button>
        <button v-if="selectedProject" type="button" class="min-h-9 rounded-lg px-3 underline underline-offset-2" @click="loadProjectsAndPreferences().then(checkIndex)">Check again</button>
      </div>
    </div>

    <nav role="tablist" aria-label="Code Quality sections" class="flex flex-wrap gap-2 border-b border-slate-800 pb-3">
      <button id="quality-assessment-tab" role="tab" :aria-selected="activeTab === 'assessment'" aria-controls="quality-assessment-panel" class="min-h-10 rounded-lg px-4 text-sm font-medium transition" :class="activeTab === 'assessment' ? 'bg-indigo-500/15 text-indigo-100 ring-1 ring-indigo-400/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'" @click="activeTab = 'assessment'">Static assessment</button>
      <button id="quality-compare-tab" role="tab" :aria-selected="activeTab === 'compare'" aria-controls="quality-compare-panel" class="min-h-10 rounded-lg px-4 text-sm font-medium transition" :class="activeTab === 'compare' ? 'bg-indigo-500/15 text-indigo-100 ring-1 ring-indigo-400/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'" @click="activeTab = 'compare'">Compare reports</button>
      <button id="quality-performance-tab" role="tab" :aria-selected="activeTab === 'performance'" aria-controls="quality-performance-panel" class="min-h-10 rounded-lg px-4 text-sm font-medium transition" :class="activeTab === 'performance' ? 'bg-indigo-500/15 text-indigo-100 ring-1 ring-indigo-400/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'" @click="activeTab = 'performance'">Setup Performance</button>
      <button id="quality-rules-tab" role="tab" :aria-selected="activeTab === 'rules'" aria-controls="quality-rules-panel" class="min-h-10 rounded-lg px-4 text-sm font-medium transition" :class="activeTab === 'rules' ? 'bg-indigo-500/15 text-indigo-100 ring-1 ring-indigo-400/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'" @click="activeTab = 'rules'">Custom rules</button>
      <button id="quality-external-analyzers-tab" role="tab" :aria-selected="activeTab === 'external'" aria-controls="quality-external-analyzers-panel" class="min-h-10 rounded-lg px-4 text-sm font-medium transition" :class="activeTab === 'external' ? 'bg-indigo-500/15 text-indigo-100 ring-1 ring-indigo-400/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'" @click="activeTab = 'external'">External analyzers</button>
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
            <button :disabled="assessing || checkingIndex || indexReadiness?.ready !== true || savingWeights || savingExclusions || loadingExclusions || exclusionSettingsUnavailable || !selectedProject || totalWeight !== 100" class="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-indigo-500 px-4 text-sm font-semibold text-white transition hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50" @click="runAssessment">
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

        <div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 class="text-lg font-semibold">Excluded files and directories</h2>
              <p class="mt-1 text-sm text-slate-400">Saved for {{ selectedProject?.project_id || 'this project' }} and applied to future assessments.</p>
            </div>
            <button type="button" :disabled="!selectedProject || loadingExclusions || exclusionSettingsUnavailable || savingExclusions || !exclusionsChanged" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 px-3 text-sm font-medium text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40" @click="saveProjectExclusions">
              <LoaderCircle v-if="savingExclusions" class="motion-safe:animate-spin" :size="15" />
              <Save v-else :size="15" />{{ savingExclusions ? 'Saving…' : 'Save exclusions' }}
            </button>
          </div>
          <p class="mt-4 text-xs font-medium text-slate-400">Select folders to exclude</p>
          <div v-if="loadingSourceDirectories" class="mt-2 flex items-center gap-2 text-sm text-slate-500"><LoaderCircle class="motion-safe:animate-spin" :size="14" />Loading revision folders…</div>
          <div v-else-if="sourceDirectoryError" class="mt-2 flex flex-wrap items-center gap-3 text-sm" role="alert">
            <span class="text-rose-300">{{ sourceDirectoryError }}</span>
            <button type="button" class="font-medium text-indigo-300 underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" @click="loadSourceDirectories">Retry folder loading</button>
          </div>
          <DirectoryExclusionTree v-else :directories="sourceDirectories" :patterns="parsedExclusionPatterns" :disabled="loadingExclusions || exclusionSettingsUnavailable || savingExclusions || assessing" class="mt-2" @toggle="toggleFolderExclusion" />
          <form class="mt-3 flex flex-wrap gap-2" @submit.prevent="addCustomExclusion">
            <label for="quality-custom-exclusion" class="sr-only">Add a file path or exclusion glob</label>
            <input id="quality-custom-exclusion" v-model="customExclusionInput" :disabled="loadingExclusions || exclusionSettingsUnavailable || savingExclusions || assessing" type="text" maxlength="300" placeholder="Add file path or custom glob, e.g. src/LegacyReport.php" class="min-h-9 min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 font-mono text-xs text-slate-200 outline-none placeholder:text-slate-600 focus:border-indigo-400 disabled:opacity-50" />
            <button type="submit" :disabled="!customExclusionInput.trim() || loadingExclusions || exclusionSettingsUnavailable || savingExclusions || assessing" class="min-h-9 rounded-lg border border-slate-700 px-3 text-xs font-medium text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40">Add path or glob</button>
          </form>
          <div v-if="parsedExclusionPatterns.length" class="mt-3">
            <p class="text-xs font-medium text-slate-400">Excluded patterns ({{ parsedExclusionPatterns.length }})</p>
            <ul class="mt-2 flex flex-wrap gap-2">
              <li v-for="pattern in parsedExclusionPatterns" :key="pattern" class="inline-flex max-w-full items-center gap-1 rounded-md border border-indigo-400/20 bg-indigo-400/5 pl-2 font-mono text-[11px] text-indigo-100">
                <span class="break-all py-1">{{ pattern }}</span>
                <button type="button" :disabled="loadingExclusions || exclusionSettingsUnavailable || savingExclusions || assessing" :aria-label="`Remove exclusion ${pattern}`" class="grid h-7 w-7 shrink-0 place-items-center rounded text-slate-400 hover:bg-slate-800 hover:text-white disabled:opacity-40" @click="exclusionPatterns = exclusionPatterns.filter((item) => item !== pattern)"><Trash2 :size="12" /></button>
              </li>
            </ul>
          </div>
          <p class="mt-2 text-xs leading-relaxed text-slate-500">The tree shows folders only. Selecting a folder excludes all its descendants. File paths and custom globs can be added above. Patterns use `/`; `*` and `?` match within a path segment, and `**` can match across directories. Built-in artifact exclusions still apply.</p>
          <p v-if="exclusionMessage" class="mt-2 text-xs text-emerald-300" role="status">{{ exclusionMessage }}</p>
        </div>

        <section v-if="currentReport" class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div><div class="text-xs font-medium uppercase tracking-wider text-slate-500">Assessment report</div><h2 class="mt-1 text-xl font-semibold">{{ currentReport.overall_score == null ? 'Overall score unavailable' : `${currentReport.overall_score} / 100` }}</h2><p v-if="currentReport.overall_score == null && unassessedCategories.length" class="mt-1 text-sm text-amber-200">Not assessed: {{ unassessedCategories.join(', ') }}. See each category for its evidence reason.</p><p class="mt-1 text-xs text-slate-400">{{ formatDate(currentReport.created_at) }} · {{ currentReport.commit_sha?.slice(0, 12) || currentReport.revision_id }}</p></div>
            <span class="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-400">{{ currentReport.coverage.scanned_files }} / {{ currentReport.coverage.files }} scanned<span v-if="currentReport.coverage.skipped_files"> · {{ currentReport.coverage.skipped_files }} over limits</span><span v-if="currentReport.coverage.excluded_files"> · {{ currentReport.coverage.excluded_files }} artifacts excluded</span><span v-if="currentReport.coverage.user_excluded_files"> · {{ currentReport.coverage.user_excluded_files }} excluded by project settings</span> · {{ Object.keys(currentReport.coverage.languages).join(', ') || 'No detected languages' }}</span>
          </div>
          <details v-if="currentReport.coverage.user_exclusion_patterns?.length" class="mt-4 rounded-lg border border-slate-800 bg-slate-950/40 px-4 py-3 text-xs">
            <summary class="cursor-pointer font-medium text-slate-300">Project exclusions used ({{ currentReport.coverage.user_exclusion_patterns.length }})</summary>
            <ul class="mt-2 space-y-1 font-mono text-slate-400"><li v-for="pattern in currentReport.coverage.user_exclusion_patterns" :key="pattern" class="break-all">{{ pattern }}</li></ul>
          </details>
          <div class="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 px-4 py-3 text-xs"><div class="font-semibold text-slate-300">Evaluator token usage</div><p v-if="currentReport.token_usage" class="mt-1 text-slate-400">{{ formatTokenUsage(currentReport.token_usage) }} · input {{ currentReport.token_usage.prompt_tokens?.toLocaleString() ?? 'unavailable' }} · output {{ currentReport.token_usage.completion_tokens?.toLocaleString() ?? 'unavailable' }} · {{ currentReport.token_usage.estimated ? 'estimated' : 'provider-reported' }}</p><p v-else class="mt-1 text-slate-500">{{ formatTokenUsage(null, currentReport.provider) }}.</p></div>
          <p v-if="currentReport.token_usage?.batch_count" class="mt-2 text-xs text-slate-400">{{ currentReport.token_usage.summarized_evidence_count }} evidence items were summarized across {{ currentReport.token_usage.batch_count }} model requests before final scoring; summaries may omit details.</p>
          <div v-if="currentReport.shadow_scoring" class="mt-3 rounded-xl border border-indigo-500/20 bg-indigo-500/5 px-4 py-3 text-xs"><div class="font-semibold text-indigo-100">Experimental analyzer baseline</div><p class="mt-1 text-indigo-200/80">These experimental scores are being evaluated and do not determine the report's official scores. Coverage and rule weights still require calibration.</p></div>
          <div class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <article v-for="category in categories" :key="category.id" class="rounded-xl border border-slate-800 bg-slate-950/60 p-4">
              <div class="text-xs font-medium text-slate-400">{{ category.label }}</div>
              <div class="mt-2 text-2xl font-semibold" :class="currentReport.categories[category.id].assessed ? 'text-slate-100' : 'text-slate-500'">{{ formatScore(currentReport.categories[category.id].score) }}<span v-if="currentReport.categories[category.id].assessed" class="ml-1 text-xs font-normal text-slate-500">/ 100</span></div>
              <p class="mt-2 text-xs leading-relaxed text-slate-400">{{ currentReport.categories[category.id].reason }}</p>
              <p class="mt-3 text-[11px] text-slate-500">{{ currentReport.coverage.categories[category.id].status || (currentReport.coverage.categories[category.id].supported ? 'covered' : 'unavailable') }} · {{ currentReport.coverage.categories[category.id].evidence_count }} evidence signals · weight {{ currentReport.weights[category.id] }}%</p>
              <p v-if="currentReport.shadow_scoring" class="mt-2 text-[11px] text-indigo-200">Experimental baseline: {{ formatScore(currentReport.shadow_scoring.categories[category.id].score) }} · {{ confidenceLabel(currentReport.shadow_scoring.categories[category.id]) }} · {{ currentReport.shadow_scoring.categories[category.id].finding_count ?? 0 }} findings</p>
            </article>
          </div>
          <BusinessRuleReviews v-if="currentReport.business_rules?.length" :rules="currentReport.business_rules" :reviews="currentReport.business_rule_reviews || []" :evidence="currentReport.evidence" @view-source="openFindingSource" />
          <div v-if="currentReport.coverage.business_rule_review" class="mt-4 rounded-xl border border-slate-800 bg-slate-900/50 p-4 text-sm text-slate-300">
            <p v-if="currentReport.coverage.business_rule_review.status === 'not_configured'">Business rule review is not configured for this project. Connect an Outline collection in Knowledge Base → Business rules.</p>
            <p v-else-if="currentReport.coverage.business_rule_review.status === 'unavailable'">Business rule review could not synchronize Outline: {{ currentReport.coverage.business_rule_review.reason }}</p>
            <p v-else-if="!currentReport.business_rules?.length">Outline synchronized successfully; the selected collection has no usable business-rule documents.</p>
            <p v-else>Outline snapshot: {{ currentReport.coverage.business_rule_review.imported_count }} documents reviewed from collection {{ currentReport.coverage.business_rule_review.collection_id }}.</p>
          </div>
          <div v-if="currentReport.coverage.semantic_context" class="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-4" aria-live="polite">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h3 class="text-sm font-semibold text-slate-200">Revision-matched code context</h3>
              <span class="rounded-full px-2.5 py-1 text-xs" :class="currentReport.coverage.semantic_context.status === 'unavailable' ? 'bg-amber-500/10 text-amber-200' : 'bg-emerald-500/10 text-emerald-200'">
                {{ currentReport.coverage.semantic_context.status === 'available' ? 'Retrieved' : currentReport.coverage.semantic_context.status === 'available_no_matches' ? 'No matches' : 'Unavailable' }}
              </span>
            </div>
            <p class="mt-2 text-sm leading-relaxed text-slate-400">
              {{ currentReport.coverage.semantic_context.evidence_count }} code excerpts from {{ currentReport.coverage.semantic_context.query_count }} bounded searches for this revision<span v-if="currentReport.coverage.semantic_context.business_rule_query_count">, including {{ currentReport.coverage.semantic_context.business_rule_query_count }} business-rule searches</span>.
              {{ currentReport.coverage.semantic_context.reason || 'Retrieved snippets provide context; they do not prove complete control-flow coverage.' }}
            </p>
            <p v-if="currentReport.coverage.semantic_context.failure_code" class="mt-2 text-xs text-slate-500">
              Diagnostic: {{ currentReport.coverage.semantic_context.failure_code.replaceAll('_', ' ') }}<span v-if="currentReport.coverage.semantic_context.diagnostic_detail"> · {{ currentReport.coverage.semantic_context.diagnostic_detail }}</span> · {{ currentReport.coverage.semantic_context.candidate_count ?? 0 }} candidates · {{ currentReport.coverage.semantic_context.searched_collection_count ?? 0 }} collections searched<span v-if="currentReport.coverage.semantic_context.rejected_revision_count"> · {{ currentReport.coverage.semantic_context.rejected_revision_count }} revision-mismatched candidates discarded</span><span v-if="currentReport.coverage.semantic_context.rejected_source_count"> · {{ currentReport.coverage.semantic_context.rejected_source_count }} out-of-scope candidates discarded</span>
            </p>
            <div v-if="currentReport.coverage.semantic_context.category_search" class="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-5" aria-label="Semantic retrieval by assessment category">
              <div v-for="category in categories" :key="category.id" class="rounded-lg border border-slate-800/80 bg-slate-900/50 px-3 py-2">
                <p class="text-xs font-medium text-slate-300">{{ category.label }}</p>
                <p class="mt-1 text-[11px] text-slate-500">
                  {{ currentReport.coverage.semantic_context.category_search[category.id]?.attempted_queries ?? 0 }} queries · {{ currentReport.coverage.semantic_context.category_search[category.id]?.accepted_count ?? 0 }} snippets accepted
                </p>
                <p v-if="(currentReport.coverage.semantic_context.category_search[category.id]?.rejected_revision_count ?? 0) + (currentReport.coverage.semantic_context.category_search[category.id]?.rejected_source_count ?? 0) > 0" class="mt-1 text-[11px] text-amber-300">
                  {{ currentReport.coverage.semantic_context.category_search[category.id]?.rejected_revision_count ?? 0 }} revision mismatch · {{ currentReport.coverage.semantic_context.category_search[category.id]?.rejected_source_count ?? 0 }} out of source scope
                </p>
              </div>
            </div>
            <p v-if="currentReport.coverage.semantic_context.omitted_business_rule_ids?.length" class="mt-2 text-xs text-amber-200">{{ currentReport.coverage.semantic_context.omitted_business_rule_ids.length }} business rules exceeded the retrieval limit; their reviews remain unknown.</p>
          </div>
          <div v-if="currentReport.coverage.category_review_context && currentReport.coverage.category_review_context.status !== 'not_needed'" class="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-4">
            <h3 class="text-sm font-semibold text-slate-200">Revision source samples used for category scores</h3>
            <p class="mt-2 text-sm leading-relaxed text-slate-400">{{ currentReport.coverage.category_review_context.excerpt_count }} bounded source excerpts from {{ currentReport.coverage.category_review_context.scanned_files }} files were selected from this revision's archive to fill categories without other category-specific evidence. These samples are review context, not findings. {{ currentReport.coverage.category_review_context.reason }}</p>
            <div class="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
              <div v-for="category in categories" :key="category.id" class="rounded-lg border border-slate-800/80 bg-slate-900/50 px-3 py-2">
                <p class="text-xs font-medium text-slate-300">{{ category.label }}</p>
                <p class="mt-1 text-[11px] text-slate-500">{{ currentReport.coverage.category_review_context.categories[category.id] || 0 }} local excerpts</p>
              </div>
            </div>
          </div>
          <div v-if="currentReport.coverage.semantic_flow_tracing" class="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-4" aria-live="polite">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h3 class="text-sm font-semibold text-slate-200">Semantic flow tracing</h3>
              <span class="rounded-full px-2.5 py-1 text-xs" :class="currentReport.coverage.semantic_flow_tracing.status === 'complete' ? 'bg-emerald-500/10 text-emerald-200' : currentReport.coverage.semantic_flow_tracing.status === 'partial' ? 'bg-amber-500/10 text-amber-200' : 'bg-slate-800 text-slate-300'">
                {{ currentReport.coverage.semantic_flow_tracing.status === 'complete' ? 'Complete within scope' : currentReport.coverage.semantic_flow_tracing.status === 'partial' ? 'Partial' : 'Unavailable' }}
              </span>
            </div>
            <p class="mt-2 text-sm leading-relaxed text-slate-400">
              {{ currentReport.coverage.semantic_flow_tracing.trace_count }} request-to-sensitive-operation call paths · {{ currentReport.coverage.semantic_flow_tracing.node_count }} symbols · {{ currentReport.coverage.semantic_flow_tracing.edge_count }} resolved calls · {{ currentReport.coverage.semantic_flow_tracing.unresolved_edge_count }} unresolved calls.
              {{ currentReport.coverage.semantic_flow_tracing.reason }}
            </p>
            <ul v-if="currentReport.coverage.semantic_flow_tracing.unresolved_edges?.length" class="mt-3 space-y-1 text-xs text-amber-200">
              <li v-for="(edge, index) in currentReport.coverage.semantic_flow_tracing.unresolved_edges.slice(0, 8)" :key="`${edge.file}:${edge.line}:${edge.source_symbol}:${index}`">
                {{ edge.file }}:{{ edge.line }} · {{ edge.source_symbol }} → {{ edge.target_symbol || 'unresolved reference' }} ({{ edge.reason }})
              </li>
            </ul>
            <p v-if="currentReport.coverage.semantic_flow_tracing.limits_reached?.length" class="mt-2 text-xs text-amber-200">Limits reached: {{ currentReport.coverage.semantic_flow_tracing.limits_reached.join(', ') }}</p>
          </div>
          <div v-if="currentReport.coverage.php_design_signals" class="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-4" aria-live="polite">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h3 class="text-sm font-semibold text-slate-200">PHP design review signals</h3>
              <span class="rounded-full px-2.5 py-1 text-xs" :class="currentReport.coverage.php_design_signals.status === 'complete' ? 'bg-emerald-500/10 text-emerald-200' : currentReport.coverage.php_design_signals.status === 'partial' ? 'bg-amber-500/10 text-amber-200' : 'bg-slate-800 text-slate-300'">
                {{ currentReport.coverage.php_design_signals.status === 'complete' ? 'Indexed scope complete' : currentReport.coverage.php_design_signals.status === 'partial' ? 'Partial' : 'Unavailable' }}
              </span>
            </div>
            <p v-if="currentReport.coverage.php_design_signals.status !== 'unavailable'" class="mt-2 text-sm leading-relaxed text-slate-400">
              {{ currentReport.coverage.php_design_signals.class_count }} classes · {{ currentReport.coverage.php_design_signals.method_count }} methods · {{ currentReport.coverage.php_design_signals.signal_count }} review signals. Static checks cover {{ currentReport.coverage.php_design_signals.principles_with_static_signals.join(', ') }} within the indexed PHP scope. {{ currentReport.coverage.php_design_signals.reason }}
            </p>
            <p v-else class="mt-2 text-sm leading-relaxed text-slate-400">{{ currentReport.coverage.php_design_signals.reason }}</p>
            <p v-if="currentReport.coverage.php_design_signals.signals_truncated" class="mt-2 text-xs text-amber-200">The signal limit was reached; additional candidates may be omitted.</p>
          </div>
          <div v-if="currentReport.coverage.analyzer_failures.length || Object.keys(currentReport.coverage.unsupported_extensions).length || currentReport.coverage.skipped_files || currentReport.coverage.excluded_files" class="mt-4 rounded-lg border border-amber-500/25 bg-amber-500/5 p-3 text-xs text-amber-200">
            <p v-if="currentReport.coverage.analyzer_failures.length" class="font-medium">Some analyzer passes could not complete: {{ currentReport.coverage.analyzer_failures.map((item) => `${item.name}: ${item.reason}`).join(' · ') }}</p>
            <p v-if="Object.keys(currentReport.coverage.unsupported_extensions).length" :class="currentReport.coverage.analyzer_failures.length ? 'mt-1' : ''">Source-like file types without a configured analyzer: {{ Object.entries(currentReport.coverage.unsupported_extensions).map(([extension, count]) => `${extension} (${count})`).join(', ') }}</p>
            <p v-if="currentReport.coverage.skipped_files" class="mt-1">{{ currentReport.coverage.skipped_files }} files exceeded source scan limits and were not analyzed.</p>
            <p v-if="currentReport.coverage.excluded_files" class="mt-1">{{ currentReport.coverage.excluded_files }} source-like files in dependency, build, cache, log, generated, or temporary paths were excluded.</p>
          </div>
          <div v-if="currentReport.custom_rules?.length" class="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-4">
            <h3 class="text-sm font-semibold text-slate-200">Custom rule snapshots used</h3>
            <p v-for="rule in currentReport.custom_rules" :key="rule.rule_id" class="mt-2 break-all text-xs text-slate-400">{{ rule.title }} · {{ rule.content_hash }}</p>
          </div>
          <SourcePreview v-if="selectedSource && currentReport && !selectedEvidenceId" :key="`${currentReport.report_id}:${selectedSource.file}:${selectedSource.line}`" class="mt-4" :project-id="currentReport.project_id" :revision-id="currentReport.revision_id" :commit-sha="currentReport.commit_sha" :file-path="selectedSource.file" :line="selectedSource.line" @close="closeSourcePreview" />
          <div id="assessment-evidence" class="mt-5 space-y-2">
            <h3 class="text-sm font-semibold text-slate-200">Findings and evidence</h3>
            <p v-if="reportEvidenceRows.length" class="text-xs text-slate-500">Each finding appears once with its source evidence and any available trace.</p>
            <p v-if="cleanCodeReviewWithoutFinding" class="rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-sm text-slate-400">The AI reviewed revision-matched Clean Code excerpts, but did not substantiate a specific Clean Code problem. The excerpts are review context, not findings.</p>
            <p v-if="!reportEvidenceRows.length" class="rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-sm text-slate-500">No findings or source-level evidence were emitted. This does not establish that the code has no issues.</p>
            <article v-for="(row, evidenceIndex) in reportEvidenceRows" :id="row.evidenceId" :key="row.evidenceId" class="scroll-mt-24 rounded-lg border border-slate-800/80 bg-slate-950/40 px-3 py-2.5">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <span class="text-sm font-semibold text-slate-200">{{ row.finding ? findingSummary(row.finding).title : evidenceLabel(row.evidenceId, evidenceIndex) }}</span>
                <span class="text-[10px] uppercase tracking-wider text-slate-500">{{ row.finding?.tool === 'ai-semantic-review' ? 'AI review' : row.finding?.severity || row.evidence?.kind || 'evidence' }} · {{ categoryLabel(row.finding?.category || row.evidence?.category || 'security') }}</span>
              </div>
              <p v-if="row.finding" class="mt-1.5 text-xs leading-relaxed text-slate-400">{{ findingSummary(row.finding).detail }}</p>
              <p v-else-if="row.evidence" class="mt-1.5 text-xs leading-relaxed text-slate-300">{{ row.evidence.message }}</p>
              <details v-if="row.finding && row.evidence" class="mt-2 rounded-md bg-slate-950/50 px-2.5 py-2">
                <summary class="cursor-pointer text-xs font-medium text-slate-400">{{ row.finding.tool === 'ai-semantic-review' ? 'Cited code context' : 'Analyzer evidence' }} · {{ ruleLabel(row.evidence.rule) }}</summary>
                <p class="mt-2 text-xs leading-relaxed text-slate-300">{{ row.evidence.message }}</p>
              </details>
              <p v-else-if="row.finding" class="mt-1.5 text-xs text-slate-400">{{ row.finding.message }}</p>
              <p v-if="row.finding?.file || row.evidence?.file" class="mt-1 font-mono text-[10px] text-slate-500">{{ row.finding?.file || row.evidence?.file }}<span v-if="row.finding?.line || row.evidence?.line">:{{ row.finding?.line || row.evidence?.line }}</span><span v-if="row.finding?.line === 1" class="ml-2 font-sans text-slate-500">· file-level anchor</span></p>
              <button v-if="(row.finding?.file || row.evidence?.file) && (row.finding?.line || row.evidence?.line)" type="button" class="mt-2 inline-flex min-h-8 items-center rounded-md border border-slate-700 px-2.5 text-xs font-medium text-indigo-200 hover:bg-indigo-400/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400" @click="openFindingSource(row.finding?.file || row.evidence?.file || '', row.finding?.line || row.evidence?.line, row.evidenceId)">View source</button>
              <ol v-if="row.evidence?.trace?.nodes?.length" class="mt-3 space-y-1 border-l border-indigo-400/25 pl-3">
                <li v-for="(node, index) in row.evidence.trace.nodes" :key="`${node.file}:${node.start_line}:${node.symbol}`" class="text-xs text-slate-300">
                  <span class="font-mono text-indigo-200">{{ node.symbol }}</span>
                  <span class="text-slate-500"> · {{ node.file }}:{{ node.start_line }}–{{ node.end_line }}</span>
                  <span v-if="index < (row.evidence?.trace?.edges.length || 0)" class="ml-1 text-slate-500">calls →</span>
                </li>
              </ol>
              <p v-if="row.evidence?.trace?.nodes?.length" class="mt-2 text-[11px] text-slate-500">Call reachability only; this trace does not prove that a specific input value reaches the operation.</p>
              <SourcePreview v-if="selectedSource && selectedEvidenceId === row.evidenceId && currentReport" :key="`${currentReport.report_id}:${selectedSource.file}:${selectedSource.line}`" class="mt-4" :project-id="currentReport.project_id" :revision-id="currentReport.revision_id" :commit-sha="currentReport.commit_sha" :file-path="selectedSource.file" :line="selectedSource.line" @close="closeSourcePreview" />
            </article>
          </div>
        </section>
        <div v-else-if="loadingReport" class="flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-900/50 p-5 text-sm text-slate-400"><LoaderCircle class="animate-spin" :size="16" />Loading report…</div>
        <div v-else-if="!loadingHistory && !history.length" class="rounded-2xl border border-dashed border-slate-700 bg-slate-900/30 p-8 text-center">
          <CheckCircle2 class="mx-auto text-slate-500" :size="26" /><h2 class="mt-3 text-lg font-semibold">No assessment reports yet</h2><p class="mx-auto mt-1 max-w-lg text-sm text-slate-400">Choose a saved weight preset or use the equal-weight default, then assess this indexed revision.</p>
        </div>

      </div>

      <aside class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5">
          <div class="flex items-center justify-between gap-2"><div><h2 class="text-lg font-semibold">Report history</h2><p class="mt-1 text-xs text-slate-500">{{ history.length }} saved for this project</p></div><LoaderCircle v-if="loadingHistory" class="motion-safe:animate-spin text-slate-500" :size="16" /></div>
        <div v-if="history.length" class="mt-4 space-y-2">
          <div v-for="report in history" :key="report.report_id" class="relative rounded-xl border p-0 transition-colors" :class="selectedReportId === report.report_id ? 'border-indigo-400/60 bg-indigo-500/10' : 'border-slate-800 bg-slate-950/40 hover:border-slate-700 hover:bg-slate-900/70'">
            <button type="button" class="block min-h-28 w-full rounded-xl p-3 pr-12 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400" :aria-label="`Show assessment report from ${formatDate(report.created_at)}`" :aria-pressed="selectedReportId === report.report_id" :aria-busy="selectedReportId === report.report_id && loadingReport" @click="chooseReport(report.report_id)">
              <span class="flex items-center justify-between gap-2"><span class="text-sm font-semibold">{{ report.overall_score == null ? 'No overall score' : `${report.overall_score} / 100` }}</span><span class="text-[11px] text-slate-500">{{ report.coverage }} files</span></span>
              <span class="mt-1.5 block text-xs text-slate-400">{{ formatDate(report.created_at) }}</span>
              <span class="mt-1 block text-xs text-slate-500">Evaluator: {{ formatTokenUsage(report.token_usage, report.provider) }}</span>
              <span class="mt-1 block truncate font-mono text-[10px] text-slate-600">{{ report.commit_sha || report.revision_id }}</span>
            </button>
            <button type="button" class="absolute right-1 top-1 z-10 rounded-lg p-2 text-slate-500 transition hover:bg-rose-500/10 hover:text-rose-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-rose-400 disabled:opacity-50" :aria-label="`Delete assessment from ${formatDate(report.created_at)}`" :title="deletingReportId === report.report_id ? 'Deleting report' : 'Delete report'" :disabled="!!deletingReportId" @click="deleteTarget = report; deleteReportError = ''">
              <LoaderCircle v-if="deletingReportId === report.report_id" class="motion-safe:animate-spin" :size="16" />
              <Trash2 v-else :size="16" />
            </button>
          </div>
        </div>
        <p v-else-if="!loadingHistory" class="mt-5 text-sm leading-relaxed text-slate-500">Reports from every indexed revision in the selected project will appear here.</p>
      </aside>
    </section>
    </div>
    <div v-else-if="activeTab === 'compare'" id="quality-compare-panel" role="tabpanel" aria-labelledby="quality-compare-tab" class="space-y-5">
      <section class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
        <div class="flex items-center gap-2"><GitCompareArrows class="text-indigo-300" :size="18" /><h2 class="text-lg font-semibold">Compare saved reports</h2></div>
        <p class="mt-1 text-sm text-slate-400">Choose two reports from {{ selectedProject?.project_id || 'this project' }} to compare their scores and findings.</p>
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
          <div class="mt-3 rounded-xl border border-slate-800 bg-slate-950/40 px-4 py-3 text-sm"><span class="text-slate-400">Evaluator tokens:</span><span class="ml-2 font-medium text-slate-200">{{ formatTokenUsage(compareReports[0].token_usage, compareReports[0].provider) }} → {{ formatTokenUsage(compareReports[1].token_usage, compareReports[1].provider) }}</span></div>
          <div class="mt-4 grid gap-3 sm:grid-cols-2">
            <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3"><div class="text-xs font-semibold text-emerald-300">New findings · {{ findingChanges.added.length }}</div><p v-for="item in findingChanges.added.slice(0, 12)" :key="item.evidence_id" class="mt-2 text-xs text-slate-300">{{ item.file }}:{{ item.line }} · {{ item.message }}</p></div>
            <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3"><div class="text-xs font-semibold text-slate-400">Findings no longer present · {{ findingChanges.removed.length }}</div><p v-for="item in findingChanges.removed.slice(0, 12)" :key="item.evidence_id" class="mt-2 text-xs text-slate-400">{{ item.file }}:{{ item.line }} · {{ item.message }}</p></div>
          </div>
        </template>
        <p v-else-if="history.length < 2" class="mt-4 rounded-lg border border-dashed border-slate-700 p-4 text-sm text-slate-500">At least two saved reports are needed to compare results.</p>
        <p v-else class="mt-4 text-sm text-slate-500">Choose two saved reports to compare their category scores and findings.</p>
      </section>
    </div>
    <div v-else-if="activeTab === 'performance'" id="quality-performance-panel" role="tabpanel" aria-labelledby="quality-performance-tab">
      <PerformanceSetup :project-id="selectedProject?.project_id || ''" :revision-id="selectedProject?.revision_id || ''" />
    </div>
    <div v-else-if="activeTab === 'rules'" id="quality-rules-panel" role="tabpanel" aria-labelledby="quality-rules-tab">
      <RuleManager :project-id="selectedProject?.project_id || ''" :revision-id="selectedProject?.revision_id || ''" />
    </div>
    <div v-else-if="activeTab === 'external'" id="quality-external-analyzers-panel" role="tabpanel" aria-labelledby="quality-external-analyzers-tab">
      <ExternalAnalyzerSetup :project-id="selectedProject?.project_id || ''" />
    </div>
  </section>
  <ConfirmModal :show="!!deleteTarget" title="Delete assessment report?" :message="deleteTarget ? `This permanently removes the report from ${formatDate(deleteTarget.created_at)} and comparison history. This cannot be undone.` : ''" confirm-label="Delete report" :busy="!!deletingReportId" :error="deleteReportError" @confirm="removeReport" @cancel="deleteTarget = null; deleteReportError = ''" />
  <ConfirmModal :show="!!revisionDeleteTarget" title="Remove project revision?" :message="revisionDeleteTarget ? `Permanently remove ${revisionDeleteTarget.project_id} (${revisionDeleteTarget.revision_id}), its uploaded source, graph, vectors, assessment reports and configured runtime telemetry. Other revisions and shared project settings remain. This cannot be undone.` : ''" confirm-label="Remove revision" :busy="deletingRevision" :error="revisionDeleteError" @confirm="removeRevision" @cancel="revisionDeleteTarget = null; revisionDeleteError = ''" />
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
