import assert from 'node:assert/strict';
import test from 'node:test';
import { describeRevision, parseRevisionOption, relativeAge, revisionOptionValue } from '../src/utils/chatRevisions';

const NOW = Date.parse('2026-11-12T13:10:00Z');

test('revision picker values round-trip and reject anything else', () => {
  const rev = 'rev_' + 'a'.repeat(32);
  assert.deepEqual(parseRevisionOption(revisionOptionValue('docker-proxy', 'latest')), { projectId: 'docker-proxy', revision: 'latest' });
  assert.deepEqual(parseRevisionOption(revisionOptionValue('shop.api', rev)), { projectId: 'shop.api', revision: rev });
  for (const value of ['upload-session-id', 'rev:shop', 'rev::latest', 'rev:shop:rev_123', 'rev:shop:../x']) {
    assert.equal(parseRevisionOption(value), null, value);
  }
});

test('ages read registry UTC timestamps', () => {
  assert.equal(relativeAge('2026-11-12 13:08:00.5', NOW), '2 min ago');
  assert.equal(relativeAge('2026-11-12 10:10:00', NOW), '3 h ago');
  assert.equal(relativeAge('2026-11-08 13:10:00', NOW), '4 d ago');
  assert.equal(relativeAge('2026-11-12 13:09:50', NOW), 'just now');
  assert.equal(relativeAge(null, NOW), '');
});

test('labels name the kind of source an answer came from', () => {
  const base = { revision_id: 'rev_' + 'b'.repeat(32), created_at: '2026-11-12 13:08:00' };
  assert.equal(describeRevision({ ...base, source_kind: 'working_tree', commit_sha: null, base_commit_sha: '1670d9ecc83f' }, NOW),
    'working tree · 2 min ago (on 1670d9e)');
  assert.equal(describeRevision({ ...base, source_kind: 'git', commit_sha: 'f74182ec4ced34ef' }, NOW), 'commit f74182e · 2 min ago');
  assert.equal(describeRevision({ ...base, source_kind: 'upload', commit_sha: null }, NOW), 'upload · 2 min ago');
});
