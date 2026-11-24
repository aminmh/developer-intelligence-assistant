import assert from 'node:assert/strict';
import test from 'node:test';
import { defaultReusedRevision, phpRuntimeSetupCommand, reusableRevisions, runtimeProjectOptions, runtimeSetupReadiness } from '../src/utils/runtimeProjects';
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

test('runtime setup distinguishes unindexed source, another commit and a ready matching commit', () => {
  assert.equal(runtimeSetupReadiness('pizza', 'abc', []).status, 'missing');
  assert.equal(runtimeSetupReadiness('pizza', 'abc', [{ ...uploaded, state: 'indexing' }]).status, 'missing');
  assert.equal(runtimeSetupReadiness('pizza', 'abc', [uploaded]).status, 'mismatch', 'ZIP identity cannot verify the running Git commit');
  assert.equal(runtimeSetupReadiness('pizza', 'abc', [{ ...uploaded, commit_sha: 'def' }]).status, 'mismatch');
  assert.deepEqual(runtimeSetupReadiness('pizza', 'abc', [{ ...uploaded, commit_sha: 'abc' }]), { status: 'ready', revisionId: 'rev_upload' });
  assert.equal(runtimeSetupReadiness('other', 'abc', [{ ...uploaded, commit_sha: 'abc' }]).status, 'missing', 'project identity must also match');
  assert.equal(runtimeSetupReadiness('pizza', 'abc', [], false).status, 'unknown', 'failed inventory is not evidence of absence');
});

test('PHP setup guidance quotes the real project path for copying into a terminal', () => {
  assert.equal(phpRuntimeSetupCommand('/Projects/Pizza App', 'pizza'), "just php-otel-setup '/Projects/Pizza App' --project-id 'pizza'");
  assert.equal(phpRuntimeSetupCommand("/Projects/Amin's pizza", 'pizza'), "just php-otel-setup '/Projects/Amin'\\''s pizza' --project-id 'pizza'");
});

test('setup reuses an existing ready revision instead of adding a Docker-only one', () => {
  const sha = (char: string) => char.repeat(40);
  const records = [
    { ...uploaded, revision_id: 'rev_old', commit_sha: sha('a'), updated_at: '2026-11-01 10:00:00' },
    { ...uploaded, revision_id: 'rev_new', commit_sha: sha('b'), updated_at: '2026-11-20 10:00:00' },
    { ...uploaded, revision_id: 'rev_new', commit_sha: sha('b'), updated_at: '2026-11-20 10:00:00' },
    { ...uploaded, revision_id: 'rev_zip', commit_sha: null },
    { ...uploaded, revision_id: 'rev_building', commit_sha: sha('c'), state: 'indexing' },
    { ...uploaded, project_id: 'other', revision_id: 'rev_other', commit_sha: sha('d') },
  ];
  const revisions = reusableRevisions('pizza', records);
  assert.deepEqual(revisions.map((row) => row.revisionId), ['rev_new', 'rev_old'], 'ready Git revisions of the project, newest first, once each');
  assert.equal(defaultReusedRevision(revisions, sha('a')), 'rev_old', 'the revision matching the detected commit wins');
  assert.equal(defaultReusedRevision(revisions, sha('f')), 'rev_new', 'otherwise the newest is reused');
  assert.equal(defaultReusedRevision([], sha('f')), '', 'a project with no revision keeps the detected commit');
  // Containers labeled with the reused revision's commit add no Docker-only option.
  const options = runtimeProjectOptions([{ ...records[1], running_commit_shas: [sha('b')] }]);
  assert.deepEqual(options.map((option) => option.key), ['pizza:rev_new']);
});
