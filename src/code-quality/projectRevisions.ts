import type { UploadedFile } from '../types';

export type ProjectRevision = {
  project_id: string;
  revision_id: string | null;
  commit_sha: string | null;
  source_ref?: string;
  state?: string;
  updated_at?: string;
  /** A working-tree revision (uncommitted edits): listed but never assessable. */
  dirty?: boolean;
};

export const isWorkingTreeRevision = (revision: ProjectRevision) =>
  revision.dirty === true || (revision.source_ref?.startsWith('wt_') ?? false);

export const revisionKey = (revision: ProjectRevision) => `${revision.project_id}:${revision.commit_sha || revision.source_ref || revision.revision_id}`;

export function assessmentRevisions(files: UploadedFile[], snapshots: ProjectRevision[]): ProjectRevision[] {
  const revisions = new Map<string, ProjectRevision>();
  for (const snapshot of snapshots) {
    if (snapshot.project_id && (snapshot.commit_sha || snapshot.source_ref || snapshot.revision_id)) revisions.set(revisionKey(snapshot), snapshot);
  }
  for (const file of files) {
    if (!file.projectId || (!file.commitSha && !file.revisionId && !file.sourceRef)) continue;
    const revision: ProjectRevision = { project_id: file.projectId, commit_sha: file.commitSha || null,
      source_ref: file.sourceRef, revision_id: file.revisionId || null, state: 'not_indexed', updated_at: file.uploadedAt };
    if (!revisions.has(revisionKey(revision))) revisions.set(revisionKey(revision), revision);
  }
  return [...revisions.values()].sort((left, right) => left.project_id.localeCompare(right.project_id)
    || (right.updated_at || '').localeCompare(left.updated_at || '') || revisionKey(left).localeCompare(revisionKey(right)));
}
