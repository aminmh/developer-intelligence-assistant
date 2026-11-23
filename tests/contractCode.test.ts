import assert from 'node:assert/strict';
import test from 'node:test';
import { CODE_TARGETS, lineCount, targetForFramework } from '../src/utils/contractCode';

test('code targets follow the project framework', () => {
  assert.deepEqual(['angular', 'next', 'nuxt', 'vue', null, 'svelte'].map(targetForFramework), ['angular', 'react', 'vue', 'vue', 'models', 'models']);
  assert.equal(lineCount('a\nb\n'), 2); assert.equal(lineCount('a'), 1);
  assert.equal(CODE_TARGETS.length, 5);
});
