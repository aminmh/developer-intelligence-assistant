import type { Express, Request, Response } from 'express';

type User = { sub: string } | null;
type Dependencies = {
  apiUrl: string;
  internalSecret: string;
  webOrigin: string;
  getUser: (response: Response) => User;
  isGlobalAdmin: (user: User) => boolean;
};

// Saved analyses and imports currently have no project ownership metadata.
// Limit drafts to the existing global administrator until that is available.
export function registerSchemaDraftRoutes(app: Express, dependencies: Dependencies) {
  const forward = async (req: Request, res: Response, path: string) => {
    const user = dependencies.getUser(res);
    if (!user?.sub) return res.status(401).json({ detail: 'Sign in to use schema drafts.' });
    if (!dependencies.isGlobalAdmin(user)) return res.status(403).json({ detail: 'Administrator access is required for schema drafts.' });
    if (req.method !== 'GET' && req.get('origin') !== dependencies.webOrigin) {
      return res.status(403).json({ detail: 'Draft changes must come from the application origin.' });
    }
    const body = req.method === 'PUT' || path === '/import' || path === '/runtime' || path.endsWith('/inspect') || /\/migration-(preview|download)$/.test(path) ? JSON.stringify(req.body ?? {}) : undefined;
    if (body && Buffer.byteLength(body, 'utf8') > 9 * 1024 * 1024) {
      return res.status(413).json({ detail: 'Draft request exceeds the size limit.' });
    }
    try {
      const upstream = await fetch(`${dependencies.apiUrl}/schema-drafts${path}`, {
        method: req.method,
        headers: { 'X-Internal-Secret': dependencies.internalSecret, 'X-User-Id': user.sub,
          'X-Application-Origin': dependencies.webOrigin,
          ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body,
        signal: AbortSignal.timeout(path.endsWith('/migration-preview') ? 75000 : 15000),
      });
      if (path.endsWith('/migration-download') && upstream.ok) {
        const disposition = upstream.headers.get('content-disposition') || '';
        const match = /^attachment;\s*filename="([A-Za-z0-9_.-]+\.php)"$/i.exec(disposition);
        if (!match) return res.status(502).json({ detail: 'Schema draft service returned an invalid filename.' });
        res.setHeader('Content-Disposition', `attachment; filename="${match[1]}"`);
        res.setHeader('Content-Type', 'text/plain; charset=utf-8');
        res.setHeader('X-Content-Type-Options', 'nosniff');
        return res.status(upstream.status).send(Buffer.from(await upstream.arrayBuffer()));
      }
      return res.status(upstream.status).json(await upstream.json().catch(() => ({ detail: 'Schema draft service returned an invalid response.' })));
    } catch {
      return res.status(502).json({ detail: 'Schema draft service is unavailable. Retry the request.' });
    }
  };
  app.get('/api/schema-drafts', (req, res) => forward(req, res, ''));
  app.post('/api/schema-drafts/analysis/:analysisId', (req, res) => forward(req, res, `/analysis/${encodeURIComponent(req.params.analysisId)}`));
  app.post('/api/schema-drafts/import', (req, res) => forward(req, res, '/import'));
  app.post('/api/schema-drafts/runtime', (req, res) => forward(req, res, '/runtime'));
  app.get('/api/schema-drafts/:draftId', (req, res) => forward(req, res, `/${encodeURIComponent(req.params.draftId)}`));
  app.delete('/api/schema-drafts/:draftId', (req, res) => forward(req, res, `/${encodeURIComponent(req.params.draftId)}`));
  app.put('/api/schema-drafts/:draftId', (req, res) => forward(req, res, `/${encodeURIComponent(req.params.draftId)}`));
  app.post('/api/schema-drafts/:draftId/inspect', (req, res) => forward(req, res, `/${encodeURIComponent(req.params.draftId)}/inspect`));
  app.post('/api/schema-drafts/:draftId/migration-preview', (req, res) => forward(req, res, `/${encodeURIComponent(req.params.draftId)}/migration-preview`));
  app.post('/api/schema-drafts/:draftId/migration-download', (req, res) => forward(req, res, `/${encodeURIComponent(req.params.draftId)}/migration-download`));
}
