import type { NormalizedSchema } from '../types';
import { tableMatches } from './erdTransformer';

export function importDraftSchema(schema: NormalizedSchema) {
  if (!schema.tables.length || schema.warnings.some((item) => item.severity === 'error')) throw new Error('Import a valid schema with at least one table before creating a draft.');
  if (schema.refs.some((ref) => ref.unsupported)) throw new Error('Resolve unsupported references before creating a draft.');
  const ids = new Set<string>();
  const reserve = (id: string) => {
    if (!id || ids.has(id)) throw new Error(`Duplicate or missing schema ID: ${id}`);
    ids.add(id); return id;
  };
  const tables = schema.tables.map((table) => {
    const id = reserve(table.id);
    const columns = table.fields.map((field) => {
      if (field.default != null && !['string', 'number', 'boolean'].includes(typeof field.default)) {
        throw new Error(`Unsupported default for ${table.name}.${field.name}.`);
      }
      return { id: reserve(field.id), name: field.name, type: field.type, nullable: field.nullable,
        default: field.default ?? null, auto_increment: Boolean(field.autoIncrement),
        ...(field.collation != null ? { collation: field.collation } : {}),
        ...(field.mysqlDefinition != null ? { mysql_definition: field.mysqlDefinition } : {}) };
    });
    const primary = table.fields.filter((field) => field.primaryKey).map((field) => field.id);
    const keys = [
      ...(primary.length ? [{ id: reserve(crypto.randomUUID()), name: null, kind: 'primary', column_ids: primary }] : []),
      ...table.fields.filter((field) => field.unique).map((field) => ({ id: reserve(crypto.randomUUID()), name: null, kind: 'unique', column_ids: [field.id] })),
    ];
    return { id, name: table.name, namespace: table.schema || null, columns, keys, indexes: [] };
  });
  const relationships = schema.refs.map((ref) => {
    const sources = schema.tables.filter((table) => tableMatches(table, ref.sourceTable));
    const targets = schema.tables.filter((table) => tableMatches(table, ref.targetTable));
    if (sources.length !== 1 || targets.length !== 1) throw new Error(`Cannot uniquely resolve relationship ${ref.id}.`);
    const source = sources[0];
    const target = targets[0];
    const sourceField = source?.fields.find((field) => field.name.toLowerCase() === ref.sourceColumn?.toLowerCase());
    const targetField = target?.fields.find((field) => field.name.toLowerCase() === ref.targetColumn?.toLowerCase());
    if (!source || !target || !sourceField || !targetField) throw new Error(`Cannot resolve relationship ${ref.id}.`);
    return { id: reserve(ref.id), name: null, source_table_id: source.id, source_column_ids: [sourceField.id],
      target_table_id: target.id, target_column_ids: [targetField.id], on_delete: null, on_update: null };
  });
  return { version: 1, tables, relationships };
}
