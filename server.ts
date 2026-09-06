import express from 'express';
import path from 'path';
import * as fs from 'fs';
import crypto from 'crypto';
import http from 'http';
import https from 'https';
import { createServer as createViteServer } from 'vite';
import dotenv from 'dotenv';
import { RAGService } from './src/server/rag';
import { UploadedFile } from './src/types';

dotenv.config();

const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://localhost:9002';
const INTERNAL_SECRET = process.env.RAG_CALLBACK_SECRET || '';
const SYS_MON_URL = process.env.SYS_MON_URL || 'http://host.docker.internal:8765';
const SYS_MON_LOCAL_URL = 'http://localhost:8765';

// Secret fields masked before returning config to the browser (write-only).
const MASKED_SECRET_KEYS = ['qdrantApiKey', 'llmRemoteApiKey'];
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

// ============================================================================
// ROUTE MAP (proxies + passthroughs to the python backend). New endpoints
// should slot under one of these families; each family proxies to PYTHON_API_URL
// with the same path unless noted.
//   /api/rag/*            upload + file list + reindex (TUS + python /api/rag/*)
//   /api/query/*          chat flow — orchestrates + streams, answers at result
//   /api/answer/*         direct python /answer (no orchestration)
//   /api/settings         GET/PUT config (masked secrets) + /api/health/*
//   /api/sandbox/*        status, command (python /sandbox/*), project analyze,
//                         analysis save/list/get/delete (python /project/analyze,
//                         /analysis). Static structure census + code-index
//                         preview jobs (python /structure/analyze, /code/index)
//                         also poll via /api/project/analyze/:jobId/status
//   /api/lmstudio/*       LM Studio model list/load (python /api/lmstudio/*)
//   /api/ollama/*         models + GGUF import (python /api/ollama/*)
//   /api/docretrieval/*   doc-lookup test/deps (python /api/docretrieval/*)
//   /api/collection/*     code collections list (python /api/rag/collections)
//   /api/feedback + /api/eval  python /api/* — served by RAGService handlers
//   /sys-monitor/*        SystemMonitor WebSocket/HTTP host service (SYS_MON_URL)
// ============================================================================
// Orchestration job_id -> use_remote flag, so the answer step uses the same LLM.
const jobToUseRemote = new Map<string, boolean>();
type OllamaImportJob = {
  id: string;
  modelName: string;
  file: string;
  status: 'pending' | 'running' | 'success' | 'failed';
  message: string;
  error?: string;
  updatedAt: string;
};
const ollamaImportJobs = new Map<string, OllamaImportJob>();

function contextsFromMatches(matches: any[]): string[] {
  return (matches ?? []).map((m) => m?.chunk?.text ?? '').filter((t) => t);
}

async function fetchWithTimeout(url: string, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function fetchSysStats(): Promise<any> {
  const urls = [SYS_MON_URL, SYS_MON_LOCAL_URL];
  for (const url of urls) {
    try {
      const response = await fetchWithTimeout(`${url}/stats`, 2500);
      if (!response.ok) continue;
      const stats = await response.json();
      return { online: true, ...stats };
    } catch {
      // Try the next endpoint (dev mode runs server.ts directly on the host).
    }
  }
  return { online: false };
}

function labelForExtension(filename: string): string {
  const ext = path.extname(filename).toLowerCase();
  if (ext === '.pdf') return 'pdf';
  if (ext === '.zip') return 'zip';
  if (ext === '.md') return 'markdown';
  return 'text';
}

const ollamaImportRoot = () => path.resolve(
  process.env.OLLAMA_IMPORT_ROOT || path.join(process.cwd(), 'ollama-imports')
);

const ollamaHostRoot = () => (
  process.env.OLLAMA_IMPORT_HOST_ROOT || ollamaImportRoot()
);

function isWithinRoot(candidate: string, root: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

function safeOllamaModelName(value: unknown): string | null {
  const name = String(value ?? '').trim();
  return /^[a-zA-Z0-9][a-zA-Z0-9._/-]{1,127}$/.test(name) ? name : null;
}

async function listGgufFiles(root: string): Promise<string[]> {
  const files: string[] = [];
  const visit = async (directory: string) => {
    const entries = await fs.promises.readdir(directory, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        await visit(fullPath);
      } else if (entry.isFile() && entry.name.toLowerCase().endsWith('.gguf')) {
        files.push(path.relative(root, fullPath).split(path.sep).join('/'));
      }
    }
  };
  await visit(root);
  return files.sort();
}

async function resolveGgufFile(relativeFile: unknown): Promise<{ localPath: string; file: string }> {
  const root = ollamaImportRoot();
  const file = String(relativeFile ?? '').trim().replaceAll('\\', '/');
  if (!file || path.isAbsolute(file) || file.includes('\0')) {
    throw new Error('Choose a relative GGUF file from the configured import directory.');
  }
  const localPath = path.resolve(root, file);
  if (!isWithinRoot(localPath, root)) {
    throw new Error('The selected file must remain inside the configured import directory.');
  }
  const realRoot = await fs.promises.realpath(root);
  const realPath = await fs.promises.realpath(localPath);
  if (!isWithinRoot(realPath, realRoot)) {
    throw new Error('The selected file must remain inside the configured import directory.');
  }
  const stat = await fs.promises.stat(realPath);
  if (!stat.isFile() || !realPath.toLowerCase().endsWith('.gguf')) {
    throw new Error('The selected path is not a GGUF file.');
  }
  const relative = path.relative(realRoot, realPath);
  return {
    localPath: realPath,
    file: relative.split(path.sep).join('/'),
  };
}

function updateOllamaImportJob(job: OllamaImportJob, update: Partial<OllamaImportJob>) {
  Object.assign(job, update, { updatedAt: new Date().toISOString() });
}

async function sha256File(filePath: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(filePath);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('error', reject);
    stream.on('end', () => resolve(`sha256:${hash.digest('hex')}`));
  });
}

async function uploadOllamaBlob(ollamaUrl: string, digest: string, filePath: string): Promise<void> {
  const target = new URL(`/api/blobs/${digest}`, ollamaUrl);
  const transport = target.protocol === 'https:' ? https : http;
  const stat = await fs.promises.stat(filePath);
  await new Promise<void>((resolve, reject) => {
    const request = transport.request(
      target,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'Content-Length': stat.size,
        },
      },
      (response) => {
        let body = '';
        response.setEncoding('utf8');
        response.on('data', (chunk) => { body += chunk; });
        response.on('end', () => {
          if ((response.statusCode || 500) >= 200 && (response.statusCode || 500) < 300) {
            resolve();
            return;
          }
          reject(new Error(
            `Ollama rejected the GGUF upload (${response.statusCode}): ${body || response.statusMessage || 'unknown error'}`
          ));
        });
      },
    );
    request.on('error', reject);
    const stream = fs.createReadStream(filePath);
    stream.on('error', reject);
    stream.pipe(request);
  });
}

async function runOllamaImport(
  job: OllamaImportJob,
  localPath: string,
  fileName: string,
  customModelfile: string,
) {
  const ollamaUrl = process.env.OLLAMA_URL || 'http://localhost:11434';
  try {
    updateOllamaImportJob(job, { status: 'running', message: 'Preparing model file…' });
    const digest = await sha256File(localPath);
    await uploadOllamaBlob(ollamaUrl, digest, localPath);

    updateOllamaImportJob(job, { message: 'Creating model in Ollama…' });
    const response = await fetch(`${ollamaUrl}/api/create`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: job.modelName,
        files: { [fileName]: digest },
        stream: true,
      }),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => response.statusText);
      throw new Error(`Ollama rejected the import (${response.status}): ${detail || response.statusText}`);
    }
    if (!response.body) {
      throw new Error('Ollama returned no import progress stream.');
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let sawSuccess = false;
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      if (chunk.done && buffer.trim()) {
        lines.push(buffer);
        buffer = '';
      }
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const event = JSON.parse(line);
          if (event.error) throw new Error(String(event.error));
          if (event.status) {
            const status = String(event.status);
            if (status.toLowerCase() === 'success') sawSuccess = true;
            updateOllamaImportJob(job, { message: status });
          }
        } catch (error: any) {
          if (error instanceof Error && error.message.startsWith('Unexpected')) continue;
          throw error;
        }
      }
      if (chunk.done) break;
    }
    if (!sawSuccess) {
      throw new Error('Ollama ended the import without reporting success.');
    }
    updateOllamaImportJob(job, { status: 'success', message: 'Model imported into Ollama.' });
  } catch (error: any) {
    if (error?.cause?.code === 'ECONNREFUSED' || error?.message === 'fetch failed') {
      error = new Error(
        `Cannot connect to Ollama at ${ollamaUrl}. Start Ollama and verify OLLAMA_URL before retrying.`
      );
    }
    if (error instanceof Error && /path or Modelfile are required|modelfile|files/i.test(error.message)) {
      try {
        const relativeFile = path.relative(ollamaImportRoot(), localPath);
        const hostPath = path.resolve(ollamaHostRoot(), relativeFile);
        const legacyModelfile = [`FROM ${hostPath}`, customModelfile].filter(Boolean).join('\n');
        updateOllamaImportJob(job, { message: 'Retrying with legacy Ollama Modelfile compatibility…' });
        const legacyResponse = await fetch(`${ollamaUrl}/api/create`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            model: job.modelName,
            modelfile: legacyModelfile,
            stream: true,
          }),
        });
        if (!legacyResponse.ok) {
          const detail = await legacyResponse.text().catch(() => legacyResponse.statusText);
          throw new Error(`Ollama rejected the legacy import (${legacyResponse.status}): ${detail || legacyResponse.statusText}`);
        }
        if (!legacyResponse.body) throw new Error('Ollama returned no legacy import progress stream.');
        const reader = legacyResponse.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let sawSuccess = false;
        while (true) {
          const chunk = await reader.read();
          buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          if (chunk.done && buffer.trim()) {
            lines.push(buffer);
            buffer = '';
          }
          for (const line of lines) {
            if (!line.trim()) continue;
            const event = JSON.parse(line);
            if (event.error) throw new Error(String(event.error));
            if (event.status) {
              const status = String(event.status);
              if (status.toLowerCase() === 'success') sawSuccess = true;
              updateOllamaImportJob(job, { message: status });
            }
          }
          if (chunk.done) break;
        }
        if (!sawSuccess) throw new Error('Ollama ended the legacy import without reporting success.');
        updateOllamaImportJob(job, { status: 'success', message: 'Model imported into Ollama.' });
        return;
      } catch (legacyError: any) {
        error = legacyError;
      }
    }
    updateOllamaImportJob(job, {
      status: 'failed',
      message: 'Model import failed.',
      error: error?.message || 'Unknown Ollama import error.',
    });
  }
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

  // List GGUF files from the operator-configured shared import directory.
  app.get('/api/ollama/import-directory', async (req, res) => {
    try {
      const root = ollamaImportRoot();
      const stat = await fs.promises.stat(root);
      if (!stat.isDirectory()) throw new Error('The configured Ollama import path is not a directory.');
      return res.json({
        success: true,
        root,
        files: await listGgufFiles(root),
      });
    } catch (error: any) {
      return res.status(400).json({
        success: false,
        error: error?.code === 'ENOENT'
          ? `Ollama import directory does not exist: ${ollamaImportRoot()}`
          : error?.message || 'Failed to scan the Ollama import directory.',
      });
    }
  });

  app.post('/api/ollama/import', async (req, res) => {
    try {
      const modelName = safeOllamaModelName(req.body?.modelName);
      if (!modelName) {
        return res.status(400).json({
          success: false,
          error: 'Model name must be 2–128 characters and use only letters, numbers, ., _, /, or -.',
        });
      }
      const selected = await resolveGgufFile(req.body?.file);
      const customModelfile = typeof req.body?.modelfile === 'string'
        ? req.body.modelfile.trim()
        : '';
      if (customModelfile.length > 12000) {
        return res.status(400).json({ success: false, error: 'Modelfile is too large.' });
      }
      if (/^\s*from\s+/im.test(customModelfile)) {
        return res.status(400).json({
          success: false,
          error: 'Custom Modelfile settings must not include a FROM line; the selected GGUF file is inserted automatically.',
        });
      }
      const id = crypto.randomUUID();
      const job: OllamaImportJob = {
        id,
        modelName,
        file: selected.file,
        status: 'pending',
        message: 'Import queued.',
        updatedAt: new Date().toISOString(),
      };
      ollamaImportJobs.set(id, job);
      void runOllamaImport(job, selected.localPath, path.basename(selected.file), customModelfile);
      return res.status(202).json({ success: true, jobId: id, job });
    } catch (error: any) {
      return res.status(400).json({ success: false, error: error?.message || 'Failed to start Ollama import.' });
    }
  });

  app.get('/api/ollama/import/:jobId', (req, res) => {
    const job = ollamaImportJobs.get(req.params.jobId);
    if (!job) return res.status(404).json({ success: false, error: 'Ollama import job not found.' });
    return res.json({ success: true, job });
  });

  // Host system stats (CPU/RAM/temperature/GPU) from the host-side sys-mon agent.
  app.get('/api/sysmon', async (req, res) => {
    try {
      const stats = await fetchSysStats();
      res.json({ success: true, ...stats });
    } catch (error: any) {
      console.error('Failed to fetch system stats:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Remote LLM provider health (proxies to Python service).
  app.get('/api/health/llm', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/health/llm`);
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Health check failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to check LLM health via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Sandbox runner status (proxies to Python service).
  app.get('/api/sandbox/status', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/sandbox/status`);
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Sandbox status check failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to check sandbox status via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Launch a sandbox analysis orchestration job (extract → framework profile
  // → composer install in DinD → runsc probes). Returns { job_id }.
  app.post('/api/project/analyze', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/project/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!pyRes.ok) {
        const body = await pyRes.json().catch(() => ({}));
        return res.status(pyRes.status).json({ success: false, error: body.detail || body.error || 'Sandbox analysis failed to start.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to start sandbox analysis via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Run an arbitrary shell command in the staged project sandbox.
  app.post('/api/sandbox/command', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/sandbox/command`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!pyRes.ok) {
        const body = await pyRes.json().catch(() => ({}));
        return res.status(pyRes.status).json({ success: false, error: body.detail || body.error || 'Sandbox command failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to run sandbox command via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Poll an orchestration job status (carries sandbox object when done).
  app.get('/api/project/analyze/:jobId/status', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/orchestrate/${req.params.jobId}/status`);
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Job status check failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to poll job status via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Launch a static structure-profile job (deterministic, no sandbox) — or the
  // full sandbox_analyze route when deep:true. Returns { job_id }.
  app.post('/api/structure/analyze', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/structure/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!pyRes.ok) {
        const body = await pyRes.json().catch(() => ({}));
        return res.status(pyRes.status).json({ success: false, error: body.detail || body.error || 'Structure analysis failed to start.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to start structure analysis via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Launch a parse-only code-index preview job. Returns { job_id }.
  app.post('/api/code/index', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/code/index`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!pyRes.ok) {
        const body = await pyRes.json().catch(() => ({}));
        return res.status(pyRes.status).json({ success: false, error: body.detail || body.error || 'Code index failed to start.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to start code index via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Save an analysis session from a completed job (persist to Qdrant + Neo4j).
  app.post('/api/analysis', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!pyRes.ok) {
        const body = await pyRes.json().catch(() => ({}));
        return res.status(pyRes.status).json({ success: false, error: body.detail || body.error || 'Save failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to save analysis via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // List saved analysis sessions.
  app.get('/api/analysis', async (req, res) => {
    try {
      const qs = typeof req.query?.limit === 'string' ? `?limit=${encodeURIComponent(req.query.limit)}` : '';
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis${qs}`);
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Analysis list failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to list analyses via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Fetch one saved analysis (includes grounding_md + profile).
  app.get('/api/analysis/:id', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis/${req.params.id}`);
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Analysis not found.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to fetch analysis via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Delete a saved analysis (row + artifact files).
  app.delete('/api/analysis/:id', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis/${req.params.id}`, { method: 'DELETE' });
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Analysis delete failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to delete analysis via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // LM Studio: list models available on the configured server.
  app.get('/api/lmstudio/models', async (req, res) => {
    try {
      const qs = typeof req.query?.base_url === 'string' && req.query.base_url
        ? `?base_url=${encodeURIComponent(req.query.base_url)}`
        : '';
      const pyRes = await fetch(`${PYTHON_API_URL}/lmstudio/models${qs}`);
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'LM Studio model list failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to list LM Studio models via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // LM Studio: load a model into the engine (MLX for mlx-format models).
  app.post('/api/lmstudio/load', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/lmstudio/load`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body || {}),
      });
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'LM Studio model load failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to load LM Studio model via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // External OpenAI-compatible provider: test connection (proxies to Python service).
  app.post('/api/health/test/remote', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/health/test/remote`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Remote connection test failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to test remote connection via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Documentation retrieval probe (Context7 → GitMCP pipeline test)
  app.post('/api/docretrieval/test', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/docretrieval/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Doc-retrieval probe failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to probe doc retrieval via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // RunPod serverless: test the API key against rest.runpod.io
  app.post('/api/health/test/runpod', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/health/test/runpod`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'RunPod connection test failed.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to test RunPod connection via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // RunPod serverless: list endpoints on the account
  app.get('/api/runpod/endpoints', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/api/runpod/endpoints`);
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: 'Failed to list RunPod endpoints.' });
      }
      res.json(await pyRes.json());
    } catch (error: any) {
      console.error('Failed to list RunPod endpoints via Python:', error.message);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Get uploaded files list (local tracking + Python/MinIO rag-uploads merged)
  app.get('/api/files', async (req, res) => {
    try {
      const files: UploadedFile[] = [...RAGService.getFiles()];
      const seen = new Set(files.map(f => f.uploadSessionId).filter(Boolean));

      // Fetch files from Python service (TUS uploads stored in MinIO/rag-uploads)
      try {
        const pyRes = await fetch(`${PYTHON_API_URL}/api/rag/files`);
        if (pyRes.ok) {
          const data = await pyRes.json() as { files?: any[] };
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
        console.error('[files] Python service unreachable, using local files only:', err.message);
      }

      res.json({ success: true, files });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Upload a file for indexing — forwards to Python service for MinIO storage.
  // Documents are not supported; only project ZIP archives are accepted.
  app.post('/api/upload', async (req, res) => {
    try {
      const { name, size, content } = req.body;
      if (!name || content === undefined) {
        return res.status(400).json({ success: false, error: 'Missing name or content in payload.' });
      }

      // Forward to Python service for storage
      const pyRes = await fetch(`${PYTHON_API_URL}/api/rag/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: name, content, size: size || 0 }),
      });

      const pyData = await pyRes.json().catch(() => ({}));
      if (!pyRes.ok) {
        return res.status(pyRes.status).json({ success: false, error: pyData.error || pyData.detail || 'Upload to Python service failed.' });
      }

      return res.json({
        success: true,
        file: {
          id: pyData.upload_session_id,
          name,
          type: 'zip',
          size: size || pyData.size || 0,
          chunkCount: 0,
          uploadedAt: new Date().toISOString(),
          status: 'ready',
          uploadSessionId: pyData.upload_session_id,
        },
      });
    } catch (error: any) {
      console.error('File upload error:', error);
      res.status(500).json({ success: false, error: error.message });
    }
  });

  // Proxy: mint a TUS resumable upload session (browser → web → Python)
  app.post('/api/rag/upload', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/api/rag/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body),
      });
      res.status(pyRes.status).json(await pyRes.json());
    } catch (error: any) {
      res.status(502).json({ success: false, error: error.message });
    }
  });

  // Proxy: parse PHP code from an uploaded ZIP (browser → web → Python)
  app.post('/api/rag/parse-code', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/api/rag/parse-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body),
      });
      res.status(pyRes.status).json(await pyRes.json());
    } catch (error: any) {
      res.status(502).json({ success: false, error: error.message });
    }
  });

  // Delete file
  app.delete('/api/files/:id', async (req, res) => {
    try {
      const fileId = req.params.id;

      // Capture filename + session before deletion so we can forward to Python
      const files = RAGService.getFiles();
      const file = files.find(f => f.id === fileId);
      const fileName = file?.name ?? req.body?.filename ?? null;
      const uploadSessionId = file?.uploadSessionId ?? null;

      const deleted = file ? await RAGService.deleteFile(fileId) : true;

      // Also delete from Python service (MinIO-backed rag-uploads storage)
      if (fileName) {
        try {
          await fetch(`${PYTHON_API_URL}/api/rag/files`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              filename: fileName,
              upload_session_id: uploadSessionId ?? fileId,
            }),
          });
        } catch {
          console.error('[files] Failed to delete from Python service');
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

  // Query RAG system (Semantic retrieval + LLM prompt generation)
  app.post('/api/query', async (req, res) => {
    try {
      const { query, filename, use_remote } = req.body;
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
            jobToUseRemote.set(job_id, !!use_remote);
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
          // Forward the client body verbatim (so preview/preview_mode ride
          // along) while pinning the computed query/route/use_remote fields.
          body: JSON.stringify({
            ...req.body,
            query,
            use_remote: !!use_remote,
            route: classification.route,
          }),
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

  // Stream an answer directly (SSE) — no orchestration job, no polling.
  // Used for chat/knowledge routes where the project is already indexed.
  app.post('/api/query/stream', async (req, res) => {
    try {
      const { query, filename, use_remote, route } = req.body;
      if (!query || query.trim() === '') {
        return res.status(400).json({ success: false, error: 'Query parameter is required.' });
      }

      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:9002';
      const queryId = crypto.randomUUID();

      // Route is provided by the client (chat/knowledge) or classified here.
      let routeName: string = route || 'chat';
      if (filename && (route === undefined || route === '')) {
        try {
          const cRes = await fetch(`${pythonUrl}/classify`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt: query }),
          });
          if (cRes.ok) {
            const classification = await cRes.json();
            routeName = classification.route || 'chat';
          }
        } catch {
          // Classification unavailable — default to chat
        }
      }

      const pyRes = await fetch(`${pythonUrl}/answer/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-RAG-Trace-Id': queryId,
        },
        // Forward the client body verbatim (so preview/preview_mode and any
        // other keys ride along) while pinning computed fields below.
        body: JSON.stringify({
          ...req.body,
          query,
          use_remote: !!use_remote,
          route: routeName,
        }),
      });

      if (!pyRes.ok) {
        const errBody = await pyRes.json().catch(() => ({}));
        return res.status(pyRes.status).json({
          success: false,
          error: (errBody as any).detail || 'Answer generation failed.',
        });
      }

      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
      });

      // Pipe the Python SSE stream straight through to the browser.
      const reader = (pyRes.body as any).getReader();
      const pump = async () => {
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            res.write(Buffer.from(value));
          }
        } catch {
          // Client disconnected or upstream died — close quietly.
        }
        res.end();
      };
      pump();

      req.on('close', () => {
        reader.cancel().catch(() => {});
      });
    } catch (error: any) {
      console.error('Stream query error:', error);
      if (!res.headersSent) {
        res.status(500).json({ success: false, error: error.message });
      } else {
        res.end();
      }
    }
  });

  // Poll orchestration status + get final LLM answer
  app.post('/api/query/result', async (req, res) => {
    try {
      const { job_id, user_query, use_remote } = req.body;
      if (!job_id) {
        return res.status(400).json({ success: false, error: 'Missing job_id.' });
      }
      const wantRemote = use_remote !== undefined ? !!use_remote : (jobToUseRemote.get(job_id) ?? false);

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
        // Gate preview support: the client may ask for the context window
        // WITHOUT running the model by sending `preview: true` (+ optional
        // `preview_mode`). Such preview-only responses are never cached so a
        // later confirmed poll generates the real answer through the normal path.
        const previewRequested = req.body?.preview === true;
        const previewModeParam = typeof req.body?.preview_mode === 'string'
          ? req.body.preview_mode
          : undefined;

        // Dedupe: only generate answer once per job (never for a preview request)
        if (!previewRequested && answerCache.has(job_id)) {
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
        if (!previewRequested) {
          answerCache.set(job_id, { answer: '', pending: true });
        }

        const aRes = await fetch(`${pythonUrl}/answer`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-RAG-Trace-Id': jobToQueryId.get(job_id) || job_id,
          },
          body: JSON.stringify({
            query: actualQuery,
            collection: targetCollection || undefined,
            filename: (status as any).filename || undefined,
            class_name: entities.class_name || undefined,
            file: entities.target_file || undefined,
            entities,
            extra_context: extraContext,
            doc_chunks: Array.isArray((status as any).doc_context?.chunks)
              ? (status as any).doc_context.chunks
              : undefined,
            project_profile: (status as any).project_profile ?? undefined,
            use_remote: wantRemote,
            route: status.route_name || 'explain',
            ...(previewRequested ? { preview: true } : {}),
            ...(previewModeParam ? { preview_mode: previewModeParam } : {}),
          }),
        });
        if (!aRes.ok) {
          // Un-poison the cache so the next poll retries the answer call
          // instead of looping on the pending placeholder forever.
          answerCache.delete(job_id);
          throw new Error(`Answer generation failed (${aRes.status})`);
        }
        const answer = await aRes.json();
        result.answer = answer.answer;
        result.matches = answer.matches;
        result.llmUsed = answer.llmUsed;
        result.durationMs = answer.durationMs;
        result.context_preview = answer.context_preview;
        result.preview_only = answer.preview_only;
        result.preview_mode = answer.preview_mode;

        if (previewRequested) {
          // Never cache a preview-only response; return it straight to the
          // client so it can render the gate confirmation.
          return res.json({ success: true, phase: 'done', job_id, steps: status.steps || [], ...result });
        }

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
          context_preview: result.context_preview,
          preview_only: result.preview_only,
          preview_mode: result.preview_mode,
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
