import type {
  ErdField,
  ErdRef,
  ErdSourceMetadata,
  ErdTable,
  ErdWarning,
  NormalizedSchema,
  SchemaVisualizationResponse,
} from '../types';
import { erdTableDimensions } from './erdMetrics';

export type SchemaInputFormat = 'sql' | 'dbml' | 'json';

export interface TransformOptions {
  format?: SchemaInputFormat;
  source?: ErdSourceMetadata;
}

const identifier = (value: unknown) => String(value ?? '').replace(/^['"`\[]|['"`\]]$/g, '').trim();
const qualifiedIdentifier = (value: unknown) => String(value ?? '').split('.').map(identifier).join('.');
const stableId = (...parts: unknown[]) => parts.map((part) => identifier(part).toLowerCase()).join('.').replace(/[^a-z0-9_.-]+/g, '_');
const tableId = (name: string, schema?: string | null) => `table:${stableId(schema ? `${schema}.${name}` : name)}`;
const fieldId = (table: string, field: string) => `${tableId(table)}:${stableId(field)}`;

const warning = (code: string, message: string, extra: Partial<ErdWarning> = {}): ErdWarning => ({ code, message, severity: 'warning', ...extra });

const normalizeTableReference = (value: unknown) => String(value ?? '').toLowerCase().replace(/^table:/, '').trim();

export const tableMatches = (table: ErdTable, value: unknown) => {
  const reference = normalizeTableReference(value);
  return [table.name, table.id, table.schema ? `${table.schema}.${table.name}` : '']
    .some((candidate) => normalizeTableReference(candidate) === reference);
};

const fieldMatches = (field: ErdField, value: unknown) => field.name.toLowerCase() === String(value ?? '').toLowerCase().trim();

export interface ErdGraphEdge {
  id: string;
  source: { cell: string; port?: string };
  target: { cell: string; port?: string };
  data: ErdRef;
  router: { name: 'manhattan'; args: { padding: number } };
  connector: { name: 'rounded'; args: { radius: number } };
}

/** Build the canonical table/column endpoints consumed by X6 and assertions. */
export function buildErdEdges(schema: NormalizedSchema): ErdGraphEdge[] {
  return schema.refs.filter((ref) => !ref.unsupported).flatMap((ref) => {
    const source = schema.tables.find((table) => tableMatches(table, ref.sourceTable));
    const target = schema.tables.find((table) => tableMatches(table, ref.targetTable));
    const sourceField = source?.fields.find((field) => fieldMatches(field, ref.sourceColumn));
    const targetField = target?.fields.find((field) => fieldMatches(field, ref.targetColumn));
    if (!source || !target || !sourceField || !targetField) return [];
    return [{
      id: ref.id,
      source: { cell: source.id, port: `${sourceField.id}:out` },
      target: { cell: target.id, port: `${targetField.id}:in` },
      data: ref,
      router: { name: 'manhattan' as const, args: { padding: 18 } },
      connector: { name: 'rounded' as const, args: { radius: 6 } },
    }];
  });
}

const normaliseType = (type: unknown) => {
  if (typeof type === 'string') return type.replace(/\s+/g, ' ').trim();
  if (type && typeof type === 'object') {
    const value = type as { type_name?: string; args?: string | null };
    return `${value.type_name || 'unknown'}${value.args ? `(${value.args})` : ''}`;
  }
  return 'unknown';
};

const addRef = (refs: ErdRef[], ref: Omit<ErdRef, 'id'>) => {
  const id = `ref:${stableId(ref.sourceTable, ref.sourceColumn, ref.targetTable, ref.targetColumn)}`;
  if (!refs.some((item) => item.id === id)) refs.push({ id, ...ref });
};

async function fromDbml(input: string, source: ErdSourceMetadata): Promise<NormalizedSchema> {
  const warnings: ErdWarning[] = [];
  const tables: ErdTable[] = [];
  const refs: ErdRef[] = [];
  try {
    const { Parser } = await import('@dbml/core');
    const raw = Parser.parseDBMLToJSONv2(input) as any;
    for (const rawTable of raw.tables || []) {
      const name = identifier(rawTable.name);
      const schema = rawTable.schemaName || null;
      const fields: ErdField[] = (rawTable.fields || []).map((rawField: any) => ({
        id: fieldId(schema ? `${schema}.${name}` : name, rawField.name),
        name: identifier(rawField.name),
        type: normaliseType(rawField.type),
        nullable: rawField.not_null !== true,
        default: rawField.dbdefault?.value ?? rawField.dbdefault ?? null,
        primaryKey: rawField.pk === true,
        unique: rawField.unique === true,
        autoIncrement: rawField.increment === true ? true : null,
        foreignKey: false,
        references: null,
        provenance: 'dbml',
      }));
      tables.push({ id: tableId(name, schema), name, schema, fields, provenance: 'dbml' });
    }
    const draftTables = tables.map((table, tableIndex) => {
      const rawTable = raw.tables[tableIndex];
      const columns = table.fields.map(field => {
        if (field.default != null && !['string', 'number', 'boolean'].includes(typeof field.default)) throw new Error(`Unsupported default for ${table.name}.${field.name}.`);
        return { id: field.id, name: field.name, type: field.type, nullable: field.nullable, default: field.default ?? null, auto_increment: field.autoIncrement ?? null };
      });
      const fieldColumns = (names: unknown[]) => names.map(name => {
        const field = table.fields.find(item => item.name === identifier(name));
        if (!field) throw new Error(`Unknown column ${String(name)} in ${table.name}.`);
        return field.id;
      });
      const prefix = encodeURIComponent(table.id);
      const primary = table.fields.filter(field => field.primaryKey).map(field => field.id);
      const keys: Array<{ id: string; name: string | null; kind: 'primary' | 'unique'; column_ids: string[] }> = [];
      const indexes: Array<{ id: string; name: string; column_ids: string[]; unique: boolean }> = [];
      if (primary.length) keys.push({ id: `key:dbml:${prefix}:primary`, name: null, kind: 'primary', column_ids: primary });
      table.fields.filter(field => field.unique).forEach(field => keys.push({ id: `key:dbml:${prefix}:unique:${encodeURIComponent(field.id)}`, name: null, kind: 'unique', column_ids: [field.id] }));
      (rawTable.indexes || []).forEach((index: any, indexPosition: number) => {
        if (!Array.isArray(index.columns) || !index.columns.length || index.columns.some((column: any) => column.type !== 'column') || index.type) throw new Error(`Index in ${table.name} uses an unsupported expression or index type.`);
        const column_ids = fieldColumns(index.columns.map((column: any) => column.value));
        if (index.pk) keys.push({ id: `key:dbml:${prefix}:index:${indexPosition}`, name: index.name || null, kind: 'primary', column_ids });
        else {
          if (!index.name) throw new Error(`Name the index in ${table.name} before creating a draft.`);
          indexes.push({ id: `index:dbml:${prefix}:${indexPosition}`, name: index.name, column_ids, unique: index.unique === true });
        }
      });
      return { id: table.id, name: table.name, namespace: table.schema || null, columns, keys, indexes };
    });
    const relationships = (raw.refs || []).map((rawRef: any, index: number) => {
      const endpoints = rawRef.endpoints || [];
      if (endpoints.length !== 2) throw new Error('DBML relationships must have two endpoints.');
      const many = (endpoint: any) => endpoint.relation === '*' || endpoint.relation === 'n';
      if (many(endpoints[0]) && many(endpoints[1])) throw new Error('Many-to-many relationships require an explicit junction table.');
      const [left, right] = many(endpoints[1]) ? [endpoints[1], endpoints[0]] : endpoints;
      const resolve = (endpoint: any) => {
        const table = tables.find(item => item.id === tableId(endpoint.tableName, endpoint.schemaName));
        if (!table || !Array.isArray(endpoint.fieldNames) || !endpoint.fieldNames.length) throw new Error(`Cannot resolve relationship ${rawRef.name || index + 1}.`);
        const columns = endpoint.fieldNames.map((name: string) => {
          const field = table.fields.find(item => item.name === identifier(name));
          if (!field) throw new Error(`Cannot resolve relationship column ${name}.`);
          return field.id;
        });
        return { table, columns };
      };
      const source = resolve(left), target = resolve(right);
      if (source.columns.length !== target.columns.length) throw new Error('Relationship column counts must match.');
      return { id: `relationship:dbml:${index}`, name: rawRef.name || null, source_table_id: source.table.id, source_column_ids: source.columns, target_table_id: target.table.id, target_column_ids: target.columns, on_delete: rawRef.onDelete?.toLowerCase() || null, on_update: rawRef.onUpdate?.toLowerCase() || null };
    });
    return fromStructuredDraft({ version: 1, tables: draftTables, relationships }, { kind: 'dbml', ...source });
  } catch (cause: any) {
    warnings.push(warning('parse-error', cause?.message || 'DBML could not be parsed.', { severity: 'error' }));
  }
  applyRefs(tables, refs, warnings);
  return { tables, refs, warnings, source: { kind: 'dbml', ...source } };
}

function fromSql(input: string, source: ErdSourceMetadata): NormalizedSchema {
  const warnings: ErdWarning[] = [];
  const tables: ErdTable[] = [];
  const refs: ErdRef[] = [];
  const constraints = new Map<string, Array<{ name: string | null; kind: 'primary' | 'unique'; columns: string[] }>>();
  const indexes: Array<{ name: string; table: string; columns: string[]; unique: boolean }> = [];
  const foreignKeys: Array<{ name: string | null; source: string; columns: string[]; target: string; targets: string[]; onDelete: string | null; onUpdate: string | null }> = [];
  const addIndex = (name: string, table: string, rawColumns: string, unique: boolean) => {
    const parts = rawColumns.split(',').map(part => part.trim());
    if (!parts.length || parts.some(part => !/^(?:[A-Za-z_]\w*|`[A-Za-z_]\w*`|"[A-Za-z_]\w*"|\[[A-Za-z_]\w*\])$/.test(part))) {
      warnings.push(warning('unsupported-sql', `Index ${name} has unsupported column expressions or attributes.`, { severity: 'error' }));
      return;
    }
    indexes.push({ name: identifier(name), table: qualifiedIdentifier(table), columns: parts.map(identifier), unique });
  };
  const addForeignKey = (name: string | null, sourceName: string, columns: string[], target: string, targets: string[], suffix: string) => {
    const action = (kind: string) => suffix.match(new RegExp(`\\bon\\s+${kind}\\s+(cascade|restrict|set\\s+null|set\\s+default|no\\s+action)\\b`, 'i'))?.[1].toLowerCase().replace(/\s+/g, ' ') || null;
    if (columns.length !== targets.length || !columns.length || /\b(?:deferrable|match)\b/i.test(suffix) || (/\bon\s+delete\b/i.test(suffix) && !action('delete')) || (/\bon\s+update\b/i.test(suffix) && !action('update'))) {
      warnings.push(warning('unsupported-sql', 'Foreign key has mismatched columns or unsupported actions or attributes.', { severity: 'error' }));
      return;
    }
    foreignKeys.push({ name, source: sourceName, columns, target, targets, onDelete: action('delete'), onUpdate: action('update') });
  };
  const statements = input.replace(/\/\*[^]*?\*\//g, '').replace(/--[^\n]*/g, '').split(';').map((item) => item.trim()).filter(Boolean);
  for (const statement of statements) {
    const createIndex = statement.match(/^create\s+(unique\s+)?index\s+([`"\[\]\w]+)\s+on\s+([`"\[\]\w.]+)\s*\(([^()]*)\)$/i);
    if (createIndex) {
      addIndex(createIndex[2], createIndex[3], createIndex[4], Boolean(createIndex[1]));
      continue;
    }
    const match = statement.match(/create\s+table\s+(?:if\s+not\s+exists\s+)?([`"\[\]\w.]+)\s*\(([\s\S]*)\)$/i);
    if (!match) {
      if (/^(?:create\s+(?:unique\s+)?index|create\s+table)\b/i.test(statement)) warnings.push(warning('unsupported-sql', 'SQL index or table syntax cannot be preserved in a draft.', { severity: 'error' }));
      continue;
    }
    const qualified = qualifiedIdentifier(match[1]);
    const pieces = splitSqlList(match[2]);
    const schema = qualified.includes('.') ? identifier(qualified.split('.').slice(0, -1).join('.')) : null;
    const name = identifier(qualified.split('.').pop());
    const fields: ErdField[] = [];
    const tableConstraints: Array<{ name: string | null; kind: 'primary' | 'unique'; columns: string[] }> = [];
    const inlineRefs: Array<{ field: string; table: string; column: string }> = [];
    for (const piece of pieces) {
      const trimmed = piece.trim();
      if (/^(constraint\s+\S+\s+)?primary\s+key/i.test(trimmed)) {
        const primaryConstraint = trimmed.match(/^(?:constraint\s+(\S+)\s+)?primary\s+key\s*\(([^)]+)\)$/i);
        if (!primaryConstraint) {
          warnings.push(warning('unsupported-sql', 'Unsupported primary-key attributes.', { severity: 'error' }));
          continue;
        }
        const cols = primaryConstraint[2].split(',').map(identifier);
        tableConstraints.push({ name: identifier(primaryConstraint[1]) || null, kind: 'primary', columns: cols });
        cols.forEach((column) => { const field = fields.find((item) => item.name === column); if (field) field.primaryKey = true; });
        continue;
      }
      const uniqueConstraint = trimmed.match(/^(?:constraint\s+(\S+)\s+)?unique\s*\(([^)]+)\)$/i);
      if (uniqueConstraint) {
        tableConstraints.push({ name: identifier(uniqueConstraint[1]) || null, kind: 'unique', columns: uniqueConstraint[2].split(',').map(identifier) });
        continue;
      }
      const inlineIndex = trimmed.match(/^(unique\s+)?(?:index|key)\s+([`"\[\]\w]+)\s*\(([^()]*)\)$/i);
      if (inlineIndex) {
        addIndex(inlineIndex[2], qualified, inlineIndex[3], Boolean(inlineIndex[1]));
        continue;
      }
      const tableConstraint = trimmed.match(/^(?:constraint\s+(\S+)\s+)?foreign\s+key\s*\(([^)]+)\)\s+references\s+([`"\[\]\w.]+)\s*\(([^)]+)\)(.*)$/i);
      if (tableConstraint) {
        const sourceColumns = tableConstraint[2].split(',').map(identifier);
        const targetColumns = tableConstraint[4].split(',').map(identifier);
        addForeignKey(identifier(tableConstraint[1]) || null, qualified, sourceColumns, qualifiedIdentifier(tableConstraint[3]), targetColumns, tableConstraint[5]);
        sourceColumns.forEach((column, index) => inlineRefs.push({ field: column, table: qualifiedIdentifier(tableConstraint[3]), column: targetColumns[index] || targetColumns[0] }));
        continue;
      }
      if (/^(constraint|unique\s*\(|check\s*\(|exclude\s*\(|primary\s+key|foreign\s+key|(?:unique\s+)?(?:index|key)\b)/i.test(trimmed)) {
        warnings.push(warning('unsupported-sql', `Unsupported table constraint: ${trimmed.slice(0, 80)}.`, { severity: 'error' }));
        continue;
      }
      const column = trimmed.match(/^([`"\[\]\w]+)\s+([\w]+(?:\s*\([^)]*\))?(?:\s+unsigned)?(?:\s+without\s+time\s+zone)?)/i);
      if (!column) {
        warnings.push(warning('unsupported-sql', `Unsupported table definition: ${trimmed.slice(0, 80)}.`, { severity: 'error' }));
        continue;
      }
      const fieldName = identifier(column[1]);
      const definition = trimmed.slice(column[0].length);
      const inlineRef = definition.match(/references\s+([`"\[\]\w.]+)\s*\(([^)]+)\)/i);
      if (inlineRef) {
        inlineRefs.push({ field: fieldName, table: qualifiedIdentifier(inlineRef[1]), column: identifier(inlineRef[2]) });
        addForeignKey(null, qualified, [fieldName], qualifiedIdentifier(inlineRef[1]), [identifier(inlineRef[2])], definition.slice((inlineRef.index || 0) + inlineRef[0].length));
      }
      fields.push({
        id: fieldId(schema ? `${schema}.${name}` : name, fieldName), name: fieldName, type: column[2],
        nullable: !/\bnot\s+null\b/i.test(definition),
        default: definition.match(/\bdefault\s+([^\s,]+)/i)?.[1] ?? null,
        primaryKey: /\bprimary\s+key\b/i.test(definition), unique: /\bunique\b/i.test(definition),
        autoIncrement: /\b(?:auto_increment|smallserial|serial|bigserial|identity)\b/i.test(`${column[2]} ${definition}`) ? true : null,
        foreignKey: Boolean(inlineRef), references: inlineRef ? { table: identifier(inlineRef[1]), column: identifier(inlineRef[2]) } : null,
        provenance: source.dialect || 'sql',
      });
    }
    const table: ErdTable = { id: tableId(name, schema), name, schema, fields, provenance: source.dialect || 'sql' };
    constraints.set(table.id, tableConstraints);
    tables.push(table);
    for (const ref of inlineRefs) {
      const sourceField = fields.find((field) => fieldMatches(field, ref.field));
      const oneToOne = Boolean(sourceField?.unique || sourceField?.primaryKey);
      addRef(refs, {
        sourceTable: table.id,
        sourceColumn: ref.field,
        targetTable: qualifiedIdentifier(ref.table),
        targetColumn: ref.column,
        sourceMany: !oneToOne,
        targetMany: false,
        cardinality: oneToOne ? '1:1' : 'N:1',
        description: `${name}.${ref.field} references ${ref.table}.${ref.column}`,
        provenance: source.dialect || 'sql',
      });
    }
  }
  for (const statement of statements) {
    const alter = statement.match(/alter\s+table\s+([`"\[\]\w.]+)[\s\S]*?foreign\s+key\s*\(([^)]+)\)\s+references\s+([`"\[\]\w.]+)\s*\(([^)]+)\)/i);
    if (!alter) {
      if (/^alter\s+table\b/i.test(statement)) warnings.push(warning('unsupported-sql', 'SQL ALTER TABLE operation cannot be preserved in a draft.', { severity: 'error' }));
      continue;
    }
    const sourceTable = qualifiedIdentifier(alter[1]);
    const targetTable = qualifiedIdentifier(alter[3]);
    const sourceColumns = alter[2].split(',').map(identifier); const targetColumns = alter[4].split(',').map(identifier);
    addForeignKey(identifier(statement.match(/\badd\s+constraint\s+(\S+)/i)?.[1]) || null, qualifiedIdentifier(alter[1]), sourceColumns, qualifiedIdentifier(alter[3]), targetColumns, statement.slice((alter.index || 0) + alter[0].length));
    sourceColumns.forEach((sourceColumn, index) => {
      const sourceField = tables.find((table) => tableMatches(table, sourceTable))?.fields.find((field) => fieldMatches(field, sourceColumn));
      const oneToOne = Boolean(sourceField?.unique || sourceField?.primaryKey);
      addRef(refs, {
        sourceTable,
        sourceColumn,
        targetTable,
        targetColumn: targetColumns[index] || targetColumns[0],
        sourceMany: !oneToOne,
        targetMany: false,
        cardinality: oneToOne ? '1:1' : 'N:1',
        description: `${sourceTable}.${sourceColumn} references ${targetTable}.${targetColumns[index] || targetColumns[0]}`,
        provenance: source.dialect || 'sql',
      });
    });
  }
  if (!tables.length) warnings.push(warning('no-tables', 'No CREATE TABLE statements were found in the SQL input.'));
  applyRefs(tables, refs, warnings);
  if (tables.length && !refs.some(ref => ref.unsupported) && !warnings.some(item => item.severity === 'error')) {
    try {
      const indexesByTable = new Map<string, typeof indexes>();
      for (const index of indexes) {
        const matches = tables.filter(table => tableMatches(table, index.table));
        if (matches.length !== 1) throw new Error(`Cannot uniquely resolve index table ${index.table}.`);
        const entries = indexesByTable.get(matches[0].id) || [];
        entries.push(index); indexesByTable.set(matches[0].id, entries);
      }
      const draftTables = tables.map(table => {
        const lookup = (names: string[]) => names.map(name => {
          const matches = table.fields.filter(field => fieldMatches(field, name));
          if (matches.length !== 1) throw new Error(`Cannot resolve constraint column ${table.name}.${name}.`);
          return matches[0].id;
        });
        const defined = constraints.get(table.id) || [];
        const primary = table.fields.filter(field => field.primaryKey).map(field => field.id);
        const keys = defined.map((key, index) => ({ id: `key:sql:${encodeURIComponent(table.id)}:${index}`, name: key.name, kind: key.kind, column_ids: lookup(key.columns) }));
        if (primary.length && !keys.some(key => key.kind === 'primary')) keys.push({ id: `key:sql:${encodeURIComponent(table.id)}:primary`, name: null, kind: 'primary', column_ids: primary });
        table.fields.filter(field => field.unique).forEach(field => keys.push({ id: `key:sql:${encodeURIComponent(table.id)}:unique:${encodeURIComponent(field.id)}`, name: null, kind: 'unique', column_ids: [field.id] }));
        const tableIndexes = (indexesByTable.get(table.id) || []).map((index, position) => ({ id: `index:sql:${encodeURIComponent(table.id)}:${position}`, name: index.name, column_ids: lookup(index.columns), unique: index.unique }));
        return { id: table.id, name: table.name, namespace: table.schema || null, columns: table.fields.map(field => ({ id: field.id, name: field.name, type: field.type, nullable: field.nullable, default: field.default ?? null, auto_increment: field.autoIncrement ?? null })), keys, indexes: tableIndexes };
      });
      const relationships = foreignKeys.map((ref, index) => {
        const resolve = (name: string, columns: string[]) => {
          const matches = tables.filter(table => tableMatches(table, name));
          if (matches.length !== 1) throw new Error(`Cannot uniquely resolve table ${name}.`);
          const table = matches[0];
          return { table, columns: columns.map(column => {
            const fields = table.fields.filter(field => fieldMatches(field, column));
            if (fields.length !== 1) throw new Error(`Cannot resolve relationship column ${table.name}.${column}.`);
            return fields[0].id;
          }) };
        };
        const left = resolve(ref.source, ref.columns), right = resolve(ref.target, ref.targets);
        return { id: `relationship:sql:${index}`, name: ref.name, source_table_id: left.table.id, source_column_ids: left.columns, target_table_id: right.table.id, target_column_ids: right.columns, on_delete: ref.onDelete, on_update: ref.onUpdate };
      });
      const result = fromStructuredDraft({ version: 1, tables: draftTables, relationships }, { kind: 'sql', ...source });
      result.warnings.push(...warnings);
      return result;
    } catch (cause: any) {
      warnings.push(warning('unsupported-sql', cause?.message || 'Could not preserve SQL constraints.', { severity: 'error' }));
    }
  }
  return { tables, refs, warnings, source: { kind: 'sql', ...source } };
}

function splitSqlList(value: string) {
  const result: string[] = []; let start = 0; let depth = 0; let quote = '';
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (quote) { if (char === quote && value[index - 1] !== '\\') quote = ''; continue; }
    if (char === '\'' || char === '"' || char === '`') { quote = char; continue; }
    if (char === '(') depth += 1; else if (char === ')') depth -= 1; else if (char === ',' && depth === 0) { result.push(value.slice(start, index)); start = index + 1; }
  }
  result.push(value.slice(start)); return result;
}

function fromJson(input: unknown, source: ErdSourceMetadata): NormalizedSchema {
  let value: any;
  try {
    value = typeof input === 'string' ? JSON.parse(input) : input;
  } catch (cause: any) {
    return {
      tables: [],
      refs: [],
      warnings: [warning('parse-error', cause?.message || 'JSON could not be parsed.', { severity: 'error' })],
      source: { kind: 'json', ...source },
    };
  }
  const candidate = value?.normalizedSchema || value?.schema || value;
  const draft = candidate?.version === 1 && Array.isArray(candidate.relationships) ? candidate : undefined;
  if (draft !== undefined) return fromStructuredDraft(draft, source);
  if (candidate?.tables && candidate?.refs) {
    const tables = (candidate.tables as any[]).map((item) => ({
      id: item.id || tableId(item.name, item.schema), name: identifier(item.name), schema: item.schema || item.schemaName || null,
      fields: (item.fields || item.columns || []).map((field: any) => ({
        id: field.id || fieldId(item.name, field.name), name: identifier(field.name), type: normaliseType(field.type), nullable: field.nullable !== false && field.not_null !== true,
        default: field.default ?? field.dbdefault ?? null, defaultExpression: field.defaultExpression ?? field.default_expression, identity: field.identity, primaryKey: Boolean(field.primaryKey ?? field.primary_key ?? field.pk), unique: Boolean(field.unique), indexed: Boolean(field.indexed), autoIncrement: typeof (field.autoIncrement ?? field.auto_increment ?? field.increment) === 'boolean' ? (field.autoIncrement ?? field.auto_increment ?? field.increment) : null, collation: field.collation ?? null, mysqlDefinition: field.mysqlDefinition ?? field.mysql_definition ?? null, foreignKey: Boolean(field.foreignKey || field.references), references: field.references || null, provenance: field.provenance || 'json',
      })), inspectionMetadata: item.inspectionMetadata ?? item.inspection_metadata, provenance: item.provenance || 'json',
    }));
    const refs = (candidate.refs as any[]).map((item) => ({ id: item.id || `ref:${stableId(item.sourceTable, item.sourceColumn, item.targetTable, item.targetColumn)}`, sourceTable: item.sourceTable || item.source?.table, sourceColumn: item.sourceColumn || item.source?.column, targetTable: item.targetTable || item.target?.table, targetColumn: item.targetColumn || item.target?.column, cardinality: item.cardinality || '1:N', sourceMany: Boolean(item.sourceMany), targetMany: Boolean(item.targetMany), description: item.description, provenance: item.provenance || 'json' }));
    const warnings = [...(candidate.warnings || [])].map((item: any) => typeof item === 'string' ? warning('source-warning', item) : item);
    applyRefs(tables, refs, warnings);
    return { tables, refs, warnings, source: { kind: 'json', ...source } };
  }
  if (Array.isArray(value?.nodes) && Array.isArray(value?.edges)) return fromLegacy(value as SchemaVisualizationResponse, source);
  throw new Error('JSON must contain normalized tables and refs, or legacy nodes and edges.');
}

/** Render structured drafts for navigation without converting their migration data. */
function fromStructuredDraft(draft: any, source: ErdSourceMetadata): NormalizedSchema {
  if (!draft || draft.version !== 1 || !Array.isArray(draft.tables) || !Array.isArray(draft.relationships)) {
    throw new Error('Invalid structured schema: expected version 1, tables, and relationships.');
  }
  const tables: ErdTable[] = draft.tables.map((table: any) => {
    if (!table || typeof table.id !== 'string' || typeof table.name !== 'string' || !Array.isArray(table.columns) || !Array.isArray(table.keys) || !Array.isArray(table.indexes)) throw new Error('Invalid structured schema table.');
    const fields: ErdField[] = table.columns.map((column: any) => {
      if (!column || typeof column.id !== 'string' || typeof column.name !== 'string' || typeof column.type !== 'string' || typeof column.nullable !== 'boolean') throw new Error(`Invalid column in ${table.name}.`);
      return { id: column.id, name: column.name, type: column.type, nullable: column.nullable, default: column.default, defaultExpression: column.default_expression, identity: column.identity, autoIncrement: column.auto_increment, collation: column.collation, mysqlDefinition: column.mysql_definition, primaryKey: table.keys.some((key: any) => key?.kind === 'primary' && Array.isArray(key.column_ids) && key.column_ids.includes(column.id)), unique: table.keys.some((key: any) => key?.kind === 'unique' && Array.isArray(key.column_ids) && key.column_ids.length === 1 && key.column_ids[0] === column.id), provenance: 'json' };
    });
    return { id: table.id, name: table.name, schema: table.namespace, fields, inspectionMetadata: table.inspection_metadata, provenance: 'json' };
  });
  const warnings: ErdWarning[] = [];
  const refs: ErdRef[] = draft.relationships.map((relation: any) => {
    const left = tables.find(table => table.id === relation?.source_table_id);
    const right = tables.find(table => table.id === relation?.target_table_id);
    const sourceColumn = left?.fields.find(field => field.id === relation?.source_column_ids?.[0]);
    const targetColumn = right?.fields.find(field => field.id === relation?.target_column_ids?.[0]);
    if (!relation || typeof relation.id !== 'string' || !left || !right || !sourceColumn || !targetColumn || !Array.isArray(relation.source_column_ids) || !Array.isArray(relation.target_column_ids) || relation.source_column_ids.length !== relation.target_column_ids.length) throw new Error('Invalid structured schema relationship.');
    if (relation.source_column_ids.length > 1) warnings.push(warning('composite-reference-display', `The preview shows the first column of composite relationship ${relation.name || relation.id}; the draft retains all columns.`));
    return { id: relation.id, sourceTable: left.id, sourceColumn: sourceColumn.name, targetTable: right.id, targetColumn: targetColumn.name, cardinality: 'N:1', provenance: 'json' };
  });
  applyRefs(tables, refs, warnings);
  return { tables, refs, warnings, source: { kind: 'json', ...source }, structuredDraft: structuredClone(draft) };
}

function fromLegacy(legacy: SchemaVisualizationResponse, source: ErdSourceMetadata): NormalizedSchema {
  const tables = (legacy.nodes || []).map((node) => ({ id: node.id.startsWith('table:') ? node.id : tableId(node.data.table), name: node.data.table, fields: node.data.columns.map((column) => ({ id: fieldId(node.data.table, column.name), name: column.name, type: column.type, nullable: column.nullable, default: column.default, primaryKey: column.primaryKey, unique: column.unique, indexed: column.indexed, foreignKey: column.foreignKey, references: column.references, provenance: 'saved-analysis' })), position: node.position, provenance: 'saved-analysis' }));
  const refs = (legacy.edges || []).flatMap((edge) => edge.columns?.length ? edge.columns.map((columns) => ({ id: `ref:${stableId(edge.source, columns.source, edge.target, columns.target)}`, sourceTable: edge.source, sourceColumn: columns.source, targetTable: edge.target, targetColumn: columns.target, cardinality: edge.cardinality, description: edge.description, provenance: edge.provenance || 'saved-analysis' })) : []);
  const normalized: NormalizedSchema = { tables, refs, warnings: legacy.warnings || [], source: { kind: 'saved-analysis', ...legacy.source, ...source }, legacy };
  applyRefs(normalized.tables, normalized.refs, normalized.warnings); return normalized;
}

function applyRefs(tables: ErdTable[], refs: ErdRef[], warnings: ErdWarning[]) {
  for (const ref of refs) {
    let source = tables.find((table) => tableMatches(table, ref.sourceTable));
    let target = tables.find((table) => tableMatches(table, ref.targetTable));
    let sourceField = source?.fields.find((field) => fieldMatches(field, ref.sourceColumn));
    let targetField = target?.fields.find((field) => fieldMatches(field, ref.targetColumn));
    if (!source || !target || !sourceField || !targetField) { warnings.push(warning('unresolved-reference', `Could not resolve ${ref.sourceTable}.${ref.sourceColumn} → ${ref.targetTable}.${ref.targetColumn}.`, { source: ref.sourceTable, target: ref.targetTable })); ref.unsupported = true; continue; }

    // Legacy Laravel graph edges were oriented parent -> child for layout. The
    // neutral contract is FK -> PK, so rebind only when the opposite endpoint
    // is explicitly marked as the foreign key.
    if (!sourceField.foreignKey && targetField.foreignKey) {
      [ref.sourceTable, ref.targetTable] = [ref.targetTable, ref.sourceTable];
      [ref.sourceColumn, ref.targetColumn] = [ref.targetColumn, ref.sourceColumn];
      if (ref.cardinality === '1:N') ref.cardinality = 'N:1';
      else if (ref.cardinality === 'N:1') ref.cardinality = '1:N';
      [source, target] = [target, source];
      [sourceField, targetField] = [targetField, sourceField];
    }
    sourceField.foreignKey = true;
    sourceField.references = { table: target.name, column: ref.targetColumn };
  }
}

export async function parseSchemaInput(input: string | unknown, options: TransformOptions = {}): Promise<NormalizedSchema> {
  const source = options.source || {};
  const format = options.format || (typeof input === 'string' && /^\s*(table|ref|project|enum)\b/im.test(input) ? 'dbml' : typeof input === 'string' ? 'sql' : 'json');
  if (format === 'dbml') return fromDbml(String(input), source);
  if (format === 'sql') return fromSql(String(input), source);
  return fromJson(input, source);
}

export function normalizeSavedSchema(value: SchemaVisualizationResponse | NormalizedSchema): NormalizedSchema {
  if (Array.isArray((value as NormalizedSchema).tables)) {
    const normalized: NormalizedSchema = {
      ...(value as NormalizedSchema),
      tables: (value as NormalizedSchema).tables.map((table) => ({
        ...table,
        fields: table.fields.map((field) => ({ ...field })),
      })),
      refs: (value as NormalizedSchema).refs.map((ref) => ({ ...ref })),
      warnings: [...((value as NormalizedSchema).warnings || [])],
    };
    applyRefs(normalized.tables, normalized.refs, normalized.warnings);
    return normalized;
  }
  return fromLegacy(value as SchemaVisualizationResponse, { kind: 'saved-analysis' });
}

export function tableDimensions(table: ErdTable) {
  return erdTableDimensions(table);
}

export async function layoutSchema(schema: NormalizedSchema): Promise<NormalizedSchema> {
  const nodes = schema.tables.map((table) => ({ id: table.id, width: tableDimensions(table).width, height: tableDimensions(table).height }));
  const edges = buildErdEdges(schema).map((edge) => ({ id: edge.id, source: edge.source.cell, target: edge.target.cell }));
  try {
    const { DagreLayout } = await import('@antv/layout');
    const layout = new DagreLayout({
      type: 'dagre',
      rankdir: 'LR',
      nodesep: 55,
      ranksep: 120,
      nodeSize: (node: { width: number; height: number }) => [node.width, node.height],
    });
    await layout.execute({ nodes, edges }, { width: 1400, height: 900 });
    const positions = new Map<string, { x: number; y: number }>();
    layout.forEachNode((node) => positions.set(String(node.id), { x: Math.max(30, node.x - ((node.size as any)?.[0] || 0) / 2), y: Math.max(30, node.y - ((node.size as any)?.[1] || 0) / 2) }));
    return { ...schema, tables: schema.tables.map((table) => ({ ...table, ...tableDimensions(table), position: positions.get(table.id) || table.position || { x: 30, y: 30 } })) };
  } catch {
    const columns = Math.max(1, Math.ceil(Math.sqrt(schema.tables.length)));
    return { ...schema, tables: schema.tables.map((table, index) => ({ ...table, ...tableDimensions(table), position: { x: 40 + (index % columns) * 360, y: 40 + Math.floor(index / columns) * 250 } })) };
  }
}

/** Convert normalized tables/refs into X6 metadata without creating a Graph. */
export function transformSchemaToGraph(schema: NormalizedSchema) {
  const nodes = schema.tables.map((table) => ({
    id: table.id,
    shape: 'erd-vue-table',
    width: tableDimensions(table).width,
    height: tableDimensions(table).height,
    position: table.position || { x: 40, y: 40 },
    data: {
      table,
      id: table.id,
      name: table.name,
      columns: table.fields.map((field) => ({
        id: field.id,
        name: field.name,
        type: field.type,
        isPk: field.primaryKey,
        isFk: field.foreignKey,
        nullable: field.nullable,
      })),
    },
  }));
  return { nodes, edges: buildErdEdges(schema) };
}

export function schemaToDbml(schema: NormalizedSchema) {
  const lines = schema.tables.map((table) => [`Table ${table.name} {`, ...table.fields.map((field) => `  ${field.name} ${field.type}${field.primaryKey ? ' [pk]' : ''}${field.unique ? ' [unique]' : ''}${field.nullable ? '' : ' [not null]'}`), `}`].join('\n'));
  schema.refs.filter((ref) => !ref.unsupported).forEach((ref) => lines.push(`Ref: ${ref.sourceTable}.${ref.sourceColumn} ${ref.cardinality === '1:1' ? '-' : '>'} ${ref.targetTable}.${ref.targetColumn}`));
  return lines.join('\n\n');
}

export function schemaToJson(schema: NormalizedSchema) {
  return JSON.stringify(schema.structuredDraft ?? { tables: schema.tables, refs: schema.refs, warnings: schema.warnings, source: schema.source }, null, 2);
}
