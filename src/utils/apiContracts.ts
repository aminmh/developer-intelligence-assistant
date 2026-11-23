/** Shapes and display helpers for API contracts (Swagger / OpenAPI documents). */

export const MAX_CONTRACT_BYTES = 10 * 1024 * 1024;

export type SourceKind = 'upload' | 'repo_path' | 'url';
export type SourceStatus = 'ok' | 'empty' | 'failed' | 'missing';

export interface ContractChanges {
  first_version: boolean;
  operations: { added: string[]; removed: string[]; changed: Array<{ operation: string; parts: string[] }> };
  schemas: { added: string[]; removed: string[]; changed: string[] };
}

export interface ContractVersion {
  version_id: string;
  source_id: string;
  state: 'valid' | 'failed' | 'missing';
  dialect: string | null;
  title: string | null;
  api_version: string | null;
  operation_count: number | null;
  schema_count: number | null;
  revision_id: string | null;
  changes: ContractChanges | null;
  errors: string[];
  created_at: string;
  base_paths?: string[];
  schemas?: string[];
  operations?: Array<{ operation: string; method: string; path: string; operation_id: string | null; summary: string; tags: string[]; deprecated: boolean }>;
}

export interface SourceSummary { source_id: string; project_id: string; kind: SourceKind; name: string; status: SourceStatus }

export interface ContractSource extends SourceSummary {
  config: { path?: string; url?: string; refresh_minutes?: number };
  last_errors: string[];
  last_checked_at: string | null;
  current_version: ContractVersion | null;
  attachments: Array<{ attachment_id: string; project_id: string; role: 'provider' | 'consumer' }>;
}

export interface ContractAttachment {
  attachment_id: string;
  source_id: string;
  project_id: string;
  role: 'provider' | 'consumer';
  mode: 'follow_latest' | 'pinned';
  pinned_version_id: string | null;
  source: SourceSummary;
  resolved_version: ContractVersion | null;
  available: boolean;
}

export const kindLabel = (kind: SourceKind): string =>
  kind === 'upload' ? 'Uploaded file' : kind === 'repo_path' ? 'File in the project code' : 'Public URL';

export const sourceDetail = (source: ContractSource): string => {
  if (source.kind === 'repo_path') return source.config.path || '';
  if (source.kind !== 'url') return '';
  const minutes = source.config.refresh_minutes;
  const every = !minutes ? 'checked on request' : minutes % 1440 === 0 ? `checked every ${minutes / 1440} day(s)`
    : minutes % 60 === 0 ? `checked every ${minutes / 60} hour(s)` : `checked every ${minutes} minutes`;
  return `${source.config.url || ''} · ${every}`;
};

export const statusTone = (status: SourceStatus): 'ok' | 'warn' | 'bad' =>
  status === 'ok' ? 'ok' : status === 'empty' ? 'warn' : 'bad';

/** "Orders API 1.4 · 12 operations · 3 Nov 2026" */
export const versionLabel = (version: ContractVersion): string => {
  const date = new Date(version.created_at);
  const day = Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  return [`${version.title || 'Untitled'} ${version.api_version || ''}`.trim(),
    `${version.operation_count ?? 0} operation${version.operation_count === 1 ? '' : 's'}`, day].filter(Boolean).join(' · ');
};

/** One line saying what a version changed compared with the one before it. */
export const changeSummary = (version: ContractVersion): string => {
  const changes = version.changes;
  if (!changes) return '';
  if (changes.first_version) return 'First version';
  const parts: string[] = [];
  const count = (items: unknown[], word: string) => { if (items.length) parts.push(`${items.length} ${word}`); };
  count(changes.operations.added, 'added');
  count(changes.operations.removed, 'removed');
  count(changes.operations.changed, 'changed');
  const schemas = changes.schemas.added.length + changes.schemas.removed.length + changes.schemas.changed.length;
  const operations = parts.length ? `Operations: ${parts.join(', ')}` : '';
  const schemaText = schemas ? `${schemas} schema${schemas === 1 ? '' : 's'} changed` : '';
  return [operations, schemaText].filter(Boolean).join(' · ') || 'Descriptions only';
};
