import { makeRequest } from '../../../ui/makeRequest';

export type Executor = 'constant_vus' | 'ramping_vus' | 'constant_arrival_rate';
export type AuthMode = 'public' | 'bearer' | 'http_login';
export type CsrfMode = 'none' | 'header' | 'field';
export type LoginConfig = {
  login_path: string; username_field: string; password_field: string; csrf_mode: CsrfMode; csrf_path: string | null;
  csrf_cookie_name: string; csrf_field_name: string; csrf_header_name: string; check_path: string | null;
};
export type Stage = { duration_seconds: number; target: number };
export type Target = {
  target_id: string; name: string; origin: string; auth_mode: AuthMode; login: LoginConfig;
  executor: Executor; virtual_users: number; duration_seconds: number; think_time_ms: number; warmup_seconds: number;
  rate_per_second?: number; max_virtual_users?: number; stages?: Stage[];
  thresholds: { p95_ms?: number; error_rate?: number }; route_params: Record<string, Record<string, string>>;
  unassigned?: boolean; updated_at?: string;
};
export type RouteSuggestion = { method: 'GET'; route: string; parameters: string[]; requests: number; failures: number;
  error_rate: number | null; p95_ms: number | null; last_seen: string | null };
export type Selection = { template: string; params: Record<string, string>; custom?: boolean };
export type RouteResult = {
  path: string; template?: string; count: number; throughput_rps: number; errors: number; error_rate: number; network_errors?: number;
  auth_rejected?: number; statuses?: Record<'2xx' | '3xx' | '4xx' | '5xx', number>; latency_ms: Record<string, number>;
  thresholds?: { latency: boolean | null; errors: boolean | null };
};
export type Run = {
  run_id: string; state: 'completed'; target_id: string; target_name: string; target_origin: string; routes: string[];
  route_templates?: string[]; auth_mode: string; workload: Partial<Target> & { executor: Executor; duration_seconds: number };
  runner_version: string; complete: boolean; cancelled: boolean; partial_reasons: string[]; thresholds: Target['thresholds'];
  thresholds_failed?: boolean; route_results: RouteResult[]; created_at: string; finished_at?: string;
  revision_id: string | null; commit_sha: string | null;
};
export type Job = {
  run_id: string; state: 'running' | 'failed' | 'completed'; target_name?: string; target_origin?: string; routes?: string[];
  error?: string | null; started_at: string; expected_seconds: number; revision_id?: string | null;
  progress?: { elapsed_seconds: number; expected_seconds: number; percent: number; requests?: number; requests_per_second?: number };
};
export type Delta = { before: number | null; after: number | null; change_pct?: number | null };
export type Comparison = {
  equivalent: boolean; differences: string[]; regressions: number;
  revisions: { before: string | null; after: string | null; changed: boolean };
  routes: { path: string; p50: Delta; p95: Delta; p99: Delta; throughput_rps: Delta; error_rate: Delta; regression: boolean; improvement: boolean }[];
};

export const LOCAL_ORIGIN = 'http://host.docker.internal:80';
export const emptyLogin = (): LoginConfig => ({ login_path: '/login', username_field: 'email', password_field: 'password',
  csrf_mode: 'header', csrf_path: '/login', csrf_cookie_name: 'XSRF-TOKEN', csrf_field_name: '_token', csrf_header_name: 'X-XSRF-TOKEN', check_path: '' });
export const emptyTarget = (): Target => ({ target_id: '', name: 'Local Docker', origin: LOCAL_ORIGIN, auth_mode: 'public', login: emptyLogin(),
  executor: 'constant_vus', virtual_users: 5, duration_seconds: 30, think_time_ms: 0, warmup_seconds: 5,
  rate_per_second: 10, max_virtual_users: 20, stages: [{ duration_seconds: 10, target: 5 }, { duration_seconds: 20, target: 20 }],
  thresholds: {}, route_params: {} });

/** Login presets for common frameworks; the developer can still edit every field. */
export const LOGIN_PRESETS: { id: string; name: string; login: Partial<LoginConfig> }[] = [
  { id: 'laravel', name: 'Laravel session', login: { login_path: '/login', username_field: 'email', password_field: 'password', csrf_mode: 'header',
    csrf_path: '/login', csrf_cookie_name: 'XSRF-TOKEN', csrf_header_name: 'X-XSRF-TOKEN' } },
  { id: 'sanctum', name: 'Laravel Sanctum SPA', login: { login_path: '/login', username_field: 'email', password_field: 'password', csrf_mode: 'header',
    csrf_path: '/sanctum/csrf-cookie', csrf_cookie_name: 'XSRF-TOKEN', csrf_header_name: 'X-XSRF-TOKEN' } },
  { id: 'symfony', name: 'Symfony form login', login: { login_path: '/login', username_field: '_username', password_field: '_password', csrf_mode: 'none', csrf_path: null } },
  { id: 'form', name: 'Form with CSRF field', login: { csrf_mode: 'field', csrf_cookie_name: 'XSRF-TOKEN', csrf_field_name: '_token' } },
];

export async function perfRequest<T = any>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init || {};
  const response = await makeRequest(path, json === undefined ? rest : { ...rest, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(json) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof body.detail === 'string' ? body.detail : 'Performance request failed.') as Error & { status?: number; activeRunId?: string };
    error.status = response.status;
    error.activeRunId = body.active_run_id;
    throw error;
  }
  return body as T;
}
export const projectPath = (projectId: string, suffix = '') => `/api/code-quality/projects/${encodeURIComponent(projectId)}/performance${suffix}`;

export const isLocalOrigin = (origin: string) => {
  try { return ['host.docker.internal', 'localhost', '127.0.0.1'].includes(new URL(origin).hostname); } catch { return false; }
};
export const peakUsers = (target: Target) => target.executor === 'constant_arrival_rate' ? Number(target.max_virtual_users || 0)
  : target.executor === 'ramping_vus' ? Math.max(0, ...(target.stages || []).map((stage) => Number(stage.target) || 0)) : Number(target.virtual_users || 0);
export const runSeconds = (target: Target) => (target.executor === 'ramping_vus'
  ? (target.stages || []).reduce((sum, stage) => sum + (Number(stage.duration_seconds) || 0), 0) : Number(target.duration_seconds || 0));
export const routeParameters = (template: string) => [...template.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\??\}/g)].map((match) => match[1]);
export const fillRoute = (selection: Selection) => selection.template.replace(/\{([A-Za-z_][A-Za-z0-9_]*)(\??)\}/g,
  (_all, name, optional) => selection.params[name] || (optional ? '' : `{${name}}`)).replace(/\/{2,}/g, '/');

export const ms = (value?: number | null) => Number.isFinite(value) ? `${Number(value).toFixed(1)} ms` : '—';
export const pct = (value?: number | null) => Number.isFinite(value) ? `${(Number(value) * 100).toFixed(1)}%` : '—';
export const shortRevision = (run: { commit_sha?: string | null; revision_id?: string | null }) =>
  run.commit_sha ? run.commit_sha.slice(0, 8) : run.revision_id ? run.revision_id.slice(4, 12) : 'no revision';
export const workloadLabel = (workload: Run['workload']) => workload.executor === 'constant_arrival_rate'
  ? `${workload.rate_per_second} req/s per route · ${workload.duration_seconds}s`
  : workload.executor === 'ramping_vus' ? `ramp to ${workload.virtual_users} VUs · ${workload.duration_seconds}s`
    : `${workload.virtual_users} VUs per route · ${workload.duration_seconds}s`;

export function download(name: string, text: string, type: string) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([text], { type }));
  link.download = name;
  link.click();
  URL.revokeObjectURL(link.href);
}
export function runCsv(run: Run): string {
  const header = ['route', 'template', 'requests', 'rps', 'error_rate', '2xx', '3xx', '4xx', '5xx', 'network_errors', 'p50_ms', 'p90_ms', 'p95_ms', 'p99_ms'];
  const quote = (value: unknown) => `"${String(value ?? '').replace(/"/g, '""')}"`;
  const rows = run.route_results.map((item) => [item.path, item.template || item.path, item.count, item.throughput_rps, item.error_rate,
    item.statuses?.['2xx'], item.statuses?.['3xx'], item.statuses?.['4xx'], item.statuses?.['5xx'], item.network_errors,
    item.latency_ms?.med, item.latency_ms?.['p(90)'], item.latency_ms?.['p(95)'], item.latency_ms?.['p(99)']].map(quote).join(','));
  return [header.join(','), ...rows].join('\n') + '\n';
}
