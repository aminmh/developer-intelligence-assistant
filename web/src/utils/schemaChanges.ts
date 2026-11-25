import type { NormalizedSchema, StructuredSchemaDraft } from '../types';

// The difference between the schema shown in the explorer (the original source) and what is currently in the
// Draw.io editor. Nothing here is saved anywhere: it only drives the "Show new changes" highlights.

export type ColumnChange = { name: string; status: 'added' | 'removed' | 'changed'; detail?: string };
export type ChangedField = { name: string; type: string; primaryKey: boolean; unique: boolean; nullable: boolean };
export type TableChange = {
  key: string;
  /** The original table's ID for changed and removed tables. */
  id?: string;
  name: string;
  namespace: string | null;
  status: 'added' | 'removed' | 'changed';
  detail?: string;
  columns: ColumnChange[];
  /** Every column of an added table, so it can be listed like the others. */
  fields?: ChangedField[];
};
export type RelationshipChange = { key: string; status: 'added' | 'removed'; text: string; sourceTable: string; sourceColumn: string; targetTable: string; targetColumn: string; /** The original relationship's ID, for removed ones. */ id?: string };
export type SchemaChanges = {
  tables: TableChange[];
  relationships: RelationshipChange[];
  total: number;
  /** Identical for identical differences, so the UI can tell whether anything new arrived. */
  signature: string;
};

type Column = { id: string; name: string; type: string; nullable: boolean; primary: boolean; unique: boolean; default: string };
type Table = { id: string; key: string; name: string; namespace: string | null; columns: Column[] };
type Relationship = { key: string; text: string; id?: string; sourceTable: string; sourceColumn: string; targetTable: string; targetColumn: string };

const lower = (value: unknown) => String(value ?? '').trim().toLowerCase();
const tableKey = (namespace: string | null | undefined, name: string) => `${lower(namespace)}.${lower(name)}`;
const typeKey = (value: unknown) => lower(value).replace(/\s+/g, '');
const defaultKey = (value: unknown) => (value === null || value === undefined ? '' : String(value).trim());

function fromOriginal(schema: NormalizedSchema): { tables: Table[]; relationships: Relationship[] } {
  const tables = schema.tables.map((table): Table => ({
    id: table.id, key: tableKey(table.schema, table.name), name: table.name, namespace: table.schema ?? null,
    columns: table.fields.map((field) => ({
      id: field.id, name: field.name, type: field.type, nullable: field.nullable !== false, primary: !!field.primaryKey,
      unique: !!field.unique, default: defaultKey(field.default),
    })),
  }));
  // Relationship ends are table IDs such as `table:posts`, or plain (schema-qualified) names.
  const resolve = (reference: string) => {
    const bare = lower(reference).replace(/^table:/, '');
    return tables.find((table) => table.id === reference)
      || tables.find((table) => lower(table.name) === bare || tableKey(table.namespace, table.name) === bare || `${lower(table.namespace)}.${lower(table.name)}`.replace(/^\./, '') === bare);
  };
  const relationships = schema.refs.map((ref): Relationship => {
    const source = resolve(ref.sourceTable), target = resolve(ref.targetTable);
    const from = `${source?.name ?? ref.sourceTable}.${ref.sourceColumn}`, to = `${target?.name ?? ref.targetTable}.${ref.targetColumn}`;
    return { key: `${lower(from)}>${lower(to)}`, text: `${from} → ${to}`, id: ref.id, sourceTable: source?.name ?? lower(ref.sourceTable).replace(/^table:/, ''), sourceColumn: ref.sourceColumn, targetTable: target?.name ?? lower(ref.targetTable).replace(/^table:/, ''), targetColumn: ref.targetColumn };
  });
  return { tables, relationships };
}

function fromDraft(draft: StructuredSchemaDraft): { tables: Table[]; relationships: Relationship[] } {
  const tables = draft.tables.map((table): Table => {
    const primary = new Set(table.keys.filter((key) => key.kind === 'primary').flatMap((key) => key.column_ids));
    const unique = new Set(table.keys.filter((key) => key.kind === 'unique' && key.column_ids.length === 1).flatMap((key) => key.column_ids));
    return {
      id: table.id, key: tableKey(table.namespace, table.name), name: table.name, namespace: table.namespace ?? null,
      columns: table.columns.map((column) => ({
        id: column.id, name: column.name, type: column.type, nullable: column.nullable !== false, primary: primary.has(column.id),
        unique: unique.has(column.id), default: defaultKey(column.default),
      })),
    };
  });
  const tableById = new Map(tables.map((table) => [table.id, table]));
  const relationships = draft.relationships.flatMap((relation): Relationship[] => {
    const source = tableById.get(relation.source_table_id), target = tableById.get(relation.target_table_id);
    const sourceColumn = source?.columns.find((column) => column.id === relation.source_column_ids[0]);
    const targetColumn = target?.columns.find((column) => column.id === relation.target_column_ids[0]);
    if (!source || !target || !sourceColumn || !targetColumn) return [];
    const from = `${source.name}.${sourceColumn.name}`, to = `${target.name}.${targetColumn.name}`;
    return [{ key: `${lower(from)}>${lower(to)}`, text: `${from} → ${to}`, id: relation.id, sourceTable: source.name, sourceColumn: sourceColumn.name, targetTable: target.name, targetColumn: targetColumn.name }];
  });
  return { tables, relationships };
}

function isDraft(value: unknown): value is StructuredSchemaDraft {
  const draft = value as StructuredSchemaDraft | null;
  return !!draft && Array.isArray(draft.tables) && Array.isArray(draft.relationships)
    && draft.tables.every((table) => table && Array.isArray(table.columns) && Array.isArray(table.keys));
}

function describeColumn(before: Column, after: Column): string[] {
  const details: string[] = [];
  if (lower(before.name) !== lower(after.name) || before.name !== after.name) details.push(`renamed from ${before.name}`);
  if (typeKey(before.type) !== typeKey(after.type)) details.push(`type ${before.type} → ${after.type}`);
  if (before.nullable !== after.nullable) details.push(after.nullable ? 'now nullable' : 'now required');
  if (before.primary !== after.primary) details.push(after.primary ? 'now primary key' : 'no longer primary key');
  if (before.unique !== after.unique) details.push(after.unique ? 'now unique' : 'no longer unique');
  if (before.default !== after.default) details.push(`default ${before.default || 'none'} → ${after.default || 'none'}`);
  return details;
}

function diffColumns(before: Column[], after: Column[]): ColumnChange[] {
  const changes: ColumnChange[] = [];
  const unmatched = new Set(after);
  const pairs: Array<[Column, Column]> = [];
  const removed: Column[] = [];
  for (const column of before) {
    const match = [...unmatched].find((candidate) => candidate.id && candidate.id === column.id)
      || [...unmatched].find((candidate) => lower(candidate.name) === lower(column.name));
    if (match) { unmatched.delete(match); pairs.push([column, match]); } else removed.push(column);
  }
  for (const [column, match] of pairs) {
    const details = describeColumn(column, match);
    if (details.length) changes.push({ name: match.name, status: 'changed', detail: details.join(', ') });
  }
  for (const column of unmatched) changes.push({ name: column.name, status: 'added', detail: column.type });
  for (const column of removed) changes.push({ name: column.name, status: 'removed' });
  return changes;
}

/** The key the explorer uses to line a displayed table up with a change. */
export const tableChangeKey = (namespace: string | null | undefined, name: string) => tableKey(namespace, name);

/** Keys of a displayed schema's relationships by ID, in the same form as `RelationshipChange.key`. */
export function relationshipKeys(schema: NormalizedSchema): Map<string, string> {
  return new Map(fromOriginal(schema).relationships.map((relation) => [relation.id as string, relation.key]));
}

/**
 * What differs between a schema and what is in the editor now; null when the editor's schema is not usable.
 * `before` is normally the saved draft, so the result is what changed since it was last saved.
 */
export function diffSchemas(original: NormalizedSchema | StructuredSchemaDraft, edited: unknown): SchemaChanges | null {
  if (!isDraft(edited)) return null;
  const before = isDraft(original) ? fromDraft(original) : fromOriginal(original as NormalizedSchema), after = fromDraft(edited);
  const tables: TableChange[] = [];
  const unmatched = new Set(after.tables);
  const removed: Table[] = [];
  for (const table of before.tables) {
    const match = [...unmatched].find((candidate) => candidate.id && candidate.id === table.id)
      || [...unmatched].find((candidate) => candidate.key === table.key);
    if (!match) { removed.push(table); continue; }
    unmatched.delete(match);
    const columns = diffColumns(table.columns, match.columns);
    const renamed = table.key !== match.key;
    if (columns.length || renamed) {
      tables.push({ key: table.key, id: table.id, name: match.name, namespace: match.namespace, status: 'changed',
        detail: renamed ? `renamed from ${table.name}` : undefined, columns });
    }
  }
  for (const table of unmatched) {
    tables.push({ key: table.key, name: table.name, namespace: table.namespace, status: 'added', columns: [],
      fields: table.columns.map((column) => ({ name: column.name, type: column.type, primaryKey: column.primary, unique: column.unique, nullable: column.nullable })) });
  }
  for (const table of removed) tables.push({ key: table.key, id: table.id, name: table.name, namespace: table.namespace, status: 'removed', columns: [] });

  const relationships: RelationshipChange[] = [];
  const beforeKeys = new Set(before.relationships.map((relation) => relation.key));
  const afterKeys = new Set(after.relationships.map((relation) => relation.key));
  for (const relation of after.relationships) if (!beforeKeys.has(relation.key)) relationships.push({ key: relation.key, status: 'added', text: relation.text, sourceTable: relation.sourceTable, sourceColumn: relation.sourceColumn, targetTable: relation.targetTable, targetColumn: relation.targetColumn });
  for (const relation of before.relationships) if (!afterKeys.has(relation.key)) relationships.push({ key: relation.key, status: 'removed', text: relation.text, id: relation.id, sourceTable: relation.sourceTable, sourceColumn: relation.sourceColumn, targetTable: relation.targetTable, targetColumn: relation.targetColumn });

  const total = tables.length + relationships.length;
  return { tables, relationships, total, signature: total ? JSON.stringify([tables, relationships]) : '' };
}

const canonical = (value: unknown): unknown => Array.isArray(value) ? value.map(canonical)
  : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]))
    : value;

/** True when two structured schemas hold the same data, regardless of key order. */
export function sameStructuredSchema(a: unknown, b: unknown): boolean {
  return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}
