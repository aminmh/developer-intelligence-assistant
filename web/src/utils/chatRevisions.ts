/** A project revision as the chat shows it (the API's `revision` on answers and in the picker). */
export interface RevisionView {
  project_id?: string;
  revision_id: string;
  source_kind: 'upload' | 'git' | 'working_tree' | null;
  commit_sha: string | null;
  base_commit_sha?: string | null;
  created_at: string | null;
  /** How the answer chose it: the project's latest, or a revision the user pinned. */
  requested?: 'latest' | 'pinned';
}

export interface ChatProjectRevisions {
  project_id: string;
  revisions: RevisionView[];
}

/** Picker values for revisions start with this; uploaded files use their own IDs. */
const PREFIX = 'rev:';

export function revisionOptionValue(projectId: string, revision: string): string {
  return `${PREFIX}${projectId}:${revision}`;
}

/** `rev:shop:latest` → { projectId: 'shop', revision: 'latest' }; anything else → null. */
export function parseRevisionOption(value: string): { projectId: string; revision: string } | null {
  if (!value.startsWith(PREFIX)) return null;
  const rest = value.slice(PREFIX.length);
  const split = rest.lastIndexOf(':');
  if (split <= 0) return null;
  const projectId = rest.slice(0, split);
  const revision = rest.slice(split + 1);
  return /^(latest|rev_[0-9a-f]{32})$/.test(revision) ? { projectId, revision } : null;
}

/** "2 min ago", "3 h ago", "4 d ago"; registry times are UTC without a zone suffix. */
export function relativeAge(createdAt: string | null | undefined, now: number = Date.now()): string {
  if (!createdAt) return '';
  const time = Date.parse(createdAt.includes('T') ? createdAt : createdAt.replace(' ', 'T') + 'Z');
  if (Number.isNaN(time)) return '';
  const minutes = Math.max(0, Math.round((now - time) / 60_000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

/** Short label for a revision: "working tree · 2 min ago (on 1670d9e)", "commit 1670d9e · 3 h ago". */
export function describeRevision(revision: RevisionView, now: number = Date.now()): string {
  const age = relativeAge(revision.created_at, now);
  const when = age ? ` · ${age}` : '';
  if (revision.source_kind === 'working_tree') {
    const base = revision.base_commit_sha ? ` (on ${revision.base_commit_sha.slice(0, 7)})` : '';
    return `working tree${when}${base}`;
  }
  if (revision.commit_sha?.startsWith('upload_')) return `upload ${revision.commit_sha.slice(7, 14)}${when}`;
  if (revision.commit_sha) return `commit ${revision.commit_sha.slice(0, 7)}${when}`;
  return `upload${when}`;
}
