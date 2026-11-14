import assert from 'node:assert/strict';
import test from 'node:test';
import { runtimeProjectOptions } from '../src/utils/runtimeProjects';
import type { RuntimeProjectRecord } from '../src/types';

const uploaded: RuntimeProjectRecord = {
  project_id: 'pizza', revision_id: 'rev_upload', commit_sha: null, state: 'ready',
  containers: [], running_containers: 0, running_project_containers: 2,
  running_commit_shas: ['abc'], deployment_mismatch: true,
};

test('ZIP revisions remain selectable alongside containers running unindexed code', () => {
  const options = runtimeProjectOptions([uploaded]);
  assert.equal(options.length, 2);
  assert.equal(options[0].key, 'pizza:rev_upload');
  assert.equal(options[0].revision_id, 'rev_upload');
  assert.equal(options[0].commit_sha, null);
  assert.equal(options[1].key, 'pizza:docker-only');
  assert.equal(options[1].revision_id, null);
  assert.equal(options[1].commit_sha, 'abc');
});

test('a matching Git deployment uses its revision without a duplicate Docker option', () => {
  const options = runtimeProjectOptions([{ ...uploaded, revision_id: 'rev_git', commit_sha: 'abc' }]);
  assert.deepEqual(options.map((option) => option.key), ['pizza:rev_git']);
});

test('uploaded revisions remain available without containers or Git commits', () => {
  assert.equal(runtimeProjectOptions([{ ...uploaded, running_commit_shas: [] }])[0].revision_id, 'rev_upload');
  const options = runtimeProjectOptions([{ ...uploaded, revision_id: null }]);
  assert.equal(options.length, 1);
  assert.equal(options[0].key, 'pizza:docker-only');
});
