import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseGrantAuthorization } from '../src/server/workingTreeRoutes';
import { isWorkingTreeRevision } from '../src/code-quality/projectRevisions';

test('grant authorization header is parsed strictly', () => {
  const grantId = `wtg_${'a'.repeat(32)}`;
  assert.deepEqual(parseGrantAuthorization(`WorkingTreeGrant ${grantId}:${'t'.repeat(43)}`), { grantId, token: 't'.repeat(43) });
  for (const header of [undefined, '', `Bearer ${grantId}:${'t'.repeat(43)}`, `WorkingTreeGrant ${grantId}:short`,
    `WorkingTreeGrant wtg_x:${'t'.repeat(43)}`, `WorkingTreeGrant ${grantId}:${'t'.repeat(43)} extra`]) {
    assert.equal(parseGrantAuthorization(header), null);
  }
});

test('working-tree revisions are recognised for Code Quality', () => {
  assert.equal(isWorkingTreeRevision({ project_id: 'p', revision_id: 'r', commit_sha: null, dirty: true }), true);
  assert.equal(isWorkingTreeRevision({ project_id: 'p', revision_id: 'r', commit_sha: null, source_ref: `wt_${'a'.repeat(64)}` }), true);
  assert.equal(isWorkingTreeRevision({ project_id: 'p', revision_id: 'r', commit_sha: 'a'.repeat(40) }), false);
});
