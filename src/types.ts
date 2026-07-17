export interface RAGConfig {
  llmMode: 'gemini' | 'local';
  localLlmUrl: string;
  localLlmModel: string;
  embeddingMode: 'gemini' | 'huggingface';
  hfModel: string;
  hfToken: string;
  chunkSize: number;
  chunkOverlap: number;
  qdrantUrl: string;
  qdrantApiKey: string;
  temperature: number;
  topP: number;
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
}

export interface QueryResponse {
  answer: string;
  matches: SearchMatch[];
  sourceFiles: string[];
  llmUsed: string;
  durationMs: number;
}
