import assert from 'node:assert/strict';
import test from 'node:test';
import type { DomainEntity } from '../src/types';
import { completeEntityLayoutPositions, hasEntityLayoutCollisions, resolveEntityLayoutCollisions } from '../src/utils/entityModelLayout';
import { findEntityModelSearchMatches, normalizeEntityModelEntityIds, wrapEntityModelSearchIndex } from '../src/utils/entityModelSearch';

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

test('prettify collision resolution separates variable-size node rectangles', () => {
  const nodes = [
    { id: 'one', width: 280, height: 180 },
    { id: 'two', width: 280, height: 260 },
    { id: 'three', width: 280, height: 150 },
  ];
  const positions = new Map([
    ['one', { x: 40, y: 40 }],
    ['two', { x: 100, y: 80 }],
    ['three', { x: 160, y: 120 }],
  ]);
  assert.equal(hasEntityLayoutCollisions(nodes, positions, 80), true);
  assert.equal(hasEntityLayoutCollisions(nodes, resolveEntityLayoutCollisions(nodes, positions, 80), 80), false);
});

test('larger prettify gaps produce a more spacious collision-free layout', () => {
  const nodes = [
    { id: 'one', width: 280, height: 180 },
    { id: 'two', width: 280, height: 260 },
  ];
  const positions = new Map([
    ['one', { x: 40, y: 40 }],
    ['two', { x: 80, y: 70 }],
  ]);
  const compact = resolveEntityLayoutCollisions(nodes, positions, 80);
  const spacious = resolveEntityLayoutCollisions(nodes, positions, 160);
  assert.equal(hasEntityLayoutCollisions(nodes, spacious, 160), false);
  assert.ok((spacious.get('two')?.y || 0) > (compact.get('two')?.y || 0));
});

test('prettify replaces missing layout-engine coordinates before rendering nodes', () => {
  const nodes = [
    { id: 'placed', width: 280, height: 180 },
    { id: 'isolated', width: 280, height: 180 },
  ];
  const positions = new Map([
    ['placed', { x: 120, y: 200 }],
    ['isolated', { x: Number.NaN, y: Number.NaN }],
  ]);
  const fallbacks = new Map([
    ['isolated', { x: 480, y: 200 }],
  ]);
  const completed = completeEntityLayoutPositions(nodes, positions, fallbacks);
  assert.deepEqual(completed.get('placed'), { x: 120, y: 200 });
  assert.deepEqual(completed.get('isolated'), { x: 480, y: 200 });
});

test('prettify collision resolution supports same-named entities once they have distinct canvas IDs', () => {
  const nodes = [
    { id: 'entity:AbstractTenantAwareModel::0', width: 280, height: 150 },
    { id: 'entity:AbstractTenantAwareModel:App\\Models:1', width: 280, height: 150 },
  ];
  const positions = new Map(nodes.map((node) => [node.id, { x: 40, y: 40 }]));
  const resolved = resolveEntityLayoutCollisions(nodes, positions, 120);
  assert.equal(hasEntityLayoutCollisions(nodes, resolved, 120), false);
});

test('normalizes duplicate source entity IDs before search, selection, and layout', () => {
  const normalizedEntities = normalizeEntityModelEntityIds([
    { ...entities[0], id: '', name: 'AbstractTenantAwareModel', namespace: '' },
    { ...entities[1], id: '', name: 'AbstractTenantAwareModel', namespace: 'App\\Models' },
  ]);
  assert.equal(new Set(normalizedEntities.map((entity) => entity.id)).size, 2);
  assert.match(normalizedEntities[0].id || '', /^entity:AbstractTenantAwareModel:orders:0$/);
  assert.equal(normalizedEntities[1].id, 'entity:AbstractTenantAwareModel:App\\Models:1');
});
