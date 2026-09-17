import type { DomainEntity, DomainEntityBehavior } from '../types';

export interface EntityModelSearchMatch {
  entity: DomainEntity;
  entityId: string;
  rank: number;
}

type EntityBehavior = string | DomainEntityBehavior;

export const entityModelEntityId = (entity: DomainEntity) => entity.id || `entity:${entity.name}`;

export const entityModelBehaviorKey = (entity: DomainEntity, behavior: EntityBehavior) => (
  typeof behavior === 'string'
    ? `${entity.name}::${behavior}()`
    : String(behavior.behavior_key || `${behavior.owner || entity.name}::${behavior.name}(${behavior.parameters || ''})`)
);

export const entityModelBehaviors = (entity: DomainEntity): EntityBehavior[] => {
  if (entity.methods?.length) return entity.methods;
  return [...(entity.scopes_or_behaviors || []), ...(entity.behaviors || [])];
};

export const entityModelBehavior = (entity: DomainEntity, behavior: EntityBehavior): DomainEntityBehavior => (
  typeof behavior === 'string'
    ? { name: behavior, behavior_key: entityModelBehaviorKey(entity, behavior) }
    : behavior
);

const normalized = (value: unknown) => String(value || '').trim().toLowerCase();
const identifier = (value: unknown) => normalized(value).replace(/[^a-z0-9]/g, '');
const singularIdentifier = (value: unknown) => {
  const compact = identifier(value);
  if (compact.endsWith('ies')) return `${compact.slice(0, -3)}y`;
  return compact.endsWith('s') ? compact.slice(0, -1) : compact;
};
const matchesExactly = (value: unknown, query: string) => (
  normalized(value) === query
  || (Boolean(identifier(query)) && (
    identifier(value) === identifier(query)
    || singularIdentifier(value) === singularIdentifier(query)
  ))
);
const includesQuery = (values: unknown[], query: string) => {
  const compactQuery = identifier(query);
  return values.some((value) => (
    normalized(value).includes(query)
    || (Boolean(compactQuery) && identifier(value).includes(compactQuery))
  ));
};

const entityExactNames = (entity: DomainEntity) => [
  entity.name,
  entity.id,
  (entity as any).class,
  (entity as any).class_name,
  (entity as any).model,
  (entity as any).model_name,
];

const behaviorSearchValues = (entity: DomainEntity, behavior: EntityBehavior) => {
  const value = entityModelBehavior(entity, behavior);
  return [value.name, value.signature, entityModelBehaviorKey(entity, behavior)];
};

const entitySearchValues = (entity: DomainEntity) => [
  ...entityExactNames(entity),
  entity.namespace,
  entity.table,
  entity.stereotype,
  entity.framework,
  ...(entity.properties || []).flatMap((property) => [property.name, property.type, property.cast_or_enum]),
  ...entityModelBehaviors(entity).flatMap((behavior) => behaviorSearchValues(entity, behavior)),
];

/** Returns matching nodes ordered by the most specific matching field. */
export const findEntityModelSearchMatches = (entities: DomainEntity[], query: string): EntityModelSearchMatch[] => {
  const needle = normalized(query);
  if (!needle) return [];

  return entities.flatMap((entity, index) => {
    const exactEntity = entityExactNames(entity).some((value) => matchesExactly(value, needle));
    const exactBehavior = entityModelBehaviors(entity).some((behavior) => (
      behaviorSearchValues(entity, behavior).some((value) => matchesExactly(value, needle))
    ));
    const partial = includesQuery(entitySearchValues(entity), needle);
    if (!partial) return [];
    return [{ entity, entityId: entityModelEntityId(entity), rank: exactEntity ? 0 : exactBehavior ? 1 : 2, index }];
  }).sort((left, right) => left.rank - right.rank || left.index - right.index)
    .map(({ entity, entityId, rank }) => ({ entity, entityId, rank }));
};

export const entityMatchesModelSearch = (entity: DomainEntity, query: string) => (
  !normalized(query) || findEntityModelSearchMatches([entity], query).length > 0
);

export const wrapEntityModelSearchIndex = (index: number, count: number) => (
  count ? ((index % count) + count) % count : -1
);

export const findEntityModelBehavior = (entities: DomainEntity[], behaviorKey: string) => {
  if (!behaviorKey) return null;
  for (const entity of entities) {
    const behavior = entityModelBehaviors(entity).find((item) => entityModelBehaviorKey(entity, item) === behaviorKey);
    if (behavior) return { entity, behavior: entityModelBehavior(entity, behavior) };
  }
  return null;
};
