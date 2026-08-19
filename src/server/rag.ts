import * as fs from 'fs';
import * as path from 'path';
import 'dotenv/config';
import { RAGConfig, UploadedFile } from '../types';

// In-memory/File-based storage path (file tracking only — answer generation and
// retrieval now live in the Python service).
const STORE_DIR = path.join(process.cwd(), 'data');
const FILES_PATH = path.join(STORE_DIR, 'files.json');

// Secret fields are write-only: masked on read, only overwritten on non-empty write.
const SECRET_CONFIG_KEYS = ['qdrantApiKey'] as const;

// Ensure directory exists
if (!fs.existsSync(STORE_DIR)) {
  fs.mkdirSync(STORE_DIR, { recursive: true });
}

// Global configurations with default values (overridden from the Python service).
let ragConfig: RAGConfig = {
  ollamaBaseUrl: process.env.OLLAMA_URL || 'http://host.docker.internal:11434',
  ollamaEmbedModel: process.env.OLLAMA_MODEL || 'BGE-M3',
  ollamaLlmModel: 'gemma3-4b',
  chunkSize: 500,
  chunkOverlap: 50,
  qdrantUrl: '',
  qdrantApiKey: '',
  temperature: 0.7,
  topP: 0.9,
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

  // Save configurations (kept for backward compat; disk persistence removed —
  // the Python service is now the source of truth).
  updateConfig(newConfig: Partial<RAGConfig>): RAGConfig {
    return this.applyConfig(newConfig);
  },

  // Get uploaded files list
  getFiles(): UploadedFile[] {
    return uploadedFiles;
  },

  // Track a file that was uploaded/stored upstream (PHP/MinIO). Indexing now
  // happens in the Python service; the web only records metadata for /api/files.
  async uploadExtractedText(name: string, type: string, size: number, text: string, uploadSessionId?: string): Promise<UploadedFile> {
    if (!text || text.trim().length === 0) {
      throw new Error('Extracted file text is empty or invalid.');
    }

    const fileId = `file_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
    const uploadedFile: UploadedFile = {
      id: fileId,
      name,
      type,
      size,
      chunkCount: 0,
      uploadedAt: new Date().toISOString(),
      status: 'ready',
      uploadSessionId,
    };

    uploadedFiles.push(uploadedFile);
    saveDatabase();
    return uploadedFile;
  },

  // Track a file that was indexed externally (Python Qdrant) — no local
  // chunking/embedding, just adds to the tracking array so it appears in /api/files.
  async addTrackedFile(name: string, type: string, size: number, chunkCount: number, uploadSessionId?: string): Promise<UploadedFile> {
    const fileId = `file_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;

    const uploadedFile: UploadedFile = {
      id: fileId,
      name,
      type,
      size: size || 0,
      chunkCount: chunkCount || 0,
      uploadedAt: new Date().toISOString(),
      status: 'ready',
      uploadSessionId,
    };

    uploadedFiles.push(uploadedFile);
    saveDatabase();
    return uploadedFile;
  },

  // Track text already extracted upstream (large-file TUS path). No longer
  // embeds/chunks locally — the Python service handles indexing; here we only
  // record the file so it appears in /api/files.
  async indexExtractedText(name: string, type: string, size: number, text: string, uploadSessionId: string): Promise<UploadedFile> {
    const existing = uploadedFiles.find((f) => f.uploadSessionId === uploadSessionId);
    if (existing) {
      return existing;
    }

    const fileId = `file_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
    const uploadedFile: UploadedFile = {
      id: fileId,
      name,
      type,
      size,
      chunkCount: 0,
      uploadedAt: new Date().toISOString(),
      status: 'ready',
      uploadSessionId,
    };

    uploadedFiles.push(uploadedFile);
    saveDatabase();
    return uploadedFile;
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
