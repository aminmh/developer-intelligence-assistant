import express from 'express';
import path from 'path';
import * as fs from 'fs';
import { createServer as createViteServer } from 'vite';
import dotenv from 'dotenv';
import { RAGService } from './src/server/rag';
import { UploadedFile } from './src/types';

dotenv.config();

const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://localhost:9002';
const INTERNAL_SECRET = process.env.RAG_CALLBACK_SECRET || '';

// Secret fields masked before returning config to the browser (write-only).
const MASKED_SECRET_KEYS = ['qdrantApiKey', 'hfToken'];
function maskConfig(config: Record<string, any>) {
  const masked = { ...config };
  for (const key of MASKED_SECRET_KEYS) {
    if (masked[key]) masked[key] = '';
  }
  return masked;
}

// Cache LLM answers for orchestration jobs to avoid re-generating on re-poll
const answerCache = new Map<string, any>();

const PROMPT_LOG_PATH = path.join(process.cwd(), 'data', 'llm-prompts.log');

function logPrompt(userQuery: string, finalPrompt: string) {
  try {
    const dir = path.dirname(PROMPT_LOG_PATH);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const entry = [
      `[${new Date().toISOString()}]`,
      `USER_QUERY: ${userQuery}`,
      `FINAL_PROMPT:`,
      finalPrompt,
      '',
      '-'.repeat(120),
      '',
    ].join('\n');
    fs.appendFileSync(PROMPT_LOG_PATH, entry);
  } catch {
    // Log file is best-effort
  }
}

function labelForExtension(filename: string): string {
  const ext = path.extname(filename).toLowerCase();
  if (ext === '.pdf') return 'pdf';
  if (ext === '.zip') return 'zip';
  if (ext === '.md') return 'markdown';
  return 'text';
}

async function startServer() {
  const app = express();
  const PORT = parseInt(process.env.PORT || '3000', 10);

  // Set large JSON limit for uploading larger documents (including base64 PDF strings,
  // and extracted plain text handed back by python-api's large-file TUS pipeline)
  app.use(express.json({ limit: '100mb' }));
  app.use(express.urlencoded({ limit: '100mb', extended: true }));

  // CORS - allow cross-origin requests from any frontend origin
  app.use((req, res, next) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, PUT, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    if (req.method === 'OPTIONS') {
      return res.sendStatus(204);
    }
    next();
  });

  // API ROUTES (Must go FIRST)
  
  // Get current configurations (Python service is the source of truth)
  app.get('/api/config', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/settings`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Failed to load settings from Python service.' });
      }
      const data = await pyRes.json();
      const config = data.config ?? {};
      // Seed real (unmasked) values into the local RAG service, then mask for UI.
      RAGService.setConfigLocal(config);
      res.json({ success: true, config: maskConfig(config) });
    } catch (error: any) {
      console.error('Failed to load configurations from Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Save configurations (proxies to Python service)
  app.post('/api/config', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body),
      });
      if (!pyRes.ok) {
        const err = await pyRes.json().catch(() => ({ error: 'Failed to save settings.' }));
        return res.status(pyRes.status).json({ success: false, error: err.error || 'Failed to save settings.' });
      }
      const data = await pyRes.json();
      // Merge what was sent into the local mirror (preserving existing secrets).
      RAGService.applyConfig(req.body);
      res.json({ success: true, config: data.config ?? maskConfig(RAGService.getConfig()) });
    } catch (error: any) {
      console.error('Failed to save configurations via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Get uploaded files list (local + PHP-service MinIO files merged)
  app.get('/api/files', async (req, res) => {
    try {
      const files: UploadedFile[] = [...RAGService.getFiles()];
      const seen = new Set(files.map(f => f.uploadSessionId).filter(Boolean));

      // Fetch files from PHP service (TUS uploads stored in MinIO/rag-uploads)
      const phpUrl = process.env.PHP_SERVICE_URL || 'http://127.0.0.1:9003';      
      try {
        const phpRes = await fetch(`${phpUrl}/api/rag/files`);        
        if (phpRes.ok) {
          const data = await phpRes.json() as { files?: any[] };
          for (const f of data.files ?? []) {
            if (f.upload_session_id && seen.has(f.upload_session_id)) continue;
            seen.add(f.upload_session_id);
            files.push({
              id: f.upload_session_id ?? f.filename,
              name: f.filename,
              type: labelForExtension(f.filename),
              size: f.size ?? 0,
              chunkCount: 0,
              uploadedAt: f.last_modified ?? new Date().toISOString(),
              status: 'ready',
              uploadSessionId: f.upload_session_id ?? undefined,
            });
          }
        }
      } catch (err: any) {
        console.error('[files] PHP service unreachable, using local files only:', err.message);
      }

      res.json({ success: true, files });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Upload a file for indexing — forwards to PHP service for storage + extraction
  app.post('/api/upload', async (req, res) => {
    try {
      const { name, size, content } = req.body;
      if (!name || content === undefined) {
        return res.status(400).json({ success: false, error: 'Missing name or content in payload.' });
      }

      // Forward to PHP service for storage and text extraction
      const phpUrl = process.env.PHP_SERVICE_URL || 'http://127.0.0.1:9003';
      const phpRes = await fetch(`${phpUrl}/api/rag/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: name, content, size: size || 0 }),
      });

      if (!phpRes.ok) {
        const phpErr = await phpRes.json().catch(() => ({ error: phpRes.statusText }));
        return res.status(phpRes.status).json({ success: false, error: phpErr.error || 'Upload to PHP service failed.' });
      }

      const phpData = await phpRes.json();

      // ZIP files: store only, no indexing — on-demand via orchestrator in chat
      if (path.extname(name).toLowerCase() === '.zip') {
        return res.json({
          success: true,
          file: {
            id: phpData.upload_session_id,
            name,
            type: 'zip',
            size: size || phpData.size || 0,
            chunkCount: 0,
            uploadedAt: new Date().toISOString(),
            status: 'ready',
            uploadSessionId: phpData.upload_session_id,
          },
        });
      }

      // Index extracted text locally
      const file = await RAGService.uploadExtractedText(
        phpData.filename,
        phpData.type,
        phpData.size ?? size ?? 0,
        phpData.text,
        phpData.upload_session_id,
      );

      res.json({ success: true, file });
    } catch (error: any) {
      console.error('File upload error:', error);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Internal callback: index text already extracted upstream (large-file TUS path).
  // Authenticated via a shared secret, not by CORS/browser origin — this is called
  // server-to-server by python-api after it downloads and extracts a tusd upload.
  app.post('/api/internal/rag-index', async (req, res) => {
    const secret = req.header('X-Internal-Secret');
    if (!secret || secret !== process.env.RAG_CALLBACK_SECRET) {
      return res.status(401).json({ success: false, error: 'Unauthorized' });
    }
    try {
      const { name, type, size, text, uploadSessionId } = req.body;
      if (!name || !type || typeof text !== 'string' || !uploadSessionId) {
        return res.status(400).json({ success: false, error: 'Missing name, type, text, or uploadSessionId in payload.' });
      }

      const file = await RAGService.indexExtractedText(name, type, size || 0, text, uploadSessionId);
      res.status(202).json({ success: true, file });
    } catch (error: any) {
      console.error('RAG index callback error:', error);
      res.status(500).json({ success: false, error: error.message });
    }
  });
  // Delete file
  app.delete('/api/files/:id', async (req, res) => {
    try {
      const fileId = req.params.id;

      // Capture filename + session before deletion so we can forward to PHP
      const files = RAGService.getFiles();
      const file = files.find(f => f.id === fileId);
      const fileName = file?.name ?? req.body?.filename ?? null;
      const uploadSessionId = file?.uploadSessionId ?? null;

      const deleted = file ? await RAGService.deleteFile(fileId) : true;

      // Also delete from PHP service (MinIO-backed rag-uploads storage)
      if (fileName) {
        const phpUrl = process.env.PHP_SERVICE_URL || 'http://127.0.0.1:9003';
        try {
          await fetch(`${phpUrl}/api/rag/files`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              filename: fileName,
              upload_session_id: uploadSessionId ?? fileId,
            }),
          });
        } catch {
          console.error('[files] Failed to delete from PHP service');
        }
      }

      if (!deleted) {
        return res.status(404).json({ success: false, error: 'File not found.' });
      }

      res.json({ success: true, message: 'File deleted successfully.' });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Re-index a stored file via Python service (Qdrant storage)
  app.post('/api/reindex', async (req, res) => {
    try {
      const { filename, uploadSessionId, fileId } = req.body;
      if (!filename) {
        return res.status(400).json({ success: false, error: 'Missing filename.' });
      }

      // Remove old local entry to avoid duplicates
      if (fileId) {
        await RAGService.deleteFile(fileId);
      }

      const phpUrl = process.env.PHP_SERVICE_URL || 'http://127.0.0.1:9003';

      // Extract text from PHP/MinIO
      const phpRes = await fetch(`${phpUrl}/api/rag/extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename, upload_session_id: uploadSessionId || undefined }),
      });
      if (!phpRes.ok) {
        const err = await phpRes.json().catch(() => ({ error: 'Extraction failed' }));
        return res.status(phpRes.status).json({ success: false, error: err.error });
      }

      const phpData = await phpRes.json();

      // Send to Python for Qdrant indexing (chunks + embed + store)
      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:9002';
      const pyRes = await fetch(`${pythonUrl}/download-jobs`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Internal-Secret': process.env.RAG_CALLBACK_SECRET || '',
        },
        body: JSON.stringify({
          filename: (phpData.filename || filename).replace(/\.[^/.]+$/, '') + '.txt',
          text: phpData.text,
        }),
      });
      if (!pyRes.ok) {
        const err = await pyRes.json().catch(() => ({ error: 'Python indexing failed' }));
        return res.status(pyRes.status).json({ success: false, error: err.error });
      }

      const pyData = await pyRes.json();

      // Track locally (no local embedding — everything in Qdrant via Python)
      const file = await RAGService.addTrackedFile(
        phpData.filename || filename,
        phpData.type,
        0,
        pyData.chunk_count || 0,
        phpData.upload_session_id,
      );

      res.json({ success: true, file });
    } catch (error: any) {
      console.error('Reindex error:', error);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Query RAG system (Semantic retrieval + LLM prompt generation)
  app.post('/api/query', async (req, res) => {
    try {
      const { query, limit, filename } = req.body;
      if (!query || query.trim() === '') {
        return res.status(400).json({ success: false, error: 'Query parameter is required.' });
      }

      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:9002';
      let classification = { route: 'chat', confidence: 0 };

      // Classify if filename attached
      if (filename) {
        try {
          const cRes = await fetch(`${pythonUrl}/classify`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt: query }),
          });
          if (cRes.ok) classification = await cRes.json();
        } catch {
          // Classification unavailable — fall through
        }

        // Start orchestration — return job_id immediately for frontend polling
        try {
          const oRes = await fetch(`${pythonUrl}/orchestrate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ route: classification.route, filename, prompt: query }),
          });
          if (oRes.ok) {
            const { job_id } = await oRes.json();
            return res.json({
              success: true,
              phase: 'orchestrating',
              job_id,
              classification,
            });
          }
        } catch {
          // Orchestration unavailable — fall through to direct RAG
        }
      }

      // No attachment — simple RAG across all Qdrant collections
      let toolPrompt = query;
      try {
        const sRes = await fetch(`${pythonUrl}/search-all`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query, limit: 10 }),
        });
        if (sRes.ok) {
          const { results } = await sRes.json();
          if (results && results.length > 0) {
            const codeContext = results.map((r: any, i: number) =>
              `[${i + 1}] ${r.collection} | ${r.payload?.name || r.payload?.class || 'symbol'} (${(r.score * 100).toFixed(0)}%)\n${r.payload?.code || ''}`
            ).join('\n\n');
            toolPrompt = `${query}\n\nRetrieved code context from indexed projects:\n${codeContext}`;
          }
        }
      } catch {
        // Search-all unavailable — use raw query
      }

      logPrompt(query, toolPrompt);
      const response = await RAGService.query(toolPrompt, limit || 4);
      res.json({ success: true, ...response, classification });
    } catch (error: any) {
      console.error('Query execution error:', error);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Poll orchestration status + get final LLM answer
  app.post('/api/query/result', async (req, res) => {
    try {
      const { job_id, user_query } = req.body;
      if (!job_id) {
        return res.status(400).json({ success: false, error: 'Missing job_id.' });
      }

      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:9002';

      // Check orchestration status
      const sRes = await fetch(`${pythonUrl}/orchestrate/${job_id}/status`);
      if (!sRes.ok) {
        return res.json({ phase: 'orchestrating', job_id, status: 'pending' });
      }

      const status = await sRes.json();
      const isDone = status.status === 'done';

      // Return steps for frontend progress
      const result: any = {
        phase: isDone ? 'done' : 'orchestrating',
        job_id,
        status: status.status,
        steps: status.steps || [],
      };

      if (isDone) {
        // Dedupe: only generate answer once per job
        if (answerCache.has(job_id)) {
          const cached = answerCache.get(job_id);
          // Also return steps so frontend can show progress
          return res.json({ success: true, phase: 'done', job_id, steps: status.steps || [], ...cached });
        }

        // Build prompt: user query + tool output + search context
        const actualQuery = user_query || status.route || 'explain';
        const targetCollection = (status as any).collection_name || null;
        const toolOutput = (status as any).tool_output || null;
        let prompt = actualQuery;

        // Prepend MCP tool output if available
        if (toolOutput && typeof toolOutput === 'string' && toolOutput.trim()) {
          prompt = `Project structure:\n\n\`\`\`\n${toolOutput}\n\`\`\`\n\nUser question: ${actualQuery}`;
        }

        // Append search results from Qdrant (if collection exists)
        if (targetCollection) {
          try {
            const sRes = await fetch(`${pythonUrl}/search-all`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ query: actualQuery, limit: 10, collection: targetCollection }),
            });
            if (sRes.ok) {
              const { results } = await sRes.json();
              if (results && results.length > 0) {
                const codeContext = results.map((r: any, i: number) =>
                  `[${i + 1}] ${r.collection} | ${r.payload?.name || r.payload?.class || 'symbol'} (score: ${(r.score * 100).toFixed(0)}%)\n\`\`\`\n${r.payload?.code || ''}\n\`\`\``
                ).join('\n\n');
                prompt += `\n\nRelevant code from indexed projects:\n\n${codeContext}`;
              }
            }
          } catch {
            // Search failed — use raw query
          }
        }

        // Cache BEFORE LLM call to prevent concurrent duplicates
        answerCache.set(job_id, { answer: '', pending: true });

        logPrompt(actualQuery, prompt);
        const llmResponse = await RAGService.query(prompt, 4);
        result.answer = llmResponse.answer;
        result.matches = llmResponse.matches;
        result.llmUsed = llmResponse.llmUsed;
        result.durationMs = llmResponse.durationMs;

        // Update cache with real answer
        answerCache.set(job_id, {
          answer: result.answer,
          matches: result.matches,
          llmUsed: result.llmUsed,
          durationMs: result.durationMs,
          steps: result.steps,
          status: result.status,
        });
      }

      res.json({ success: true, ...result });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Retrieve matching chunks only (Semantic search only)
  app.post('/api/retrieve', async (req, res) => {
    try {
      const { query, limit } = req.body;
      if (!query || query.trim() === '') {
        return res.status(400).json({ success: false, error: 'Query parameter is required.' });
      }

      const matches = await RAGService.retrieve(query, limit || 4);
      res.json({ success: true, matches });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Mount Vite middleware for asset serving in Development
  if (process.env.NODE_ENV !== 'production') {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    // Serve production static assets from dist folder
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('*', (req, res) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
  }

  app.listen(PORT, '0.0.0.0', () => {
    console.log(`[RAG Server] Running on http://localhost:${PORT}`);
  });
}

startServer();
