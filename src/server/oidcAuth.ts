import 'dotenv/config';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import express, { type Request, type Response, type NextFunction } from 'express';

type Role = 'viewer' | 'operator' | 'admin';
type User = { sub: string; email?: string; name?: string };
type Store = {
  sessions: Record<string, { user: User; expires: number }>;
  flows: Record<string, { state: string; nonce: string; verifier: string; expires: number }>;
  memberships: Array<{ project_id: string; subject: string; role: Role }>;
};

const COOKIE = 'devassistant_session';
const FLOW_COOKIE = 'devassistant_oidc_flow';
const SESSION_TTL_MS = Number(process.env.SESSION_TTL_MS || 8 * 60 * 60 * 1000);
const issuer = (process.env.OIDC_ISSUER || '').replace(/\/+$/, '');
const internalIssuer = (process.env.OIDC_INTERNAL_ISSUER || issuer).replace(/\/+$/, '');
const clientId = process.env.OIDC_CLIENT_ID || '';
const clientSecret = process.env.OIDC_CLIENT_SECRET || '';
const redirectUri = process.env.OIDC_REDIRECT_URI || '';
const bootstrapAdmin = process.env.OIDC_BOOTSTRAP_ADMIN_SUB || '';
const sessionSecret = process.env.SESSION_SECRET || '';
const storePath = process.env.AUTH_STORE_PATH || path.join(process.cwd(), 'data', 'auth-store.json');
const secureCookie = process.env.SESSION_COOKIE_SECURE !== 'false';
const developmentUser: User = { sub: 'local-development', name: 'Local development' };
const developmentMode = () => process.env.NODE_ENV === 'development';

function emptyStore(): Store { return { sessions: {}, flows: {}, memberships: [] }; }
function readStore(): Store {
  try {
    const parsed = JSON.parse(fs.readFileSync(storePath, 'utf8')) as Partial<Store>;
    return { ...emptyStore(), ...parsed };
  } catch { return emptyStore(); }
}
function writeStore(store: Store): void {
  fs.mkdirSync(path.dirname(storePath), { recursive: true });
  const tmp = `${storePath}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(store), { mode: 0o600 });
  fs.renameSync(tmp, storePath);
  try { fs.chmodSync(storePath, 0o600); } catch { /* best effort on non-POSIX filesystems */ }
}
function configured(): boolean {
  let parsedIssuer: URL;
  let parsedInternalIssuer: URL;
  try {
    parsedIssuer = new URL(issuer);
    parsedInternalIssuer = new URL(internalIssuer);
  } catch { return false; }
  const localIssuer = parsedIssuer.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(parsedIssuer.hostname);
  const internalIsLocal = parsedInternalIssuer.protocol === 'http:' && ['localhost', '127.0.0.1', 'keycloak'].includes(parsedInternalIssuer.hostname);
  return Boolean((parsedIssuer.protocol === 'https:' || localIssuer)
    && (parsedInternalIssuer.protocol === 'https:' || internalIsLocal)
    && parsedIssuer.pathname === parsedInternalIssuer.pathname
    && !parsedIssuer.username && !parsedIssuer.password && !parsedIssuer.search && !parsedIssuer.hash
    && !parsedInternalIssuer.username && !parsedInternalIssuer.password && !parsedInternalIssuer.search && !parsedInternalIssuer.hash
    && clientId && clientSecret && redirectUri && bootstrapAdmin && sessionSecret.length >= 32);
}
function parseCookies(req: Request): Record<string, string> {
  return Object.fromEntries((req.headers.cookie || '').split(';').map((part) => {
    const idx = part.indexOf('=');
    if (idx < 0) return ['', ''];
    try { return [part.slice(0, idx).trim(), decodeURIComponent(part.slice(idx + 1).trim())]; }
    catch { return ['', '']; }
  }).filter(([key]) => key));
}
function sessionKey(sessionId: string): string {
  return crypto.createHmac('sha256', sessionSecret).update(sessionId).digest('hex');
}
function setCookie(res: Response, name: string, value: string, maxAge: number): void {
  const pieces = [`${name}=${encodeURIComponent(value)}`, 'Path=/', 'HttpOnly', 'SameSite=Lax', `Max-Age=${Math.floor(maxAge / 1000)}`];
  if (secureCookie) pieces.push('Secure');
  res.append('Set-Cookie', pieces.join('; '));
}
function clearCookie(res: Response, name: string): void {
  const pieces = [`${name}=`, 'Path=/', 'HttpOnly', 'SameSite=Lax', 'Max-Age=0'];
  if (secureCookie) pieces.push('Secure');
  res.append('Set-Cookie', pieces.join('; '));
}
function sessionUser(req: Request): User | null {
  if (developmentMode()) return developmentUser;
  const sid = parseCookies(req)[COOKIE];
  if (!sid) return null;
  const store = readStore();
  const key = sessionKey(sid);
  const session = store.sessions[key];
  if (!session || session.expires < Date.now()) {
    if (session) { delete store.sessions[key]; writeStore(store); }
    return null;
  }
  return session.user;
}
function roleFor(user: User, projectId: string, store = readStore()): Role | null {
  if (authIsBootstrapAdmin(user)) return 'admin';
  return store.memberships.find((row) => row.project_id === projectId && row.subject === user.sub)?.role ?? null;
}

let discoveryCache: { expires: number; value: any } | null = null;
async function discovery(): Promise<any> {
  if (discoveryCache && discoveryCache.expires > Date.now()) return discoveryCache.value;
  const response = await fetch(`${internalIssuer}/.well-known/openid-configuration`, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error('OIDC discovery failed');
  const metadata = await response.json();
  if (metadata.issuer !== issuer || !metadata.authorization_endpoint || !metadata.token_endpoint || !metadata.jwks_uri) throw new Error('Invalid OIDC discovery document');
  const internalEndpoint = (endpoint: unknown): string => {
    if (typeof endpoint !== 'string' || !endpoint.startsWith(`${issuer}/`)) throw new Error('Invalid OIDC back-channel endpoint');
    return `${internalIssuer}${endpoint.slice(issuer.length)}`;
  };
  const value = { ...metadata, token_endpoint: internalEndpoint(metadata.token_endpoint), jwks_uri: internalEndpoint(metadata.jwks_uri) };
  discoveryCache = { expires: Date.now() + 10 * 60_000, value };
  return value;
}
function base64url(value: Buffer | string): string { return Buffer.from(value).toString('base64url'); }
async function verifyIdToken(token: string, nonce: string, jwksUri: string): Promise<User> {
  const parts = token.split('.');
  if (parts.length !== 3) throw new Error('Malformed ID token');
  const header = JSON.parse(Buffer.from(parts[0], 'base64url').toString('utf8'));
  const claims = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
  if (!['RS256', 'PS256', 'ES256'].includes(header.alg) || !header.kid) throw new Error('Unsupported ID token signing algorithm');
  const jwksResponse = await fetch(jwksUri, { headers: { Accept: 'application/json' } });
  if (!jwksResponse.ok) throw new Error('OIDC key lookup failed');
  const jwks = await jwksResponse.json() as { keys?: any[] };
  const jwk = jwks.keys?.find((key) => key.kid === header.kid && (!key.use || key.use === 'sig') && (!key.alg || key.alg === header.alg));
  if (!jwk) throw new Error('OIDC signing key was not found');
  const publicKey = crypto.createPublicKey({ key: jwk, format: 'jwk' });
  const digest = header.alg === 'ES256' ? 'sha256' : 'RSA-SHA256';
  const signedBytes = Buffer.from(`${parts[0]}.${parts[1]}`);
  const signature = Buffer.from(parts[2], 'base64url');
  const valid = header.alg === 'PS256'
    ? crypto.verify(digest, signedBytes, { key: publicKey, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength: 32 }, signature)
    : header.alg === 'ES256'
      ? crypto.verify(digest, signedBytes, { key: publicKey, dsaEncoding: 'ieee-p1363' }, signature)
      : crypto.verify(digest, signedBytes, publicKey, signature);
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!valid || claims.iss !== issuer || !aud.includes(clientId) || (claims.azp && claims.azp !== clientId)
      || !Number.isFinite(claims.exp) || !Number.isFinite(claims.iat)
      || claims.exp <= Date.now() / 1000 || claims.iat > Date.now() / 1000 + 60 || claims.nonce !== nonce || typeof claims.sub !== 'string') {
    throw new Error('ID token validation failed');
  }
  return { sub: claims.sub, email: claims.email, name: claims.name };
}

export function installOidcAuth(app: express.Express): void {
  const router = express.Router();
  router.get('/login', async (_req, res) => {
    if (!configured()) return res.status(503).json({ error: 'OIDC authentication is not configured.' });
    try {
      const provider = await discovery();
      const flowId = crypto.randomBytes(32).toString('base64url');
      const state = crypto.randomBytes(32).toString('base64url');
      const nonce = crypto.randomBytes(32).toString('base64url');
      const verifier = crypto.randomBytes(48).toString('base64url');
      const store = readStore();
      store.flows[flowId] = { state, nonce, verifier, expires: Date.now() + 10 * 60_000 };
      writeStore(store);
      setCookie(res, FLOW_COOKIE, flowId, 10 * 60_000);
      const url = new URL(provider.authorization_endpoint);
      url.search = new URLSearchParams({ response_type: 'code', client_id: clientId, redirect_uri: redirectUri,
        scope: 'openid profile email', state, nonce, code_challenge: base64url(crypto.createHash('sha256').update(verifier).digest()), code_challenge_method: 'S256' }).toString();
      return res.redirect(url.toString());
    } catch { return res.status(502).json({ error: 'OIDC provider could not be reached.' }); }
  });
  router.get('/callback', async (req, res) => {
    const flowId = parseCookies(req)[FLOW_COOKIE];
    const store = readStore();
    const flow = flowId ? store.flows[flowId] : undefined;
    if (!configured() || !flow || flow.expires < Date.now() || req.query.state !== flow.state || typeof req.query.code !== 'string') {
      return res.status(401).send('OIDC sign-in could not be validated. Please restart sign-in.');
    }
    delete store.flows[flowId!];
    writeStore(store);
    try {
      const provider = await discovery();
      const authMethods = provider.token_endpoint_auth_methods_supported || ['client_secret_basic'];
      const tokenForm = new URLSearchParams({ grant_type: 'authorization_code', code: String(req.query.code), redirect_uri: redirectUri,
        client_id: clientId, code_verifier: flow.verifier });
      const tokenHeaders: Record<string, string> = { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' };
      if (authMethods.includes('client_secret_post')) tokenForm.set('client_secret', clientSecret);
      else if (authMethods.includes('client_secret_basic')) {
        tokenHeaders.Authorization = `Basic ${Buffer.from(`${encodeURIComponent(clientId)}:${encodeURIComponent(clientSecret)}`).toString('base64')}`;
      } else throw new Error('Unsupported OIDC token endpoint authentication method');
      const tokenResponse = await fetch(provider.token_endpoint, { method: 'POST', headers: tokenHeaders, body: tokenForm });
      const tokens = await tokenResponse.json() as any;
      if (!tokenResponse.ok || typeof tokens.id_token !== 'string') throw new Error('Code exchange failed');
      const user = await verifyIdToken(tokens.id_token, flow.nonce, provider.jwks_uri);
      const sid = crypto.randomBytes(32).toString('base64url');
      const next = readStore();
      next.sessions[sessionKey(sid)] = { user, expires: Date.now() + SESSION_TTL_MS };
      writeStore(next);
      setCookie(res, COOKIE, sid, SESSION_TTL_MS);
      clearCookie(res, FLOW_COOKIE);
      return res.redirect('/');
    } catch { clearCookie(res, FLOW_COOKIE); return res.status(401).send('OIDC sign-in failed. Please restart sign-in.'); }
  });
  router.post('/logout', (req, res) => {
    const sid = parseCookies(req)[COOKIE];
    const store = readStore();
    if (sid) delete store.sessions[sessionKey(sid)];
    writeStore(store);
    clearCookie(res, COOKIE);
    res.json({ success: true });
  });
  router.get('/me', (req, res) => {
    const user = sessionUser(req);
    if (!user) return res.status(401).json({ error: 'Authentication required.' });
    const store = readStore();
    const projects = store.memberships.filter((row) => row.subject === user.sub).map(({ project_id, role }) => ({ project_id, role }));
    if (authIsBootstrapAdmin(user)) projects.push({ project_id: '*', role: 'admin' });
    res.json({ user, projects, developmentMode: developmentMode() });
  });
  router.get('/projects/:projectId/members', (req, res) => {
    const user = sessionUser(req);
    if (!user || roleFor(user, req.params.projectId) !== 'admin') return res.status(user ? 403 : 401).json({ error: 'Project admin access required.' });
    res.json({ members: readStore().memberships.filter((row) => row.project_id === req.params.projectId).map(({ subject, role }) => ({ subject, role })) });
  });
  router.put('/projects/:projectId/members/:subject', express.json(), (req, res) => {
    const user = sessionUser(req);
    if (!user || roleFor(user, req.params.projectId) !== 'admin') return res.status(user ? 403 : 401).json({ error: 'Project admin access required.' });
    const role = req.body?.role as Role;
    if (!['viewer', 'operator', 'admin'].includes(role) || !req.params.subject || req.params.subject.length > 256) return res.status(400).json({ error: 'Invalid project membership.' });
    const store = readStore();
    store.memberships = store.memberships.filter((row) => !(row.project_id === req.params.projectId && row.subject === req.params.subject));
    store.memberships.push({ project_id: req.params.projectId, subject: req.params.subject, role });
    writeStore(store);
    res.json({ success: true });
  });
  router.delete('/projects/:projectId/members/:subject', (req, res) => {
    const user = sessionUser(req);
    if (!user || roleFor(user, req.params.projectId) !== 'admin') return res.status(user ? 403 : 401).json({ error: 'Project admin access required.' });
    const store = readStore();
    store.memberships = store.memberships.filter((row) => !(row.project_id === req.params.projectId && row.subject === req.params.subject));
    writeStore(store);
    res.json({ success: true });
  });
  app.use('/api/auth', router);
  app.use('/api', (req: Request, res: Response, next: NextFunction) => {
    if (req.method === 'OPTIONS') return next();
    if (!developmentMode() && !configured()) return res.status(503).json({ error: 'OIDC authentication is not configured.' });
    const user = sessionUser(req);
    if (!user) return res.status(401).json({ error: 'Authentication required.' });
    (res.locals as any).authUser = user;
    const projectId = String(req.params.projectId || req.body?.project_id || req.query.project_id || '').trim();
    const readOnlyPost = ['/query', '/query/stream', '/query/result', '/answer'].includes(req.path)
      || /^\/query\/[^/]+\/evaluate$/.test(req.path);
    if (req.method === 'POST' && req.path === '/runtime/setup-authorization') {
      if (!authCanRunRuntimeSetup(user)) return res.status(403).json({ error: 'Operator or administrator access is required to set up Runtime.' });
      return next();
    }
    if (projectId) {
      const role = roleFor(user, projectId);
      if (!role) return res.status(403).json({ error: 'Project access denied.' });
      if (!['GET', 'HEAD'].includes(req.method) && !(req.method === 'POST' && readOnlyPost) && role === 'viewer') {
        return res.status(403).json({ error: 'Operator access required.' });
      }
    } else {
      if (req.path.startsWith('/config') && !isAdmin(user)) {
        return res.status(403).json({ error: 'Administrator access required.' });
      }
      if (req.path.startsWith('/runtime/settings') && !isAdmin(user)) {
        return res.status(403).json({ error: 'Administrator access required.' });
      }
      const safeMethod = ['GET', 'HEAD'].includes(req.method);
      if (!safeMethod && !(req.method === 'POST' && readOnlyPost) && !isAdmin(user)) {
        return res.status(403).json({ error: 'Project scope or administrator access is required.' });
      }
    }
    next();
  });
}

export function authRoleFor(user: User, projectId: string): Role | null { return roleFor(user, projectId); }
export function authStoreSnapshot(): Store { return readStore(); }
export function authIsBootstrapAdmin(user: User): boolean {
  return Boolean((developmentMode() && user.sub === developmentUser.sub) || (bootstrapAdmin && user.sub === bootstrapAdmin));
}
export function authCanRunRuntimeSetup(user: User): boolean {
  if (authIsBootstrapAdmin(user)) return true;
  return readStore().memberships.some((row) => row.subject === user.sub && (row.role === 'operator' || row.role === 'admin'));
}
function isAdmin(user: User): boolean {
  return authIsBootstrapAdmin(user) || readStore().memberships.some((row) => row.subject === user.sub && row.role === 'admin');
}
