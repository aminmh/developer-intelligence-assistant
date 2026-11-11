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
import { openDocument, complete as completeDockerfile, format as formatDockerfile, closeSession as closeDockerfileSession, closeAllSessions, bridgeStatus } from './src/server/dockerfileLanguageBridge';
import { installOidcAuth, authRoleFor, authIsBootstrapAdmin, authCanRunRuntimeSetup } from './src/server/oidcAuth';
import { registerCodeQualityRoutes } from './src/server/codeQualityRoutes';
import { registerSchemaDraftRoutes } from './src/server/schemaDraftRoutes';

dotenv.config();

const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://localhost:9002';
const INTERNAL_SECRET = process.env.RAG_CALLBACK_SECRET || '';
const SYS_MON_URL = process.env.SYS_MON_URL || 'http://host.docker.internal:8765';
const SYS_MON_LOCAL_URL = 'http://localhost:8765';

// Secret fields masked before returning config to the browser (write-only).
const MASKED_SECRET_KEYS = ['qdrantApiKey', 'llmRemoteApiKey', 'cohereApiKey', 'cloudflareApiToken'];
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
const jobToOwner = new Map<string, string>();
const queryToOwner = new Map<string, string>();
const downloadToOwner = new Map<string, string>();
const embeddingJobToOwner = new Map<string, string>();

// ============================================================================
// ROUTE MAP (proxies + passthroughs to the python backend). New endpoints
// should slot under one of these families; each family proxies to PYTHON_API_URL
// with the same path unless noted.
//   /api/rag/*            upload + file list + reindex (TUS + python /api/rag/*)
//   /api/query/*          chat flow — orchestrates + streams, answers at result
//   /api/answer/*         direct python /answer (no orchestration)
//   /api/settings         GET/PUT config (masked secrets) + /api/health/*
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

const runtimeAnswerCache = new Map<string, { expires: number; data: any }>();
async function runtimeAnswerContext(projectId: string, commitSha: string): Promise<any | null> {
  const key = `${projectId}:${commitSha}`;
  const cached = runtimeAnswerCache.get(key);
  if (cached && cached.expires > Date.now()) return cached.data;
  try {
    const endpoint = `${PYTHON_API_URL}/telemetry/runtime/projects/${encodeURIComponent(projectId)}/snapshots/${encodeURIComponent(commitSha)}`;
    const response = await fetch(endpoint, { headers: { 'X-Internal-Secret': INTERNAL_SECRET }, signal: AbortSignal.timeout(8000) });
    if (!response.ok) return null;
    const data = await response.json();
    runtimeAnswerCache.set(key, { expires: Date.now() + 30_000, data });
    return data;
  } catch {
    return null;
  }
}

function runtimeQuestion(query: string): boolean {
  return /\b(used|usage|used by|calls?|called by|hot|dead code|unused|slow|latency|failure|fails|route|endpoint|table|database|break|impact|runtime|running)\b/i.test(query);
}

function indexedProjectKey(filename: string, projectId?: string, sourceRef?: string): string {
  return projectId && sourceRef
    ? `snapshot:${projectId}:${sourceRef}`
    : `legacy:${filename}`;
}

async function findStoredUpload(filename: string, uploadSessionId: string): Promise<any | null> {
  const response = await fetch(`${PYTHON_API_URL}/api/rag/files`, {
    headers: { 'X-Internal-Secret': INTERNAL_SECRET },
  });
  if (!response.ok) return null;
  const data = await response.json() as { files?: any[] };
  return (data.files ?? []).find((file) => file.filename === filename
    && file.upload_session_id === uploadSessionId) ?? null;
}

// Business rules ride along with chat only for a project the signed-in user can access.
function businessRulesAllowed(res: express.Response, projectId: unknown): boolean {
  const user = (res.locals as any).authUser;
  return typeof projectId === 'string' && !!projectId && Boolean(user && (authIsBootstrapAdmin(user) || authRoleFor(user, projectId)));
}

// Python trusts X-User-Id only with the internal secret; it makes the signed-in
// user the owner of any agent run that pauses for a tool approval.
function answerOwnerHeaders(res: express.Response): Record<string, string> {
  const user = (res.locals as any).authUser;
  return user?.sub ? { 'X-Internal-Secret': INTERNAL_SECRET, 'X-User-Id': String(user.sub) } : {};
}

async function authorizeAttachedUpload(
  res: express.Response,
  filename: unknown,
  uploadSessionId: unknown,
  projectId: unknown,
  commitSha: unknown,
  allowNonUploadContext: boolean,
): Promise<boolean> {
  if (typeof filename !== 'string' || !filename) return true;
  const user = (res.locals as any).authUser;
  // Filename-only requests are the retained legacy and saved-analysis paths.
  // New project-scoped uploads always carry their authoritative upload session.
  if (!uploadSessionId && !projectId && !commitSha) {
    if (allowNonUploadContext || authIsBootstrapAdmin(user)) return true;
    res.status(403).json({ success: false, error: 'Unattributed legacy uploads are restricted to the bootstrap administrator.' });
    return false;
  }
  if (typeof uploadSessionId !== 'string' || !uploadSessionId) {
    res.status(400).json({ success: false, error: 'An uploaded archive session is required.' });
    return false;
  }
  let file: any;
  try { file = await findStoredUpload(filename, uploadSessionId); }
  catch { file = null; }
  if (!file) {
    res.status(404).json({ success: false, error: 'Uploaded archive not found.' });
    return false;
  }
  if (file.project_id) {
    if (projectId !== file.project_id || (commitSha || null) !== (file.commit_sha || null)) {
      res.status(403).json({ success: false, error: 'Upload project identity does not match the archive record.' });
      return false;
    }
    if (!authRoleFor(user, file.project_id)) {
      res.status(403).json({ success: false, error: 'Project access denied.' });
      return false;
    }
    res.locals.uploadSourceRef = file.source_ref || file.commit_sha;
  } else if (projectId || commitSha) {
    res.status(409).json({ success: false, error: 'Legacy uploads cannot be assigned an inferred project identity.' });
    return false;
  } else if (!authIsBootstrapAdmin(user)) {
    res.status(403).json({ success: false, error: 'Unattributed legacy uploads are restricted to the bootstrap administrator.' });
    return false;
  }
  return true;
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
  app.use(express.json({ limit: '100mb', verify: (req, _res, buf) => {
    if ((req as any).path === '/api/code-quality/github/webhook') (req as any).rawBody = Buffer.from(buf);
  } }));
  app.use(express.urlencoded({ limit: '100mb', extended: true }));

  // Dockerfile language intelligence is isolated from runtime management.
  app.get('/api/dockerfile/language/status', (_req, res) => res.json({ success: true, ...bridgeStatus() }));
  app.post('/api/dockerfile/language/open', async (req, res) => {
    try { res.json({ success: true, ...(await openDocument(req.body ?? {})) }); }
    catch (error: any) { res.status(400).json({ success: false, error: error?.message || 'Dockerfile language server unavailable.' }); }
  });
  app.post('/api/dockerfile/language/complete', async (req, res) => {
    try { res.json({ success: true, items: await completeDockerfile(req.body ?? {}) }); }
    catch (error: any) { res.status(400).json({ success: false, error: error?.message || 'Completion unavailable.' }); }
  });
  app.post('/api/dockerfile/language/format', async (req, res) => {
    try { res.json({ success: true, edits: await formatDockerfile(req.body ?? {}) }); }
    catch (error: any) { res.status(400).json({ success: false, error: error?.message || 'Formatting unavailable.' }); }
  });
  app.delete('/api/dockerfile/language/:sessionId', (req, res) => res.json({ success: closeDockerfileSession(req.params.sessionId) }));

  // CORS - allow cross-origin requests from any frontend origin
  app.use((req, res, next) => {
    res.setHeader('Access-Control-Allow-Origin', process.env.WEB_ORIGIN || 'http://localhost:3000');
    res.setHeader('Access-Control-Allow-Credentials', 'true');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, PUT, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    if (req.method === 'OPTIONS') {
      return res.sendStatus(204);
    }
    next();
  });

  installOidcAuth(app);
  registerSchemaDraftRoutes(app, {
    apiUrl: PYTHON_API_URL,
    internalSecret: INTERNAL_SECRET,
    webOrigin: process.env.WEB_ORIGIN || 'http://localhost:3000',
    getUser: (res) => (res.locals as any).authUser,
    isGlobalAdmin: (user) => Boolean(user && authIsBootstrapAdmin(user)),
  });

  app.post('/api/runtime/setup-authorization', (req, res) => {
    const user = (res.locals as any).authUser;
    const origin = req.get('origin') || '';
    const allowedOrigin = process.env.WEB_ORIGIN || 'http://localhost:3000';
    const sharedSecret = process.env.RAG_CALLBACK_SECRET || '';
    if (origin !== allowedOrigin) return res.status(403).json({ detail: 'Runtime setup requests must come from the configured application origin.' });
    if (!authCanRunRuntimeSetup(user)) return res.status(403).json({ detail: 'Operator or administrator access is required to set up Runtime.' });
    if (sharedSecret.length < 32) return res.status(503).json({ detail: 'Runtime setup authorization is not configured.' });
    const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
    const header = encode({ alg: 'HS256', typ: 'JWT' });
    const claims = encode({ aud: 'runtime-helper', origin, exp: Math.floor(Date.now() / 1000) + 300, jti: crypto.randomUUID() });
    const signingKey = crypto.createHmac('sha256', sharedSecret).update('devassistant-runtime-helper-v1').digest();
    const signature = crypto.createHmac('sha256', signingKey).update(`${header}.${claims}`).digest('base64url');
    return res.json({ token: `${header}.${claims}.${signature}`, expires_in: 300 });
  });

  app.post('/api/runtime/local-container-removal-authorization', (req, res) => {
    const user = (res.locals as any).authUser;
    const origin = req.get('origin') || '';
    const allowedOrigin = process.env.WEB_ORIGIN || 'http://localhost:3000';
    const sharedSecret = process.env.RAG_CALLBACK_SECRET || '';
    const projectId = req.body?.project_id;
    if (origin !== allowedOrigin) return res.status(403).json({ detail: 'Runtime removal requests must come from the configured application origin.' });
    if (typeof projectId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(projectId)) {
      return res.status(400).json({ detail: 'A valid Runtime project ID is required.' });
    }
    if (!user || (!authIsBootstrapAdmin(user) && authRoleFor(user, projectId) !== 'admin')) {
      return res.status(403).json({ detail: 'Project administrator access is required to remove local Runtime containers.' });
    }
    if (sharedSecret.length < 32) return res.status(503).json({ detail: 'Runtime removal authorization is not configured.' });
    const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
    const header = encode({ alg: 'HS256', typ: 'JWT' });
    const claims = encode({
      aud: 'runtime-helper',
      origin,
      operation: 'runtime-local-container-removal',
      project_id: projectId,
      exp: Math.floor(Date.now() / 1000) + 300,
      jti: crypto.randomUUID(),
    });
    const signingKey = crypto.createHmac('sha256', sharedSecret).update('devassistant-runtime-helper-v1').digest();
    const signature = crypto.createHmac('sha256', signingKey).update(`${header}.${claims}`).digest('base64url');
    return res.json({ token: `${header}.${claims}.${signature}`, expires_in: 300 });
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

  // LLM tool catalog (Python service owns persistence and execution).
  app.get('/api/tools', async (req, res) => {
    try {
      const qs = req.query.include_deleted ? '?include_deleted=true' : '';
      const pyRes = await fetch(`${PYTHON_API_URL}/tools${qs}`, { headers: { 'X-Internal-Secret': INTERNAL_SECRET } });
      const data = await pyRes.json().catch(() => ({ success: false, error: 'Failed to load tools.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) { return res.status(500).json({ success: false, error: error.message }); }
  });
  const proxyToolMutation = (method: string, suffix: string) => async (req: any, res: any) => {
    try {
      // POST /api/tools (create) has no id; every other mutation targets /tools/{id}.
      const target = req.params.id === undefined ? '/tools' : `/tools/${encodeURIComponent(req.params.id)}${suffix}`;
      const pyRes = await fetch(`${PYTHON_API_URL}${target}`, { method, headers: { 'Content-Type': 'application/json', 'X-Internal-Secret': INTERNAL_SECRET }, body: method === 'GET' ? undefined : JSON.stringify(req.body || {}) });
      const data = await pyRes.json().catch(() => ({ success: false, error: 'Tool request failed.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) { return res.status(500).json({ success: false, error: error.message }); }
  };
  app.post('/api/tools', proxyToolMutation('POST', ''));
  app.put('/api/tools/:id', proxyToolMutation('PUT', ''));
  app.post('/api/tools/:id/test', proxyToolMutation('POST', '/test'));
  app.post('/api/tools/:id/enable', proxyToolMutation('POST', '/enable'));
  app.post('/api/tools/:id/disable', proxyToolMutation('POST', '/disable'));
  app.delete('/api/tools/:id', proxyToolMutation('DELETE', ''));

  app.get('/api/logs', async (req, res) => {
    try {
      const query = new URLSearchParams();
      for (const key of ['search', 'types', 'severity', 'start', 'end', 'offset', 'limit']) {
        const value = req.query[key];
        if (typeof value === 'string' && value) query.set(key, value);
      }
      const pyRes = await fetch(`${PYTHON_API_URL}/logs?${query.toString()}`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to load logs.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Failed to load logs.' });
    }
  });

  app.delete('/api/logs', async (_req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/logs`, {
        method: 'DELETE',
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to clear logs.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Failed to clear logs.' });
    }
  });

  app.delete('/api/logs/:type', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/logs/${encodeURIComponent(req.params.type)}`, {
        method: 'DELETE',
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to clear log.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Failed to clear log.' });
    }
  });

  app.get('/api/logs/config', async (_req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/logs/config`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to load log configuration.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Failed to load log configuration.' });
    }
  });

  app.put('/api/logs/config', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/logs/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-Internal-Secret': INTERNAL_SECRET },
        body: JSON.stringify(req.body),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to save log configuration.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Failed to save log configuration.' });
    }
  });

  registerCodeQualityRoutes(app, {
    apiUrl: PYTHON_API_URL,
    internalSecret: INTERNAL_SECRET,
    helperUrl: process.env.RUNTIME_HELPER_URL || 'http://127.0.0.1:47831',
    webOrigin: process.env.WEB_ORIGIN || 'http://localhost:3000',
    getUser: (res) => (res.locals as any).authUser,
    canAccessProject: (user, projectId) => Boolean(user && (authIsBootstrapAdmin(user) || authRoleFor(user, projectId))),
    canManageProject: (user, projectId) => Boolean(user && (authIsBootstrapAdmin(user) || authRoleFor(user, projectId) === 'admin')),
    canOperateProject: (user, projectId) => Boolean(user && (authIsBootstrapAdmin(user) || ['operator', 'admin'].includes(authRoleFor(user, projectId) || ''))),
    isGlobalAdmin: (user) => Boolean(user && authIsBootstrapAdmin(user)),
  });

  app.get('/api/runtime/projects', async (_req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/projects`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      const data = await pyRes.json().catch(() => []);
      if (!pyRes.ok || !Array.isArray(data)) {
        return res.status(pyRes.status || 502).json({ detail: 'Runtime project inventory is unavailable.' });
      }
      const user = (res.locals as any).authUser;
      const projects = data.filter((project: any) => typeof project.project_id === 'string'
        && (authIsBootstrapAdmin(user) || Boolean(authRoleFor(user, project.project_id))))
        .map((project: any) => ({
          ...project,
          can_remove: authIsBootstrapAdmin(user) || authRoleFor(user, project.project_id) === 'admin',
          can_delete_telemetry: authIsBootstrapAdmin(user) || authRoleFor(user, project.project_id) === 'admin',
        }));
      return res.json({ projects });
    } catch {
      return res.status(502).json({ detail: 'Runtime project inventory is unavailable.' });
    }
  });

  app.get('/api/runtime/projects/:projectId/snapshots/:commitSha', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && !authRoleFor(user, req.params.projectId)) {
        return res.status(403).json({ detail: 'Project access denied.' });
      }
      const url = new URL(`${PYTHON_API_URL}/telemetry/runtime/projects/${encodeURIComponent(req.params.projectId)}/snapshots/${encodeURIComponent(req.params.commitSha)}`);
      const pyRes = await fetch(url, { headers: { 'X-Internal-Secret': INTERNAL_SECRET } });
      const data = await pyRes.json().catch(() => ({ detail: 'Runtime snapshot is unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Runtime snapshot is unavailable.' });
    }
  });

  app.get('/api/runtime/projects/:projectId/revisions/:revisionId', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && !authRoleFor(user, req.params.projectId)) {
        return res.status(403).json({ detail: 'Project access denied.' });
      }
      const url = new URL(`${PYTHON_API_URL}/telemetry/runtime/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}`);
      const pyRes = await fetch(url, { headers: { 'X-Internal-Secret': INTERNAL_SECRET } });
      const data = await pyRes.json().catch(() => ({ detail: 'Runtime revision is unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Runtime revision is unavailable.' });
    }
  });

  app.get('/api/runtime/projects/:projectId/revisions/:revisionId/requests', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && !authRoleFor(user, req.params.projectId)) {
        return res.status(403).json({ detail: 'Project access denied.' });
      }
      const outcome = typeof req.query.outcome === 'string' ? req.query.outcome : 'all';
      if (!['all', 'errors', 'slow'].includes(outcome)) {
        return res.status(422).json({ detail: 'Request outcome filter must be all, errors, or slow.' });
      }
      const path = `/telemetry/runtime/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/requests?outcome=${outcome}`;
      const pyRes = await fetch(`${PYTHON_API_URL}${path}`, { headers: { 'X-Internal-Secret': INTERNAL_SECRET } });
      const data = await pyRes.json().catch(() => ({ detail: 'Runtime requests are unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Runtime requests are unavailable.' });
    }
  });

  app.delete('/api/runtime/projects/:projectId/revisions/:revisionId/requests', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && authRoleFor(user, req.params.projectId) !== 'admin') {
        return res.status(403).json({ detail: 'Project administrator access is required to delete Runtime telemetry.' });
      }
      const traceIds = req.body?.trace_ids;
      if (!Array.isArray(traceIds) || traceIds.length < 1 || traceIds.length > 50
        || traceIds.some((traceId: unknown) => typeof traceId !== 'string' || !/^[0-9a-f]{32}$/i.test(traceId))
        || new Set(traceIds.map((traceId: string) => traceId.toLowerCase())).size !== traceIds.length) {
        return res.status(400).json({ detail: 'Select between 1 and 50 unique request traces.' });
      }
      const path = `/telemetry/runtime/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/requests`;
      const pyRes = await fetch(`${PYTHON_API_URL}${path}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json', 'X-Internal-Secret': INTERNAL_SECRET },
        body: JSON.stringify({ trace_ids: traceIds.map((traceId: string) => traceId.toLowerCase()) }),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Request telemetry could not be deleted.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Request telemetry could not be deleted.' });
    }
  });

  app.get('/api/runtime/projects/:projectId/revisions/:revisionId/requests/:traceId', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && !authRoleFor(user, req.params.projectId)) {
        return res.status(403).json({ detail: 'Project access denied.' });
      }
      const path = `/telemetry/runtime/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/requests/${encodeURIComponent(req.params.traceId)}`;
      const pyRes = await fetch(`${PYTHON_API_URL}${path}`, { headers: { 'X-Internal-Secret': INTERNAL_SECRET } });
      const data = await pyRes.json().catch(() => ({ detail: 'Request details are unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Request details are unavailable.' });
    }
  });

  app.post('/api/runtime/projects/:projectId/revisions/:revisionId/requests/:traceId/analysis', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && !authRoleFor(user, req.params.projectId)) {
        return res.status(403).json({ detail: 'Project access denied.' });
      }
      const path = `/telemetry/runtime/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/requests/${encodeURIComponent(req.params.traceId)}/analysis`;
      const pyRes = await fetch(`${PYTHON_API_URL}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Internal-Secret': INTERNAL_SECRET },
        body: JSON.stringify({ provider: req.body?.provider }),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Request analysis is unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Request analysis is unavailable. Retry the request.' });
    }
  });

  app.post('/api/runtime/projects/:projectId/revisions/:revisionId/requests/:traceId/analysis/diff', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && !authRoleFor(user, req.params.projectId)) {
        return res.status(403).json({ detail: 'Project access denied.' });
      }
      const path = `/telemetry/runtime/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/requests/${encodeURIComponent(req.params.traceId)}/analysis/diff`;
      const pyRes = await fetch(`${PYTHON_API_URL}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Internal-Secret': INTERNAL_SECRET },
        body: JSON.stringify({ finding: req.body?.finding, analysis: req.body?.analysis }),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Diff preview is unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Diff preview is unavailable. Retry the request.' });
    }
  });

  app.get('/api/runtime/settings', async (_req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/settings`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Runtime settings are unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Runtime settings are unavailable.' });
    }
  });

  app.put('/api/runtime/settings', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/settings`, {
        method: 'PUT',
        headers: { 'X-Internal-Secret': INTERNAL_SECRET, 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Runtime settings could not be saved.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Runtime settings could not be saved.' });
    }
  });

  app.get('/api/runtime/settings/storage', async (_req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/settings/storage`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Runtime storage settings are unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Runtime storage settings are unavailable.' });
    }
  });

  app.get('/api/runtime/host-metrics', async (_req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/host-metrics`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET },
        signal: AbortSignal.timeout(40_000),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Remote host metrics are unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Remote host metrics are unavailable.' });
    }
  });

  app.put('/api/runtime/settings/storage/mode', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/settings/storage/mode`, {
        method: 'PUT',
        headers: { 'X-Internal-Secret': INTERNAL_SECRET, 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: req.body?.mode }),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Runtime storage mode could not be saved.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Runtime storage mode could not be saved.' });
    }
  });

  app.post('/api/runtime/settings/storage/host-key', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/settings/storage/host-key`, {
        method: 'POST',
        headers: { 'X-Internal-Secret': INTERNAL_SECRET, 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'SSH host fingerprint could not be retrieved.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'SSH host fingerprint could not be retrieved.' });
    }
  });

  app.post('/api/runtime/settings/storage/connect', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/settings/storage/connect`, {
        method: 'POST',
        headers: { 'X-Internal-Secret': INTERNAL_SECRET, 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
        signal: AbortSignal.timeout(30_000),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Remote ClickHouse could not be verified.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Remote ClickHouse could not be verified or the request timed out.' });
    }
  });

  app.post('/api/runtime/settings/storage/test', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/settings/storage/test`, {
        method: 'POST',
        headers: { 'X-Internal-Secret': INTERNAL_SECRET, 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'SSH and sudo checks failed.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'SSH and sudo checks failed.' });
    }
  });

  app.post('/api/runtime/settings/storage/install', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/telemetry/runtime/settings/storage/install`, {
        method: 'POST',
        headers: { 'X-Internal-Secret': INTERNAL_SECRET, 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
        signal: AbortSignal.timeout(20 * 60 * 1000),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Remote ClickHouse setup failed.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'Remote ClickHouse setup failed or timed out.' });
    }
  });

  app.get('/api/runtime/events', async (_req, res) => {
    const controller = new AbortController();
    res.on('close', () => controller.abort());
    try {
      const upstream = await fetch(`${PYTHON_API_URL}/telemetry/runtime/events`, {
        headers: { 'X-Internal-Secret': INTERNAL_SECRET, Accept: 'text/event-stream' },
        signal: controller.signal,
      });
      if (!upstream.ok || !upstream.body) {
        return res.status(upstream.status || 502).json({ detail: 'Runtime update stream is unavailable.' });
      }
      res.status(200);
      res.set({
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache, no-transform',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'no',
      });
      res.flushHeaders();
      const reader = upstream.body.getReader();
      try {
        while (!controller.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;
          if (!res.write(Buffer.from(value))) await new Promise<void>((resolve) => res.once('drain', resolve));
        }
      } finally {
        await reader.cancel().catch(() => undefined);
        if (!res.writableEnded) res.end();
      }
    } catch (error: any) {
      if (!controller.signal.aborted && !res.headersSent) {
        res.status(502).json({ detail: error?.message || 'Runtime update stream is unavailable.' });
      } else if (!res.writableEnded) {
        res.end();
      }
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

  // Launch a static structure-profile job. Returns { job_id }.
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

  // Regenerate a deterministic schema graph from a saved Laravel analysis.
  app.get('/api/analysis/:id/schema-visualization', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis/${encodeURIComponent(req.params.id)}/schema-visualization`);
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to build schema visualization.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      console.error('Failed to fetch schema visualization via Python:', error.message);
      return res.status(500).json({ detail: error?.message || 'Failed to build schema visualization.' });
    }
  });

  // Read-only domain knowledge browsing for a saved analysis.
  app.get('/api/analysis/:id/domain-concepts', async (req, res) => {
    try {
      const params = new URLSearchParams();
      for (const key of ['search', 'concept_type', 'type', 'offset', 'limit']) {
        const value = req.query?.[key];
        if (typeof value === 'string' && value) params.set(key, value);
      }
      const suffix = params.toString() ? `?${params.toString()}` : '';
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis/${encodeURIComponent(req.params.id)}/domain-concepts${suffix}`);
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to load domain concepts.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      console.error('Failed to fetch domain concepts via Python:', error.message);
      return res.status(500).json({ detail: error?.message || 'Failed to load domain concepts.' });
    }
  });

  app.get('/api/analysis/:id/domain-context', async (req, res) => {
    try {
      const params = new URLSearchParams();
      for (const key of ['query', 'concept_id']) {
        const value = req.query?.[key];
        if (typeof value === 'string' && value) params.set(key, value);
      }
      const suffix = params.toString() ? `?${params.toString()}` : '';
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis/${encodeURIComponent(req.params.id)}/domain-context${suffix}`);
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to load domain context.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      console.error('Failed to fetch domain context via Python:', error.message);
      return res.status(500).json({ detail: error?.message || 'Failed to load domain context.' });
    }
  });

  // Deterministic entity model and explicit, session-scoped LLM enrichment.
  app.get('/api/analysis/:id/domain-model', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis/${encodeURIComponent(req.params.id)}/domain-model`);
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to load domain model.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      console.error('Failed to fetch domain model via Python:', error.message);
      return res.status(500).json({ detail: error?.message || 'Failed to load domain model.' });
    }
  });

  app.get('/api/analysis/:id/domain-model/usages', async (req, res) => {
    try {
      const params = new URLSearchParams();
      for (const key of ['entity', 'behavior', 'offset', 'limit']) {
        const value = req.query?.[key];
        if (typeof value === 'string' && value) params.set(key, value);
      }
      const suffix = params.toString() ? `?${params.toString()}` : '';
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis/${encodeURIComponent(req.params.id)}/domain-model/usages${suffix}`);
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to load behavior usages.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      console.error('Failed to fetch domain behavior usages via Python:', error.message);
      return res.status(500).json({ detail: error?.message || 'Failed to load behavior usages.' });
    }
  });

  app.post('/api/analysis/:id/domain-model/enrich', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/analysis/${encodeURIComponent(req.params.id)}/domain-model/enrich`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body ?? {}),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to enrich domain model.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      console.error('Failed to enrich domain model via Python:', error.message);
      return res.status(500).json({ detail: error?.message || 'Failed to enrich domain model.' });
    }
  });

  app.get('/api/domain/dictionary', async (req, res) => {
    try {
      const params = new URLSearchParams();
      for (const key of ['search', 'offset', 'limit']) {
        const value = req.query?.[key];
        if (typeof value === 'string' && value) params.set(key, value);
      }
      const suffix = params.toString() ? `?${params.toString()}` : '';
      const pyRes = await fetch(`${PYTHON_API_URL}/domain/dictionary${suffix}`);
      const data = await pyRes.json().catch(() => ({ detail: 'Failed to load domain dictionary.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      console.error('Failed to fetch domain dictionary via Python:', error.message);
      return res.status(500).json({ detail: error?.message || 'Failed to load domain dictionary.' });
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

  // Database evaluator lifecycle (native slow-log/EXPLAIN MVP).
  app.post('/api/evaluator/runs', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/evaluator/runs`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(req.body ?? {}),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Evaluator start failed.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ success: false, error: error?.message || 'Evaluator start failed.' });
    }
  });

  app.get('/api/evaluator/runs/:runId', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/evaluator/runs/${encodeURIComponent(req.params.runId)}`);
      const data = await pyRes.json().catch(() => ({ detail: 'Evaluator status failed.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Evaluator status failed.' });
    }
  });

  app.get('/api/evaluator/runs/:runId/reports', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/evaluator/runs/${encodeURIComponent(req.params.runId)}/reports`);
      const data = await pyRes.json().catch(() => ({ detail: 'Evaluator reports failed.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Evaluator reports failed.' });
    }
  });

  app.get('/api/evaluator/runs/:runId/context', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/evaluator/runs/${encodeURIComponent(req.params.runId)}/context`);
      const data = await pyRes.json().catch(() => ({ detail: 'Evaluator context failed.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Evaluator context failed.' });
    }
  });

  app.post('/api/evaluator/runs/:runId/recommendation', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/evaluator/runs/${encodeURIComponent(req.params.runId)}/recommendation`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(req.body ?? {}),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'Evaluator recommendation failed.' }));
      return res.status(pyRes.status).json(data);
    } catch (error: any) {
      return res.status(500).json({ detail: error?.message || 'Evaluator recommendation failed.' });
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

  // Embedding provider: test the same query-embedding path used by retrieval.
  app.post('/api/health/test/embedding', async (req, res) => {
    try {
      const pyRes = await fetch(`${PYTHON_API_URL}/health/test/embedding`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      const body = await pyRes.text();
      let payload: any;
      try {
        payload = body ? JSON.parse(body) : { ok: false, error: 'Embedding test returned an empty response.' };
      } catch {
        payload = { ok: false, error: 'Embedding test returned an invalid response.' };
      }
      return res.status(pyRes.status).json(payload);
    } catch (error: any) {
      console.error('Failed to test embedding provider via Python:', error.message);
      return res.status(500).json({ ok: false, error: error.message || 'Embedding test failed.' });
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
              projectId: f.project_id ?? undefined,
              commitSha: f.commit_sha ?? undefined,
              revisionId: f.revision_id ?? undefined,
              sourceRef: f.source_ref ?? undefined,
              archiveSha256: f.archive_sha256 ?? undefined,
            });
          }
        }
      } catch (err: any) {
        console.error('[files] Python service unreachable, using local files only:', err.message);
      }

      const user = (res.locals as any).authUser;
      const visibleFiles = files.filter((file) => file.projectId
        ? Boolean(authRoleFor(user, file.projectId))
        : authIsBootstrapAdmin(user));
      res.json({ success: true, files: visibleFiles });
    } catch (error: any) {
      res.status(500).json({ success: false, error: error.message });
    }
  });

  app.get('/api/download-jobs/:downloadId/status', async (req, res) => {
    try {
      if (downloadToOwner.get(req.params.downloadId) !== (res.locals as any).authUser.sub) {
        return res.status(403).json({ error: 'Indexing job access denied.' });
      }
      const response = await fetch(`${PYTHON_API_URL}/download-jobs/${encodeURIComponent(req.params.downloadId)}/status`);
      res.status(response.status).json(await response.json());
    } catch (error: any) {
      res.status(502).json({ error: error?.message || 'Python service is unavailable.' });
    }
  });

  app.post('/api/embeddings/rebuild', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      const projectId = req.body?.project_id;
      const role = projectId ? authRoleFor(user, projectId) : null;
      if (!role || role === 'viewer') {
        return res.status(403).json({ detail: 'Project access denied.' });
      }
      const response = await fetch(`${PYTHON_API_URL}/api/embeddings/rebuild`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body),
      });
      const data = await response.json().catch(() => ({})) as any;
      if (response.ok && data.job_id) {
        embeddingJobToOwner.set(data.job_id, user.sub);
      }
      res.status(response.status).json(data);
    } catch (error: any) {
      res.status(502).json({ detail: error?.message || 'Python service is unavailable.' });
    }
  });

  app.get('/api/embeddings/rebuild/:jobId', async (req, res) => {
    if (embeddingJobToOwner.get(req.params.jobId) !== (res.locals as any).authUser.sub) {
      return res.status(403).json({ detail: 'Embedding job access denied.' });
    }
    try {
      const response = await fetch(`${PYTHON_API_URL}/api/embeddings/rebuild/${encodeURIComponent(req.params.jobId)}`);
      res.status(response.status).json(await response.json());
    } catch (error: any) {
      res.status(502).json({ detail: error?.message || 'Python service is unavailable.' });
    }
  });

  app.post('/api/embeddings/rebuild-route-feedback', async (req, res) => {
    const user = (res.locals as any).authUser;
    if (!authIsBootstrapAdmin(user)) {
      return res.status(403).json({ detail: 'Administrator access is required.' });
    }
    try {
      const response = await fetch(`${PYTHON_API_URL}/api/embeddings/rebuild-route-feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      const data = await response.json().catch(() => ({})) as any;
      if (response.ok && data.job_id) embeddingJobToOwner.set(data.job_id, user.sub);
      res.status(response.status).json(data);
    } catch (error: any) {
      res.status(502).json({ detail: error?.message || 'Python service is unavailable.' });
    }
  });

  // Upload a file for indexing — forwards to Python service for MinIO storage.
  // Documents are not supported; only project ZIP archives are accepted.
  app.post('/api/upload', async (req, res) => {
    try {
      const { name, size, content, project_id, commit_sha } = req.body;
      if (!name || content === undefined) {
        return res.status(400).json({ success: false, error: 'Missing name or content in payload.' });
      }

      // Forward to Python service for storage
      const pyRes = await fetch(`${PYTHON_API_URL}/api/rag/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: name, content, size: size || 0, project_id, commit_sha }),
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
          projectId: pyData.project_id,
          commitSha: pyData.commit_sha,
          revisionId: pyData.revision_id,
          sourceRef: pyData.source_ref,
          archiveSha256: pyData.archive_sha256,
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
      const data = await pyRes.json() as any;
      if (pyRes.ok && data.download_id) {
        downloadToOwner.set(data.download_id, (res.locals as any).authUser.sub);
      }
      res.status(pyRes.status).json(data);
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

      let storedFile: any = null;
      const listResponse = await fetch(`${PYTHON_API_URL}/api/rag/files`);
      if (listResponse.ok) {
        const listed = await listResponse.json() as { files?: any[] };
        storedFile = (listed.files ?? []).find((item) => item.upload_session_id === (uploadSessionId ?? fileId)) ?? null;
      }
      const user = (res.locals as any).authUser;
      if (storedFile?.project_id) {
        if (req.body?.project_id !== storedFile.project_id || !authRoleFor(user, storedFile.project_id)) {
          return res.status(403).json({ success: false, error: 'Project access denied.' });
        }
      } else if (!authIsBootstrapAdmin(user)) {
        return res.status(403).json({ success: false, error: 'Administrator access is required to delete an unattributed legacy upload.' });
      }

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
      const { query, filename, use_remote, upload_session_id, project_id, commit_sha } = req.body;
      if (!await authorizeAttachedUpload(res, filename, upload_session_id, project_id, commit_sha,
        Boolean(req.body?.domain_analysis_id))) return;
      if (!query || query.trim() === '') {
        return res.status(400).json({ success: false, error: 'Query parameter is required.' });
      }

      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:9002';
      let classification = { route: 'chat', confidence: 0 };
      const queryId = crypto.randomUUID();
      queryToOwner.set(queryId, (res.locals as any).authUser.sub);

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
              upload_session_id,
              project_id,
              commit_sha,
              prompt: query,
              skip_index: indexedProjects.has(indexedProjectKey(filename, project_id, res.locals.uploadSourceRef)),
              entities,
            }),
          });
          if (oRes.ok) {
            const { job_id } = await oRes.json();
            jobToQueryId.set(job_id, queryId);
            jobToOwner.set(job_id, (res.locals as any).authUser.sub);
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
            ...answerOwnerHeaders(res),
          },
          // Forward the client body verbatim (so preview/preview_mode ride
          // along) while pinning the computed query/route/use_remote fields.
          body: JSON.stringify({
            ...req.body,
            query,
            use_remote: !!use_remote,
            route: classification.route,
            business_rules: businessRulesAllowed(res, project_id),
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

  // Per-project consent for sending project source to the external model
  // (ADR remote-model-source-access-requires-project-consent). Project members
  // can read it; only project administrators can change it.
  const remoteSourcePath = (projectId: string) =>
    `${PYTHON_API_URL}/projects/${encodeURIComponent(projectId)}/remote-source`;

  app.get('/api/auth/projects/:projectId/remote-source', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && !authRoleFor(user, req.params.projectId)) {
        return res.status(403).json({ detail: 'Project access denied.' });
      }
      const pyRes = await fetch(remoteSourcePath(req.params.projectId), { headers: { 'X-Internal-Secret': INTERNAL_SECRET } });
      const data = await pyRes.json().catch(() => ({ detail: 'The setting is unavailable.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'The setting is unavailable.' });
    }
  });

  app.put('/api/auth/projects/:projectId/remote-source', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      if (!authIsBootstrapAdmin(user) && authRoleFor(user, req.params.projectId) !== 'admin') {
        return res.status(403).json({ detail: 'Project administrator access is required to change this setting.' });
      }
      if (typeof req.body?.allowed !== 'boolean') {
        return res.status(400).json({ detail: 'allowed must be true or false.' });
      }
      const pyRes = await fetch(remoteSourcePath(req.params.projectId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-Internal-Secret': INTERNAL_SECRET },
        body: JSON.stringify({ allowed: req.body.allowed }),
      });
      const data = await pyRes.json().catch(() => ({ detail: 'The setting could not be saved.' }));
      return res.status(pyRes.status).json(data);
    } catch {
      return res.status(502).json({ detail: 'The setting could not be saved.' });
    }
  });

  // Approve or deny a paused agent tool call (ADR paused-agent-runs-resume-from-
  // checkpoint-on-new-stream). The response is the continued answer as SSE.
  // Only the user who started the chat may decide (enforced in Python), and on a
  // project only operators and administrators may approve side effects.
  app.post('/api/tool-approvals/:approvalId', async (req, res) => {
    try {
      const user = (res.locals as any).authUser;
      const approvalId = req.params.approvalId;
      if (!/^[0-9a-f]{32}$/.test(approvalId)) return res.status(400).json({ success: false, error: 'Invalid approval.' });
      const decision = req.body?.decision;
      if (decision !== 'approve' && decision !== 'deny') {
        return res.status(400).json({ success: false, error: 'decision must be approve or deny.' });
      }
      const infoRes = await fetch(`${PYTHON_API_URL}/tool-approvals/${approvalId}`, { headers: { 'X-Internal-Secret': INTERNAL_SECRET } });
      const info = await infoRes.json().catch(() => ({}));
      if (!infoRes.ok) return res.status(infoRes.status).json({ success: false, error: info.detail || 'Approval unavailable.' });
      if (decision === 'approve' && info.project_id && !authIsBootstrapAdmin(user)
        && !['operator', 'admin'].includes(authRoleFor(user, info.project_id) || '')) {
        return res.status(403).json({ success: false, error: 'Operator or administrator access is required to approve tool calls.' });
      }
      const pyRes = await fetch(`${PYTHON_API_URL}/tool-approvals/${approvalId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...answerOwnerHeaders(res) },
        body: JSON.stringify({ decision }),
      });
      if (!pyRes.ok || !pyRes.body) {
        const err = await pyRes.json().catch(() => ({}));
        return res.status(pyRes.status).json({ success: false, error: (err as any).detail || 'Approval failed.' });
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no' });
      const reader = (pyRes.body as any).getReader();
      req.on('close', () => { reader.cancel().catch(() => {}); });
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
    } catch (error: any) {
      if (!res.headersSent) res.status(502).json({ success: false, error: error.message || 'Approval failed.' });
      else res.end();
    }
  });

  // Source preview for `path:line` citations in chat answers. Same upload
  // authorization as the chat request; Python applies the code_read checks
  // (project-relative paths, no dependency/build paths, no credential files).
  app.post('/api/code/read', async (req, res) => {
    try {
      const { filename, upload_session_id, project_id, commit_sha, path, line } = req.body ?? {};
      if (typeof filename !== 'string' || !filename) {
        return res.status(400).json({ success: false, error: 'Attach a project to preview cited source.' });
      }
      if (!await authorizeAttachedUpload(res, filename, upload_session_id, project_id, commit_sha, false)) return;
      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:9002';
      const pyRes = await fetch(`${pythonUrl}/code-tools/read`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename, project_id, commit_sha, path, line }),
      });
      const data = await pyRes.json().catch(() => ({}));
      if (!pyRes.ok) return res.status(pyRes.status).json({ success: false, error: data.detail || 'Source unavailable.' });
      return res.json(data);
    } catch (error: any) {
      return res.status(502).json({ success: false, error: error.message || 'Source unavailable.' });
    }
  });

  // Stream an answer directly (SSE) — no orchestration job, no polling.
  // Used for chat/knowledge routes where the project is already indexed.
  app.post('/api/query/stream', async (req, res) => {
    try {
      const { query, filename, use_remote, route, upload_session_id, project_id, commit_sha } = req.body;
      if (!await authorizeAttachedUpload(res, filename, upload_session_id, project_id, commit_sha,
        Boolean(req.body?.domain_analysis_id))) return;
      if (!query || query.trim() === '') {
        return res.status(400).json({ success: false, error: 'Query parameter is required.' });
      }

      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:9002';
      let runtimeExtraContext = '';
      let runtimeSymbolMetrics: Record<string, any> = {};
      if (project_id && commit_sha && runtimeQuestion(query)) {
        const runtimeData = await runtimeAnswerContext(project_id, commit_sha);
        if (runtimeData?.telemetry) {
          const telemetry = runtimeData.telemetry;
          runtimeSymbolMetrics = Object.fromEntries((telemetry.symbols || []).map((row: any) => [row.symbol_id, row]));
          const lines = [
            `Runtime evidence for ${project_id} at commit ${commit_sha} (last ${telemetry.retention_hours} retained hours): ${telemetry.total_spans} spans, ${telemetry.match_percent}% matched to indexed symbols, ${telemetry.error_spans} error spans.`,
            ...(telemetry.symbols || []).slice(0, 100).map((row: any) => `Symbol ${row.symbol_id}: ${row.runs} observed calls, p95 ${row.p95_ms} ms, ${row.failures} failures, last seen ${row.last_seen || 'unknown'}.`),
            ...(telemetry.routes || []).slice(0, 50).map((row: any) => `Route ${row.method} ${row.route}: ${row.requests} requests.`),
            ...(telemetry.tables || []).slice(0, 50).map((row: any) => `Database table ${row.table_name}, ${row.operation || 'operation'}: ${row.operations} observed operations.`),
            'These counts cover retained and sampled telemetry only. No observations do not prove code is unused.',
          ];
          runtimeExtraContext = lines.join('\n').slice(0, 24_000);
        }
      }
      const queryId = crypto.randomUUID();
      queryToOwner.set(queryId, (res.locals as any).authUser.sub);

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
            ...answerOwnerHeaders(res),
        },
        // Forward the client body verbatim (so preview/preview_mode and any
        // other keys ride along) while pinning computed fields below.
        body: JSON.stringify({
          ...req.body,
          ...(runtimeExtraContext ? { extra_context: [req.body.extra_context, runtimeExtraContext].filter((value) => typeof value === 'string' && value.trim()).join('\n\n'), runtime_symbol_metrics: runtimeSymbolMetrics } : {}),
          query,
          use_remote: !!use_remote,
          business_rules: businessRulesAllowed(res, project_id),
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
      if (jobToOwner.get(job_id) !== (res.locals as any).authUser.sub) {
        return res.status(403).json({ success: false, error: 'Query job access denied.' });
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
      // A failed job never reaches 'done'; say so instead of letting the chat poll until it times out.
      if (status.status === 'error' || status.status === 'failed') {
        return res.json({
          phase: 'error',
          job_id,
          status: status.status,
          steps: status.steps || [],
          error: status.error || 'Processing failed.',
        });
      }

      // Once a project's orchestration completes, remember it so subsequent
      // queries in this session skip re-indexing.
      // A structure-only route (for example `knowledge`) has no code
      // collection. Do not mark it indexed, otherwise a later code question
      // will incorrectly pass skip_index=true and never create the collection.
      if (isDone && status.filename && status.collection_name) {
        indexedProjects.add(indexedProjectKey(status.filename, status.project_id, status.source_ref || status.commit_sha));
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
            ...answerOwnerHeaders(res),
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
            // Project identity from the job record lets code tools read the
            // indexed revision instead of a retained upload workspace.
            ...((status as any).project_id
              ? { project_id: (status as any).project_id, commit_sha: (status as any).commit_sha || undefined }
              : {}),
            ...(typeof req.body?.domain_context_mode === 'string'
              ? { domain_context_mode: req.body.domain_context_mode }
              : {}),
            ...(typeof req.body?.domain_concept_id === 'string'
              ? { domain_concept_id: req.body.domain_concept_id }
              : {}),
            ...(typeof req.body?.domain_analysis_id === 'string'
              ? { domain_analysis_id: req.body.domain_analysis_id }
              : {}),
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
        result.usage = answer.usage;
        result.context_usage = answer.context_usage;
        result.context_preview = answer.context_preview;
        result.preview_only = answer.preview_only;
        result.preview_mode = answer.preview_mode;
        result.tool_calls = answer.tool_calls || [];
        result.tools = answer.tools || null;
        result.paused = Boolean(answer.paused);
        result.approval = answer.approval || null;

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
          usage: result.usage,
          context_usage: result.context_usage,
          context_preview: result.context_preview,
          preview_only: result.preview_only,
          preview_mode: result.preview_mode,
          tool_calls: result.tool_calls,
          tools: result.tools,
          paused: result.paused,
          approval: result.approval,
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
      if (queryToOwner.get(traceId) !== (res.locals as any).authUser.sub) {
        return res.status(403).json({ success: false, error: 'Query access denied.' });
      }
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
      if (queryToOwner.get(req.params.queryId) !== (res.locals as any).authUser.sub) {
        return res.status(403).json({ success: false, error: 'Query access denied.' });
      }
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

  const listenHost = process.env.WEB_LISTEN_HOST || (process.env.NODE_ENV === 'development' ? '127.0.0.1' : '0.0.0.0');
  app.listen(PORT, listenHost, () => {
    console.log(`[RAG Server] Running on http://localhost:${PORT}`);
  });
  const cleanup = () => closeAllSessions();
  process.once('SIGINT', cleanup);
  process.once('SIGTERM', cleanup);
}

startServer();
