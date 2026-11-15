/**
 * API contracts (Swagger / OpenAPI documents) per project.
 * Spec: .archcore/frontend-assist/api-contract-sources-and-versions.spec.md
 *
 * The Python service stores contracts; this file decides who may reach them:
 * - any project member reads the project's contracts;
 * - an operator or administrator changes them;
 * - a user sees a contract source only through a project they can access: the project that
 *   owns it, or a project it is attached to;
 * - attaching a contract to a project needs operator access to that project and access to
 *   the project that owns the contract.
 */
import express, { type Express, type Request, type Response } from 'express';

type User = { sub: string } | null | undefined;

export interface ContractDependencies {
  apiUrl: string;
  internalSecret: string;
  webOrigin: string;
  getUser: (res: Response) => User;
  canAccessProject: (user: User, projectId: string) => boolean;
  canOperateProject: (user: User, projectId: string) => boolean;
}

export const MAX_CONTRACT_BYTES = 10 * 1024 * 1024;
const PROJECT_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const IDS = { source: /^cs_[0-9a-f]{32}$/, version: /^cv_[0-9a-f]{32}$/, attachment: /^ca_[0-9a-f]{32}$/ };

export function registerContractRoutes(app: Express, deps: ContractDependencies): void {
  const python = async (path: string, init: RequestInit = {}, timeoutMs = 15_000) => {
    const response = await fetch(`${deps.apiUrl}/contracts${path}`, {
      ...init,
      headers: { 'X-Internal-Secret': deps.internalSecret, ...(init.headers as Record<string, string> || {}) },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const data = await response.json().catch(() => ({ detail: 'The contract service returned an invalid response.' }));
    return { response, data };
  };
  const json = (body: unknown): RequestInit => ({
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  /** Signed-in user and a valid project the user may read (or change, when `operate`). */
  const enter = (req: Request, res: Response, operate: boolean) => {
    const user = deps.getUser(res);
    if (!user?.sub) { res.status(401).json({ detail: 'Sign in to use API contracts.' }); return null; }
    const projectId = req.params.projectId;
    if (!PROJECT_ID.test(projectId || '')) { res.status(400).json({ detail: 'Invalid project identity.' }); return null; }
    if (operate) {
      if (req.get('origin') !== deps.webOrigin) {
        res.status(403).json({ detail: 'Contract changes must come from the application origin.' }); return null;
      }
      if (!deps.canOperateProject(user, projectId)) {
        res.status(403).json({ detail: 'Operator or administrator access is required to change API contracts.' }); return null;
      }
    } else if (!deps.canAccessProject(user, projectId)) {
      res.status(403).json({ detail: 'Project access denied.' }); return null;
    }
    return { user, projectId };
  };
  const validId = (res: Response, kind: keyof typeof IDS, value: string | undefined) => {
    if (IDS[kind].test(value || '')) return true;
    res.status(400).json({ detail: `Invalid contract ${kind}.` });
    return false;
  };
  /** The source, when the user can access its owning project or a project it is attached to. */
  const visibleSource = async (user: User, sourceId: string) => {
    const { response, data } = await python(`/sources/${sourceId}`);
    if (!response.ok) return null;
    const source = data.source;
    const projects = [source.project_id, ...(source.attachments || []).map((row: any) => row.project_id)];
    return projects.some((projectId: string) => deps.canAccessProject(user, projectId)) ? source : null;
  };
  const guard = (unavailable: string, handler: (req: Request, res: Response) => Promise<unknown>) =>
    async (req: Request, res: Response) => {
      try { return await handler(req, res); }
      catch { return res.status(502).json({ detail: unavailable }); }
    };
  const forward = async (res: Response, path: string, init?: RequestInit, timeoutMs?: number) => {
    const { response, data } = await python(path, init, timeoutMs);
    return res.status(response.status).json(data);
  };

  // ── reading ────────────────────────────────────────────────────────────────

  app.get('/api/projects/:projectId/contracts', guard('API contracts are unavailable.', async (req, res) => {
    const scope = enter(req, res, false);
    if (!scope) return;
    return forward(res, `/projects/${encodeURIComponent(scope.projectId)}`);
  }));

  // Contracts the user could attach: those owned by a project they can access.
  app.get('/api/contracts/sources', guard('API contracts are unavailable.', async (_req, res) => {
    const user = deps.getUser(res);
    if (!user?.sub) return res.status(401).json({ detail: 'Sign in to use API contracts.' });
    const { response, data } = await python('/sources');
    if (!response.ok) return res.status(response.status).json(data);
    return res.json({ sources: (data.sources || []).filter((source: any) => deps.canAccessProject(user, source.project_id)) });
  }));

  app.get('/api/contracts/sources/:sourceId/versions', guard('Contract versions are unavailable.', async (req, res) => {
    const user = deps.getUser(res);
    if (!user?.sub) return res.status(401).json({ detail: 'Sign in to use API contracts.' });
    if (!validId(res, 'source', req.params.sourceId)) return;
    if (!await visibleSource(user, req.params.sourceId)) return res.status(404).json({ detail: 'No such contract.' });
    return forward(res, `/sources/${req.params.sourceId}/versions`);
  }));

  app.get('/api/contracts/sources/:sourceId/versions/:versionId', guard('The contract version is unavailable.', async (req, res) => {
    const user = deps.getUser(res);
    if (!user?.sub) return res.status(401).json({ detail: 'Sign in to use API contracts.' });
    if (!validId(res, 'source', req.params.sourceId) || !validId(res, 'version', req.params.versionId)) return;
    if (!await visibleSource(user, req.params.sourceId)) return res.status(404).json({ detail: 'No such contract.' });
    return forward(res, `/sources/${req.params.sourceId}/versions/${req.params.versionId}`);
  }));

  // Typed client code or mocks from one version. It changes nothing, so reading access is enough.
  app.post('/api/contracts/sources/:sourceId/versions/:versionId/generate', guard('Code could not be generated.', async (req, res) => {
    const user = deps.getUser(res);
    if (!user?.sub) return res.status(401).json({ detail: 'Sign in to use API contracts.' });
    if (!validId(res, 'source', req.params.sourceId) || !validId(res, 'version', req.params.versionId)) return;
    if (!await visibleSource(user, req.params.sourceId)) return res.status(404).json({ detail: 'No such contract.' });
    const { target, base_url, search } = req.body || {};
    return forward(res, `/sources/${req.params.sourceId}/versions/${req.params.versionId}/generate`,
      json({ target, base_url, search }), 30_000);
  }));

  // ── sources ────────────────────────────────────────────────────────────────

  app.post('/api/projects/:projectId/contracts/sources', guard('The contract could not be created.', async (req, res) => {
    const scope = enter(req, res, true);
    if (!scope) return;
    const { kind, name, path, url, refresh_minutes } = req.body || {};
    return forward(res, `/projects/${encodeURIComponent(scope.projectId)}/sources`,
      json({ kind, name, path, url, refresh_minutes, user_id: scope.user!.sub }));
  }));

  app.delete('/api/projects/:projectId/contracts/sources/:sourceId', guard('The contract could not be removed.', async (req, res) => {
    const scope = enter(req, res, true);
    if (!scope || !validId(res, 'source', req.params.sourceId)) return;
    return forward(res, `/projects/${encodeURIComponent(scope.projectId)}/sources/${req.params.sourceId}`, { method: 'DELETE' });
  }));

  app.post('/api/projects/:projectId/contracts/sources/:sourceId/document',
    express.text({ type: 'text/plain', limit: MAX_CONTRACT_BYTES }),
    guard('The document could not be uploaded.', async (req, res) => {
      const scope = enter(req, res, true);
      if (!scope || !validId(res, 'source', req.params.sourceId)) return;
      if (typeof req.body !== 'string' || !req.body.trim()) {
        return res.status(400).json({ detail: 'Send the contract document as text.' });
      }
      return forward(res, `/projects/${encodeURIComponent(scope.projectId)}/sources/${req.params.sourceId}/document`,
        { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: req.body }, 30_000);
    }));

  app.post('/api/projects/:projectId/contracts/sources/:sourceId/refresh', guard('The contract could not be checked.', async (req, res) => {
    const scope = enter(req, res, true);
    if (!scope || !validId(res, 'source', req.params.sourceId)) return;
    return forward(res, `/projects/${encodeURIComponent(scope.projectId)}/sources/${req.params.sourceId}/refresh`,
      { method: 'POST' }, 45_000);
  }));

  app.delete('/api/projects/:projectId/contracts/sources/:sourceId/versions/:versionId',
    guard('The version could not be removed.', async (req, res) => {
      const scope = enter(req, res, true);
      if (!scope || !validId(res, 'source', req.params.sourceId) || !validId(res, 'version', req.params.versionId)) return;
      return forward(res,
        `/projects/${encodeURIComponent(scope.projectId)}/sources/${req.params.sourceId}/versions/${req.params.versionId}`,
        { method: 'DELETE' });
    }));

  // ── attachments ────────────────────────────────────────────────────────────

  app.post('/api/projects/:projectId/contracts/attachments', guard('The contract could not be attached.', async (req, res) => {
    const scope = enter(req, res, true);
    if (!scope) return;
    const { source_id: sourceId, role, mode, pinned_version_id } = req.body || {};
    if (!validId(res, 'source', sourceId)) return;
    const { response, data } = await python(`/sources/${sourceId}`);
    if (!response.ok || !deps.canAccessProject(scope.user, data.source.project_id)) {
      return res.status(403).json({ detail: 'You need access to the project that owns this contract to attach it.' });
    }
    return forward(res, `/projects/${encodeURIComponent(scope.projectId)}/attachments`,
      json({ source_id: sourceId, role, mode, pinned_version_id, user_id: scope.user!.sub }));
  }));

  // Reports for a consumer attachment: how the project's code agrees with the contract, and what a change touches.
  const report = (kind: 'conformance' | 'impact' | 'runtime') => guard('The contract check is unavailable.', async (req, res) => {
    const scope = enter(req, res, false);
    if (!scope || !validId(res, 'attachment', req.params.attachmentId)) return;
    const query = new URLSearchParams();
    const pass = (name: string, pattern: RegExp) => {
      const value = req.query[name];
      if (value === undefined || value === '') return true;
      if (typeof value !== 'string' || !pattern.test(value)) { res.status(400).json({ detail: `Invalid ${name}.` }); return false; }
      query.set(name, value);
      return true;
    };
    if (!pass('revision_id', /^rev_[0-9a-f]{32}$/)) return;
    if (kind === 'impact' && (!pass('from_version', IDS.version) || !pass('to_version', IDS.version))) return;
    return forward(res, `/projects/${encodeURIComponent(scope.projectId)}/attachments/${req.params.attachmentId}/${kind}?${query}`,
      undefined, 90_000);
  });
  app.get('/api/projects/:projectId/contracts/attachments/:attachmentId/conformance', report('conformance'));
  app.get('/api/projects/:projectId/contracts/attachments/:attachmentId/impact', report('impact'));
  app.get('/api/projects/:projectId/contracts/attachments/:attachmentId/runtime', report('runtime'));

  app.put('/api/projects/:projectId/contracts/attachments/:attachmentId', guard('The attachment could not be changed.', async (req, res) => {
    const scope = enter(req, res, true);
    if (!scope || !validId(res, 'attachment', req.params.attachmentId)) return;
    const { mode, pinned_version_id } = req.body || {};
    return forward(res, `/projects/${encodeURIComponent(scope.projectId)}/attachments/${req.params.attachmentId}`,
      { ...json({ mode, pinned_version_id }), method: 'PUT' });
  }));

  app.delete('/api/projects/:projectId/contracts/attachments/:attachmentId', guard('The contract could not be detached.', async (req, res) => {
    const scope = enter(req, res, true);
    if (!scope || !validId(res, 'attachment', req.params.attachmentId)) return;
    return forward(res, `/projects/${encodeURIComponent(scope.projectId)}/attachments/${req.params.attachmentId}`, { method: 'DELETE' });
  }));
}
