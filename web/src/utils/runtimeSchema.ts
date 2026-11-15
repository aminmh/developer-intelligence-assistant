import type { RuntimeSchemaDiscovery, RuntimeSchemaExtraction, RuntimeSchemaSource, SavedSchemaDraft } from '../types';

export const RUNTIME_SCHEMA_HELPER = 'http://127.0.0.1:47831';
export type RuntimeSchemaOperation = 'discover' | 'extract' | 'ssh-scan' | 'associate';
export type RuntimeSchemaScope = { source: RuntimeSchemaSource; database?: string; schemas?: string[] };
export type RuntimeSchemaSsh = { host: string; ssh_user?: string; ssh_port?: number; host_fingerprint?: string; confirmed?: boolean };
type Request = (url: string, init?: RequestInit) => Promise<Response>;

async function jsonResponse(response: Response): Promise<any> {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : typeof data.error === 'string' ? data.error : `Request failed (${response.status}).`);
  return data;
}

/** Every helper operation receives a fresh grant limited to its exact selection. */
export async function runtimeSchemaOperation<T>(request: Request, operation: RuntimeSchemaOperation, scope: RuntimeSchemaScope, ssh?: RuntimeSchemaSsh, helperRequest: Request = fetch): Promise<T> {
  const authorization = await jsonResponse(await request('/api/runtime/schema-authorization', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...scope, operation }),
  }));
  if (typeof authorization.token !== 'string' || !authorization.token) throw new Error('Schema authorization was unavailable. Sign in as a global administrator and retry.');
  let response: Response;
  try {
    response = await helperRequest(`${RUNTIME_SCHEMA_HELPER}/runtime-schema/${operation}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${authorization.token}` },
      body: JSON.stringify({ ...scope, ...(ssh ? { ssh } : {}) }), signal: AbortSignal.timeout(60_000),
    });
  } catch {
    throw new Error('The local Runtime helper did not respond. Start the helper on this computer, check browser local-network access, and retry.');
  }
  return jsonResponse(response);
}

export function discoverRuntimeSchema(request: Request, scope: RuntimeSchemaScope): Promise<RuntimeSchemaDiscovery> {
  return runtimeSchemaOperation(request, 'discover', scope);
}

/** Extraction stays opaque: the API verifies the helper receipt before saving. */
export async function saveRuntimeSchema(request: Request, scope: RuntimeSchemaScope, helperRequest: Request = fetch): Promise<SavedSchemaDraft> {
  const extraction = await runtimeSchemaOperation<RuntimeSchemaExtraction>(request, 'extract', scope, undefined, helperRequest);
  return jsonResponse(await request('/api/schema-drafts/runtime', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(extraction),
  }));
}

export function runtimeSchemaNavigation(current: string, projectId: string, environment: string, containerId = ''): string {
  const url = new URL(current);
  url.searchParams.set('tab', 'schema');
  url.searchParams.set('schema_source', 'runtime');
  url.searchParams.set('project_id', projectId);
  if (environment) url.searchParams.set('environment', environment); else url.searchParams.delete('environment');
  if (containerId) url.searchParams.set('container_id', containerId); else url.searchParams.delete('container_id');
  url.searchParams.delete('panel');
  return url.toString();
}
