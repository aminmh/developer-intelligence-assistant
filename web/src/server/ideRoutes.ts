/**
 * IDE extension API: contract change notifications for front-end developers.
 * Spec: .archcore/frontend-assist/contract-change-notifications.spec.md
 * Decision: .archcore/frontend-assist/ide-tokens-are-personal-read-only-bearer-tokens.adr.md
 *
 * - `/api/ide/*` has no browser session. It authenticates with a personal IDE token
 *   (`Authorization: Bearer ide_…`) and acts as the token's owner, re-checking project access on
 *   every request, so removing a membership stops the IDE at once.
 * - An IDE token only reads (events, impact, generated code) and acknowledges events for its owner.
 * - `/api/ide-tokens` lets a signed-in user create, list, and revoke their own tokens.
 */
import type { Express, NextFunction, Request, Response } from 'express';

type User = { sub: string; email?: string; name?: string };
type TokenRow = { token_id: string; name: string; created_at: string; expires_at: string; last_used_at: string | null };

export interface IdeDependencies {
  apiUrl: string;
  internalSecret: string;
  webOrigin: string;
  userForToken: (authorization: string | undefined) => User | null;
  canAccessProject: (user: User, projectId: string) => boolean;
  /** Session user, for token management routes registered after session authentication. */
  getUser: (res: Response) => User | undefined;
  createToken: (user: User, name: string) => { token: string; row: TokenRow };
  listTokens: (user: User) => TokenRow[];
  revokeToken: (user: User, tokenId: string) => boolean;
}

const PROJECT_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const EVENT_ID = /^ce_[0-9a-f]{32}$/;
const SOURCE_ID = /^cs_[0-9a-f]{32}$/;
const VERSION_ID = /^cv_[0-9a-f]{32}$/;
const TOKEN_ID = /^[0-9a-f]{24}$/;
export const MAX_STREAM_PROJECTS = 20;

/** A git remote URL → GitHub `owner/name`, the form project Git sources are stored in; null otherwise. */
export function githubRepository(remote: string): string | null {
  const text = remote.trim();
  const match = /^(?:https?:\/\/(?:[^@/]+@)?github\.com\/|git@github\.com:|ssh:\/\/git@github\.com\/)([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/i.exec(text);
  return match ? `${match[1]}/${match[2]}` : null;
}

export function registerIdeApi(app: Express, deps: IdeDependencies): void {
  const python = async (path: string, init: RequestInit = {}, timeoutMs = 30_000) => {
    const response = await fetch(`${deps.apiUrl}${path}`, {
      ...init,
      headers: { 'X-Internal-Secret': deps.internalSecret, ...(init.headers as Record<string, string> || {}) },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const data = await response.json().catch(() => ({ detail: 'The service returned an invalid response.' }));
    return { response, data };
  };
  const userOf = (res: Response) => (res.locals as any).ideUser as User;
  const guard = (unavailable: string, handler: (req: Request, res: Response) => Promise<unknown>) =>
    async (req: Request, res: Response) => {
      try { return await handler(req, res); } catch { return res.status(502).json({ detail: unavailable }); }
    };
  /** The project in the path, when valid and readable by the token's owner. */
  const project = (req: Request, res: Response) => {
    const projectId = req.params.projectId;
    if (!PROJECT_ID.test(projectId || '')) { res.status(400).json({ detail: 'Invalid project identity.' }); return null; }
    if (!deps.canAccessProject(userOf(res), projectId)) { res.status(403).json({ detail: 'Project access denied.' }); return null; }
    return projectId;
  };
  const event = (req: Request, res: Response) => {
    if (EVENT_ID.test(req.params.eventId || '')) return req.params.eventId;
    res.status(400).json({ detail: 'Invalid contract change.' });
    return null;
  };

  app.use('/api/ide', (req: Request, res: Response, next: NextFunction) => {
    if (req.method === 'OPTIONS') return next();
    const user = deps.userForToken(req.get('authorization'));
    if (!user) return res.status(401).json({ detail: 'A valid IDE token is required. Create one in Developer Assistant settings.' });
    (res.locals as any).ideUser = user;
    return next();
  });

  app.get('/api/ide/me', (_req, res) => {
    const user = userOf(res);
    res.json({ user: { sub: user.sub, name: user.name, email: user.email } });
  });

  // Which projects a workspace's git remote belongs to, among those the user can read.
  app.get('/api/ide/projects/match', guard('Project matching is unavailable.', async (req, res) => {
    const remote = typeof req.query.remote === 'string' ? req.query.remote : '';
    const repository = githubRepository(remote);
    if (!repository) return res.json({ repository: null, projects: [] });
    const { response, data } = await python('/telemetry/git/sources');
    if (!response.ok) return res.status(502).json({ detail: 'Project matching is unavailable.' });
    const projects = [...new Set<string>((data.sources || [])
      .filter((row: any) => String(row.repository).toLowerCase() === repository.toLowerCase())
      .map((row: any) => row.project_id))]
      .filter((projectId) => deps.canAccessProject(userOf(res), projectId))
      .sort();
    return res.json({ repository, projects });
  }));

  app.get('/api/ide/projects/:projectId/contracts', guard('API contracts are unavailable.', async (req, res) => {
    const projectId = project(req, res);
    if (!projectId) return;
    const { response, data } = await python(`/contracts/projects/${encodeURIComponent(projectId)}`);
    return res.status(response.status).json(data);
  }));

  app.get('/api/ide/projects/:projectId/events', guard('Contract changes are unavailable.', async (req, res) => {
    const projectId = project(req, res);
    if (!projectId) return;
    const after = Number.parseInt(String(req.query.after ?? '0'), 10);
    const query = new URLSearchParams({ after: String(Number.isFinite(after) && after > 0 ? after : 0),
      limit: '50', user_id: userOf(res).sub });
    const { response, data } = await python(`/contracts/projects/${encodeURIComponent(projectId)}/events?${query}`);
    return res.status(response.status).json(data);
  }));

  app.get('/api/ide/projects/:projectId/events/:eventId', guard('The contract change is unavailable.', async (req, res) => {
    const projectId = project(req, res);
    const eventId = projectId && event(req, res);
    if (!eventId) return;
    const { response, data } = await python(`/contracts/projects/${encodeURIComponent(projectId!)}/events/${eventId}`, {}, 90_000);
    return res.status(response.status).json(data);
  }));

  app.post('/api/ide/projects/:projectId/events/:eventId/ack', guard('The change could not be acknowledged.', async (req, res) => {
    const projectId = project(req, res);
    const eventId = projectId && event(req, res);
    if (!eventId) return;
    const { response, data } = await python(`/contracts/projects/${encodeURIComponent(projectId!)}/events/${eventId}/ack`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: userOf(res).sub }),
    });
    return res.status(response.status).json(data);
  }));

  // Regenerate typed client code from a contract version the user can see through one of their projects.
  app.post('/api/ide/contracts/sources/:sourceId/versions/:versionId/generate', guard('Code could not be generated.', async (req, res) => {
    const { sourceId, versionId } = req.params;
    if (!SOURCE_ID.test(sourceId || '') || !VERSION_ID.test(versionId || '')) return res.status(400).json({ detail: 'Invalid contract version.' });
    const found = await python(`/contracts/sources/${sourceId}`);
    const source = found.data.source;
    const projects = found.response.ok ? [source.project_id, ...(source.attachments || []).map((row: any) => row.project_id)] : [];
    if (!projects.some((projectId: string) => deps.canAccessProject(userOf(res), projectId))) {
      return res.status(404).json({ detail: 'No such contract.' });
    }
    const { target, base_url, search } = req.body || {};
    const { response, data } = await python(`/contracts/sources/${sourceId}/versions/${versionId}/generate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target, base_url, search }),
    });
    return res.status(response.status).json(data);
  }));

  // Live events for up to MAX_STREAM_PROJECTS consumer projects, as server-sent events.
  app.get('/api/ide/events/stream', async (req, res) => {
    const projects = [...new Set(String(req.query.projects || '').split(',').map((part) => part.trim()).filter(Boolean))];
    if (!projects.length || projects.length > MAX_STREAM_PROJECTS || projects.some((id) => !PROJECT_ID.test(id))) {
      return res.status(400).json({ detail: `Name between 1 and ${MAX_STREAM_PROJECTS} projects.` });
    }
    if (projects.some((projectId) => !deps.canAccessProject(userOf(res), projectId))) {
      return res.status(403).json({ detail: 'Project access denied.' });
    }
    await pipeEventStream(res, `${deps.apiUrl}/contracts/events/stream?projects=${encodeURIComponent(projects.join(','))}`,
      deps.internalSecret);
  });
}

/** Forward a server-sent event stream from the Python service until either side closes. */
export async function pipeEventStream(res: Response, url: string, internalSecret: string): Promise<void> {
  const controller = new AbortController();
  res.on('close', () => controller.abort());
  try {
    const upstream = await fetch(url, {
      headers: { 'X-Internal-Secret': internalSecret, Accept: 'text/event-stream' }, signal: controller.signal });
    if (!upstream.ok || !upstream.body) {
      res.status(upstream.status || 502).json({ detail: 'The update stream is unavailable.' });
      return;
    }
    res.status(200);
    res.set({ 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache, no-transform', Connection: 'keep-alive',
      'X-Accel-Buffering': 'no' });
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
  } catch {
    if (!res.headersSent) res.status(502).json({ detail: 'The update stream is unavailable.' });
    else if (!res.writableEnded) res.end();
  }
}

/** Token management for the signed-in user (registered after session authentication). */
export function registerIdeTokenRoutes(app: Express, deps: IdeDependencies): void {
  const signedIn = (res: Response) => {
    const user = deps.getUser(res);
    if (!user?.sub) { res.status(401).json({ detail: 'Sign in to manage IDE tokens.' }); return null; }
    return user;
  };
  const sameOrigin = (req: Request, res: Response) => {
    if (req.get('origin') === deps.webOrigin) return true;
    res.status(403).json({ detail: 'Requests must come from the application origin.' });
    return false;
  };

  app.get('/api/ide-tokens', (_req, res) => {
    const user = signedIn(res);
    if (user) res.json({ tokens: deps.listTokens(user) });
  });

  app.post('/api/ide-tokens', (req, res) => {
    const user = signedIn(res);
    if (!user || !sameOrigin(req, res)) return;
    const name = typeof req.body?.name === 'string' ? req.body.name : '';
    try {
      const { token, row } = deps.createToken(user, name);
      // The token text is shown once; only its hash is stored.
      return res.json({ token, row });
    } catch (error: any) {
      return res.status(409).json({ detail: error?.message || 'The token could not be created.' });
    }
  });

  app.delete('/api/ide-tokens/:tokenId', (req, res) => {
    const user = signedIn(res);
    if (!user || !sameOrigin(req, res)) return;
    if (!TOKEN_ID.test(req.params.tokenId || '')) return res.status(400).json({ detail: 'Invalid token.' });
    return deps.revokeToken(user, req.params.tokenId)
      ? res.json({ revoked: true })
      : res.status(404).json({ detail: 'No such token.' });
  });
}
