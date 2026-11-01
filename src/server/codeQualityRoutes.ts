import type { Express, Response } from 'express';
import { createHmac, randomUUID, timingSafeEqual } from 'node:crypto';
import { RAGService } from './rag';

type AuthUser = { sub: string; email?: string; name?: string } | null;

type RouteDependencies = {
  apiUrl: string;
  internalSecret: string;
  getUser: (response: Response) => AuthUser;
  canAccessProject: (user: AuthUser, projectId: string) => boolean;
  canManageProject?: (user: AuthUser, projectId: string) => boolean;
  isGlobalAdmin: (user: AuthUser) => boolean;
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
  app.get('/api/code-quality/developer-mode', async (_req, res) => {
    const user = authenticated(res); if (!user) return;
    try {
      const upstream = await forward('/code-quality/developer-mode', user);
      const data = await upstream.json().catch(() => ({ detail: 'Developer Mode status is unavailable.' })) as any;
      return res.status(upstream.status).json({ ...data, can_manage: dependencies.isGlobalAdmin(user) });
    } catch { return res.status(502).json({ detail: 'Developer Mode status is unavailable.' }); }
  });

  app.put('/api/code-quality/developer-mode', async (req, res) => {
    const user = authenticated(res); if (!user) return;
    if (!dependencies.isGlobalAdmin(user)) {
      return res.status(403).json({ detail: 'Only the global administrator can change Developer Mode.' });
    }
    if (!req.body || typeof req.body !== 'object' || Array.isArray(req.body)
        || Object.keys(req.body).length !== 1 || typeof req.body.enabled !== 'boolean') {
      return res.status(400).json({ detail: 'Developer Mode settings must contain an enabled boolean.' });
    }
    try {
      const upstream = await forward('/code-quality/developer-mode', user,
        { method: 'PUT', body: JSON.stringify({ enabled: req.body.enabled }) });
      return await reply(res, upstream, 'Developer Mode could not be updated.');
    } catch { return res.status(502).json({ detail: 'Developer Mode could not be updated.' }); }
  });

  const helperAuthorization = (operation: string, targetId: string, projectId?: string) => {
    const now = Math.floor(Date.now() / 1000);
    const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
    const header = encode({ alg: 'HS256', typ: 'JWT' });
    const claims = encode({ aud: 'runtime-helper', origin: dependencies.webOrigin, operation,
      target_id: targetId, ...(projectId ? { project_id: projectId } : {}), exp: now + 120, jti: randomUUID() });
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
  const callGitCredentialHelper = async (projectId: string, targetId: string,
    operation: 'code-quality-github-manage' | 'code-quality-github-fetch',
    method: 'POST' | 'DELETE', body: Record<string, unknown>) => {
    const token = helperAuthorization(operation, targetId, projectId);
    const path = operation === 'code-quality-github-fetch' ? '/code-quality-github-use' : '/code-quality-github';
    const response = await fetch(`${dependencies.helperUrl}${path}`, {
      method,
      headers: { Origin: dependencies.webOrigin, Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, project_id: projectId, target_id: targetId }),
      signal: AbortSignal.timeout(15_000),
    });
    const data = await response.json().catch(() => ({ detail: 'The desktop OS credential helper returned an invalid response.' }));
    return { response, data };
  };
  const callOutlineCredentialHelper = async (projectId: string, targetId: string,
    operation: 'code-quality-outline-manage' | 'code-quality-outline-fetch',
    method: 'POST' | 'DELETE', body: Record<string, unknown>) => {
    const authorization = helperAuthorization(operation, targetId, projectId);
    const path = operation === 'code-quality-outline-fetch' ? '/code-quality-outline-use' : '/code-quality-outline';
    const response = await fetch(dependencies.helperUrl + path, {
      method,
      headers: { Origin: dependencies.webOrigin, Authorization: 'Bearer ' + authorization, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, project_id: projectId, target_id: targetId }),
      signal: AbortSignal.timeout(15_000),
    });
    const data = await response.json().catch(() => ({ detail: 'The desktop OS credential helper returned an invalid response.' }));
    return { response, data };
  };
  const callSonarCredentialHelper = async (projectId: string, targetId: string,
    operation: 'code-quality-sonarqube-manage' | 'code-quality-sonarqube-fetch',
    method: 'POST' | 'DELETE', body: Record<string, unknown>) => {
    const authorization = helperAuthorization(operation, targetId, projectId);
    const path = operation === 'code-quality-sonarqube-fetch' ? '/code-quality-sonarqube-use' : '/code-quality-sonarqube';
    const response = await fetch(dependencies.helperUrl + path, {
      method,
      headers: { Origin: dependencies.webOrigin, Authorization: 'Bearer ' + authorization, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, project_id: projectId, target_id: targetId }),
      signal: AbortSignal.timeout(15_000),
    });
    const data = await response.json().catch(() => ({ detail: 'The desktop OS credential helper returned an invalid response.' }));
    return { response, data };
  };

  app.get('/api/code-quality/projects/:projectId/git-source', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(`/telemetry/git/projects/${encodeURIComponent(req.params.projectId)}`, user), 'Git source settings are unavailable.');
    } catch { return res.status(502).json({ detail: 'Git source settings are unavailable.' }); }
  });

  app.put('/api/code-quality/projects/:projectId/git-source', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const previousResponse = await forward(`/telemetry/git/projects/${encodeURIComponent(req.params.projectId)}`, user);
      const previous = await previousResponse.json().catch(() => ({})) as any;
      const upstream = await forward(`/telemetry/git/projects/${encodeURIComponent(req.params.projectId)}`, user,
        { method: 'PUT', body: JSON.stringify({ repository: req.body?.repository, branch: req.body?.branch }) });
      const result = await upstream.json().catch(() => ({})) as any;
      if (upstream.ok && previous.source?.credential_id
          && previous.source.credential_id !== result.source?.credential_id) {
        await callGitCredentialHelper(req.params.projectId, previous.source.credential_id,
          'code-quality-github-manage', 'DELETE', {}).catch(() => undefined);
      }
      return res.status(upstream.status).json(result);
    } catch { return res.status(502).json({ detail: 'Git source settings could not be saved.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/git-source/credential', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const config = await forward(`/telemetry/git/projects/${encodeURIComponent(req.params.projectId)}`, user);
      const data = await config.json().catch(() => ({})) as any;
      const credentialId = data.source?.credential_id;
      if (!config.ok || typeof credentialId !== 'string') return res.status(config.status || 404).json(data);
      const result = await callGitCredentialHelper(req.params.projectId, credentialId, 'code-quality-github-manage', 'POST',
        { action: 'save', token: req.body?.token });
      return res.status(result.response.status).json(result.data);
    } catch { return res.status(503).json({ detail: 'The GitHub token could not be saved to the OS credential store.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/git-source/credential', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const config = await forward(`/telemetry/git/projects/${encodeURIComponent(req.params.projectId)}`, user);
      const data = await config.json().catch(() => ({})) as any;
      const credentialId = data.source?.credential_id;
      if (!config.ok || typeof credentialId !== 'string') return res.status(config.status || 404).json(data);
      const result = await callGitCredentialHelper(req.params.projectId, credentialId, 'code-quality-github-manage', 'POST', { action: 'status' });
      return res.status(result.response.status).json(result.data);
    } catch { return res.status(503).json({ detail: 'The OS credential store is unavailable.' }); }
  });

  app.delete('/api/code-quality/projects/:projectId/git-source/credential', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const config = await forward(`/telemetry/git/projects/${encodeURIComponent(req.params.projectId)}`, user);
      const data = await config.json().catch(() => ({})) as any;
      const credentialId = data.source?.credential_id;
      if (!config.ok || typeof credentialId !== 'string') return res.status(config.status || 404).json(data);
      const result = await callGitCredentialHelper(req.params.projectId, credentialId, 'code-quality-github-manage', 'DELETE', {});
      return res.status(result.response.status).json(result.data);
    } catch { return res.status(503).json({ detail: 'The GitHub token could not be removed from the OS credential store.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/git-source/sync', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const config = await forward(`/telemetry/git/projects/${encodeURIComponent(req.params.projectId)}`, user);
      const data = await config.json().catch(() => ({})) as any;
      const credentialId = data.source?.credential_id;
      if (!config.ok || typeof credentialId !== 'string') return res.status(config.status || 404).json(data);
      const credentialStatus = await callGitCredentialHelper(req.params.projectId, credentialId,
        'code-quality-github-manage', 'POST', { action: 'status' });
      if (!credentialStatus.response.ok) return res.status(credentialStatus.response.status).json(credentialStatus.data);
      let token: string | undefined;
      if (credentialStatus.data.saved === true) {
        const credential = await callGitCredentialHelper(req.params.projectId, credentialId,
          'code-quality-github-fetch', 'POST', {});
        if (!credential.response.ok || typeof credential.data.token !== 'string') return res.status(credential.response.status).json(credential.data);
        token = credential.data.token;
      }
      const result = await forward(`/telemetry/git/projects/${encodeURIComponent(req.params.projectId)}/sync`, user,
        { method: 'POST', body: JSON.stringify(token ? { token } : {}) });
      return await reply(res, result, 'Git source synchronization failed.');
    } catch { return res.status(502).json({ detail: 'Git source synchronization failed. Check the repository and retry.' }); }
  });

  app.post('/api/code-quality/github/webhook', async (req, res) => {
    const secret = process.env.CODE_QUALITY_GITHUB_WEBHOOK_SECRET || '';
    const rawBody = (req as any).rawBody as Buffer | undefined;
    if (!Buffer.isBuffer(rawBody) || rawBody.length > 1_000_000) return res.status(413).json({ detail: 'GitHub webhook payload exceeds the configured limit.' });
    if (secret.length < 32) return res.status(503).json({ detail: 'GitHub webhook verification is not configured.' });
    const signature = req.header('x-hub-signature-256') || '';
    const expected = `sha256=${createHmac('sha256', secret).update(rawBody).digest('hex')}`;
    const signatureBytes = Buffer.from(signature);
    const expectedBytes = Buffer.from(expected);
    if (signatureBytes.length !== expectedBytes.length || !timingSafeEqual(signatureBytes, expectedBytes)) {
      return res.status(401).json({ detail: 'GitHub webhook verification failed.' });
    }
    if (req.header('x-github-event') !== 'push') return res.status(202).json({ accepted: true, ignored: true });
    try {
      const payload = JSON.parse(rawBody.toString('utf8'));
      const repository = payload.repository?.full_name;
      const ref = payload.ref;
      if (typeof repository !== 'string' || typeof ref !== 'string' || payload.deleted === true) {
        return res.status(202).json({ accepted: true, ignored: true });
      }
      const targetResponse = await forward(`/telemetry/git/webhook-target?repository=${encodeURIComponent(repository)}`, null);
      const target = await targetResponse.json().catch(() => ({})) as any;
      if (!targetResponse.ok || ref !== `refs/heads/${target.branch}`) return res.status(202).json({ accepted: true, ignored: true });
      const credentialStatus = await callGitCredentialHelper(target.project_id, target.credential_id,
        'code-quality-github-manage', 'POST', { action: 'status' });
      if (!credentialStatus.response.ok) return res.status(503).json({ detail: 'Verified webhook could not start synchronization.' });
      let token: string | undefined;
      if (credentialStatus.data.saved === true) {
        const credential = await callGitCredentialHelper(target.project_id, target.credential_id, 'code-quality-github-fetch', 'POST', {});
        if (!credential.response.ok || typeof credential.data.token !== 'string') return res.status(503).json({ detail: 'Verified webhook could not start synchronization.' });
        token = credential.data.token;
      }
      const sync = await forward(`/telemetry/git/projects/${encodeURIComponent(target.project_id)}/sync`,
        { sub: target.user_id }, { method: 'POST', body: JSON.stringify(token ? { token } : {}) });
      if (!sync.ok) return res.status(503).json({ detail: 'Verified webhook synchronization failed.' });
      return res.status(202).json({ accepted: true });
    } catch { return res.status(503).json({ detail: 'Verified webhook synchronization failed.' }); }
  });

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
      const projects = data.filter((item: any) => typeof item.project_id === 'string' && typeof item.revision_id === 'string'
        && dependencies.canAccessProject(user, item.project_id))
        .map((item: any) => ({ project_id: item.project_id, revision_id: item.revision_id,
          commit_sha: item.commit_sha, source_ref: item.source_ref, state: item.state, updated_at: item.updated_at }));
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

  app.get('/api/code-quality/projects/:projectId/exclusions', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/exclusions`;
      return await reply(res, await forward(path, user), 'Assessment exclusions are unavailable.');
    } catch {
      return res.status(502).json({ detail: 'Assessment exclusions are unavailable.' });
    }
  });

  app.put('/api/code-quality/projects/:projectId/exclusions', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/exclusions`;
      return await reply(res, await forward(path, user, {
        method: 'PUT', body: JSON.stringify({ patterns: req.body?.patterns }),
      }), 'Assessment exclusions could not be saved.');
    } catch {
      return res.status(502).json({ detail: 'Assessment exclusions could not be saved.' });
    }
  });

  app.get('/api/code-quality/projects/:projectId/business-rules', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/business-rules`, user), 'Business rules are unavailable.');
    } catch { return res.status(502).json({ detail: 'Business rules are unavailable.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/business-rules', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/business-rules`;
      return await reply(res, await forward(path, user, { method: 'POST', body: JSON.stringify(req.body) }), 'Business rule could not be saved.');
    } catch { return res.status(502).json({ detail: 'Business rule could not be saved.' }); }
  });

  app.put('/api/code-quality/projects/:projectId/business-rules/:ruleId', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/business-rules/${encodeURIComponent(req.params.ruleId)}`;
      return await reply(res, await forward(path, user, { method: 'PUT', body: JSON.stringify(req.body) }), 'Business rule could not be updated.');
    } catch { return res.status(502).json({ detail: 'Business rule could not be updated.' }); }
  });

  app.delete('/api/code-quality/projects/:projectId/business-rules/:ruleId', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/business-rules/${encodeURIComponent(req.params.ruleId)}`;
      return await reply(res, await forward(path, user, { method: 'DELETE' }), 'Business rule could not be deleted.');
    } catch { return res.status(502).json({ detail: 'Business rule could not be deleted.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/outline', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const base = '/code-quality/projects/' + encodeURIComponent(req.params.projectId) + '/outline';
      const upstream = await forward(base, user);
      const data = await upstream.json().catch(() => ({})) as any;
      const credentialId = data.connection?.credential_id;
      let credential: Record<string, unknown> = { saved: false };
      if (upstream.ok && typeof credentialId === 'string') {
        try {
          const result = await callOutlineCredentialHelper(req.params.projectId, credentialId,
            'code-quality-outline-manage', 'POST', { action: 'status' });
          credential = result.response.ok ? result.data : { saved: false, detail: result.data.detail };
        } catch {
          credential = { saved: false, detail: 'The host OS credential helper is unavailable. Restart the local helper and retry.' };
        }
      }
      return res.status(upstream.status).json({ ...data, credential });
    } catch { return res.status(502).json({ detail: 'Outline settings are unavailable.' }); }
  });

  app.put('/api/code-quality/projects/:projectId/outline', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const base = '/code-quality/projects/' + encodeURIComponent(req.params.projectId) + '/outline';
      const previousResponse = await forward(base, user);
      const previous = await previousResponse.json().catch(() => ({})) as any;
      const endpointChanged = !!previous.connection
        && req.body?.instance_url !== previous.connection.instance_url;
      if (endpointChanged && !(typeof req.body?.token === 'string' && req.body.token.trim())) {
        return res.status(422).json({ detail: 'Enter the API token for the new Outline instance before saving it.' });
      }
      const credentialId = !endpointChanged && typeof previous.connection?.credential_id === 'string'
        ? previous.connection.credential_id : randomUUID();
      const upstream = await forward(base, user, { method: 'PUT', body: JSON.stringify({
        instance_url: req.body?.instance_url, collection_id: req.body?.collection_id, credential_id: credentialId,
      }) });
      const data = await upstream.json().catch(() => ({})) as any;
      if (!upstream.ok || !data.connection) return res.status(upstream.status).json(data);
      const submittedToken = typeof req.body?.token === 'string' ? req.body.token : '';
      if (submittedToken.trim()) {
        const saved = await callOutlineCredentialHelper(req.params.projectId, credentialId,
          'code-quality-outline-manage', 'POST', { action: 'save', token: submittedToken });
        if (!saved.response.ok) {
          if (previous.connection) {
            await forward(base, user, { method: 'PUT', body: JSON.stringify(previous.connection) }).catch(() => undefined);
          } else {
            await forward(base, user, { method: 'DELETE' }).catch(() => undefined);
          }
          return res.status(saved.response.status).json(saved.data);
        }
      }
      if (previous.connection?.credential_id && previous.connection.credential_id !== credentialId) {
        await callOutlineCredentialHelper(req.params.projectId, previous.connection.credential_id,
          'code-quality-outline-manage', 'DELETE', {}).catch(() => undefined);
      }
      const status = await callOutlineCredentialHelper(req.params.projectId, credentialId,
        'code-quality-outline-manage', 'POST', { action: 'status' });
      return res.status(upstream.status).json({ ...data, credential: status.response.ok ? status.data : { saved: false, detail: status.data.detail } });
    } catch { return res.status(502).json({ detail: 'Outline settings could not be saved.' }); }
  });

  app.delete('/api/code-quality/projects/:projectId/outline', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const base = '/code-quality/projects/' + encodeURIComponent(req.params.projectId) + '/outline';
      const previousResponse = await forward(base, user);
      const previous = await previousResponse.json().catch(() => ({})) as any;
      if (previousResponse.ok && previous.connection?.credential_id) {
        const removed = await callOutlineCredentialHelper(req.params.projectId, previous.connection.credential_id,
          'code-quality-outline-manage', 'DELETE', {});
        if (!removed.response.ok) return res.status(removed.response.status).json(removed.data);
      }
      const upstream = await forward(base, user, { method: 'DELETE' });
      const data = await upstream.json().catch(() => ({})) as any;
      return res.status(upstream.status).json(data);
    } catch { return res.status(502).json({ detail: 'Outline connection could not be removed.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/outline/collections', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const base = '/code-quality/projects/' + encodeURIComponent(req.params.projectId) + '/outline';
      const configResponse = await forward(base, user);
      const config = await configResponse.json().catch(() => ({})) as any;
      const credentialId = config.connection?.credential_id;
      let outlineToken = typeof req.body?.token === 'string' ? req.body.token : '';
      const requestedUrl = req.body?.instance_url;
      const savedInstanceMatches = requestedUrl === config.connection?.instance_url;
      if (!outlineToken.trim() && savedInstanceMatches && typeof credentialId === 'string') {
        const result = await callOutlineCredentialHelper(req.params.projectId, credentialId,
          'code-quality-outline-fetch', 'POST', {});
        if (!result.response.ok) return res.status(result.response.status).json(result.data);
        outlineToken = typeof result.data.token === 'string' ? result.data.token : '';
      }
      if (!outlineToken.trim()) return res.status(422).json({ detail: 'Enter the API token for this Outline instance.' });
      const upstream = await forward(base + '/collections', user,
        { method: 'POST', body: JSON.stringify({ instance_url: req.body?.instance_url || config.connection?.instance_url, token: outlineToken }) });
      return await reply(res, upstream, 'Outline collections could not be loaded.');
    } catch { return res.status(502).json({ detail: 'Outline collections could not be loaded.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/outline/documents', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const base = '/code-quality/projects/' + encodeURIComponent(req.params.projectId) + '/outline';
      const configResponse = await forward(base, user);
      const config = await configResponse.json().catch(() => ({})) as any;
      const credentialId = config.connection?.credential_id;
      if (!configResponse.ok || !config.connection) return res.status(configResponse.status).json(config);
      if (typeof credentialId !== 'string') return res.status(503).json({ detail: 'The saved Outline credential is unavailable.' });
      const credential = await callOutlineCredentialHelper(req.params.projectId, credentialId,
        'code-quality-outline-fetch', 'POST', {});
      if (!credential.response.ok || typeof credential.data.token !== 'string') {
        return res.status(credential.response.status || 503).json({ detail: credential.data.detail || 'The saved Outline token could not be read from the OS credential store.' });
      }
      const documents = await forward(base + '/documents', user, {
        method: 'POST', body: JSON.stringify({ token: credential.data.token }),
      });
      return await reply(res, documents, 'Outline documents could not be loaded.');
    } catch { return res.status(502).json({ detail: 'Outline documents could not be loaded.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/outline/credential', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const base = '/code-quality/projects/' + encodeURIComponent(req.params.projectId) + '/outline';
      const config = await forward(base, user);
      const data = await config.json().catch(() => ({})) as any;
      const credentialId = data.connection?.credential_id;
      if (!config.ok || typeof credentialId !== 'string') return res.status(config.status || 404).json(data);
      const result = await callOutlineCredentialHelper(req.params.projectId, credentialId,
        'code-quality-outline-manage', 'POST', { action: 'save', token: req.body?.token });
      return res.status(result.response.status).json(result.data);
    } catch { return res.status(503).json({ detail: 'The Outline token could not be saved to the OS credential store.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/external-analyzers', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const upstream = await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/external-analyzers`, user);
      const data = await upstream.json().catch(() => ({})) as any;
      const credentialId = data.settings?.sonarqube?.credential_id;
      if (!upstream.ok || typeof credentialId !== 'string') return res.status(upstream.status).json(data);
      const status = await callSonarCredentialHelper(req.params.projectId, credentialId,
        'code-quality-sonarqube-manage', 'POST', { action: 'status' });
      return res.status(upstream.status).json({ ...data, sonarqube_credential: status.response.ok ? status.data : { saved: false, detail: status.data.detail } });
    } catch { return res.status(502).json({ detail: 'External analyzer settings are unavailable.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/external-analyzers/worker/check', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/external-analyzers/worker/check`;
      return await reply(res, await forward(path, user, {
        method: 'POST', body: JSON.stringify({ worker: req.body?.worker || {} }),
      }), 'Analyzer worker connection could not be verified.');
    } catch {
      return res.status(502).json({ detail: 'Analyzer worker connection could not be verified.' });
    }
  });

  app.post('/api/code-quality/projects/:projectId/external-analyzers/worker/fingerprint', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/external-analyzers/worker/fingerprint`;
      return await reply(res, await forward(path, user, {
        method: 'POST', body: JSON.stringify({ endpoint: req.body?.endpoint }),
      }), 'Analyzer worker fingerprint could not be fetched.');
    } catch {
      return res.status(502).json({ detail: 'Analyzer worker fingerprint could not be fetched.' });
    }
  });

  app.post('/api/code-quality/projects/:projectId/external-analyzers/sonarqube/check', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const base = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/external-analyzers`;
      const configResponse = await forward(base, user);
      const config = await configResponse.json().catch(() => ({})) as any;
      const credentialId = config.settings?.sonarqube?.credential_id;
      if (!configResponse.ok) return res.status(configResponse.status).json(config);
      if (typeof credentialId !== 'string') {
        return res.status(409).json({ detail: 'Save a SonarQube token in the OS credential store before testing access.' });
      }
      const credential = await callSonarCredentialHelper(req.params.projectId, credentialId,
        'code-quality-sonarqube-fetch', 'POST', {});
      if (!credential.response.ok || typeof credential.data.token !== 'string') {
        return res.status(credential.response.status || 503).json({
          detail: credential.data.detail || 'The saved SonarQube token could not be read from the OS credential store.',
        });
      }
      const path = `${base}/sonarqube/check`;
      const upstream = await forward(path, user, {
        method: 'POST',
        body: JSON.stringify({
          server_url: req.body?.server_url,
          project_key: req.body?.project_key,
          worker: req.body?.worker,
          sonarqube_token: credential.data.token,
        }),
      });
      return await reply(res, upstream, 'SonarQube project access could not be verified.');
    } catch {
      return res.status(502).json({ detail: 'SonarQube project access could not be verified.' });
    }
  });

  app.put('/api/code-quality/projects/:projectId/external-analyzers', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const base = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/external-analyzers`;
      const oldResponse = await forward(base, user);
      const oldData = await oldResponse.json().catch(() => ({})) as any;
      const credentialId = req.body?.sonarqube?.credential_id || oldData.settings?.sonarqube?.credential_id || randomUUID();
      const payload = { ...req.body, sonarqube: { ...req.body?.sonarqube, credential_id: credentialId } };
      const upstream = await forward(base, user, { method: 'PUT', body: JSON.stringify(payload) });
      const data = await upstream.json().catch(() => ({})) as any;
      if (!upstream.ok) return res.status(upstream.status).json(data);
      if (typeof req.body?.sonarqube?.token === 'string' && req.body.sonarqube.token.trim()) {
        const saved = await callSonarCredentialHelper(req.params.projectId, credentialId,
          'code-quality-sonarqube-manage', 'POST', { action: 'save', token: req.body.sonarqube.token });
        if (!saved.response.ok) return res.status(saved.response.status).json(saved.data);
      }
      if (oldData.settings?.sonarqube?.credential_id && oldData.settings.sonarqube.credential_id !== credentialId) {
        await callSonarCredentialHelper(req.params.projectId, oldData.settings.sonarqube.credential_id,
          'code-quality-sonarqube-manage', 'DELETE', {}).catch(() => undefined);
      }
      const status = await callSonarCredentialHelper(req.params.projectId, credentialId,
        'code-quality-sonarqube-manage', 'POST', { action: 'status' });
      return res.status(upstream.status).json({ ...data, sonarqube_credential: status.response.ok ? status.data : { saved: false, detail: status.data.detail } });
    } catch { return res.status(502).json({ detail: 'External analyzer settings could not be saved.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/external-analyzers/credential', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const config = await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/external-analyzers`, user);
      const data = await config.json().catch(() => ({})) as any;
      const credentialId = data.settings?.sonarqube?.credential_id;
      if (!config.ok || typeof credentialId !== 'string') return res.status(config.status || 404).json(data);
      const result = await callSonarCredentialHelper(req.params.projectId, credentialId,
        'code-quality-sonarqube-manage', 'POST', { action: 'save', token: req.body?.token });
      return res.status(result.response.status).json(result.data);
    } catch { return res.status(503).json({ detail: 'The SonarQube token could not be saved to the OS credential store.' }); }
  });

  app.delete('/api/code-quality/projects/:projectId/external-analyzers/credential', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const config = await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/external-analyzers`, user);
      const data = await config.json().catch(() => ({})) as any;
      const credentialId = data.settings?.sonarqube?.credential_id;
      if (!config.ok || typeof credentialId !== 'string') return res.status(config.status || 404).json(data);
      const result = await callSonarCredentialHelper(req.params.projectId, credentialId,
        'code-quality-sonarqube-manage', 'DELETE', {});
      return res.status(result.response.status).json(result.data);
    } catch { return res.status(503).json({ detail: 'The SonarQube token could not be removed from the OS credential store.' }); }
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

  app.delete('/api/code-quality/projects/:projectId/reports/:reportId', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId);
      if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/reports/${encodeURIComponent(req.params.reportId)}`;
      return await reply(res, await forward(path, user, { method: 'DELETE' }), 'Assessment report could not be removed.');
    } catch {
      return res.status(502).json({ detail: 'Assessment report could not be removed.' });
    }
  });

  app.get('/api/code-quality/projects/:projectId/revisions/:revisionId/source', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId);
      if (!user) return;
      const filePath = typeof req.query.file_path === 'string' ? req.query.file_path : '';
      const line = typeof req.query.line === 'string' ? req.query.line : '1';
      const startLine = typeof req.query.start_line === 'string' ? req.query.start_line : '';
      if (!filePath || !/^\d+$/.test(line) || (startLine && !/^\d+$/.test(startLine))) {
        return res.status(400).json({ detail: 'Select a valid source file and line.' });
      }
      const query = new URLSearchParams({ file_path: filePath, line });
      if (startLine) query.set('start_line', startLine);
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/source?${query.toString()}`;
      return await reply(res, await forward(path, user), 'Revision source is unavailable.');
    } catch {
      return res.status(502).json({ detail: 'Revision source is unavailable.' });
    }
  });

  app.get('/api/code-quality/projects/:projectId/revisions/:revisionId/directories', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId);
      if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/directories`;
      return await reply(res, await forward(path, user), 'Revision directories are unavailable.');
    } catch {
      return res.status(502).json({ detail: 'Revision directories are unavailable.' });
    }
  });

  app.delete('/api/code-quality/projects/:projectId/revisions/:revisionId', async (req, res) => {
    const user = allowed(res, req.params.projectId);
    if (!user) return;
    if (!dependencies.canManageProject?.(user, req.params.projectId)) {
      return res.status(403).json({ detail: 'Only a project administrator can remove a revision.' });
    }
    try {
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}`;
      const upstream = await forward(path, user, { method: 'DELETE' });
      if (upstream.ok) {
        for (const file of [...RAGService.getFiles()]) {
          if (file.projectId === req.params.projectId && file.revisionId === req.params.revisionId) await RAGService.deleteFile(file.id);
        }
      }
      return await reply(res, upstream, 'Revision could not be removed.');
    } catch { return res.status(502).json({ detail: 'Revision removal could not complete. Retry after checking API availability.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/revisions/:revisionId/precheck', async (req, res) => {
    const user = allowed(res, req.params.projectId);
    if (!user) return;
    try {
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/precheck`;
      return await reply(res, await forward(path, user), 'The project index could not be checked.');
    } catch {
      return res.status(502).json({ detail: 'The project index could not be checked. Retry after checking API availability.' });
    }
  });

  app.post('/api/code-quality/projects/:projectId/revisions/:revisionId/assess', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId);
      if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/assess`;
      const projectPath = '/code-quality/projects/' + encodeURIComponent(req.params.projectId) + '/outline';
      const configResponse = await forward(projectPath, user);
      const config = await configResponse.json().catch(() => ({})) as any;
      let outlineToken: string | undefined;
      let outlineCredentialError: string | undefined;
      const credentialId = config.connection?.credential_id;
      if (configResponse.ok && typeof credentialId === 'string') {
        try {
          const result = await callOutlineCredentialHelper(req.params.projectId, credentialId,
            'code-quality-outline-fetch', 'POST', {});
          if (result.response.ok && typeof result.data.token === 'string') outlineToken = result.data.token;
          else outlineCredentialError = result.data.detail || 'The saved Outline token could not be read from the OS credential store.';
        } catch {
          outlineCredentialError = 'The host OS credential helper is unavailable. Restart the local helper and retry.';
        }
      }
      const externalConfigResponse = await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/external-analyzers`, user);
      const externalConfig = await externalConfigResponse.json().catch(() => ({})) as any;
      let sonarqubeToken: string | undefined;
      const sonar = externalConfig.settings?.sonarqube;
      if (externalConfigResponse.ok && sonar?.enabled === true && typeof sonar.credential_id === 'string') {
        try {
          const credential = await callSonarCredentialHelper(req.params.projectId, sonar.credential_id,
            'code-quality-sonarqube-fetch', 'POST', {});
          if (credential.response.ok && typeof credential.data.token === 'string') sonarqubeToken = credential.data.token;
        } catch { /* The API reports an unavailable integration without failing other analyzers. */ }
      }
      return await reply(res, await forward(path, user, {
        method: 'POST', body: JSON.stringify({
          ...(outlineToken ? { outline_token: outlineToken } : {}),
          ...(outlineCredentialError ? { outline_credential_error: outlineCredentialError } : {}),
          ...(sonarqubeToken ? { sonarqube_token: sonarqubeToken } : {}),
        }),
      }), 'Assessment could not be completed. Retry the request.');
    } catch {
      return res.status(502).json({ detail: 'Assessment could not be completed. Retry the request.' });
    }
  });

  app.get('/api/code-quality/rules', async (_req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      return await reply(res, await forward('/code-quality/rules', user), 'Custom rules are unavailable.');
    } catch { return res.status(502).json({ detail: 'Custom rules are unavailable.' }); }
  });

  app.post('/api/code-quality/rules', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      return await reply(res, await forward('/code-quality/rules', user, { method: 'POST', body: JSON.stringify(req.body) }), 'Rule set could not be saved.');
    } catch { return res.status(502).json({ detail: 'Rule set could not be saved.' }); }
  });

  app.put('/api/code-quality/rules/:ruleId', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      return await reply(res, await forward(`/code-quality/rules/${encodeURIComponent(req.params.ruleId)}`, user,
        { method: 'PUT', body: JSON.stringify(req.body) }), 'Rule set could not be updated.');
    } catch { return res.status(502).json({ detail: 'Rule set could not be updated.' }); }
  });

  app.patch('/api/code-quality/rules/:ruleId/enabled', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      return await reply(res, await forward(`/code-quality/rules/${encodeURIComponent(req.params.ruleId)}/enabled`, user,
        { method: 'PATCH', body: JSON.stringify(req.body) }), 'Rule state could not be changed.');
    } catch { return res.status(502).json({ detail: 'Rule state could not be changed.' }); }
  });

  app.delete('/api/code-quality/rules/:ruleId', async (req, res) => {
    try {
      const user = authenticated(res); if (!user) return;
      return await reply(res, await forward(`/code-quality/rules/${encodeURIComponent(req.params.ruleId)}`, user,
        { method: 'DELETE' }), 'Rule set could not be deleted.');
    } catch { return res.status(502).json({ detail: 'Rule set could not be deleted.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/revisions/:revisionId/rules/preview', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/rules/preview`;
      return await reply(res, await forward(path, user, { method: 'POST', body: JSON.stringify(req.body) }), 'Rule preview could not complete. Retry.');
    } catch { return res.status(502).json({ detail: 'Rule preview could not complete. Check the project revision and retry.' }); }
  });
}
