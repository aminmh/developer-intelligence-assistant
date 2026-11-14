import type { RuntimeProjectRecord } from '../types';

export type RuntimeProjectOption = RuntimeProjectRecord & { key: string };

// Source revisions have their own identity; ZIP uploads need no Git commit.
export function runtimeProjectOptions(projects: RuntimeProjectRecord[]): RuntimeProjectOption[] {
  const options = new Map<string, RuntimeProjectOption>();
  for (const project of projects) {
    if (project.revision_id) {
      const key = `${project.project_id}:${project.revision_id}`;
      options.set(key, { ...project, key });
    }
    for (const commit of [project.commit_sha, ...(project.running_commit_shas || [])]) {
      if (!commit || (project.revision_id && commit === project.commit_sha)) continue;
      const key = `${project.project_id}:docker-only`;
      if (!options.has(key)) options.set(key, {
        ...project, key, commit_sha: commit, revision_id: null, state: 'awaiting_source',
      });
    }
  }
  return [...options.values()];
}
