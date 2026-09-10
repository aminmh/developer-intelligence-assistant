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
