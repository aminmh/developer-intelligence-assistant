import assert from 'node:assert/strict';
import test from 'node:test';
import { requestProjectScope } from '../../src/server/oidcAuth';

test('the project in the URL path decides the access check', () => {
  assert.deepEqual(requestProjectScope('/code-quality/projects/pizza/performance/runs', undefined, undefined), { projectId: 'pizza' });
  assert.deepEqual(requestProjectScope('/runtime/projects/my%20app/revisions/rev_1', undefined, undefined), { projectId: 'my app' });
  assert.deepEqual(requestProjectScope('/query', 'pizza', undefined), { projectId: 'pizza' });
  assert.deepEqual(requestProjectScope('/code-quality/projects', undefined, undefined), { projectId: '' });
});

test('a different project in the body or query cannot stand in for the path project', () => {
  assert.ok('error' in requestProjectScope('/code-quality/projects/victim/performance/runs', 'mine', undefined));
  assert.ok('error' in requestProjectScope('/code-quality/projects/victim/rule-knowledge/links', undefined, 'mine'));
  assert.deepEqual(requestProjectScope('/code-quality/projects/pizza/x', 'pizza', 'pizza'), { projectId: 'pizza' });
  assert.ok('error' in requestProjectScope('/code-quality/projects/%E0%A4%A/x', undefined, undefined));
});
