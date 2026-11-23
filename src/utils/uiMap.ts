/** Shapes and lookups for the UI map of a front-end project. */

export interface UiRoute {
  path: string; component: string | null; component_id: string | null; file: string | null;
  source_file: string; line: number; kind: 'config' | 'file'; lazy: boolean; redirect: string | null;
}
export interface UiComponent {
  id: string; name: string; kind: string; file: string; line: number; selector?: string | null;
  uses: string[]; stores: string[]; http_calls: number[]; via_imports: number[];
}
export interface UiStore { id: string; name: string; kind: string; file: string; line: number }
export interface UiHttpCall {
  id: number; method: string; url: string | null; raw: string; file: string; line: number; symbol: string; client: string;
}
export interface UiApp { root: string; framework: string; meta_framework: string | null; version: string | null }
export interface UiMap {
  project_id: string; revision_id: string; framework: string | null; meta_framework: string | null;
  version: string | null; app_root: string; apps: UiApp[]; files_scanned: number;
  routes: UiRoute[]; components: UiComponent[]; stores: UiStore[]; http_calls: UiHttpCall[]; notes: string[];
}

export const frameworkLabel = (map: Pick<UiMap, 'framework' | 'meta_framework' | 'version'>): string => {
  const names: Record<string, string> = { angular: 'Angular', react: 'React', vue: 'Vue', next: 'Next.js', nuxt: 'Nuxt' };
  const name = names[map.meta_framework || map.framework || ''] || 'Unknown';
  return map.version ? `${name} ${map.version.replace(/^[\^~]/, '')}` : name;
};

export const storeKindLabel = (kind: string): string => ({
  pinia: 'Pinia store', redux: 'Redux', store: 'Store', context: 'React context', ngrx: 'NgRx',
  zustand: 'Zustand store', service: 'Service',
} as Record<string, string>)[kind] || kind;

/** Components that render the given component, for "used by". */
export const usedBy = (map: UiMap, componentId: string): UiComponent[] =>
  map.components.filter((component) => component.uses.includes(componentId));

/** Routes that render the given component. */
export const routesOf = (map: UiMap, componentId: string): UiRoute[] =>
  map.routes.filter((route) => route.component_id === componentId);

/** A component's requests: made in its own file, then through the modules it imports. */
export const requestsOf = (map: UiMap, component: UiComponent): Array<UiHttpCall & { direct: boolean }> => {
  const seen = new Set<number>();
  const rows: Array<UiHttpCall & { direct: boolean }> = [];
  for (const [ids, direct] of [[component.http_calls, true], [component.via_imports, false]] as const) {
    for (const id of ids) {
      const call = map.http_calls[id];
      if (call && !seen.has(id)) { seen.add(id); rows.push({ ...call, direct }); }
    }
  }
  return rows;
};

export const matches = (needle: string, ...fields: Array<string | null | undefined>): boolean => {
  const query = needle.trim().toLowerCase();
  return !query || fields.some((field) => (field || '').toLowerCase().includes(query));
};

export type CheckArea = 'accessibility' | 'security' | 'reliability' | 'performance' | 'maintainability';
export interface CheckFinding { file: string; line: number; rule: string; area: CheckArea; severity: 'ERROR' | 'WARNING' | 'INFO'; message: string }
export interface FrontendChecks {
  version: string; files_checked: number; findings: CheckFinding[]; by_area: Record<CheckArea, number>;
  rule_counts: Record<string, number>; notes: string[];
}

export const CHECK_AREAS: Array<{ id: CheckArea; label: string }> = [
  { id: 'accessibility', label: 'Accessibility' }, { id: 'security', label: 'Security' },
  { id: 'reliability', label: 'Reliability' }, { id: 'performance', label: 'Performance' },
  { id: 'maintainability', label: 'Maintainability' },
];

/** Findings of one area grouped by file, files with the most findings first. */
export const findingsByFile = (checks: FrontendChecks, area: CheckArea, needle = ''): Array<{ file: string; findings: CheckFinding[] }> => {
  const groups = new Map<string, CheckFinding[]>();
  for (const finding of checks.findings) {
    if (finding.area !== area || !matches(needle, finding.file, finding.message, finding.rule)) continue;
    groups.set(finding.file, [...(groups.get(finding.file) || []), finding]);
  }
  return [...groups.entries()].map(([file, findings]) => ({ file, findings }))
    .sort((a, b) => b.findings.length - a.findings.length || a.file.localeCompare(b.file));
};

export interface ScreenView {
  trace_id: string; timestamp: string; route: string; duration_ms: number | null; lcp_ms: number | null; inp_ms: number | null;
  cls: number | null; errors: number; slow_interactions: number; requests: number; failed_requests: number; request_ms: number;
}
export interface ScreenSummary {
  route: string; views: number; lcp_p75_ms: number | null; inp_p75_ms: number | null; cls_p75: number | null;
  errors: number; slow_interactions: number; requests_per_view: number;
}
export interface ScreenViews { available: boolean; reason: string | null; window_hours?: number; views: ScreenView[]; screens: ScreenSummary[] }
export interface ScreenViewDetail {
  available: boolean; reason: string | null; view?: ScreenView;
  errors?: Array<{ timestamp: string; error_type: string }>;
  slow_interactions?: Array<{ timestamp: string; duration_ms: number }>;
  requests?: Array<{ trace_id: string; method: string; route: string; status_code: number | null; duration_ms: number | null; failed: boolean }>;
}

/** Google's published thresholds: good, needs improvement, poor. */
export const vitalTone = (metric: 'lcp' | 'inp' | 'cls', value: number | null | undefined): 'ok' | 'warn' | 'bad' | 'dim' => {
  if (value === null || value === undefined) return 'dim';
  const [good, poor] = { lcp: [2500, 4000], inp: [200, 500], cls: [0.1, 0.25] }[metric];
  return value <= good ? 'ok' : value <= poor ? 'warn' : 'bad';
};

export const milliseconds = (value: number | null | undefined): string =>
  value === null || value === undefined ? '–' : value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;

/** The component a screen name refers to: an Inertia page ("Admin/Orders") or a route path. */
export const componentForScreen = (map: UiMap | null, route: string): UiComponent | null => {
  if (!map) return null;
  const routed = map.routes.find((item) => item.path === route && item.component_id);
  if (routed) return map.components.find((component) => component.id === routed.component_id) || null;
  const suffix = `/${route.replace(/^\/+/, '')}.`;
  return map.components.find((component) => `/${component.file}`.toLowerCase().includes(`/pages${suffix}`.toLowerCase())) || null;
};
