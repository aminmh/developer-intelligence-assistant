export interface RAGConfig {
  ollamaBaseUrl: string;
  ollamaEmbedModel: string;
  embeddingBaseUrl: string;
  ollamaLlmModel: string;
  ollamaEvalModel: string;
  qdrantUrl: string;
  qdrantApiKey: string;
  sandboxDockerHost: string;
  sandboxJobsRoot: string;
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

export type DefinitionKind =
  | 'class' | 'interface' | 'trait' | 'enum' | 'method'
  | 'property' | 'class_const' | 'function' | 'const';

export interface IndexedSymbolProject {
  collection: string;
  stem: string;
  definitions: number;
}

export interface ExtractedDefinition {
  kind: DefinitionKind | string;
  name: string;
  fqn: string;
  file: string;
  line?: number | null;
  end_line?: number | null;
  visibility?: string | null;
  static?: boolean | null;
  abstract?: boolean | null;
  owner?: string | null;
}

export interface ExtractedDefinitionsResponse {
  items: ExtractedDefinition[];
  total: number;
  offset: number;
  limit: number;
}

// --- Sandbox analysis persistence (Python backend analysis_service) --------

export interface SandboxProbe {
  name: string;
  status: 'done' | 'skipped' | 'failed' | 'unavailable';
  exit_code?: number;
  command?: string;
  image?: string;
  reason?: string;
  stdout?: string;
}

export interface SandboxResult {
  staged?: boolean;
  source?: string;
  runtime_image?: string;
  install?: { status: string; exit_code?: number; error?: string; reason?: string; stdout?: string };
  probes?: SandboxProbe[];
  status?: string;
  error?: string;
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
