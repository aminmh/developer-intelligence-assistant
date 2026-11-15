import assert from 'node:assert/strict';
import test from 'node:test';
import { fileIndexStatus } from '../src/utils/fileIndexStatus';
import type { UploadedFile } from '../src/types';
import type { ScopeProject } from '../src/composables/useProjectScope';

test('upload status follows the matching project revision, not its placeholder chunk count', () => {
  const file: UploadedFile = { id: 'upload', name: 'code.zip', type: 'zip', size: 1, chunkCount: 0,
    uploadedAt: '', status: 'ready', projectId: 'pizza', revisionId: 'rev_a' };
  const projects: ScopeProject[] = [{ project_id: 'pizza', indexed: true, revisions: [
    { revision_id: 'rev_a', source_kind: 'upload', commit_sha: null, created_at: null, indexed: true },
  ] }];
  assert.equal(fileIndexStatus(file, projects, true), 'indexed');
  assert.equal(fileIndexStatus({ ...file, revisionId: 'rev_b' }, projects, true), 'pending');
  assert.equal(fileIndexStatus({ ...file, projectId: 'other' }, projects, true), 'pending');
  assert.equal(fileIndexStatus(file, projects, false), 'unknown');
  projects[0].revisions[0].indexed = false;
  assert.equal(fileIndexStatus({ ...file, chunkCount: 42 }, projects, true), 'pending');
  assert.equal(fileIndexStatus({ ...file, projectId: undefined, revisionId: undefined, chunkCount: 42 }, [], true), 'indexed');
});
