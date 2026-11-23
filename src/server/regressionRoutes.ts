/**
 * Regression checks between a base and a candidate revision (@.archcore/regression/regression-check.spec.md).
 *
 * Readers of the project may list and read checks. Starting or deleting one is an operator action,
 * because a check spends model budget on equivalence review. Only the documented fields are forwarded.
 * Replay runs send HTTP to the user's own saved targets; a Bearer target's token is fetched from the
 * OS credential helper here, passed to Python for that run, and never stored.
 */
import type { Express, Response } from 'express';

type User = { sub: string } | null | undefined;

export interface RegressionDependencies {
  apiUrl: string;
  internalSecret: string;
  getUser: (res: Response) => User;
  canReadProject: (user: User, projectId: string) => boolean;
  canOperateProject: (user: User, projectId: string) => boolean;
  /** Bearer token of a saved benchmark target, from the desktop OS credential helper. */
  bearerToken?: (targetId: string) => Promise<{ status: number; token?: string; detail?: string }>;
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

  // --- replay ---------------------------------------------------------------------------------------
  const checkPath = (req: any, suffix: string) => checksPath(req.params.projectId, req.params.checkId) + suffix;
  const body = (req: any) => (req.body && typeof req.body === 'object' && !Array.isArray(req.body) ? req.body : {});
  const pick = (value: Record<string, unknown>, keys: string[]) =>
    Object.fromEntries(keys.filter((key) => value[key] !== undefined).map((key) => [key, value[key]]));
  const upstreamJson = async (user: { sub: string }, path: string) => {
    const response = await fetch(`${deps.apiUrl}${path}`, {
      headers: { 'X-Internal-Secret': deps.internalSecret, 'X-User-Id': user.sub }, signal: AbortSignal.timeout(30_000),
    });
    return { response, data: await response.json().catch(() => ({})) as any };
  };

  app.get('/api/regression/projects/:projectId/checks/:checkId/replay', async (req, res) => {
    const user = access(res, req.params.projectId, false); if (!user) return;
    return forward(res, user, checkPath(req, '/replay'), 'Replay details are unavailable.');
  });

  app.get('/api/regression/projects/:projectId/checks/:checkId/replay-runs/:runId', async (req, res) => {
    const user = access(res, req.params.projectId, false); if (!user) return;
    return forward(res, user, checkPath(req, `/replay-runs/${encodeURIComponent(req.params.runId)}`), 'The replay run is unavailable.');
  });

  app.put('/api/regression/projects/:projectId/checks/:checkId/probes', async (req, res) => {
    const user = access(res, req.params.projectId, true); if (!user) return;
    return forward(res, user, checkPath(req, '/probes'), 'The probes could not be saved.',
      { method: 'PUT', body: JSON.stringify(pick(body(req), ['probes'])) });
  });

  app.post('/api/regression/projects/:projectId/checks/:checkId/probes/generate', async (req, res) => {
    const user = access(res, req.params.projectId, true); if (!user) return;
    return forward(res, user, checkPath(req, '/probes/generate'), 'Probes could not be proposed.', { method: 'POST', body: '{}' });
  });

  app.post('/api/regression/projects/:projectId/checks/:checkId/probes/import', async (req, res) => {
    const user = access(res, req.params.projectId, true); if (!user) return;
    return forward(res, user, checkPath(req, '/probes/import'), 'The requests could not be imported.',
      { method: 'POST', body: JSON.stringify(pick(body(req), ['text', 'target_id', 'mode'])) });
  });

  app.put('/api/regression/projects/:projectId/checks/:checkId/replay-settings', async (req, res) => {
    const user = access(res, req.params.projectId, true); if (!user) return;
    return forward(res, user, checkPath(req, '/replay-settings'), 'Replay settings could not be saved.', {
      method: 'PUT',
      body: JSON.stringify(pick(body(req), ['baseline_target_id', 'candidate_target_id', 'disposable', 'reset_path', 'ignore'])),
    });
  });

  app.post('/api/regression/projects/:projectId/checks/:checkId/replay-runs', async (req, res) => {
    const user = access(res, req.params.projectId, true); if (!user) return;
    const fallback = 'The replay could not be started.';
    const { kind } = body(req);
    if (kind !== 'baseline' && kind !== 'candidate') return res.status(422).json({ detail: 'kind must be baseline or candidate.' });
    try {
      let targetId = typeof body(req).target_id === 'string' ? body(req).target_id : '';
      if (!targetId) {
        const replay = await upstreamJson(user, checkPath(req, '/replay'));
        if (!replay.response.ok) return res.status(replay.response.status).json(replay.data);
        targetId = replay.data.settings?.[`${kind}_target_id`] || '';
      }
      if (!targetId) return res.status(422).json({ detail: `Choose the ${kind} target first.` });
      const target = await upstreamJson(user,
        `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/performance/targets/${encodeURIComponent(targetId)}`);
      if (!target.response.ok) return res.status(target.response.status).json(target.data);
      const forwarded: Record<string, unknown> = { kind, target_id: targetId };
      if (target.data.target?.auth_mode === 'bearer') {
        const credential = deps.bearerToken ? await deps.bearerToken(targetId) : { status: 503, detail: 'The credential helper is unavailable.' };
        if (!credential.token) return res.status(credential.status === 200 ? 503 : credential.status).json({ detail: credential.detail || fallback });
        forwarded.bearer_token = credential.token;
      }
      return forward(res, user, checkPath(req, '/replay-runs'), fallback, { method: 'POST', body: JSON.stringify(forwarded) });
    } catch {
      return res.status(502).json({ detail: fallback });
    }
  });
}
