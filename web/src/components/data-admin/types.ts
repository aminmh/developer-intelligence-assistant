export type StoreName = 'qdrant' | 'neo4j' | 'minio' | 'clickhouse' | 'sqlite' | 'files';
export type Owner = 'revision' | 'project' | 'shared' | 'legacy' | 'orphan' | 'global' | 'transient';

export type InventoryItem = {
  store: StoreName;
  id: string;
  name: string;
  kind: string;
  owner: Owner;
  project_id: string | null;
  revision_id: string | null;
  reason: string;
  count: number | null;
  bytes: number | null;
  updated_at: string | null;
  detail: Record<string, any>;
  deletable: boolean;
};

export type StoreSummary = { status: string; error: string | null; items: number; count: number; bytes: number; orphans: number };

export type DeletionTarget =
  | { kind: 'revision'; project_id: string; revision_id: string }
  | { kind: 'project'; project_id: string }
  | { kind: 'items'; items: Array<{ store: StoreName; id: string }> };

export type PlanStep = {
  action: string; store: string; description: string; estimate: number | null;
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped'; error: string | null;
};

export const STORE_LABELS: Record<StoreName, string> = {
  qdrant: 'Qdrant vectors',
  neo4j: 'Neo4j graphs',
  minio: 'Object storage',
  clickhouse: 'Runtime telemetry',
  sqlite: 'SQLite stores',
  files: 'Files on disk',
};

export const STORE_UNITS: Record<StoreName, string> = {
  qdrant: 'points', neo4j: 'nodes', minio: 'objects', clickhouse: 'rows', sqlite: 'rows', files: 'files',
};

export function formatBytes(value: number | null | undefined): string {
  if (!value) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(size >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : new Intl.NumberFormat().format(value);
}

export async function readJson(response: Response): Promise<any> {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `Request failed (${response.status}).`);
  return data;
}
