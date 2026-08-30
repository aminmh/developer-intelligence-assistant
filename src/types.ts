export interface RAGConfig {
  ollamaBaseUrl: string;
  ollamaEmbedModel: string;
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
  temperature: number;
  topP: number;
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
}

export interface QueryResponse {
  answer: string;
  matches: SearchMatch[];
  sourceFiles: string[];
  llmUsed: string;
  durationMs: number;
}
