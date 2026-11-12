/**
 * Working-tree sync (revision-sync plan phase 6).
 *
 * A project operator or administrator creates a grant here; the browser hands the grant to
 * the desktop helper, which asks the user to choose and approve a folder, then uploads a
 * ZIP of it after edits settle. The upload route has no browser session: it authenticates
 * with the grant, and every upload re-checks that the granting user still has operator or
 * administrator access to the project, so removing their access stops sync immediately.
 */
import express, { type Express, type Request, type Response } from 'express';
import { createHmac, randomUUID } from 'node:crypto';

type User = { sub: string; email?: string; name?: string };
type Role = 'viewer' | 'operator' | 'admin';

export interface WorkingTreeDependencies {
  pythonApiUrl: string;
  internalSecret: string;
  webOrigin: string;
  roleFor: (user: User, projectId: string) => Role | null;
  isGlobalAdmin: (user: User) => boolean;
  getUser: (res: Response) => User | undefined;
  /** Shortest time between two uploads accepted from one grant. */
  minUploadIntervalMs?: number;
}

export const MAX_WORKING_TREE_BYTES = 64 * 1024 * 1024;
const PROJECT_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const GRANT_ID = /^wtg_[0-9a-f]{32}$/;

/** `WorkingTreeGrant wtg_…:<token>` → its parts, or null. */
export function parseGrantAuthorization(header: string | undefined): { grantId: string; token: string } | null {
  const match = /^WorkingTreeGrant (wtg_[0-9a-f]{32}):([A-Za-z0-9_-]{20,200})$/.exec(header || '');
  return match ? { grantId: match[1], token: match[2] } : null;
}

const canSync = (deps: WorkingTreeDependencies, user: User, projectId: string) =>
  deps.isGlobalAdmin(user) || ['operator', 'admin'].includes(deps.roleFor(user, projectId) || '');

async function python(deps: WorkingTreeDependencies, path: string, init: RequestInit = {}) {
  const headers = { 'X-Internal-Secret': deps.internalSecret, ...(init.headers as Record<string, string> || {}) };
  const response = await fetch(`${deps.pythonApiUrl}${path}`, { ...init, headers });
  const data = await response.json().catch(() => ({}));
  return { response, data };
}

/** The helper's upload route. Register before session authentication: it carries a grant, not a session. */
export function registerWorkingTreeUpload(app: Express, deps: WorkingTreeDependencies): void {
  const lastUpload = new Map<string, number>();
  const minInterval = deps.minUploadIntervalMs ?? 30_000;
  app.post('/api/working-tree/upload',
    express.raw({ type: 'application/zip', limit: MAX_WORKING_TREE_BYTES }),
    async (req: Request, res: Response) => {
      const grant = parseGrantAuthorization(req.get('authorization'));
      if (!grant) return res.status(401).json({ detail: 'A working-tree grant is required.' });
      if (!Buffer.isBuffer(req.body) || req.body.length === 0) {
        return res.status(400).json({ detail: 'Send the working tree as an application/zip body.' });
      }
      try {
        const verified = await python(deps, `/telemetry/working-tree/grants/${grant.grantId}/verify`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: grant.token }),
        });
        if (!verified.response.ok) return res.status(401).json({ detail: 'The working-tree grant is invalid or revoked.' });
        const { project_id: projectId, user } = verified.data.grant || {};
        if (typeof projectId !== 'string' || !user?.sub || !canSync(deps, user, projectId)) {
          return res.status(403).json({ detail: 'The user who granted this folder no longer has operator access to the project.' });
        }
        const now = Date.now();
        if (now - (lastUpload.get(grant.grantId) ?? 0) < minInterval) {
          return res.status(429).json({ detail: 'Working-tree uploads are too frequent. Wait and retry.' });
        }
        lastUpload.set(grant.grantId, now);
        const baseCommit = req.get('x-base-commit') || '';
        const upload = await python(deps, `/telemetry/working-tree/grants/${grant.grantId}/upload`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/zip', ...(baseCommit ? { 'X-Base-Commit': baseCommit } : {}) },
          body: req.body,
        });
        return res.status(upload.response.status).json(upload.data);
      } catch {
        return res.status(502).json({ detail: 'The working tree could not be staged. Retry later.' });
      }
    });
}

/** Grant management for signed-in users (registered after session authentication). */
export function registerWorkingTreeGrants(app: Express, deps: WorkingTreeDependencies): void {
  const helperAuthorization = (projectId: string, grantId: string) => {
    const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
    const header = encode({ alg: 'HS256', typ: 'JWT' });
    const claims = encode({ aud: 'runtime-helper', origin: deps.webOrigin, operation: 'working-tree-grant',
      project_id: projectId, target_id: grantId, exp: Math.floor(Date.now() / 1000) + 300, jti: randomUUID() });
    const key = createHmac('sha256', deps.internalSecret).update('devassistant-runtime-helper-v1').digest();
    return `${header}.${claims}.${createHmac('sha256', key).update(`${header}.${claims}`).digest('base64url')}`;
  };
  const projectFrom = (req: Request, res: Response) => {
    const projectId = req.params.projectId;
    if (!PROJECT_ID.test(projectId)) { res.status(400).json({ detail: 'Invalid project identity.' }); return null; }
    return projectId;
  };

  app.get('/api/projects/:projectId/working-tree/grants', async (req, res) => {
    const user = deps.getUser(res); const projectId = projectFrom(req, res);
    if (!user || !projectId) return;
    if (!deps.isGlobalAdmin(user) && !deps.roleFor(user, projectId)) return res.status(403).json({ detail: 'Project access denied.' });
    try {
      const { response, data } = await python(deps, `/telemetry/working-tree/projects/${encodeURIComponent(projectId)}/grants`);
      return res.status(response.status).json(data);
    } catch { return res.status(502).json({ detail: 'Working-tree grants are unavailable.' }); }
  });

  app.post('/api/projects/:projectId/working-tree/grants', async (req, res) => {
    const user = deps.getUser(res); const projectId = projectFrom(req, res);
    if (!user || !projectId) return;
    if (req.get('origin') !== deps.webOrigin) return res.status(403).json({ detail: 'Requests must come from the application origin.' });
    if (!canSync(deps, user, projectId)) return res.status(403).json({ detail: 'Operator or administrator access is required to sync a working tree.' });
    if (deps.internalSecret.length < 32) return res.status(503).json({ detail: 'Working-tree sync is not configured.' });
    try {
      const { response, data } = await python(deps, `/telemetry/working-tree/projects/${encodeURIComponent(projectId)}/grants`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user: { sub: user.sub, email: user.email, name: user.name } }),
      });
      if (!response.ok) return res.status(response.status).json(data);
      // The grant token goes to the browser once, which passes it to the desktop helper.
      return res.json({ grant: data.grant, helper_authorization: helperAuthorization(projectId, data.grant.grant_id) });
    } catch { return res.status(502).json({ detail: 'The working-tree grant could not be created.' }); }
  });

  app.delete('/api/projects/:projectId/working-tree/grants/:grantId', async (req, res) => {
    const user = deps.getUser(res); const projectId = projectFrom(req, res);
    if (!user || !projectId) return;
    if (!GRANT_ID.test(req.params.grantId)) return res.status(400).json({ detail: 'Invalid grant.' });
    try {
      const listing = await python(deps, `/telemetry/working-tree/projects/${encodeURIComponent(projectId)}/grants`);
      const grant = (listing.data.grants || []).find((row: any) => row.grant_id === req.params.grantId);
      if (!grant) return res.status(404).json({ detail: 'No such grant for this project.' });
      const owner = grant.user?.sub === user.sub;
      if (!owner && !deps.isGlobalAdmin(user) && deps.roleFor(user, projectId) !== 'admin') {
        return res.status(403).json({ detail: 'Only the grant owner or a project administrator can revoke it.' });
      }
      const { response, data } = await python(deps,
        `/telemetry/working-tree/projects/${encodeURIComponent(projectId)}/grants/${req.params.grantId}`, { method: 'DELETE' });
      return res.status(response.status).json(data);
    } catch { return res.status(502).json({ detail: 'The grant could not be revoked.' }); }
  });
}
