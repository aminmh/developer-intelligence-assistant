export interface RAGConfig {
  embeddingProvider: 'ollama' | 'openAICompatible' | 'cohere' | 'cloudflareWorkersAI';
  cohereApiKey: string;
  cloudflareAccountId: string;
  cloudflareApiToken: string;
  cloudflareEmbeddingModel: string;
  ollamaBaseUrl: string;
  ollamaEmbedModel: string;
  embeddingBaseUrl: string;
  ollamaLlmModel: string;
  ollamaEvalModel: string;
  qdrantUrl: string;
  qdrantApiKey: string;
  llmRemoteBaseUrl: string;
  llmRemoteModel: string;
  llmRemoteApiKey: string;
  runpodApiKey: string;
  docRetrievalEnabled: boolean;
  docContext7Enabled: boolean;
  docGitmcpEnabled: boolean;
  docStrictMode: boolean;
  docContext7TimeoutS: number;
  docGitmcpTimeoutS: number;
  domainConceptsEnabled: boolean;
  temperature: number;
  topP: number;
  contextPreviewMode: 'off' | 'inline' | 'gate';
  rerankerEnabled: boolean;
  rerankerModel: string;
  rerankerMaxCandidates: number;
  rerankerTimeoutS: number;
}

export type AppearanceMode = 'dark' | 'light' | 'system';
export type AppearancePalette =
  | 'default'
  | 'forest-teal'
  | 'ember-violet'
  | 'graphite-sage'
  | 'royal-lilac'
  | 'ember-copper'
  | 'black-emerald'
  | 'midnight-cobalt'
  | 'deep-teal-plum'
  | 'monochrome'
  | 'obsidian-plum'
  | 'black-coffee';
export type AppearanceAccent = 'indigo' | 'cyan' | 'emerald' | 'orange' | 'rose';
export type NeonBorderTheme = 'cyan' | 'violet' | 'amber' | 'emerald' | 'rose';
export type UiFontFamily = 'Inter' | 'IBM Plex Sans';
export type CodeFontFamily = 'Fira Code' | 'JetBrains Mono';

export interface AppearancePreferences {
  mode: AppearanceMode;
  palette: AppearancePalette;
  accent: AppearanceAccent;
  neonBorder: NeonBorderTheme;
  uiFontFamily: UiFontFamily;
  codeFontFamily: CodeFontFamily;
  showSystemMonitor: boolean;
  showWelcomeBanner: boolean;
  lowPerformanceMode: boolean;
}

export interface OllamaImportFile {
  file: string;
}

export interface OllamaImportJob {
  id: string;
  modelName: string;
  file: string;
  status: 'pending' | 'running' | 'success' | 'failed';
  message: string;
  error?: string;
  updatedAt: string;
}

export interface UploadedFile {
  id: string;
  name: string;
  type: string;
  size: number;
  chunkCount: number;
  uploadedAt: string;
  status: 'ready' | 'processing' | 'failed';
  error?: string;
  uploadSessionId?: string;
  projectId?: string;
  commitSha?: string;
  revisionId?: string;
  sourceRef?: string;
  archiveSha256?: string;
}

export interface FileChunk {
  id: string;
  fileId: string;
  fileName: string;
  text: string;
  index: number;
}

export interface SearchMatch {
  chunk: FileChunk;
  score: number;
  viaGraph?: boolean;
  symbol_id?: string;
  runtime_usage?: RuntimeSymbolMetric | null;
}

export interface RuntimeSymbolMetric {
  symbol_id: string;
  runs: number;
  last_seen: string | null;
  p95_ms: number;
  failures: number;
  callers?: Array<{ caller_symbol_id: string; calls: number }>;
  routes?: Array<{ route: string; calls: number }>;
  tables?: Array<{ table_name: string; calls: number }>;
}

export interface RuntimeContainer {
  container_id: string;
  project_id: string;
  environment: string;
  service: string;
  commit_sha: string;
  revision_id?: string | null;
  image_id: string;
  state: 'running' | 'stopped';
  updated_at: string;
}

export interface RuntimeProjectRecord {
  project_id: string;
  can_remove?: boolean;
  can_delete_telemetry?: boolean;
  commit_sha: string | null;
  revision_id: string | null;
  state: string;
  updated_at?: string;
  containers: RuntimeContainer[];
  running_containers: number;
  running_project_containers: number;
  running_commit_shas: string[];
  deployment_mismatch: boolean;
  source_archive_sha256?: string;
}

export interface RuntimeTelemetrySettings {
  enabled: boolean;
  store: string;
  connection: string | null;
  retention_hours: number;
  retention_days: number;
  aggregation_retention_days: number;
  collector_baseline_ratio: number;
  collector_baseline_percent: number;
  otlp_endpoint: string | null;
  scope: string;
  sample_window: string;
  p95_window_hours: number;
  refresh_minutes: number;
  aggregation_retention_hours: number;
  updated_at: string | null;
}

export interface RuntimeStorageTarget {
  mode: 'disabled' | 'local' | 'remote';
  remote_configured: boolean;
  host: string | null;
  http_port: number | null;
  native_port: number | null;
  ssh_port: number | null;
  ssh_user: string | null;
  host_fingerprint: string | null;
  database: string;
  username: string | null;
  health: string;
  verified_at: string | null;
  last_error: string | null;
}

export interface RuntimeHostMetricsValues {
  cpu_percent: number | null;
  memory_used_bytes: number;
  memory_total_bytes: number;
  load_average: [number, number, number];
  disk_free_bytes: number;
  disk_total_bytes: number;
}

export interface RuntimeHostMetricsResponse {
  mode: 'disabled' | 'local' | 'remote' | 'unavailable';
  available: boolean;
  stale: boolean;
  sampled_at: string | null;
  ssh_health: 'connected' | 'unavailable' | 'not-required';
  ssh_message: string | null;
  clickhouse_health: 'available' | 'unavailable' | 'unknown' | 'not-required';
  clickhouse_message: string | null;
  metrics: RuntimeHostMetricsValues | null;
}

export interface RuntimeTelemetryData {
  available: boolean;
  reason?: string | null;
  total_spans: number;
  symbol_spans: number;
  matched_spans: number;
  match_percent: number;
  error_spans: number;
  last_seen: string | null;
  symbols: RuntimeSymbolMetric[];
  routes: Array<{ method: string; route: string; route_id: string; requests: number; p95_ms: number; failures: number; error_rate: number; last_seen: string | null }>;
  tables: Array<{ table_id: string; table_name: string; operation: string; operations: number; last_seen: string | null }>;
  callers: Array<{ symbol_id: string; caller_symbol_id: string; calls: number }>;
  method_calls: Array<{
    caller_symbol_id: string;
    symbol_id: string;
    calls: number;
    hook_calls: number;
    span_edges: number;
    sample_weight: number;
    hot_path_share: number;
    sources: Array<'span' | 'hook' | 'excimer'>;
  }>;
  hot_path: Array<{ symbol_id: string; sample_share: number }>;
  profile_count: number;
  profiled_samples: number;
  method_edge_coverage?: {
    observed_edges: number;
    static_edges: number;
    percent: number | null;
    basis: string;
  };
  symbol_routes: Array<{ symbol_id: string; route_id: string; method?: string; route: string; calls: number }>;
  symbol_tables: Array<{ symbol_id: string; table_id: string; table_name: string; calls: number }>;
  retention_hours: number;
  sampled_trace_retention_hours: number;
  unmatched_spans: number;
  unmatched_percent: number;
  aggregation_window_hours: number;
  pipeline_health: string;
}

export interface RuntimeRequest {
  trace_id: string;
  span_id: string;
  timestamp: string | null;
  method: string;
  route: string;
  status_code: number | null;
  duration_ms: number | null;
  span_status: string;
  php_cpu_time_us: number | null;
  php_peak_memory_bytes: number | null;
}

export interface RuntimeDatabaseCall {
  span_id: string;
  parent_span_id: string;
  timestamp: string | null;
  duration_ms: number | null;
  db_system: string | null;
  db_namespace: string | null;
  operation: string | null;
  table_ids: string[];
  symbol_id: string | null;
  caller_symbol_id: string | null;
  span_status: string;
}

export interface RuntimeRequestDetail {
  available: boolean;
  reason?: string | null;
  sampled?: boolean;
  request: RuntimeRequest | null;
  database_calls: RuntimeDatabaseCall[];
  method_activity?: {
    available: boolean;
    reason?: string | null;
    edges: Array<{ caller_symbol_id: string; symbol_id: string; calls: number; source: 'hook' }>;
  };
  ai_summary?: {
    trace_id: string;
    endpoint: string;
    total_latency_ms: number;
    otel_breakdown: Record<string, number>;
    excimer_hotspots: Array<{ frame: string; sample_percentage: number }>;
    profile_event_type: string | null;
    actionable_insight: string;
  };
}

export interface RuntimeAnalysisFinding {
  id: string;
  title: string;
  summary: string;
  recommendations: string[];
  evidence_refs: string[];
  source_refs: string[];
  severity: 'low' | 'medium' | 'high' | 'informational';
}

export interface RuntimeRequestAnalysis {
  findings: RuntimeAnalysisFinding[];
  coverage_gaps: string[];
  provider: string;
  requested_provider: 'local' | 'remote';
  model: string;
  revision_id: string;
  source_available: boolean;
}

export interface RuntimeDiffPreview {
  diff: string;
  file_path: string;
  revision_id: string;
  provider: string;
  model: string;
  applied: false;
}

export interface RuntimeSnapshotData {
  project_id: string;
  commit_sha: string;
  revision_id: string | null;
  snapshot: Record<string, any>;
  symbol_index_available?: boolean;
  symbol_index_reason?: string | null;
  deployments: RuntimeContainer[];
  matching_deployments: RuntimeContainer[];
  deployment_mismatch: boolean;
  telemetry: RuntimeTelemetryData;
  settings: RuntimeTelemetrySettings;
}

export interface RetrievalInsightSource {
  file: string;
  class_name: string;
  name: string;
  via_graph: boolean;
  score: number;
}

export interface RetrievalInsights {
  trace_id: string;
  query_hash: string;
  mode: 'hybrid' | 'vector_only' | 'no_results' | 'vector_unavailable' | 'explicit_context';
  collection?: string | null;
  searched_collections: string[];
  vector_result_count: number;
  graph_neighbor_count: number;
  graph_context_count: number;
  graph_context_with_code_count: number;
  graph_available: boolean;
  graph_fallback: boolean;
  sources: RetrievalInsightSource[];
  timestamp: string;
  available?: boolean;
  reranker?: {
    enabled: boolean;
    model: string;
    candidate_count: number;
    reranked_count: number;
    latency_ms: number;
    fallback_reason?: string | null;
  };
}

export interface QueryResponse {
  answer: string;
  matches: SearchMatch[];
  sourceFiles: string[];
  llmUsed: string;
  durationMs: number;
  usage?: LLMUsage | null;
  context_usage?: ContextUsage | null;
}

export interface LLMUsage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  estimated: boolean;
  source: 'provider' | 'estimate';
}

export interface ContextUsage {
  input_tokens: number;
  limit_tokens: number;
  percent: number;
  estimated: boolean;
}

export type DeveloperLogType = 'llm_prompt' | 'exception' | 'router_fallback';
export interface DeveloperLogEvent {
  id: string;
  type: DeveloperLogType;
  timestamp: string;
  severity: 'info' | 'warning' | 'error';
  title: string;
  summary: string;
  details: {
    query?: string;
    prompt?: string;
    message?: string;
    thread?: string;
    stackTrace?: string;
    route?: string;
    finalChoice?: string;
    reason?: string;
    confidence?: number;
    metadata?: Record<string, any>;
    raw?: string;
    parserWarning?: string;
  };
}
export interface DeveloperLogsResponse {
  events: DeveloperLogEvent[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  deletion_mode: 'soft' | 'hard';
}

export interface AnalysisSession {
  id: string;
  job_id: string;
  filename: string;
  collection_name: string | null;
  label: string;
  status: string;
  created_at: string;
  summary: string;
  grounding_md?: string;
  // Known kinds so far; unknown values are tolerated defensively at runtime
  // (the python service may add more kinds without a client redeploy).
  kind?: 'structure' | 'code_index' | 'analysis';
  profile?: Record<string, any>;
  runtime?: Record<string, any>;
  counts?: Record<string, any>;
  ingest?: { ingested?: number; chunk_count?: number; error?: string };
  link?: { linked?: number; reason?: string };
  knowledge_chunks?: number;
}

export interface DomainConceptSource {
  concept_identity?: string;
  file: string;
  source_file?: string;
  source_hash?: string;
  start_line?: number | null;
  end_line?: number | null;
  method?: string;
  confidence?: number;
  metadata?: Record<string, unknown> | null;
}

export interface DomainConcept {
  id?: string;
  identity_key: string;
  concept_type: string;
  canonical_name: string;
  qualified_name?: string | null;
  normalized_name?: string;
  description?: string | null;
  roles?: string[];
  aliases?: Array<{ alias: string; alias_type?: string; confidence?: number; provenance?: string }>;
  sources?: DomainConceptSource[];
  metadata?: Record<string, unknown>;
  relationship_count?: number;
  distance?: number;
  final_score?: number;
  match_kinds?: string[];
  matched_alias?: string | null;
}

export interface DomainRelationship {
  id?: string;
  relation_type: string;
  source_identity: string;
  target_identity: string;
  source?: Record<string, unknown>;
  target?: Record<string, unknown>;
  confidence?: number;
  metadata?: Record<string, unknown>;
}

export interface DomainContextEvidence {
  kind: string;
  name?: string;
  file?: string;
  start_line?: number | null;
  end_line?: number | null;
  score?: number | null;
  excerpt?: string | null;
  source?: string;
}

export interface DomainKnowledgeSource {
  analysis_id: string;
  filename: string;
  project_id: string;
  framework?: string;
  provenance?: string[];
  generated_at?: string;
}

export interface DomainConceptsResponse {
  success: boolean;
  source: DomainKnowledgeSource;
  items: DomainConcept[];
  total: number;
  offset: number;
  limit: number;
  warnings?: string[];
}

export interface DomainContextResponse {
  success: boolean;
  source: DomainKnowledgeSource;
  recognized: DomainConcept[];
  ambiguous: DomainConcept[];
  rejected: DomainConcept[];
  relationships: DomainRelationship[];
  evidence: DomainContextEvidence[];
  warnings: string[];
  truncated: boolean;
  degraded: boolean;
  analysis_context?: string | null;
}

/** Framework-neutral saved entity model produced by the domain-model API. */
export interface DomainEntityProperty {
  name: string;
  type: string;
  visibility?: 'public' | 'protected' | 'private' | 'internal' | string;
  is_primary?: boolean;
  is_foreign?: boolean;
  is_nullable?: boolean;
  cast_or_enum?: string | null;
  primaryKey?: boolean;
  foreignKey?: boolean;
  nullable?: boolean;
  [key: string]: unknown;
}

export interface DomainEntityRelationship {
  id?: string;
  name?: string;
  type?: string;
  target_entity: string;
  foreign_key?: string | null;
  cardinality?: string;
  relation_name?: string;
  source_entity?: string;
  description?: string | null;
  [key: string]: unknown;
}

export interface DomainEntity {
  id?: string;
  name: string;
  namespace?: string | null;
  table?: string | null;
  framework?: string | null;
  stereotype?: string;
  description?: string | null;
  properties: DomainEntityProperty[];
  relationships?: DomainEntityRelationship[];
  methods?: Array<string | DomainEntityBehavior>;
  extends?: string[];
  implements?: string[];
  scopes_or_behaviors?: string[];
  behaviors?: string[];
  provenance?: string | null;
  [key: string]: unknown;
}

export interface DomainEntityBehavior {
  name: string;
  visibility?: string;
  return_type?: string;
  parameters?: string;
  is_static?: boolean;
  signature?: string;
  owner?: string | null;
  source_file?: string | null;
  definition_line?: number | null;
  lookup_key?: string | null;
  behavior_key?: string;
  has_source_identity?: boolean;
  usage_available?: boolean;
  [key: string]: unknown;
}

export interface DomainBehaviorUsage {
  file: string;
  line: number;
  location: string;
  usage_kind?: string;
  caller_class?: string | null;
  caller_method?: string | null;
  caller_fqn?: string | null;
}

export interface DomainBehaviorUsagesResponse {
  success: boolean;
  analysis_id: string;
  entity: string;
  behavior?: DomainEntityBehavior & {
    signature?: string;
    file?: string | null;
    line?: number | null;
  } | null;
  definition?: DomainBehaviorUsagesResponse['behavior'];
  usages: DomainBehaviorUsage[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  next_offset?: number | null;
  index?: { available: boolean; collection?: string };
  warnings?: Array<{ code?: string; message?: string } | string>;
}

export interface DomainModelGraphNode {
  id: string;
  shape?: string;
  x?: number;
  y?: number;
  position?: { x: number; y: number };
  width?: number;
  height?: number;
  data?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface DomainModelGraphEdge {
  id?: string;
  source: string | { cell: string; port?: string };
  target: string | { cell: string; port?: string };
  labels?: Array<Record<string, unknown>>;
  label?: string;
  router?: Record<string, unknown>;
  connector?: Record<string, unknown>;
  attrs?: Record<string, unknown>;
  data?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface DomainModelResponse {
  success?: boolean;
  entities: DomainEntity[];
  relationships?: DomainEntityRelationship[];
  plantuml: string;
  antv_x6?: { nodes?: DomainModelGraphNode[]; edges?: DomainModelGraphEdge[] };
  source?: DomainKnowledgeSource & Record<string, unknown>;
  warnings?: Array<string | { code?: string; message?: string; severity?: string; [key: string]: unknown }>;
  deterministic?: boolean;
  enriched?: boolean;
  status?: string;
  [key: string]: unknown;
}

export interface DomainDictionaryResponse {
  success: boolean;
  source: 'domain_dictionary.txt' | string;
  scope: 'global' | string;
  semantic_context_available?: boolean;
  items: Array<{ term: string; count: number }>;
  total: number;
  offset: number;
  limit: number;
}

export interface SchemaVisualizationColumn {
  name: string;
  type: string;
  primaryKey: boolean;
  foreignKey: boolean;
  nullable: boolean;
  default: unknown;
  unique: boolean;
  indexed: boolean;
  references: { table: string; column: string } | null;
}

export interface SchemaVisualizationNode {
  id: string;
  type: 'databaseTable' | string;
  position: { x: number; y: number };
  data: {
    table: string;
    columns: SchemaVisualizationColumn[];
    searchText?: string;
  };
}

export interface SchemaVisualizationEdge {
  id: string;
  source: string;
  target: string;
  columns: Array<{ source: string; target: string }>;
  cardinality: '1:1' | '1:N' | 'N:M' | string;
  description: string;
  provenance?: string;
}

export interface SchemaVisualizationWarning {
  code: string;
  message: string;
  table?: string;
  column?: string;
  target?: string;
  source?: string;
}

export interface SchemaVisualizationResponse {
  nodes: SchemaVisualizationNode[];
  edges: SchemaVisualizationEdge[];
  warnings: SchemaVisualizationWarning[];
  source: {
    framework: string | null;
    analysisId: string;
    migrationProvenance: string;
    modelProvenance: string;
    migrationTableCount: number;
    modelCount: number;
    generatedAt: string;
  };
}

/** Framework-neutral schema model consumed by the X6 visualizer. */
export interface ErdSourceMetadata {
  kind?: 'saved-analysis' | 'sql' | 'dbml' | 'json' | string;
  framework?: string | null;
  dialect?: 'mysql' | 'postgres' | 'sqlite' | string | null;
  analysisId?: string | null;
  generatedAt?: string | null;
  provenance?: string | null;
  migrationProvenance?: string | null;
  modelProvenance?: string | null;
  [key: string]: unknown;
}

export interface MySQLColumnDefinition {
  unsigned: boolean;
  charset: string | null;
  collation: string | null;
  comment: string | null;
  default_kind: 'none' | 'null' | 'literal';
  other_attributes_absent: boolean;
}

export interface ErdField {
  id: string;
  name: string;
  type: string;
  nullable: boolean;
  default?: unknown;
  primaryKey: boolean;
  unique?: boolean;
  indexed?: boolean;
  autoIncrement?: boolean;
  collation?: string | null;
  mysqlDefinition?: MySQLColumnDefinition | null;
  foreignKey?: boolean;
  references?: { table: string; column: string } | null;
  provenance?: string | null;
  unsupported?: boolean;
}

export interface ErdTable {
  id: string;
  name: string;
  schema?: string | null;
  fields: ErdField[];
  position?: { x: number; y: number };
  width?: number;
  height?: number;
  provenance?: string | null;
}

export interface ErdRef {
  id: string;
  sourceTable: string;
  sourceColumn: string;
  targetTable: string;
  targetColumn: string;
  cardinality: '1:1' | '1:N' | 'N:M' | string;
  sourceMany?: boolean;
  targetMany?: boolean;
  description?: string;
  provenance?: string | null;
  unsupported?: boolean;
}

export interface ErdWarning {
  code: string;
  message: string;
  severity?: 'info' | 'warning' | 'error';
  table?: string;
  column?: string;
  target?: string;
  source?: string;
}

export interface NormalizedSchema {
  tables: ErdTable[];
  refs: ErdRef[];
  warnings: ErdWarning[];
  source: ErdSourceMetadata;
  /** Kept for compatibility while older API responses are being migrated. */
  legacy?: SchemaVisualizationResponse;
}

export interface EvaluatorSourceTrace {
  file?: string | null;
  line?: number | null;
  class_name?: string | null;
  method?: string | null;
  namespace?: string | null;
  context?: string[] | string;
  language?: string | null;
  available?: boolean;
  error?: string | null;
}

export interface EvaluatorReport {
  query_fingerprint: string;
  query?: string;
  database?: string | null;
  metrics: Record<string, unknown>;
  source_trace: EvaluatorSourceTrace;
  profiler: Record<string, unknown> | string;
  provenance?: Record<string, unknown>;
  issues?: string[];
  availability: 'available' | 'partial' | 'unavailable' | 'error';
  error?: string | null;
  evidence?: string[];
}

export interface EvaluatorRunStatus {
  run_id: string;
  analysis_id?: string;
  filename: string;
  status: 'queued' | 'running' | 'completed' | 'error';
  reports: EvaluatorReport[];
  context?: Record<string, unknown>;
  error?: string | null;
}

export interface EvaluatorRecommendation {
  availability: 'available' | 'insufficient_evidence' | 'unavailable' | string;
  root_cause_analysis?: string;
  evidence?: string[];
  confidence?: 'high' | 'medium' | 'low' | string;
  application_optimization?: string;
  database_recommendation?: string;
  migration_or_ddl?: string;
  unified_diff_patch?: string;
  review_required: boolean;
  auto_apply: boolean;
  notes?: string[];
}
