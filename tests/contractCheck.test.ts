import assert from 'node:assert/strict';
import test from 'node:test';
import { changeLabel, findingGroups, severityLabel, type Finding } from '../src/utils/contractCheck';

test('findings group by severity and labels read naturally', () => {
  const finding = (severity: string, id: string) => ({ severity, finding_id: id }) as Finding;
  const groups = findingGroups([finding('info', 'a'), finding('error', 'b'), finding('info', 'c')]);
  assert.deepEqual(groups.map((group) => [group.severity, group.findings.length]), [['error', 1], ['info', 2]]);
  assert.equal(severityLabel('info', 2), 'notes'); assert.equal(severityLabel('error', 1), 'error');
  assert.equal(changeLabel({ change: 'removed', parts: [], schemas: [] }), 'Removed');
  assert.equal(changeLabel({ change: 'schema', parts: [], schemas: ['Order'] }), 'Schema changed: Order');
  assert.equal(changeLabel({ change: 'changed', parts: ['request_body', 'responses'], schemas: ['Order'] }),
    'Changed: request body, responses, schema Order');
});
