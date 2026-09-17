import assert from 'node:assert/strict';
import test from 'node:test';
import type { DomainEntity } from '../src/types';
import { findEntityModelSearchMatches, wrapEntityModelSearchIndex } from '../src/utils/entityModelSearch';

const entities: DomainEntity[] = [
  {
    id: 'order', name: 'Order', namespace: 'App\\Models', table: 'orders', stereotype: 'Model', framework: 'Laravel',
    properties: [{ name: 'status', type: 'OrderStatus' }],
    methods: [{ name: 'cancel', signature: 'cancel(string $reason): void', behavior_key: 'App\\Models\\Order::cancel(string $reason)' }],
  },
  {
    id: 'invoice', name: 'Invoice', namespace: 'App\\Models', table: 'invoices', stereotype: 'Model', framework: 'Laravel',
    properties: [{ name: 'order_id', type: 'integer' }],
    methods: [{ name: 'order', signature: 'order(): BelongsTo', behavior_key: 'App\\Models\\Invoice::order()' }],
  },
];

test('prioritizes exact entity, class, and model matches over exact behavior matches', () => {
  const matches = findEntityModelSearchMatches([...entities, { ...entities[1], name: 'cancel', id: 'cancel-model' }], 'cancel');
  assert.deepEqual(matches.map((match) => match.entityId), ['cancel-model', 'order']);
});

test('matches behavior names, signatures, and canonical keys case-insensitively', () => {
  assert.deepEqual(findEntityModelSearchMatches(entities, 'CANCEL').map((match) => match.entityId), ['order']);
  assert.deepEqual(findEntityModelSearchMatches(entities, 'string $reason').map((match) => match.entityId), ['order']);
  assert.deepEqual(findEntityModelSearchMatches(entities, 'app\\models\\invoice::order()').map((match) => match.entityId), ['invoice']);
});

test('finds partial entity metadata and property matches, and reports no match', () => {
  assert.deepEqual(findEntityModelSearchMatches(entities, 'orderstatus').map((match) => match.entityId), ['order']);
  assert.deepEqual(findEntityModelSearchMatches(entities, 'laravel').map((match) => match.entityId), ['order', 'invoice']);
  assert.deepEqual(findEntityModelSearchMatches(entities, 'missing').map((match) => match.entityId), []);
});

test('matches class identifiers across camelCase, snake_case, and plural table forms', () => {
  const activityLog: DomainEntity = {
    id: 'entity:activity_logs', name: 'activity_logs', namespace: 'App\\Models', table: 'activity_logs',
    stereotype: 'Model', framework: 'Laravel', properties: [], methods: [],
  };
  const matches = findEntityModelSearchMatches([activityLog], 'ActivityLog');
  assert.deepEqual(matches.map((match) => match.entityId), ['entity:activity_logs']);
  assert.equal(matches[0].rank, 0);
});

test('wraps previous and next navigation indexes', () => {
  assert.equal(wrapEntityModelSearchIndex(-1, 2), 1);
  assert.equal(wrapEntityModelSearchIndex(2, 2), 0);
  assert.equal(wrapEntityModelSearchIndex(0, 0), -1);
});
