import type { Express, Response } from 'express';
import { createHmac, randomUUID } from 'node:crypto';

type AuthUser = { sub: string; email?: string; name?: string } | null;

type RouteDependencies = {
  apiUrl: string;
  internalSecret: string;
  getUser: (response: Response) => AuthUser;
  canAccessProject: (user: AuthUser, projectId: string) => boolean;
  helperUrl: string;
  webOrigin: string;
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
  const helperAuthorization = (operation: string, targetId: string) => {
    const now = Math.floor(Date.now() / 1000);
    const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
    const header = encode({ alg: 'HS256', typ: 'JWT' });
    const claims = encode({ aud: 'runtime-helper', origin: dependencies.webOrigin, operation,
      target_id: targetId, exp: now + 120, jti: randomUUID() });
    const key = createHmac('sha256', dependencies.internalSecret).update('devassistant-runtime-helper-v1').digest();
    const signature = createHmac('sha256', key).update(`${header}.${claims}`).digest('base64url');
    return `${header}.${claims}.${signature}`;
  };
  const callCredentialHelper = async (targetId: string, operation: string, method: 'POST' | 'DELETE', body: Record<string, unknown>) => {
    const token = helperAuthorization(operation, targetId);
    const response = await fetch(`${dependencies.helperUrl}/code-quality-bearer${operation === 'code-quality-benchmark-use' ? '-use' : ''}`, {
      method,
      headers: { Origin: dependencies.webOrigin, Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, target_id: targetId }),
      signal: AbortSignal.timeout(10_000),
    });
    const data = await response.json().catch(() => ({ detail: 'The desktop OS credential helper returned an invalid response.' }));
    return { response, data };
  };

  app.get('/api/code-quality/performance/targets', async (_req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      return await reply(res, await forward('/code-quality/performance/targets', user), 'Performance targets are unavailable.');
    } catch { return res.status(502).json({ detail: 'Performance targets are unavailable.' }); }
  });

  app.post('/api/code-quality/performance/targets', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      return await reply(res, await forward('/code-quality/performance/targets', user, { method: 'POST', body: JSON.stringify(req.body) }), 'Performance target could not be saved.');
    } catch { return res.status(502).json({ detail: 'Performance target could not be saved.' }); }
  });

  app.get('/api/code-quality/performance/targets/:targetId', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      return await reply(res, await forward(`/code-quality/performance/targets/${encodeURIComponent(req.params.targetId)}`, user), 'Performance target is unavailable.');
    } catch { return res.status(502).json({ detail: 'Performance target is unavailable.' }); }
  });

  app.delete('/api/code-quality/performance/targets/:targetId', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      const target = await forward(`/code-quality/performance/targets/${encodeURIComponent(req.params.targetId)}`, user);
      if (!target.ok) return await reply(res, target, 'Performance target could not be removed.');
      const targetBody = await target.json() as any;
      if (targetBody.target?.auth_mode === 'bearer') {
        const credential = await callCredentialHelper(req.params.targetId, 'code-quality-bearer-manage', 'DELETE', {});
        if (!credential.response.ok) return res.status(credential.response.status).json(credential.data);
      }
      const result = await forward(`/code-quality/performance/targets/${encodeURIComponent(req.params.targetId)}`, user, { method: 'DELETE' });
      return await reply(res, result, 'Performance target could not be removed.');
    } catch { return res.status(502).json({ detail: 'Performance target could not be removed.' }); }
  });

  app.get('/api/code-quality/performance/targets/:targetId/credential', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      const target = await forward(`/code-quality/performance/targets/${encodeURIComponent(req.params.targetId)}`, user);
      if (!target.ok) return await reply(res, target, 'Performance target is unavailable.');
      const credential = await callCredentialHelper(req.params.targetId, 'code-quality-bearer-manage', 'POST', { action: 'status' });
      return res.status(credential.response.status).json(credential.data);
    } catch { return res.status(503).json({ detail: 'The desktop OS credential helper is unavailable. Start or restart Developer Assistant and retry.' }); }
  });

  app.post('/api/code-quality/performance/targets/:targetId/credential', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      const target = await forward(`/code-quality/performance/targets/${encodeURIComponent(req.params.targetId)}`, user);
      if (!target.ok) return await reply(res, target, 'Performance target is unavailable.');
      const token = req.body?.token;
      const credential = await callCredentialHelper(req.params.targetId, 'code-quality-bearer-manage', 'POST', { action: 'save', token });
      return res.status(credential.response.status).json(credential.data);
    } catch { return res.status(503).json({ detail: 'The Bearer token could not be saved to the OS credential store.' }); }
  });

  app.delete('/api/code-quality/performance/targets/:targetId/credential', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      const target = await forward(`/code-quality/performance/targets/${encodeURIComponent(req.params.targetId)}`, user);
      if (!target.ok) return await reply(res, target, 'Performance target is unavailable.');
      const credential = await callCredentialHelper(req.params.targetId, 'code-quality-bearer-manage', 'DELETE', {});
      return res.status(credential.response.status).json(credential.data);
    } catch { return res.status(503).json({ detail: 'The Bearer token could not be removed from the OS credential store.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/revisions/:revisionId/performance/routes', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/performance/routes`;
      return await reply(res, await forward(path, user), 'Sampled Runtime routes are unavailable.');
    } catch { return res.status(502).json({ detail: 'Sampled Runtime routes are unavailable.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/performance/runs', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const targetId = String(req.body?.target_id || '');
      const targetResponse = await forward(`/code-quality/performance/targets/${encodeURIComponent(targetId)}`, user);
      if (!targetResponse.ok) return await reply(res, targetResponse, 'Performance target is unavailable.');
      const { target } = await targetResponse.json() as any;
      const body: Record<string, unknown> = { target_id: targetId, routes: req.body?.routes };
      if (target.auth_mode === 'bearer') {
        const credential = await callCredentialHelper(targetId, 'code-quality-benchmark-use', 'POST', {});
        if (!credential.response.ok || typeof credential.data.token !== 'string') {
          return res.status(credential.response.status).json(credential.data);
        }
        body.bearer_token = credential.data.token;
      } else if (target.auth_mode === 'http_login') {
        body.login_credentials = req.body?.login_credentials;
      }
      const result = await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/performance/runs`, user,
        { method: 'POST', body: JSON.stringify(body) });
      return await reply(res, result, 'Performance benchmark could not be completed.');
    } catch { return res.status(502).json({ detail: 'Performance benchmark could not be completed. Check the target and authentication, then retry.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/performance/runs', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/performance/runs`, user), 'Performance history is unavailable.');
    } catch { return res.status(502).json({ detail: 'Performance history is unavailable.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/performance/runs/:runId', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/performance/runs/${encodeURIComponent(req.params.runId)}`;
      return await reply(res, await forward(path, user), 'Performance run is unavailable.');
    } catch { return res.status(502).json({ detail: 'Performance run is unavailable.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/performance/compare', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/performance/compare`, user,
        { method: 'POST', body: JSON.stringify(req.body) }), 'Performance runs could not be compared.');
    } catch { return res.status(502).json({ detail: 'Performance runs could not be compared.' }); }
  });

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
