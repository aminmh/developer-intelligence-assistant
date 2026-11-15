import type { ScopeProject } from '../composables/useProjectScope';
import type { UploadedFile } from '../types';

/** Registered uploads use revision readiness; legacy files use their chunk count. */
export function fileIndexStatus(file: UploadedFile, projects: ScopeProject[], inventoryAvailable: boolean): 'indexed' | 'pending' | 'unknown' {
  if (!file.projectId || !file.revisionId) return file.chunkCount > 0 ? 'indexed' : 'pending';
  if (!inventoryAvailable) return 'unknown';
  const revision = projects.find((project) => project.project_id === file.projectId)
    ?.revisions.find((item) => item.revision_id === file.revisionId);
  return revision?.indexed ? 'indexed' : 'pending';
}
