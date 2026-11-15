import { makeRequest } from '../../ui/makeRequest';

export type RuleLinkState = 'suggested' | 'confirmed' | 'rejected' | 'manual';
export type RuleDocument = { doc_id: string; url_id: string; url: string; title: string; revision: number; synced_at: string };
export type RuleLink = {
  doc_id: string; symbol_id: string; state: RuleLinkState; score: number | null; file: string | null; line: number | null;
  revision_id: string | null; rule_changed: boolean; code_changed: boolean; needs_review: boolean;
  title?: string; url?: string; synced_at?: string;
};
export type RuleSync = { status: 'synchronized' | 'unavailable' | 'never_synced'; synced_at: string | null; reason?: string | null;
  document_count: number; limit_exceeded?: boolean; oversized_count?: number };
export type RuleRuntime = { doc_id: string; calls: number; failures: number; error_rate: number; p95_ms: number | null;
  last_seen: string | null; never_seen: boolean; high_error_rate: boolean; linked_symbols: number };
export type ImpactReview = { doc_id: string; title: string; url: string; synced_at: string;
  verdict: 'unaffected' | 'changes_behaviour' | 'may_break' | 'unclear'; reason: string; hunk_ids: string[]; confirmed: boolean };
export type ImpactResult = { reviews: ImpactReview[]; changed_symbols: string[]; reason?: string; provider?: string;
  hunks: { id: string; file: string; start: number; end: number; symbol_ids: string[] }[] };

export const ruleKnowledgePath = (projectId: string, suffix = '') =>
  `/api/code-quality/projects/${encodeURIComponent(projectId)}/rule-knowledge${suffix}`;

export async function ruleRequest<T = any>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init || {};
  const response = await makeRequest(path, json === undefined ? rest : {
    ...rest, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(json),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'The business rule request failed.');
  return data as T;
}

/** Outline's in-app link for a document. The bundled Outline container is published on port 3020. */
export function outlineHref(instanceUrl: string | null | undefined, path: string): string {
  try {
    const base = new URL(instanceUrl || 'http://outline:3000');
    if (base.hostname === 'outline' && base.port === '3000' && typeof window !== 'undefined') {
      base.protocol = window.location.protocol; base.hostname = window.location.hostname; base.port = '3020';
    }
    const target = new URL(path, base);
    return target.origin === base.origin && target.pathname.startsWith('/doc/') ? target.href : base.href;
  } catch { return path; }
}

export function syncedAgo(value: string | null | undefined): string {
  if (!value) return 'never synced';
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60_000));
  if (minutes < 1) return 'synced just now';
  if (minutes < 60) return `synced ${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `synced ${hours} h ago`;
  return `synced ${new Date(value).toLocaleDateString()}`;
}

export const symbolLabel = (symbolId: string) => symbolId.startsWith('route:') ? symbolId.slice(6)
  : symbolId.startsWith('table:') ? `table ${symbolId.slice(6)}` : symbolId;
