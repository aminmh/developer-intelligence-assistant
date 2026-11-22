/**
 * Change impact over a revision's symbol graph (@.archcore/agent-tools/change-impact-analysis.spec.md).
 *
 * Python's POST /code-tools/impact does no authorization of its own, so this proxy checks that the
 * signed-in user may read `project_id` before forwarding, and forwards only the fields the route reads.
 */
import express from 'express';
import type { Express, NextFunction, Request, Response } from 'express';

type User = { sub: string } | null | undefined;

export interface CodeImpactDependencies {
  apiUrl: string;
  getUser: (res: Response) => User;
  canReadProject: (user: User, projectId: string) => boolean;
}

/** Python accepts a diff of up to 200 KB; the rest leaves room for the other fields. */
export const IMPACT_BODY_LIMIT = '210kb';
const FORWARDED_FIELDS = ['project_id', 'revision_id', 'revision', 'symbols', 'diff', 'base_revision_id', 'depth', 'max_nodes'];

/**
 * Parses the impact request with its own size cap. Mount it before the application-wide JSON
 * parser (whose limit is far larger); that parser then leaves the already-parsed body alone.
 */
export function codeImpactBodyParser() {
  const parse = express.json({ limit: IMPACT_BODY_LIMIT });
  return (req: Request, res: Response, next: NextFunction) => parse(req, res, (error?: any) => {
    if (!error) return next();
    if (error.type === 'entity.too.large') return res.status(413).json({ success: false, error: 'The impact request is larger than 210 KB.' });
    return res.status(400).json({ success: false, error: 'The impact request is not valid JSON.' });
  });
}

export function registerCodeImpactRoutes(app: Express, deps: CodeImpactDependencies): void {
  app.post('/api/code/impact', async (req, res) => {
    const user = deps.getUser(res);
    if (!user?.sub) return res.status(401).json({ success: false, error: 'Authentication required.' });
    const body = req.body && typeof req.body === 'object' && !Array.isArray(req.body) ? req.body : {};
    const projectId = body.project_id;
    if (typeof projectId !== 'string' || !projectId.trim()) {
      return res.status(400).json({ success: false, error: 'A project is required.' });
    }
    if (!deps.canReadProject(user, projectId)) return res.status(403).json({ success: false, error: 'Project access denied.' });
    const forwarded = Object.fromEntries(FORWARDED_FIELDS.filter((key) => body[key] !== undefined).map((key) => [key, body[key]]));
    try {
      const upstream = await fetch(`${deps.apiUrl}/code-tools/impact`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(forwarded),
        signal: AbortSignal.timeout(60_000),
      });
      const data: any = await upstream.json().catch(() => ({}));
      if (!upstream.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : 'Impact analysis is unavailable.';
        return res.status(upstream.status).json({ success: false, error: detail });
      }
      return res.json(data);
    } catch {
      return res.status(502).json({ success: false, error: 'Impact analysis is unavailable. Retry the request.' });
    }
  });
}
