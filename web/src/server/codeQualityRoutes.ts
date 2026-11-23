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
  /** Operator or admin of the project: may change settings and start work such as benchmarks. */
  canOperateProject?: (user: AuthUser, projectId: string) => boolean;
  isGlobalAdmin: (user: AuthUser) => boolean;
  helperUrl: string;
  webOrigin: string;
  /** How often to re-sync Outline business rules for every connected project; 0 disables it. */
  ruleSyncIntervalMs?: number;
  /** How often to fetch every connected Git branch, catching missed webhooks; 0 disables it. */
  gitReconcileIntervalMs?: number;
};

/** Signed requests to the desktop OS credential helper (Bearer tokens of saved benchmark targets). */
export function createCredentialHelper(dependencies: { helperUrl: string; webOrigin: string; internalSecret: string }) {
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
  return { helperAuthorization, callCredentialHelper };
}

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

  const { helperAuthorization, callCredentialHelper } = createCredentialHelper(dependencies);
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

  // Fetch the branch with the saved token (from the desktop helper) and sync it as the configuring user.
  // The webhook and the scheduled reconcile share this; neither trusts a pushed commit on its own.
  type GitTarget = { project_id: string; user_id: string; credential_id: string; branch?: string };
  const syncGitTarget = async (target: GitTarget): Promise<boolean> => {
    const credentialStatus = await callGitCredentialHelper(target.project_id, target.credential_id,
      'code-quality-github-manage', 'POST', { action: 'status' });
    if (!credentialStatus.response.ok) return false;
    let token: string | undefined;
    if (credentialStatus.data.saved === true) {
      const credential = await callGitCredentialHelper(target.project_id, target.credential_id, 'code-quality-github-fetch', 'POST', {});
      if (!credential.response.ok || typeof credential.data.token !== 'string') return false;
      token = credential.data.token;
    }
    const sync = await forward(`/telemetry/git/projects/${encodeURIComponent(target.project_id)}/sync`,
      { sub: target.user_id }, { method: 'POST', body: JSON.stringify(token ? { token } : {}) });
    return sync.ok;
  };
  const runScheduledGitReconcile = async () => {
    try {
      const listed = await forward('/telemetry/git/sources', null);
      if (!listed.ok) return;
      const { sources = [] } = await listed.json().catch(() => ({})) as { sources?: GitTarget[] };
      for (const source of sources) {
        // A stopped desktop helper or unreachable GitHub only skips this round; the next one retries.
        await syncGitTarget(source).catch(() => false);
      }
    } catch { /* API unavailable: try again next round */ }
  };
  const gitReconcileInterval = dependencies.gitReconcileIntervalMs ?? 10 * 60_000;
  if (gitReconcileInterval > 0) {
    setTimeout(() => void runScheduledGitReconcile(), 60_000).unref?.();
    setInterval(() => void runScheduledGitReconcile(), gitReconcileInterval).unref?.();
  }

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
      if (!await syncGitTarget(target)) return res.status(503).json({ detail: 'Verified webhook synchronization failed.' });
      return res.status(202).json({ accepted: true });
    } catch { return res.status(503).json({ detail: 'Verified webhook synchronization failed.' }); }
  });

  // ----- Setup Performance: project-scoped targets and background benchmarks -----
  // Starting load or changing targets is an operator action; viewers may only read results.
  const operator = (res: Response, projectId: string) => {
    const user = allowed(res, projectId);
    if (!user) return null;
    if (dependencies.canOperateProject && !dependencies.canOperateProject(user, projectId)) {
      res.status(403).json({ detail: 'Operator access is required to configure or run benchmarks.' });
      return null;
    }
    return user;
  };
  const perfPath = (projectId: string, suffix = '') => `/code-quality/projects/${encodeURIComponent(projectId)}/performance${suffix}`;
  const ownedTarget = async (projectId: string, targetId: string, user: AuthUser) => {
    const upstream = await forward(perfPath(projectId, `/targets/${encodeURIComponent(targetId)}`), user);
    const data = await upstream.json().catch(() => ({})) as any;
    return { upstream, target: upstream.ok ? data.target : null, data };
  };

  app.get('/api/code-quality/projects/:projectId/performance/targets', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(perfPath(req.params.projectId, '/targets'), user), 'Performance targets are unavailable.');
    } catch { return res.status(502).json({ detail: 'Performance targets are unavailable.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/performance/targets', async (req, res) => {
    try {
      const user = operator(res, req.params.projectId); if (!user) return;
      const previous = typeof req.body?.target_id === 'string' ? await ownedTarget(req.params.projectId, req.body.target_id, user) : null;
      const saved = await forward(perfPath(req.params.projectId, '/targets'), user, { method: 'POST', body: JSON.stringify(req.body ?? {}) });
      const data = await saved.json().catch(() => ({ detail: 'Performance target could not be saved.' })) as any;
      // A target that no longer uses a Bearer token must not keep one in the OS credential store.
      if (saved.ok && previous?.target?.auth_mode === 'bearer' && data.target?.auth_mode !== 'bearer') {
        await callCredentialHelper(data.target.target_id, 'code-quality-bearer-manage', 'DELETE', {}).catch(() => undefined);
      }
      return res.status(saved.status).json(data);
    } catch { return res.status(502).json({ detail: 'Performance target could not be saved.' }); }
  });

  app.delete('/api/code-quality/projects/:projectId/performance/targets/:targetId', async (req, res) => {
    try {
      const user = operator(res, req.params.projectId); if (!user) return;
      const owned = await ownedTarget(req.params.projectId, req.params.targetId, user);
      if (!owned.upstream.ok) return res.status(owned.upstream.status).json(owned.data);
      if (owned.target?.auth_mode === 'bearer') {
        const credential = await callCredentialHelper(req.params.targetId, 'code-quality-bearer-manage', 'DELETE', {});
        if (!credential.response.ok) return res.status(credential.response.status).json(credential.data);
      }
      return await reply(res, await forward(perfPath(req.params.projectId, `/targets/${encodeURIComponent(req.params.targetId)}`), user,
        { method: 'DELETE' }), 'Performance target could not be removed.');
    } catch { return res.status(502).json({ detail: 'Performance target could not be removed.' }); }
  });

  const credentialRoute = (method: 'get' | 'post' | 'delete', action: (targetId: string, body: any) => Promise<{ response: globalThis.Response; data: any }>, fallback: string) => {
    app[method]('/api/code-quality/projects/:projectId/performance/targets/:targetId/credential', async (req, res) => {
      try {
        const user = method === 'get' ? allowed(res, req.params.projectId) : operator(res, req.params.projectId); if (!user) return;
        const owned = await ownedTarget(req.params.projectId, req.params.targetId, user);
        if (!owned.upstream.ok) return res.status(owned.upstream.status).json(owned.data);
        const credential = await action(req.params.targetId, req.body);
        return res.status(credential.response.status).json(credential.data);
      } catch { return res.status(503).json({ detail: fallback }); }
    });
  };
  credentialRoute('get', (id) => callCredentialHelper(id, 'code-quality-bearer-manage', 'POST', { action: 'status' }),
    'The desktop OS credential helper is unavailable. Start or restart Developer Assistant and retry.');
  credentialRoute('post', (id, body) => callCredentialHelper(id, 'code-quality-bearer-manage', 'POST', { action: 'save', token: body?.token }),
    'The Bearer token could not be saved to the OS credential store.');
  credentialRoute('delete', (id) => callCredentialHelper(id, 'code-quality-bearer-manage', 'DELETE', {}),
    'The Bearer token could not be removed from the OS credential store.');

  app.get('/api/code-quality/projects/:projectId/revisions/:revisionId/performance/routes', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/revisions/${encodeURIComponent(req.params.revisionId)}/performance/routes`;
      return await reply(res, await forward(path, user), 'Sampled Runtime routes are unavailable.');
    } catch { return res.status(502).json({ detail: 'Sampled Runtime routes are unavailable.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/performance/runs', async (req, res) => {
    try {
      const user = operator(res, req.params.projectId); if (!user) return;
      const targetId = String(req.body?.target_id || '');
      const owned = await ownedTarget(req.params.projectId, targetId, user);
      if (!owned.upstream.ok) return res.status(owned.upstream.status).json(owned.data);
      const body: Record<string, unknown> = { target_id: targetId, routes: req.body?.routes, revision_id: req.body?.revision_id };
      if (owned.target.auth_mode === 'bearer') {
        const credential = await callCredentialHelper(targetId, 'code-quality-benchmark-use', 'POST', {});
        if (!credential.response.ok || typeof credential.data.token !== 'string') {
          return res.status(credential.response.status === 200 ? 503 : credential.response.status).json(credential.data);
        }
        body.bearer_token = credential.data.token;
      } else if (owned.target.auth_mode === 'http_login') {
        body.login_credentials = req.body?.login_credentials;
      }
      // Returns at once (202): the benchmark runs in the background and the browser polls it.
      return await reply(res, await forward(perfPath(req.params.projectId, '/runs'), user, { method: 'POST', body: JSON.stringify(body) }),
        'Performance benchmark could not be started.');
    } catch { return res.status(502).json({ detail: 'Performance benchmark could not be started. Check the target and authentication, then retry.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/performance/runs', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(perfPath(req.params.projectId, '/runs'), user), 'Performance history is unavailable.');
    } catch { return res.status(502).json({ detail: 'Performance history is unavailable.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/performance/runs/:runId', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(perfPath(req.params.projectId, `/runs/${encodeURIComponent(req.params.runId)}`), user),
        'Performance run is unavailable.');
    } catch { return res.status(502).json({ detail: 'Performance run is unavailable.' }); }
  });

  app.delete('/api/code-quality/projects/:projectId/performance/runs/:runId', async (req, res) => {
    try {
      const user = operator(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(perfPath(req.params.projectId, `/runs/${encodeURIComponent(req.params.runId)}`), user,
        { method: 'DELETE' }), 'The benchmark run could not be cancelled or deleted.');
    } catch { return res.status(502).json({ detail: 'The benchmark run could not be cancelled or deleted.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/performance/runs/:runId/traces', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(perfPath(req.params.projectId, `/runs/${encodeURIComponent(req.params.runId)}/traces`), user),
        'Traced benchmark requests are unavailable.');
    } catch { return res.status(502).json({ detail: 'Traced benchmark requests are unavailable.' }); }
  });

  app.get('/api/code-quality/projects/:projectId/performance/runs/:runId/script', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const upstream = await forward(perfPath(req.params.projectId, `/runs/${encodeURIComponent(req.params.runId)}/script`), user);
      if (!upstream.ok) return await reply(res, upstream, 'The k6 script could not be exported.');
      res.setHeader('Content-Type', 'text/javascript; charset=utf-8');
      res.setHeader('Content-Disposition', `attachment; filename="benchmark-${String(req.params.runId).replace(/[^a-f0-9-]/gi, '').slice(0, 36)}.js"`);
      return res.status(200).send(await upstream.text());
    } catch { return res.status(502).json({ detail: 'The k6 script could not be exported.' }); }
  });

  app.post('/api/code-quality/projects/:projectId/performance/compare', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      return await reply(res, await forward(perfPath(req.params.projectId, '/compare'), user,
        { method: 'POST', body: JSON.stringify(req.body ?? {}) }), 'Performance runs could not be compared.');
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
          commit_sha: item.commit_sha, source_ref: item.source_ref, state: item.state, updated_at: item.updated_at,
          dirty: item.dirty === true }));
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

  // Routes the project declares intentionally public for the route-authorization check.
  app.get('/api/code-quality/projects/:projectId/public-routes', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/public-routes`;
      return await reply(res, await forward(path, user), 'Public routes are unavailable.');
    } catch {
      return res.status(502).json({ detail: 'Public routes are unavailable.' });
    }
  });

  app.put('/api/code-quality/projects/:projectId/public-routes', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const path = `/code-quality/projects/${encodeURIComponent(req.params.projectId)}/public-routes`;
      return await reply(res, await forward(path, user, {
        method: 'PUT', body: JSON.stringify({ patterns: req.body?.patterns }),
      }), 'Public routes could not be saved.');
    } catch {
      return res.status(502).json({ detail: 'Public routes could not be saved.' });
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

  // ----- Business rule knowledge: synced Outline index, links and developer aids -----
  // Only this server can read the Outline token (from the desktop credential helper), so it starts every sync.
  const syncOutlineRules = async (projectId: string, credentialId: string, user: AuthUser) => {
    const credential = await callOutlineCredentialHelper(projectId, credentialId, 'code-quality-outline-fetch', 'POST', {});
    if (!credential.response.ok || typeof credential.data.token !== 'string') {
      return { status: credential.response.status || 503,
        data: { detail: credential.data.detail || 'The saved Outline token could not be read from the OS credential store.' } };
    }
    const upstream = await forward(`/code-quality/projects/${encodeURIComponent(projectId)}/outline/sync`, user,
      { method: 'POST', body: JSON.stringify({ token: credential.data.token }) });
    return { status: upstream.status, data: await upstream.json().catch(() => ({ detail: 'Business rules could not be synced.' })) };
  };
  const ruleSyncUser = { sub: 'system:business-rule-sync' };
  const runScheduledRuleSync = async () => {
    try {
      const listed = await forward('/code-quality/outline/connections', ruleSyncUser);
      if (!listed.ok) return;
      const { connections = [] } = await listed.json().catch(() => ({})) as { connections?: { project_id: string; credential_id: string }[] };
      for (const connection of connections) {
        // A stopped desktop helper or unreachable Outline only skips this round; the last synced copy stays.
        await syncOutlineRules(connection.project_id, connection.credential_id, ruleSyncUser).catch(() => undefined);
      }
    } catch { /* API unavailable: try again next round */ }
  };
  const ruleSyncInterval = dependencies.ruleSyncIntervalMs ?? 10 * 60_000;
  if (ruleSyncInterval > 0) {
    setTimeout(() => void runScheduledRuleSync(), 30_000).unref?.();
    setInterval(() => void runScheduledRuleSync(), ruleSyncInterval).unref?.();
  }

  app.post('/api/code-quality/projects/:projectId/outline/sync', async (req, res) => {
    try {
      const user = allowed(res, req.params.projectId); if (!user) return;
      const configResponse = await forward(`/code-quality/projects/${encodeURIComponent(req.params.projectId)}/outline`, user);
      const config = await configResponse.json().catch(() => ({})) as any;
      if (!configResponse.ok || !config.connection) {
        return res.status(configResponse.ok ? 404 : configResponse.status).json({ detail: config.detail || 'Connect an Outline collection before syncing business rules.' });
      }
      if (typeof config.connection.credential_id !== 'string') return res.status(503).json({ detail: 'The saved Outline credential is unavailable.' });
      const result = await syncOutlineRules(req.params.projectId, config.connection.credential_id, user);
      return res.status(result.status).json(result.data);
    } catch { return res.status(502).json({ detail: 'Business rules could not be synced.' }); }
  });

  const ruleKnowledgePath = (projectId: string, suffix = '') =>
    `/code-quality/projects/${encodeURIComponent(projectId)}/rule-knowledge${suffix}`;
  const queryString = (query: Record<string, unknown>, keys: string[]) => {
    const params = new URLSearchParams();
    for (const key of keys) {
      const value = query[key];
      for (const item of Array.isArray(value) ? value : value === undefined ? [] : [value]) {
        if (typeof item === 'string') params.append(key, item);
      }
    }
    const text = params.toString();
    return text ? `?${text}` : '';
  };
  const ruleKnowledgeRoute = (method: 'get' | 'post' | 'put', suffix: string, upstreamSuffix: (req: any) => string,
    fallback: string) => {
    app[method](`/api/code-quality/projects/:projectId/rule-knowledge${suffix}`, async (req, res) => {
      try {
        const user = allowed(res, req.params.projectId); if (!user) return;
        const init: RequestInit = method === 'get' ? {} : { method: method.toUpperCase(), body: JSON.stringify(req.body ?? {}) };
        return await reply(res, await forward(ruleKnowledgePath(req.params.projectId, upstreamSuffix(req)), user, init), fallback);
      } catch { return res.status(502).json({ detail: fallback }); }
    });
  };
  ruleKnowledgeRoute('get', '', () => '', 'Business rules are unavailable.');
  ruleKnowledgeRoute('put', '/settings', () => '/settings', 'Business rule settings could not be saved.');
  ruleKnowledgeRoute('get', '/links', (req) => '/links' + queryString(req.query, ['doc_id', 'symbol_id', 'file']), 'Business rule links are unavailable.');
  ruleKnowledgeRoute('post', '/links/suggest', () => '/links/suggest', 'Code links could not be suggested.');
  ruleKnowledgeRoute('put', '/links', () => '/links', 'The business rule link could not be saved.');
  ruleKnowledgeRoute('get', '/coverage', (req) => '/coverage' + queryString(req.query, ['revision_id']), 'Business rule coverage is unavailable.');
  ruleKnowledgeRoute('post', '/impact', () => '/impact', 'The diff could not be checked against business rules.');
  ruleKnowledgeRoute('get', '/runtime', (req) => '/runtime' + queryString(req.query, ['revision_id']), 'Business rule runtime data is unavailable.');
  ruleKnowledgeRoute('post', '/:docId/tests', (req) => `/${encodeURIComponent(req.params.docId)}/tests`, 'Test skeletons could not be generated.');

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
