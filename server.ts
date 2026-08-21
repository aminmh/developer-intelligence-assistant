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
const MASKED_SECRET_KEYS = ['qdrantApiKey'];
function maskConfig(config: Record<string, any>) {
  const masked = { ...config };
  for (const key of MASKED_SECRET_KEYS) {
    if (masked[key]) masked[key] = '';
  }
  return masked;
}

// Cache LLM answers for orchestration jobs to avoid re-generating on re-poll
const answerCache = new Map<string, any>();

// Session-scoped set of project filenames already indexed (so re-queries skip
// the expensive re-embedding step). Reset on server restart.
const indexedProjects = new Set<string>();

// Per-response evaluation data: queryId -> { query, answer, contexts }.
const evaluationCache = new Map<string, { query: string; answer: string | null; contexts: string[] }>();
// Orchestration job_id -> queryId, so /api/query/result can attach the answer.
const jobToQueryId = new Map<string, string>();

function contextsFromMatches(matches: any[]): string[] {
  return (matches ?? []).map((m) => m?.chunk?.text ?? '').filter((t) => t);
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

  // List models pulled in Ollama (for the model dropdowns)
  app.get('/api/ollama/models', async (req, res) => {
    try {
      const ollamaUrl = process.env.OLLAMA_URL || 'http://host.docker.internal:11434';
      const r = await fetch(`${ollamaUrl}/api/tags`);
      if (!r.ok) {
        return res.status(r.status).json({ success: false, error: 'Failed to list Ollama models.' });
      }
      const data = await r.json();
      const models = Array.from(
        new Set(
          (data.models ?? []).map((m: any) =>
            String(m.name ?? m.model ?? m).replace(/:latest$/, '')
          )
        )
      ).sort();
      res.json({ success: true, models });
    } catch (error: any) {
      console.error('Failed to list Ollama models:', error.message);
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
      const { query, filename } = req.body;
      if (!query || query.trim() === '') {
        return res.status(400).json({ success: false, error: 'Query parameter is required.' });
      }

      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:9002';
      let classification = { route: 'chat', confidence: 0 };
      const queryId = crypto.randomUUID();

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

        // Extract intent & entities for downstream filtering/prompt enrichment
        let entities: any = {};
        try {
          const eRes = await fetch(`${pythonUrl}/extract`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt: query }),
          });
          if (eRes.ok) entities = await eRes.json();
        } catch {
          // Extraction unavailable — proceed without entities
        }

        // Start orchestration — return job_id immediately for frontend polling
        try {
          const oRes = await fetch(`${pythonUrl}/orchestrate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              route: classification.route,
              filename,
              prompt: query,
              skip_index: indexedProjects.has(filename),
              entities,
            }),
          });
          if (oRes.ok) {
            const { job_id } = await oRes.json();
            jobToQueryId.set(job_id, queryId);
            evaluationCache.set(queryId, { query, answer: null, contexts: [] });
            return res.json({
              success: true,
              phase: 'orchestrating',
              job_id,
              query_id: queryId,
              classification,
            });
          }
        } catch {
          // Orchestration unavailable — fall through to direct RAG
        }
      }

      // No attachment — generate answer directly from Python (retrieval + LLM)
      try {
        const aRes = await fetch(`${pythonUrl}/answer`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-RAG-Trace-Id': queryId,
          },
          body: JSON.stringify({ query }),
        });
        if (aRes.ok) {
          const answer = await aRes.json();
          evaluationCache.set(queryId, {
            query,
            answer: answer.answer ?? '',
            contexts: contextsFromMatches(answer.matches),
          });
          return res.json({ success: true, ...answer, classification, query_id: queryId });
        }
      } catch {
        // Python answer unavailable — fall through to error
      }
      return res.status(502).json({ success: false, error: 'Answer generation failed.' });
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

      // Once a project's orchestration completes, remember it so subsequent
      // queries in this session skip re-indexing.
      if (isDone && status.filename) {
        indexedProjects.add(status.filename);
      }

      // Return steps for frontend progress
      const result: any = {
        phase: isDone ? 'done' : 'orchestrating',
        job_id,
        status: status.status,
        steps: status.steps || [],
        route_name: status.route_name || null,
        collection_name: status.collection_name || null,
      };

      if (isDone) {
        // Dedupe: only generate answer once per job
        if (answerCache.has(job_id)) {
          const cached = answerCache.get(job_id);
          // Also return steps so frontend can show progress
          return res.json({ success: true, phase: 'done', job_id, steps: status.steps || [], ...cached });
        }

        // Build answer in Python: retrieval (code) + LLM generation
        const actualQuery = user_query || status.route || 'explain';
        const targetCollection = (status as any).collection_name || null;
        const entities = (status as any).entities || {};
        const toolOutput = (status as any).tool_output || null;
        const extraContext = typeof toolOutput === 'string' && toolOutput.trim()
          ? toolOutput
          : undefined;

        // Cache BEFORE answer call to prevent concurrent duplicates
        answerCache.set(job_id, { answer: '', pending: true });

        const aRes = await fetch(`${pythonUrl}/answer`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-RAG-Trace-Id': jobToQueryId.get(job_id) || job_id,
          },
          body: JSON.stringify({
            query: actualQuery,
            collection: targetCollection || undefined,
            class_name: entities.class_name || undefined,
            file: entities.target_file || undefined,
            entities,
            extra_context: extraContext,
          }),
        });
        if (!aRes.ok) {
          throw new Error(`Answer generation failed (${aRes.status})`);
        }
        const answer = await aRes.json();
        result.answer = answer.answer;
        result.matches = answer.matches;
        result.llmUsed = answer.llmUsed;
        result.durationMs = answer.durationMs;

        // Attach the final answer to the evaluation cache.
        const queryId = jobToQueryId.get(job_id);
        if (queryId) {
          evaluationCache.set(queryId, {
            query: actualQuery,
            answer: answer.answer ?? '',
            contexts: contextsFromMatches(answer.matches),
          });
        }

        // Update cache with real answer
        answerCache.set(job_id, {
          answer: result.answer,
          matches: result.matches,
          llmUsed: result.llmUsed,
          durationMs: result.durationMs,
          steps: result.steps,
          status: result.status,
          route_name: result.route_name,
          collection_name: result.collection_name,
        });
      }

      res.json({ success: true, ...result });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Submit routing feedback (proxies to Python service)
  app.post('/api/feedback', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body),
      });
      const data = await pyRes.json();
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: data.detail || data.error || 'Feedback failed.' });
      }
      res.json(data);
    } catch (error: any) {
      console.error('Feedback submission error:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Retrieval provenance is fetched separately so the existing answer and
  // orchestration response contracts remain unchanged.
  app.get('/api/query/:queryId/insights', async (req, res) => {
    try {
      const traceId = req.params.queryId;
      const pyRes = await fetch(`${PYTHON_API_URL}/retrieval-insights/${encodeURIComponent(traceId)}`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      const data = await pyRes.json().catch(() => ({}));
      if (!pyRes.ok) {
        if (pyRes.status === 404) {
          return res.json({
            success: true,
            insights: {
              trace_id: traceId,
              query_hash: '',
              mode: 'vector_unavailable',
              collection: null,
              searched_collections: [],
              vector_result_count: 0,
              graph_neighbor_count: 0,
              graph_context_count: 0,
              graph_context_with_code_count: 0,
              graph_available: false,
              graph_fallback: false,
              sources: [],
              timestamp: new Date().toISOString(),
              available: false,
            },
          });
        }
        return res.status(pyRes.status).json({
          success: false,
          error: data.detail || data.error || 'Retrieval insights unavailable.',
        });
      }
      return res.json({ success: true, insights: data });
    } catch (error: any) {
      console.error('Retrieval insights error:', error.message);
      return res.status(500).json({ success: false, error: 'Retrieval insights unavailable.' });
    }
  });

  // Evaluate a single response (reference-free RAGAS metrics)
  app.post('/api/query/:queryId/evaluate', async (req, res) => {
    try {
      const entry = evaluationCache.get(req.params.queryId);
      if (!entry) {
        return res.status(404).json({ success: false, error: 'Response not found.' });
      }
      if (!entry.answer) {
        return res.status(409).json({ success: false, error: 'Response not ready yet.' });
      }

      const pyRes = await fetch(`${PYTHON_API_URL}/evaluate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: entry.query,
          answer: entry.answer,
          contexts: entry.contexts,
        }),
      });
      const data = await pyRes.json();
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: data.error || 'Evaluation failed.' });
      }
      res.json(data);
    } catch (error: any) {
      console.error('Evaluation error:', error.message);
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
