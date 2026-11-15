import type { UploadIdentity } from './tusUpload';

export function validateUploadIdentity(projectId: string, commitSha: string): {
  identity: UploadIdentity | null;
  projectError: string | null;
  commitError: string | null;
} {
  const project = projectId.trim();
  const commit = commitSha.trim().toLowerCase();
  const projectError = !project
    ? 'Enter a project ID, for example pizza.'
    : /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(project)
      ? null : 'Use 1–128 letters, numbers, dots, underscores or hyphens, starting with a letter or number.';
  const commitError = !commit || /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(commit)
      ? null : 'Use the full 40- or 64-character hexadecimal commit SHA; a short SHA or branch name is not enough.';
  return { identity: projectError || commitError ? null : { project_id: project, ...(commit ? { commit_sha: commit } : {}) },
    projectError, commitError };
}
