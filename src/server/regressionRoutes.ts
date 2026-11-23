/**
 * Regression checks between a base and a candidate revision (@.archcore/regression/regression-check.spec.md).
 *
 * Readers of the project may list and read checks. Starting or deleting one is an operator action,
 * because a check spends model budget on equivalence review. Only the documented fields are forwarded.
 */
import type { Express, Response } from 'express';

type User = { sub: string } | null | undefined;

export interface RegressionDependencies {
  apiUrl: string;
  internalSecret: string;
  getUser: (res: Response) => User;
  canReadProject: (user: User, projectId: string) => boolean;
  canOperateProject: (user: User, projectId: string) => boolean;
}

const checksPath = (projectId: string, checkId = '') =>
  `/projects/${encodeURIComponent(projectId)}/regression-checks${checkId ? `/${encodeURIComponent(checkId)}` : ''}`;

export function registerRegressionRoutes(app: Express, deps: RegressionDependencies): void {
  const access = (res: Response, projectId: string, operate: boolean) => {
    const user = deps.getUser(res);
    if (!user?.sub) { res.status(401).json({ detail: 'Sign in to use regression checks.' }); return null; }
    if (!deps.canReadProject(user, projectId)) { res.status(403).json({ detail: 'Project access denied.' }); return null; }
    if (operate && !deps.canOperateProject(user, projectId)) {
      res.status(403).json({ detail: 'Operator access is required to start or delete regression checks.' });
      return null;
    }
    return user;
  };
  const forward = async (res: Response, user: { sub: string }, path: string, fallback: string, init: RequestInit = {}) => {
    try {
      const upstream = await fetch(`${deps.apiUrl}${path}`, {
        ...init,
        headers: { 'X-Internal-Secret': deps.internalSecret, 'X-User-Id': user.sub,
          ...(init.body ? { 'Content-Type': 'application/json' } : {}) },
        signal: AbortSignal.timeout(60_000),
      });
      if (upstream.status === 204) return res.status(204).end();
      const data: any = await upstream.json().catch(() => ({ detail: fallback }));
      // FastAPI wraps a dict detail ({detail, active_check_id}) in another detail.
      const body = data && typeof data.detail === 'object' && data.detail ? data.detail : data;
      return res.status(upstream.status).json(body);
    } catch {
      return res.status(502).json({ detail: fallback });
    }
  };

  app.get('/api/regression/projects/:projectId/checks', async (req, res) => {
    const user = access(res, req.params.projectId, false); if (!user) return;
    return forward(res, user, checksPath(req.params.projectId), 'Regression checks are unavailable.');
  });

  app.post('/api/regression/projects/:projectId/checks', async (req, res) => {
    const user = access(res, req.params.projectId, true); if (!user) return;
    const body = req.body && typeof req.body === 'object' ? req.body : {};
    const forwarded = { base_revision_id: body.base_revision_id, candidate_revision_id: body.candidate_revision_id };
    return forward(res, user, checksPath(req.params.projectId), 'The regression check could not be started.',
      { method: 'POST', body: JSON.stringify(forwarded) });
  });

  app.get('/api/regression/projects/:projectId/checks/:checkId', async (req, res) => {
    const user = access(res, req.params.projectId, false); if (!user) return;
    return forward(res, user, checksPath(req.params.projectId, req.params.checkId), 'The regression check is unavailable.');
  });

  app.delete('/api/regression/projects/:projectId/checks/:checkId', async (req, res) => {
    const user = access(res, req.params.projectId, true); if (!user) return;
    return forward(res, user, checksPath(req.params.projectId, req.params.checkId), 'The regression check could not be deleted.',
      { method: 'DELETE' });
  });
}
