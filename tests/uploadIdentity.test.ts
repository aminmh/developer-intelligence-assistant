import assert from 'node:assert/strict';
import test from 'node:test';
import { validateUploadIdentity } from '../src/services/uploadIdentity';

test('upload identity requires a project and validates Git metadata only when supplied', () => {
  const empty = validateUploadIdentity('', '');
  assert.equal(empty.identity, null);
  assert.match(empty.projectError!, /project ID/);
  assert.equal(empty.commitError, null);
  assert.deepEqual(validateUploadIdentity('pizza', '').identity, { project_id: 'pizza' });
  assert.deepEqual(validateUploadIdentity('pizza', '   ').identity, { project_id: 'pizza' });
  for (const revision of ['main', 'a'.repeat(7), 'a'.repeat(39), 'a'.repeat(41), 'x'.repeat(40)]) {
    assert.equal(validateUploadIdentity('pizza', revision).identity, null);
  }
});

test('upload identity preserves project scope and normalizes pasted full Git hashes', () => {
  for (const size of [40, 64]) {
    assert.deepEqual(validateUploadIdentity(' Pizza-API_2.0 ', ` ${'A'.repeat(size)}\n`).identity,
      { project_id: 'Pizza-API_2.0', commit_sha: 'a'.repeat(size) });
  }
});

test('invalid project scopes cannot enable uploading', () => {
  for (const project of ['../pizza', 'pizza/api', 'my project', '-pizza', 'a'.repeat(129)]) {
    const result = validateUploadIdentity(project, 'a'.repeat(40));
    assert.equal(result.identity, null);
    assert.ok(result.projectError);
    assert.equal(result.commitError, null);
  }
});
