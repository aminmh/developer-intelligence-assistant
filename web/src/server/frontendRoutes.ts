/** Front-end project understanding: the UI map of an Angular, React, or Vue project revision. */
import type { Express, Response } from 'express';

type User = { sub: string } | null | undefined;

export interface FrontendDependencies {
  apiUrl: string;
  internalSecret: string;
  getUser: (res: Response) => User;
  canAccessProject: (user: User, projectId: string) => boolean;
}

const PROJECT_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const REVISION_ID = /^rev_[0-9a-f]{32}$/;
const APP_ROOT = /^[A-Za-z0-9_.@-]+(\/[A-Za-z0-9_.@-]+){0,3}$/;

export function registerFrontendRoutes(app: Express, deps: FrontendDependencies): void {
  // Browser screen views recorded by the snippet, and one view with the requests it sent.
  const screens = async (req: any, res: any) => {
    const user = deps.getUser(res);
    if (!user?.sub) return res.status(401).json({ detail: 'Sign in to view screen views.' });
    const projectId = req.params.projectId;
    if (!PROJECT_ID.test(projectId)) return res.status(400).json({ detail: 'Invalid project identity.' });
    if (!deps.canAccessProject(user, projectId)) return res.status(403).json({ detail: 'Project access denied.' });
    const traceId = req.params.traceId;
    if (traceId !== undefined && !/^[0-9a-f]{32}$/.test(traceId)) return res.status(400).json({ detail: 'Invalid screen view.' });
    try {
      const upstream = await fetch(`${deps.apiUrl}/telemetry/runtime/projects/${encodeURIComponent(projectId)}/screens${traceId ? `/${traceId}` : ''}`, {
        headers: { 'X-Internal-Secret': deps.internalSecret }, signal: AbortSignal.timeout(20_000) });
      return res.status(upstream.status).json(await upstream.json().catch(() => ({ detail: 'The runtime service returned an invalid response.' })));
    } catch {
      return res.status(502).json({ detail: 'Screen views are unavailable. Retry the request.' });
    }
  };
  app.get('/api/projects/:projectId/screens', screens);
  app.get('/api/projects/:projectId/screens/:traceId', screens);

  app.get('/api/projects/:projectId/frontend-checks', async (req, res) => {
    const user = deps.getUser(res);
    if (!user?.sub) return res.status(401).json({ detail: 'Sign in to view front-end checks.' });
    const projectId = req.params.projectId;
    if (!PROJECT_ID.test(projectId)) return res.status(400).json({ detail: 'Invalid project identity.' });
    if (!deps.canAccessProject(user, projectId)) return res.status(403).json({ detail: 'Project access denied.' });
    const revision = typeof req.query.revision_id === 'string' ? req.query.revision_id : '';
    if (revision && !REVISION_ID.test(revision)) return res.status(400).json({ detail: 'Invalid revision.' });
    try {
      const query = revision ? `?revision_id=${revision}` : '';
      const upstream = await fetch(`${deps.apiUrl}/frontend/projects/${encodeURIComponent(projectId)}/checks${query}`, {
        headers: { 'X-Internal-Secret': deps.internalSecret }, signal: AbortSignal.timeout(90_000) });
      return res.status(upstream.status).json(await upstream.json().catch(() => ({ detail: 'The checks service returned an invalid response.' })));
    } catch {
      return res.status(502).json({ detail: 'Front-end checks are unavailable. Retry the request.' });
    }
  });

  app.get('/api/projects/:projectId/ui-map', async (req, res) => {
    const user = deps.getUser(res);
    if (!user?.sub) return res.status(401).json({ detail: 'Sign in to view the UI map.' });
    const projectId = req.params.projectId;
    if (!PROJECT_ID.test(projectId)) return res.status(400).json({ detail: 'Invalid project identity.' });
    if (!deps.canAccessProject(user, projectId)) return res.status(403).json({ detail: 'Project access denied.' });
    const query = new URLSearchParams();
    const revision = typeof req.query.revision_id === 'string' ? req.query.revision_id : '';
    const appRoot = typeof req.query.app === 'string' ? req.query.app : '';
    if (revision) {
      if (!REVISION_ID.test(revision)) return res.status(400).json({ detail: 'Invalid revision.' });
      query.set('revision_id', revision);
    }
    if (appRoot) {
      if (!APP_ROOT.test(appRoot) || appRoot.split('/').includes('..')) return res.status(400).json({ detail: 'Invalid application folder.' });
      query.set('app', appRoot);
    }
    try {
      const upstream = await fetch(`${deps.apiUrl}/frontend/projects/${encodeURIComponent(projectId)}/ui-map?${query}`, {
        headers: { 'X-Internal-Secret': deps.internalSecret }, signal: AbortSignal.timeout(90_000) });
      return res.status(upstream.status).json(await upstream.json().catch(() => ({ detail: 'The UI map service returned an invalid response.' })));
    } catch {
      return res.status(502).json({ detail: 'The UI map is unavailable. Retry the request.' });
    }
  });
}
