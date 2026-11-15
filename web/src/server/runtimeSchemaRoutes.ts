import crypto from 'node:crypto';
import type { Express, Request, Response } from 'express';

type User = { sub: string } | null;
type Dependencies = {
  apiUrl: string;
  internalSecret: string;
  webOrigin: string;
  getUser: (response: Response) => User;
  isGlobalAdmin: (user: User) => boolean;
};
type Source = {
  project_id: string; environment: string; host_id: string; container_id: string;
  image_id: string; service: string; state: string; updated_at: string;
};
const slug = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const operations = new Set(['discover', 'extract', 'associate', 'ssh-scan']);
const sourceFields = ['project_id', 'environment', 'host_id', 'container_id', 'image_id'] as const;
const identifier = (value: unknown): value is string => typeof value === 'string'
  && value.length > 0 && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value);

function validSource(value: unknown): value is Source {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const row = value as Source;
  return sourceFields.every(key => identifier(row[key])) && slug.test(row.project_id)
    && slug.test(row.environment) && /^[0-9a-f]{64}$/.test(row.container_id)
    && row.state === 'running';
}

export function registerRuntimeSchemaRoutes(app: Express, dependencies: Dependencies) {
  function authorize(req: Request, res: Response, write: boolean): User {
    const user = dependencies.getUser(res);
    if (!user?.sub) { res.status(401).json({ detail: 'Sign in to load Runtime database schemas.' }); return null; }
    if (!dependencies.isGlobalAdmin(user)) { res.status(403).json({ detail: 'Administrator access is required for schema drafts.' }); return null; }
    if (write && req.get('origin') !== dependencies.webOrigin) {
      res.status(403).json({ detail: 'Schema requests must come from the application origin.' }); return null;
    }
    if (dependencies.internalSecret.length < 32) {
      res.status(503).json({ detail: 'Runtime schema authorization is not configured. Configure the shared application secret.' }); return null;
    }
    return user;
  }

  async function sources(project: string, environment: string, user: NonNullable<User>): Promise<Source[]> {
    const url = new URL(`${dependencies.apiUrl}/schema-drafts/runtime/sources`);
    url.searchParams.set('project_id', project);
    if (environment) url.searchParams.set('environment', environment);
    const response = await fetch(url, {
      headers: { 'X-Internal-Secret': dependencies.internalSecret, 'X-User-Id': user.sub },
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error('Runtime database sources are unavailable. Refresh Runtime and retry.');
    const data = await response.json() as { sources?: unknown };
    if (!Array.isArray(data.sources) || data.sources.length > 2000) throw new Error('Runtime source inventory is invalid. Refresh Runtime and retry.');
    return data.sources.filter(validSource).filter(row => row.project_id === project && (!environment || row.environment === environment));
  }

  app.get('/api/runtime/schema-sources', async (req, res) => {
    const user = authorize(req, res, false);
    if (!user) return;
    const project = req.query.project_id;
    const environment = req.query.environment ?? '';
    if (typeof project !== 'string' || !slug.test(project) || typeof environment !== 'string' || (environment && !slug.test(environment))) {
      return res.status(400).json({ detail: 'Select a valid Runtime project and environment.' });
    }
    try { return res.json({ sources: await sources(project, environment, user) }); }
    catch { return res.status(502).json({ detail: 'Runtime database sources are unavailable. Refresh Runtime and retry.' }); }
  });

  app.post('/api/runtime/schema-authorization', async (req, res) => {
    const user = authorize(req, res, true);
    if (!user) return;
    const body = req.body ?? {};
    const { source, operation } = body;
    const database = body.database ?? '';
    const schemas = body.schemas ?? [];
    if (!validSource(source) || !operations.has(operation)
      || (database !== '' && !identifier(database)) || (operation === 'extract' && !identifier(database))
      || !Array.isArray(schemas) || schemas.length > 64 || !schemas.every(identifier)
      || new Set(schemas).size !== schemas.length
      || Object.keys(body).some(key => !['source', 'operation', 'database', 'schemas'].includes(key))) {
      return res.status(400).json({ detail: 'Select a reporting container, operation, database, and applicable schemas.' });
    }
    try {
      const reporting = await sources(source.project_id, source.environment, user);
      if (!reporting.some(row => sourceFields.every(key => row[key] === source[key]))) {
        return res.status(409).json({ detail: 'The container is no longer reporting in this project and environment. Refresh Runtime and select it again.' });
      }
      const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
      const header = encode({ alg: 'HS256', typ: 'JWT' });
      const claims = encode({ aud: 'runtime-helper', origin: dependencies.webOrigin, sub: user.sub,
        exp: Math.floor(Date.now() / 1000) + 120, jti: crypto.randomUUID(),
        operation: `runtime-schema-${operation}`, project_id: source.project_id,
        environment: source.environment, host_id: source.host_id, container_id: source.container_id,
        image_id: source.image_id, database, schemas });
      const key = crypto.createHmac('sha256', dependencies.internalSecret).update('devassistant-runtime-helper-v1').digest();
      const signature = crypto.createHmac('sha256', key).update(`${header}.${claims}`).digest('base64url');
      res.setHeader('Cache-Control', 'no-store');
      return res.json({ token: `${header}.${claims}.${signature}`, expires_in: 120 });
    } catch {
      return res.status(502).json({ detail: 'Runtime inventory could not be verified. Refresh Runtime and retry.' });
    }
  });
}
