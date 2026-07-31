import { GoogleGenAI } from '@google/genai';
import { QdrantClient } from '@qdrant/js-client-rest';
import * as fs from 'fs';
import * as path from 'path';
import 'dotenv/config';
import { RAGConfig, UploadedFile, FileChunk, SearchMatch, QueryResponse } from '../types';

// In-memory/File-based storage path
const STORE_DIR = path.join(process.cwd(), 'data');
const FILES_PATH = path.join(STORE_DIR, 'files.json');
const CHUNKS_PATH = path.join(STORE_DIR, 'chunks.json');
const VECTORS_PATH = path.join(STORE_DIR, 'vectors.json');
const CONFIG_PATH = path.join(STORE_DIR, 'config.json');

// Ensure directory exists
if (!fs.existsSync(STORE_DIR)) {
  fs.mkdirSync(STORE_DIR, { recursive: true });
}

// Global configurations with default values
let ragConfig: RAGConfig = {
  llmMode: 'gemini',
  localLlmUrl: 'http://localhost:11434',
  localLlmModel: 'llama3',
  embeddingMode: 'ollama',
  ollamaUrl: process.env.OLLAMA_URL || 'http://localhost:11434',
  ollamaModel: process.env.OLLAMA_MODEL || 'BGE-M3',
  hfModel: 'sentence-transformers/all-MiniLM-L6-v2',
  hfToken: '',
  chunkSize: 500,
  chunkOverlap: 50,
  qdrantUrl: '',
  qdrantApiKey: '',
  temperature: 0.7,
  topP: 0.9,
};

// In-memory caches loaded from disk if they exist
let uploadedFiles: UploadedFile[] = [];
let fileChunks: FileChunk[] = [];
// Map from chunk ID to embedding float vector
let chunkVectors: Record<string, number[]> = {};

// Load persistence database
function loadDatabase() {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      ragConfig = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
    }
    if (fs.existsSync(FILES_PATH)) {
      uploadedFiles = JSON.parse(fs.readFileSync(FILES_PATH, 'utf-8'));
    }
    if (fs.existsSync(CHUNKS_PATH)) {
      fileChunks = JSON.parse(fs.readFileSync(CHUNKS_PATH, 'utf-8'));
    }
    if (fs.existsSync(VECTORS_PATH)) {
      chunkVectors = JSON.parse(fs.readFileSync(VECTORS_PATH, 'utf-8'));
    }
  } catch (error) {
    console.error('Error loading RAG databases:', error);
  }
}

// Save persistence database
function saveDatabase() {
  try {
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(ragConfig, null, 2), 'utf-8');
    fs.writeFileSync(FILES_PATH, JSON.stringify(uploadedFiles, null, 2), 'utf-8');
    fs.writeFileSync(CHUNKS_PATH, JSON.stringify(fileChunks, null, 2), 'utf-8');
    fs.writeFileSync(VECTORS_PATH, JSON.stringify(chunkVectors, null, 2), 'utf-8');
  } catch (error) {
    console.error('Error saving RAG databases:', error);
  }
}

// Initialize database
loadDatabase();

// Initialize Google AI client lazily
let aiClient: GoogleGenAI | null = null;
function getGeminiClient(): GoogleGenAI {
  if (!aiClient) {
    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
      throw new Error('GEMINI_API_KEY is not defined. Please configure it in Settings > Secrets.');
    }
    aiClient = new GoogleGenAI({
      apiKey,
      httpOptions: {
        headers: {
          'User-Agent': 'aistudio-build',
        },
      },
    });
  }
  return aiClient;
}

// Initialize Qdrant Client lazily
let qdrantClient: QdrantClient | null = null;
function getQdrantClient(): QdrantClient | null {
  if (ragConfig.qdrantUrl) {
    qdrantClient = new QdrantClient({
      url: ragConfig.qdrantUrl,
      apiKey: ragConfig.qdrantApiKey || undefined,
    });
    return qdrantClient;
  }
  return null;
}

// Helper to calculate cosine similarity between two vectors
function cosineSimilarity(vecA: number[], vecB: number[]): number {
  let dotProduct = 0.0;
  let normA = 0.0;
  let normB = 0.0;
  
  const len = Math.min(vecA.length, vecB.length);
  for (let i = 0; i < len; i++) {
    dotProduct += vecA[i] * vecB[i];
    normA += vecA[i] * vecA[i];
    normB += vecB[i] * vecB[i];
  }
  
  if (normA === 0 || normB === 0) return 0;
  return dotProduct / (Math.sqrt(normA) * Math.sqrt(normB));
}

// Get embedding for a text chunk
async function getEmbedding(text: string): Promise<number[]> {
  if (ragConfig.embeddingMode === 'gemini') {
    const client = getGeminiClient();
    const response = await client.models.embedContent({
      model: 'gemini-embedding-2-preview',
      contents: text,
    });
    
    // Extract embedding vector
    const embedding = response.embeddings?.[0];
    if (embedding && Array.isArray(embedding.values)) {
      return embedding.values;
    } else if (Array.isArray(embedding)) {
      return embedding as number[];
    } else {
      throw new Error('Failed to extract embedding values from Gemini API response');
    }
  }

  if (ragConfig.embeddingMode === 'ollama') {
    const response = await fetch(`${ragConfig.ollamaUrl}/api/embeddings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: ragConfig.ollamaModel,
        prompt: text,
      }),
    });

    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`Ollama Embedding failed (Status ${response.status}): ${errText || response.statusText}. Is Ollama running at ${ragConfig.ollamaUrl}?`);
    }

    const result = await response.json();
    if (result.embedding && Array.isArray(result.embedding)) {
      return result.embedding;
    }

    throw new Error('Ollama Embedding did not return a valid embedding vector');
  }

  // HuggingFace embedding API
  if (ragConfig.embeddingMode === 'huggingface') {
    const url = `https://api-inference.huggingface.co/pipeline/feature-extraction/${ragConfig.hfModel}`;
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (ragConfig.hfToken) {
      headers['Authorization'] = `Bearer ${ragConfig.hfToken}`;
    }

    const response = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        inputs: [text],
        options: { wait_for_model: true },
      }),
    });

    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`HuggingFace Embedding failed (Status ${response.status}): ${errText || response.statusText}`);
    }

    const result = await response.json();
    if (Array.isArray(result)) {
      let arr = result;
      while (Array.isArray(arr[0])) {
        arr = arr[0];
      }
      return arr as number[];
    }
    throw new Error('HuggingFace Embedding did not return a valid nested numeric vector');
  }

  throw new Error(`Unsupported embedding mode: ${ragConfig.embeddingMode}`);
}

// Qdrant integration helpers
const QDRANT_COLLECTION = 'developer_assistant_rag';

async function setupQdrantCollection(vectorSize: number) {
  const client = getQdrantClient();
  if (!client) return;

  try {
    const collections = await client.getCollections();
    const exists = collections.collections.some(c => c.name === QDRANT_COLLECTION);
    
    if (!exists) {
      await client.createCollection(QDRANT_COLLECTION, {
        vectors: {
          size: vectorSize,
          distance: 'Cosine',
        },
      });
      console.log(`Created Qdrant collection "${QDRANT_COLLECTION}" with size ${vectorSize}`);
    }
  } catch (error) {
    console.error('Qdrant setup collection error (ignoring to allow local fallback):', error);
  }
}

// Smart text chunker
export function splitTextIntoChunks(text: string, chunkSize: number, chunkOverlap: number): string[] {
  const chunks: string[] = [];
  let startIndex = 0;
  
  if (chunkSize <= 0) chunkSize = 500;
  if (chunkOverlap < 0) chunkOverlap = 0;
  if (chunkOverlap >= chunkSize) chunkOverlap = Math.floor(chunkSize / 2);

  while (startIndex < text.length) {
    let endIndex = startIndex + chunkSize;
    if (endIndex > text.length) endIndex = text.length;

    // Smart split: try to end on a newline or space
    if (endIndex < text.length) {
      const windowStart = endIndex - Math.min(150, Math.floor(chunkSize / 3));
      const windowText = text.substring(windowStart, endIndex + 50);
      
      const newlineIdx = windowText.lastIndexOf('\n');
      const spaceIdx = windowText.lastIndexOf(' ');
      
      if (newlineIdx !== -1 && (windowStart + newlineIdx) > startIndex) {
        endIndex = windowStart + newlineIdx + 1;
      } else if (spaceIdx !== -1 && (windowStart + spaceIdx) > startIndex) {
        endIndex = windowStart + spaceIdx + 1;
      }
    }

    chunks.push(text.substring(startIndex, endIndex).trim());

    if (endIndex >= text.length) break;

    const nextStartIndex = endIndex - chunkOverlap;
    // Hard guard against infinite loops: once endIndex is pinned at text.length
    // on a preceding iteration this wouldn't apply (we already broke above),
    // but nextStartIndex can still fail to advance past startIndex earlier in
    // the document if chunkOverlap is large relative to the smart-split
    // adjustment — fall back to endIndex (no overlap) to guarantee progress.
    startIndex = nextStartIndex > startIndex ? nextStartIndex : endIndex;
  }

  return chunks.filter(c => c.length > 10); // ignore micro chunks
}

// Number of chunks embedded concurrently per batch (bounds embedding wall-clock
// for huge documents without depending on an unverified provider batch API).
const EMBED_BATCH_SIZE = 5;

// Chunk, embed and index already-extracted plain text. Shared by both the
// inline small-file upload path and the async large-file (TUS) path.
async function chunkEmbedAndStore(fileId: string, name: string, text: string): Promise<{ chunkCount: number }> {
  if (!text || text.trim().length === 0) {
    throw new Error('Extracted file text is empty or invalid.');
  }

  // Chunk text
  const chunks = splitTextIntoChunks(text, ragConfig.chunkSize, ragConfig.chunkOverlap);
  if (chunks.length === 0) {
    throw new Error('No content chunks could be generated from the file.');
  }

  const createdChunks: FileChunk[] = chunks.map((chunkText, i) => ({
    id: `${fileId}_chunk_${i}`,
    fileId,
    fileName: name,
    text: chunkText,
    index: i,
  }));
  const createdVectors: Record<string, number[]> = {};

  // For tracking first vector size (used to size the Qdrant collection)
  let firstVectorSize = 0;

  // Embed chunks in small concurrent batches instead of one at a time
  for (let start = 0; start < createdChunks.length; start += EMBED_BATCH_SIZE) {
    const batch = createdChunks.slice(start, start + EMBED_BATCH_SIZE);
    const vectors = await Promise.all(batch.map(async (chunk, offset) => {
      try {
        return await getEmbedding(chunk.text);
      } catch (error: any) {
        throw new Error(`Embedding chunk ${start + offset + 1}/${createdChunks.length} failed: ${error.message}. Please verify your embedding model config or secrets.`);
      }
    }));

    batch.forEach((chunk, offset) => {
      if (start === 0 && offset === 0) firstVectorSize = vectors[offset].length;
      createdVectors[chunk.id] = vectors[offset];
    });
  }

  // If Qdrant is enabled, try uploading to Qdrant
  const qClient = getQdrantClient();
  if (qClient && firstVectorSize > 0) {
    try {
      await setupQdrantCollection(firstVectorSize);

      // Prepare Qdrant payload
      const points = createdChunks.map((chunk) => ({
        id: iToUUID(chunk.id), // UUID format
        vector: createdVectors[chunk.id],
        payload: {
          id: chunk.id,
          fileId: chunk.fileId,
          fileName: chunk.fileName,
          text: chunk.text,
          index: chunk.index,
        },
      }));

      await qClient.upsert(QDRANT_COLLECTION, {
        wait: true,
        points,
      });
      console.log(`Successfully indexed ${points.length} chunks in Qdrant.`);
    } catch (error: any) {
      console.error('Failed to upload vectors to Qdrant, falling back to local memory storage:', error.message);
    }
  }

  fileChunks.push(...createdChunks);
  chunkVectors = { ...chunkVectors, ...createdVectors };
  saveDatabase();

  return { chunkCount: createdChunks.length };
}

// Exported controllers and service functions
export const RAGService = {
  // Get current configurations
  getConfig(): RAGConfig {
    return ragConfig;
  },

  // Save configurations
  updateConfig(newConfig: Partial<RAGConfig>): RAGConfig {
    ragConfig = { ...ragConfig, ...newConfig };
    saveDatabase();
    return ragConfig;
  },

  // Get uploaded files list
  getFiles(): UploadedFile[] {
    return uploadedFiles;
  },

  // Index pre-extracted text (from PHP service upload). Replaces the old
  // uploadFile which decoded base64 and parsed PDF locally — now the PHP
  // service handles storage and extraction, the web just indexes.
  async uploadExtractedText(name: string, type: string, size: number, text: string, uploadSessionId?: string): Promise<UploadedFile> {
    if (!text || text.trim().length === 0) {
      throw new Error('Extracted file text is empty or invalid.');
    }

    const fileId = `file_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;

    const { chunkCount } = await chunkEmbedAndStore(fileId, name, text);

    const uploadedFile: UploadedFile = {
      id: fileId,
      name,
      type,
      size,
      chunkCount,
      uploadedAt: new Date().toISOString(),
      status: 'ready',
      uploadSessionId,
    };

    uploadedFiles.push(uploadedFile);
    saveDatabase();

    return uploadedFile;
  },

  // Index text already extracted upstream (large-file TUS path). Responds
  // immediately with a 'processing' record and indexes in the background,
  // since a huge document's chunk/embed pipeline can take far longer than
  // the caller (python-api's webhook forward) is willing to hold a connection open.
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
      status: 'processing',
      uploadSessionId,
    };

    uploadedFiles.push(uploadedFile);
    saveDatabase();

    chunkEmbedAndStore(fileId, name, text)
      .then(({ chunkCount }) => {
        uploadedFile.chunkCount = chunkCount;
        uploadedFile.status = 'ready';
        saveDatabase();
      })
      .catch((error: any) => {
        console.error(`Background indexing failed for ${name}:`, error.message);
        uploadedFile.status = 'failed';
        uploadedFile.error = error.message;
        saveDatabase();
      });

    return uploadedFile;
  },

  // Delete an uploaded file and all its chunks
  async deleteFile(fileId: string): Promise<boolean> {
    const fileIndex = uploadedFiles.findIndex(f => f.id === fileId);
    if (fileIndex === -1) return false;

    // Get chunk IDs to delete
    const chunksToDelete = fileChunks.filter(c => c.fileId === fileId);
    const chunkIdsToDelete = chunksToDelete.map(c => c.id);

    // Delete from Qdrant if client exists
    const qClient = getQdrantClient();
    if (qClient) {
      try {
        await qClient.delete(QDRANT_COLLECTION, {
          points: chunkIdsToDelete.map(id => iToUUID(id)),
        });
      } catch (error) {
        console.error('Qdrant delete points error (ignoring):', error);
      }
    }

    // Delete from in-memory/file-based backup
    uploadedFiles.splice(fileIndex, 1);
    fileChunks = fileChunks.filter(c => c.fileId !== fileId);
    for (const chunkId of chunkIdsToDelete) {
      delete chunkVectors[chunkId];
    }

    saveDatabase();
    return true;
  },

  // Semantic retrieval only
  async retrieve(query: string, limit: number = 4): Promise<SearchMatch[]> {
    if (fileChunks.length === 0) return [];

    // Get embedding of query
    const queryVector = await getEmbedding(query);

    // Try retrieving via Qdrant if active
    const qClient = getQdrantClient();
    if (qClient) {
      try {
        const searchResults = await qClient.search(QDRANT_COLLECTION, {
          vector: queryVector,
          limit,
          with_payload: true,
        });

        // Map results back to SearchMatch format
        const matches: SearchMatch[] = searchResults.map(res => {
          const payload = res.payload as any;
          return {
            chunk: {
              id: payload.id || res.id,
              fileId: payload.fileId,
              fileName: payload.fileName,
              text: payload.text,
              index: payload.index,
            },
            score: res.score,
          };
        });

        if (matches.length > 0) {
          return matches;
        }
      } catch (error: any) {
        console.error('Qdrant search retrieval failed, falling back to local memory vector search:', error.message);
      }
    }

    // Fallback Local Memory Search (exact cosine similarity)
    const matches: SearchMatch[] = [];
    for (const chunk of fileChunks) {
      const vec = chunkVectors[chunk.id];
      if (!vec) continue;

      const score = cosineSimilarity(queryVector, vec);
      matches.push({ chunk, score });
    }

    // Sort by descending score
    matches.sort((a, b) => b.score - a.score);
    return matches.slice(0, limit);
  },

  // Perform semantic search and generate LLM answer
  async query(userQuery: string, limit: number = 4): Promise<QueryResponse> {
    const startTime = Date.now();
    
    // Retrieve context
    const matches = await this.retrieve(userQuery, limit);
    
    // Format retrieved context
    const contextText = matches
      .map((m, idx) => `[Source ${idx + 1} - ${m.chunk.fileName}]\n${m.chunk.text}`)
      .join('\n\n---\n\n');

    const sourceFiles: string[] = Array.from(new Set(matches.map(m => m.chunk.fileName))) as string[];

    let answer = '';
    let llmUsed = '';

    const systemPrompt = `You are an expert developer assistant RAG system. Answer the user's coding query strictly using the following retrieved code snippets and file context. 
If the retrieved context does not contain enough information to solve the request, state clearly what is missing and then use your broad pre-trained software development knowledge to give a comprehensive, helpful solution.
Always output beautiful Markdown with complete type declarations and clear syntax-highlighted code blocks where appropriate.

RETIREVED FILE CONTEXT:
${contextText || "No context snippets retrieved from the uploaded files."}`;

    if (ragConfig.llmMode === 'gemini') {
      const client = getGeminiClient();
      llmUsed = 'Gemini 3.5 Flash';

      const response = await client.models.generateContent({
        model: 'gemini-3.5-flash',
        contents: userQuery,
        config: {
          systemInstruction: systemPrompt,
          temperature: ragConfig.temperature,
          topP: ragConfig.topP,
        },
      });

      answer = response.text || 'Error: Empty response from Gemini API.';
    } else {
      // Local LLM Mode
      llmUsed = `Local LLM (${ragConfig.localLlmModel}) at ${ragConfig.localLlmUrl}`;
      const fullPrompt = `System instructions: ${systemPrompt}\n\nUser Question: ${userQuery}\n\nAnswer:`;

      try {
        const response = await fetch(`${ragConfig.localLlmUrl}/api/generate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            model: ragConfig.localLlmModel,
            prompt: fullPrompt,
            stream: false,
            options: {
              temperature: ragConfig.temperature,
              top_p: ragConfig.topP,
            }
          }),
        });

        if (!response.ok) {
          throw new Error(`Local LLM API error: ${response.statusText}`);
        }

        const data = await response.json();
        answer = data.response || data.text || 'Empty response from Local LLM.';
      } catch (error: any) {
        console.error('Local LLM generation failure:', error.message);
        answer = `Failed to generate response using Local LLM at ${ragConfig.localLlmUrl}. 
Error: ${error.message}

Please make sure your local LLM service (like Ollama) is running and accessible. 
Below are the retrieved semantic search matches for your reference:

${matches.map((m, idx) => `**Chunk ${idx+1} from ${m.chunk.fileName} (Score: ${(m.score * 100).toFixed(1)}%)**:\n\`\`\`\n${m.chunk.text}\n\`\`\``).join('\n\n')}`;
      }
    }

    const durationMs = Date.now() - startTime;

    return {
      answer,
      matches,
      sourceFiles,
      llmUsed,
      durationMs,
    };
  }
};

// Map file ID string cleanly to UUID format for Qdrant client compatibility
function iToUUID(id: string): string {
  // UUID format: 8-4-4-4-12 hex characters
  // Let's generate a simple repeatable hash-based UUID from the id string
  let hash = 0;
  for (let i = 0; i < id.length; i++) {
    hash = id.charCodeAt(i) + ((hash << 5) - hash);
  }
  
  const s = Math.abs(hash).toString(16).padEnd(32, 'a');
  return `${s.substring(0,8)}-${s.substring(8,12)}-${s.substring(12,16)}-${s.substring(16,20)}-${s.substring(20,32)}`;
}
