import * as fs from 'fs';
import * as path from 'path';
import 'dotenv/config';
import { RAGConfig, UploadedFile } from '../types';

// In-memory/File-based storage path (file tracking only — answer generation and
// retrieval now live in the Python service).
const STORE_DIR = path.join(process.cwd(), 'data');
const FILES_PATH = path.join(STORE_DIR, 'files.json');

// Secret fields are write-only: masked on read, only overwritten on non-empty write.
const SECRET_CONFIG_KEYS = ['qdrantApiKey', 'llmRemoteApiKey', 'cohereApiKey', 'cloudflareApiToken'] as const;

// Ensure directory exists
if (!fs.existsSync(STORE_DIR)) {
  fs.mkdirSync(STORE_DIR, { recursive: true });
}

// Global configurations with default values (overridden from the Python service).
let ragConfig: RAGConfig = {
  embeddingProvider: 'ollama',
  cohereApiKey: '',
  cloudflareAccountId: '',
  cloudflareApiToken: '',
  cloudflareEmbeddingModel: '@cf/baai/bge-base-en-v1.5',
  ollamaBaseUrl: process.env.OLLAMA_URL || 'http://host.docker.internal:11434',
  ollamaEmbedModel: process.env.OLLAMA_MODEL || 'BGE-M3',
  embeddingBaseUrl: '',
  ollamaLlmModel: 'gemma3-4b',
  ollamaEvalModel: process.env.OLLAMA_EVAL_MODEL || 'gemma3-4b',
  qdrantUrl: '',
  qdrantApiKey: '',
  llmRemoteBaseUrl: '',
  llmRemoteModel: '',
  llmRemoteApiKey: '',
  assessmentRequestTokenLimit: 0,
  docRetrievalEnabled: true,
  docContext7Enabled: true,
  docGitmcpEnabled: true,
  docStrictMode: false,
  docContext7TimeoutS: 10,
  docGitmcpTimeoutS: 15,
  domainConceptsEnabled: true,
  temperature: 0.7,
  topP: 0.9,
  contextPreviewMode: 'off',
  rerankerEnabled: false,
  rerankerModel: 'BAAI/bge-reranker-v2-m3',
  rerankerMaxCandidates: 40,
  rerankerTimeoutS: 10,
};

// In-memory file tracking loaded from disk if it exists.
let uploadedFiles: UploadedFile[] = [];

// Load persistence database
function loadDatabase() {
  try {
    if (fs.existsSync(FILES_PATH)) {
      uploadedFiles = JSON.parse(fs.readFileSync(FILES_PATH, 'utf-8'));
    }
  } catch (error) {
    console.error('Error loading file database:', error);
  }
}

// Save persistence database
function saveDatabase() {
  try {
    fs.writeFileSync(FILES_PATH, JSON.stringify(uploadedFiles, null, 2), 'utf-8');
  } catch (error) {
    console.error('Error saving file database:', error);
  }
}

// Initialize database
loadDatabase();

// Exported controllers and service functions
export const RAGService = {
  // Get current configurations
  getConfig(): RAGConfig {
    return ragConfig;
  },

  // Replace the in-memory config wholesale (seed from Python service).
  setConfigLocal(config: RAGConfig): RAGConfig {
    ragConfig = config;
    return ragConfig;
  },

  // Merge a partial config, preserving existing secret values when the incoming
  // value is empty/masked.
  applyConfig(newConfig: Partial<RAGConfig>): RAGConfig {
    const merged: RAGConfig = { ...ragConfig, ...newConfig };
    for (const key of SECRET_CONFIG_KEYS) {
      if (!newConfig[key]) {
        merged[key] = ragConfig[key];
      }
    }
    ragConfig = merged;
    return ragConfig;
  },

  // Get uploaded files list
  getFiles(): UploadedFile[] {
    return uploadedFiles;
  },

  // Delete a tracked file record
  async deleteFile(fileId: string): Promise<boolean> {
    const fileIndex = uploadedFiles.findIndex(f => f.id === fileId);
    if (fileIndex === -1) return false;

    uploadedFiles.splice(fileIndex, 1);
    saveDatabase();
    return true;
  },
};
