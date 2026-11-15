/** Shapes and display helpers for the contract conformance and change impact reports. */
import type { ContractVersion } from './apiContracts';

export type Severity = 'error' | 'warning' | 'info';

export interface Finding {
  finding_id: string; check: string; code: string; severity: Severity; message: string;
  operation: string | null; location: { file?: string; line?: number; symbol?: string }; evidence: Record<string, unknown>;
}

export interface ConformanceReport {
  available: boolean; project_id: string; revision_id: string | null; version: ContractVersion;
  /** Which side was checked: a consumer's requests and models, or a provider's routes. */
  check: 'consumer' | 'provider' | 'runtime';
  summary: { errors: number; warnings: number; info: number };
  coverage: {
    operations_total: number; inferred_prefix: string | null;
    // consumer
    requests_total?: number; requests_checked?: number; requests_unresolved?: number; requests_matched?: number;
    operations_used?: number; unused_operations?: string[];
    // provider
    routes_total?: number; routes_in_scope?: number; routes_documented?: number; operations_implemented?: number; scope?: string;
    // runtime
    requests_outside_api?: number; operations_observed?: number; unobserved_operations?: string[];
  };
  findings: Finding[]; notes: string[];
}

export interface ImpactEntry {
  operation: string; change: 'removed' | 'changed' | 'schema'; parts: string[]; schemas: string[];
  calls: Array<{ file: string; line: number; symbol: string; request: string }>;
  components: Array<{ id: string; name: string; file: string }>; routes: string[];
}

export interface ImpactReport {
  available: boolean; from_version?: ContractVersion; to_version?: ContractVersion; versions: ContractVersion[];
  summary: { operations_changed: number; operations_in_use: number; components: number; routes: number; operations_added: number };
  entries: ImpactEntry[]; added_operations: string[]; changed_schemas: string[]; notes: string[];
}

const ORDER: Severity[] = ['error', 'warning', 'info'];

/** Findings grouped by severity, most severe first; empty groups are left out. */
export const findingGroups = (findings: Finding[]): Array<{ severity: Severity; findings: Finding[] }> =>
  ORDER.map((severity) => ({ severity, findings: findings.filter((finding) => finding.severity === severity) }))
    .filter((group) => group.findings.length > 0);

export const severityLabel = (severity: Severity, count: number): string => {
  const one = { error: 'error', warning: 'warning', info: 'note' }[severity];
  return count === 1 ? one : `${one}s`;
};

const PARTS: Record<string, string> = { parameters: 'parameters', request_body: 'request body', responses: 'responses', deprecated: 'deprecation' };

/** "Removed", "Changed: parameters, responses", or "Schema changed: Order". */
export const changeLabel = (entry: Pick<ImpactEntry, 'change' | 'parts' | 'schemas'>): string => {
  if (entry.change === 'removed') return 'Removed';
  const schemas = entry.schemas.length ? `schema ${entry.schemas.join(', ')}` : '';
  if (entry.change === 'schema') return `Schema changed: ${entry.schemas.join(', ')}`;
  return `Changed: ${[...entry.parts.map((part) => PARTS[part] || part), schemas].filter(Boolean).join(', ')}`;
};
