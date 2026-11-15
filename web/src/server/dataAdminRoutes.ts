import type { Express, Response } from 'express';

type AuthUser = { sub: string; email?: string; name?: string } | null;

export type DataAdminDependencies = {
  apiUrl: string;
  internalSecret: string;
  getUser: (response: Response) => AuthUser;
  isGlobalAdmin: (user: AuthUser) => boolean;
  /** Projects the user is a member of (any role). */
  projectsFor: (user: AuthUser) => string[];
  /** Called once a project purge job succeeds. */
  removeProjectMemberships: (projectId: string) => void;
};

type Item = {
  store: string; id: string; owner: string; project_id: string | null; deletable: boolean;
  detail: Record<string, any>; [key: string]: unknown;
};

/**
 * Narrow an inventory item to what a non-admin may see: items of their projects, and shared items
 * reduced to the rows of their projects. Global tables, legacy and orphan data are admin-only.
 */
export function scopeItem(item: Item, projects: Set<string>): Item | null {
  if (item.project_id) return projects.has(item.project_id) ? { ...item, deletable: false } : null;
  const perProject = item.detail?.projects as Record<string, number> | undefined;
  if (item.owner !== 'shared' || !perProject) return null;
  const mine = Object.fromEntries(Object.entries(perProject).filter(([project]) => projects.has(project)));
  if (!Object.keys(mine).length) return null;
  const count = Object.values(mine).reduce((sum, n) => sum + n, 0);
  return { ...item, count, bytes: null, deletable: false,
    detail: { projects: mine, ...(item.detail.table ? { table: item.detail.table } : {}) } };
}

export function registerDataAdminRoutes(app: Express, deps: DataAdminDependencies) {
  const purgedMemberships = new Set<string>();
  const forward = (path: string, user: AuthUser, init: RequestInit = {}) => fetch(`${deps.apiUrl}${path}`, {
    ...init,
    headers: {
      'X-Internal-Secret': deps.internalSecret,
      ...(user?.sub ? { 'X-User-Id': user.sub } : {}),
      ...(user?.email ? { 'X-User-Email': user.email } : {}),
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
    },
    signal: AbortSignal.timeout(180_000),
  });
  const context = (res: Response) => {
    const user = deps.getUser(res);
    if (!user?.sub) { res.status(401).json({ detail: 'Authentication required.' }); return null; }
    const admin = deps.isGlobalAdmin(user);
    return { user, admin, projects: new Set(deps.projectsFor(user)) };
  };
  const adminOnly = (res: Response) => {
    const ctx = context(res);
    if (!ctx) return null;
    if (!ctx.admin) { res.status(403).json({ detail: 'Global administrator access required.' }); return null; }
    return ctx;
  };
  const relay = async (res: Response, path: string, user: AuthUser, init: RequestInit = {},
    shape: (data: any) => any = (data) => data) => {
    try {
      const upstream = await forward(path, user, init);
      const data = await upstream.json().catch(() => ({ detail: 'Data management service returned an invalid response.' }));
      return res.status(upstream.status).json(upstream.ok ? shape(data) : data);
    } catch {
      return res.status(502).json({ detail: 'Data management service is unavailable.' });
    }
  };
  const refresh = (query: any) => (query.refresh === 'true' ? '?refresh=true' : '');

  app.get('/api/data-admin/me', (_req, res) => {
    const ctx = context(res); if (!ctx) return;
    res.json({ globalAdmin: ctx.admin, projects: [...ctx.projects] });
  });

  app.get('/api/data-admin/projects', async (req, res) => {
    const ctx = context(res); if (!ctx) return;
    return relay(res, `/data-admin/projects${refresh(req.query)}`, ctx.user, {}, (data) => ({
      globalAdmin: ctx.admin,
      projects: (data.projects || [])
        .filter((row: any) => ctx.admin || ctx.projects.has(row.project_id))
        .map((row: any) => ({ ...row, can_delete: ctx.admin })),
    }));
  });

  app.get('/api/data-admin/summary', async (req, res) => {
    const ctx = context(res); if (!ctx) return;
    if (ctx.admin) return relay(res, `/data-admin/summary${refresh(req.query)}`, ctx.user, {}, (data) => ({ ...data, globalAdmin: true }));
    // A viewer's overview is the sum over their own projects; global and orphan data stay hidden.
    return relay(res, `/data-admin/projects${refresh(req.query)}`, ctx.user, {}, (data) => {
      const stores: Record<string, any> = {};
      const mine = (data.projects || []).filter((row: any) => ctx.projects.has(row.project_id));
      for (const row of mine) {
        for (const [name, totals] of Object.entries<any>(row.stores)) {
          const entry = stores[name] ||= { status: 'ok', error: null, items: 0, count: 0, bytes: 0, orphans: 0 };
          entry.items += totals.items; entry.count += totals.count; entry.bytes += totals.bytes;
        }
      }
      return { globalAdmin: false, scope: 'projects', stores, orphans: 0, projects: mine.length };
    });
  });

  app.get('/api/data-admin/stores/:store/items', async (req, res) => {
    const ctx = context(res); if (!ctx) return;
    const query = new URLSearchParams();
    for (const key of ['project_id', 'owner', 'refresh']) {
      if (typeof req.query[key] === 'string') query.set(key, req.query[key] as string);
    }
    return relay(res, `/data-admin/stores/${encodeURIComponent(req.params.store)}/items?${query}`, ctx.user, {}, (data) => ({
      ...data,
      globalAdmin: ctx.admin,
      items: ctx.admin ? data.items : (data.items || []).map((item: Item) => scopeItem(item, ctx.projects)).filter(Boolean),
    }));
  });

  app.get('/api/data-admin/stores/:store/preview', async (req, res) => {
    const ctx = context(res); if (!ctx) return;
    const id = typeof req.query.id === 'string' ? req.query.id : '';
    if (!id) return res.status(400).json({ detail: 'An item id is required.' });
    try {
      const upstream = await forward(`/data-admin/stores/${encodeURIComponent(req.params.store)}/preview?id=${encodeURIComponent(id)}`, ctx.user);
      const data = await upstream.json().catch(() => ({ detail: 'Preview unavailable.' })) as any;
      // Samples of shared or global items can hold other projects' data, so viewers see only their own items.
      if (upstream.ok && !ctx.admin && !(data.item?.project_id && ctx.projects.has(data.item.project_id))) {
        return res.status(403).json({ detail: 'Previews of shared data are limited to global administrators.' });
      }
      return res.status(upstream.status).json(data);
    } catch { return res.status(502).json({ detail: 'Data management service is unavailable.' }); }
  });

  app.get('/api/data-admin/orphans', async (req, res) => {
    const ctx = adminOnly(res); if (!ctx) return;
    return relay(res, `/data-admin/orphans${refresh(req.query)}`, ctx.user);
  });

  app.post('/api/data-admin/deletions/plan', async (req, res) => {
    const ctx = adminOnly(res); if (!ctx) return;
    return relay(res, '/data-admin/deletions/plan', ctx.user, { method: 'POST', body: JSON.stringify({ target: req.body?.target }) });
  });

  app.post('/api/data-admin/deletions/execute', async (req, res) => {
    const ctx = adminOnly(res); if (!ctx) return;
    const target = req.body?.target;
    if (target?.kind === 'project' && req.body?.confirm !== target.project_id) {
      return res.status(400).json({ detail: 'Type the project id to confirm a project purge.' });
    }
    return relay(res, '/data-admin/deletions/execute', ctx.user,
      { method: 'POST', body: JSON.stringify({ target, hash: req.body?.hash }) });
  });

  app.get('/api/data-admin/jobs', async (_req, res) => {
    const ctx = adminOnly(res); if (!ctx) return;
    return relay(res, '/data-admin/jobs', ctx.user);
  });

  app.get('/api/data-admin/jobs/:jobId', async (req, res) => {
    const ctx = adminOnly(res); if (!ctx) return;
    return relay(res, `/data-admin/jobs/${encodeURIComponent(req.params.jobId)}`, ctx.user, {}, (job) => {
      // Memberships live in the web auth store, so a purged project's access is revoked here.
      if (job.state === 'succeeded' && job.target?.kind === 'project' && !purgedMemberships.has(job.job_id)) {
        purgedMemberships.add(job.job_id);
        deps.removeProjectMemberships(job.target.project_id);
      }
      return job;
    });
  });

  app.get('/api/data-admin/audit', async (_req, res) => {
    const ctx = context(res); if (!ctx) return;
    const scope = ctx.admin ? '' : `?project_ids=${encodeURIComponent([...ctx.projects].join(','))}`;
    return relay(res, `/data-admin/audit${scope}`, ctx.user);
  });
}
