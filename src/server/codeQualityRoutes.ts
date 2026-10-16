import type { Express, Response } from 'express';

type AuthUser = { sub: string; email?: string; name?: string } | null;

type RouteDependencies = {
  apiUrl: string;
  internalSecret: string;
  getUser: (response: Response) => AuthUser;
  canAccessProject: (user: AuthUser, projectId: string) => boolean;
};

export function registerCodeQualityRoutes(app: Express, dependencies: RouteDependencies) {
  const userFor = (res: Response) => dependencies.getUser(res);
  const authenticated = (res: Response) => {
    const user = userFor(res);
    if (!user?.sub) {
      res.status(401).json({ detail: 'Sign in to use code quality assessment.' });
      return null;
    }
    return user;
  };
  const forward = (path: string, user: AuthUser, init: RequestInit = {}) => fetch(`${dependencies.apiUrl}${path}`, {
    ...init,
    headers: {
      'X-Internal-Secret': dependencies.internalSecret,
      ...(user?.sub ? { 'X-User-Id': user.sub } : {}),
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers as Record<string, string> || {}),
    },
  });
  const reply = async (response: Response, upstream: globalThis.Response, fallback: string) =>
    response.status(upstream.status).json(await upstream.json().catch(() => ({ detail: fallback })));
  const allowed = (res: Response, projectId: string) => {
    const user = authenticated(res);
    if (!user) return null;
    if (!dependencies.canAccessProject(user, projectId)) {
      res.status(403).json({ detail: 'Project access denied.' });
      return null;
    }
    return user;
  };

  app.get('/api/code-quality/projects', async (_req, res) => {
    try {
      const user = authenticated(res);
      if (!user) return;
      const upstream = await forward('/telemetry/runtime/projects', user);
      const data = await upstream.json().catch(() => []);
      if (!upstream.ok || !Array.isArray(data)) return res.status(upstream.status || 502).json({ detail: 'Indexed project inventory is unavailable.' });
      const projects = data.filter((item: any) => item.state === 'ready'
        && typeof item.project_id === 'string' && typeof item.revision_id === 'string'
        && dependencies.canAccessProject(user, item.project_id))
        .map((item: any) => ({ project_id: item.project_id, revision_id: item.revision_id,
          commit_sha: item.commit_sha, updated_at: item.updated_at }));
      return res.json({ projects });
    } catch {
      return res.status(502).json({ detail: 'Indexed project inventory is unavailable.' });
    }
  });

  app.get('/api/code-quality/preferences', async (_req, res) => {
    try {
      const user = authenticated(res);
      if (!user) return;
      return await reply(res, await forward('/code-quality/preferences', user), 'Assessment preferences are unavailable.');
    } catch {
      return res.status(502).json({ detail: 'Assessment preferences are unavailable.' });
    }
  });

  app.put('/api/code-quality/preferences', async (req, res) => {
    try {
      const user = authenticated(res);
      if (!user) return;
      return await reply(res, await forward('/code-quality/preferences', user, {
        method: 'PUT', body: JSON.stringify({ weights: req.body?.weights }),
      }), 'Assessment preferences could not be saved.');
    } catch {
      return res.status(502).json({ detail: 'Assessment preferences could not be saved.' });
    }
  });

  app.get('/api/code-quality/projects/:projectId/reports', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId);
      if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/reports`;
      return await reply(res, await forward(path, user), 'Assessment history is unavailable.');
    } catch {
      return res.status(502).json({ detail: 'Assessment history is unavailable.' });
    }
  });

  app.get('/api/code-quality/projects/:projectId/reports/:reportId', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId);
      if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/reports/${encodeURIComponent(req.params.reportId)}`;
      return await reply(res, await forward(path, user), 'Assessment report is unavailable.');
    } catch {
      return res.status(502).json({ detail: 'Assessment report is unavailable.' });
    }
  });

  app.post('/api/code-quality/projects/:projectId/revisions/:revisionId/assess', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId);
      if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/assess`;
      return await reply(res, await forward(path, user, { method: 'POST' }), 'Assessment could not be completed. Retry the request.');
    } catch {
      return res.status(502).json({ detail: 'Assessment could not be completed. Retry the request.' });
    }
  });
}
