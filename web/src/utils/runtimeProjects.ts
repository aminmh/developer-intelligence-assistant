import type { RuntimeProjectRecord } from '../types';

export type RuntimeProjectOption = RuntimeProjectRecord & { key: string };

export function runtimeSetupReadiness(
  projectId: string, commit: string,
  revisions: Pick<RuntimeProjectRecord, 'project_id' | 'revision_id' | 'commit_sha' | 'state'>[],
  inventoryAvailable = true,
): { status: 'ready' | 'missing' | 'mismatch' | 'unknown'; revisionId?: string } {
  if (!inventoryAvailable) return { status: 'unknown' };
  const ready = revisions.filter((row) => row.project_id === projectId && row.state === 'ready' && row.revision_id);
  const matching = ready.find((row) => commit && row.commit_sha === commit);
  if (matching) return { status: 'ready', revisionId: matching.revision_id! };
  return { status: ready.length ? 'mismatch' : 'missing' };
}

export function phpRuntimeSetupCommand(directory: string, projectId: string): string {
  const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
  return `just php-otel-setup ${quote(directory)} --project-id ${quote(projectId)}`;
}

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
