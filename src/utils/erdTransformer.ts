import { DagreLayout } from '@antv/layout';
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

const cardinality = (sourceMany: boolean, targetMany: boolean): ErdRef['cardinality'] => {
  if (sourceMany && targetMany) return 'N:M';
  if (sourceMany) return '1:N';
  if (targetMany) return 'N:1';
  return '1:1';
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
        id: fieldId(name, rawField.name),
        name: identifier(rawField.name),
        type: normaliseType(rawField.type),
        nullable: rawField.not_null !== true,
        default: rawField.dbdefault?.value ?? rawField.dbdefault ?? null,
        primaryKey: rawField.pk === true,
        unique: rawField.unique === true,
        autoIncrement: rawField.increment === true,
        foreignKey: false,
        references: null,
        provenance: 'dbml',
      }));
      tables.push({ id: tableId(name, schema), name, schema, fields, provenance: 'dbml' });
    }
    for (const rawRef of raw.refs || []) {
      const endpoints = rawRef.endpoints || [];
      if (endpoints.length < 2) {
        warnings.push(warning('incomplete-ref', `Reference ${rawRef.name || 'unnamed'} does not have two endpoints.`));
        continue;
      }
      const left = endpoints[0];
      const right = endpoints[1];
      const sourceTable = identifier(left.tableName);
      const targetTable = identifier(right.tableName);
      const sourceColumn = identifier(left.fieldNames?.[0]);
      const targetColumn = identifier(right.fieldNames?.[0]);
      if (!sourceTable || !targetTable || !sourceColumn || !targetColumn) {
        warnings.push(warning('incomplete-ref', `Reference ${rawRef.name || 'unnamed'} is missing a table or column.`));
        continue;
      }
      const sourceMany = String(left.relation || '').includes('*') || String(left.relation || '').includes('n');
      const targetMany = String(right.relation || '').includes('*') || String(right.relation || '').includes('n');
      addRef(refs, {
        sourceTable,
        sourceColumn,
        targetTable,
        targetColumn,
        sourceMany,
        targetMany,
        cardinality: cardinality(sourceMany, targetMany),
        description: `${sourceTable}.${sourceColumn} references ${targetTable}.${targetColumn}`,
        provenance: 'dbml',
      });
    }
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
  const tableByName = new Map<string, ErdTable>();
  const statements = input.replace(/\/\*[^]*?\*\//g, '').replace(/--[^\n]*/g, '').split(';').map((item) => item.trim()).filter(Boolean);
  for (const statement of statements) {
    const match = statement.match(/create\s+table\s+(?:if\s+not\s+exists\s+)?([`"\[\]\w.]+)\s*\(([\s\S]*)\)$/i);
    if (!match) continue;
    const qualified = identifier(match[1]);
    const pieces = splitSqlList(match[2]);
    const schema = qualified.includes('.') ? identifier(qualified.split('.').slice(0, -1).join('.')) : null;
    const name = identifier(qualified.split('.').pop());
    const fields: ErdField[] = [];
    const inlineRefs: Array<{ field: string; table: string; column: string }> = [];
    for (const piece of pieces) {
      const trimmed = piece.trim();
      if (/^(constraint\s+\S+\s+)?primary\s+key/i.test(trimmed)) {
        const cols = trimmed.match(/\(([^)]+)\)/)?.[1]?.split(',').map(identifier) || [];
        cols.forEach((column) => { const field = fields.find((item) => item.name === column); if (field) field.primaryKey = true; });
        continue;
      }
      const tableConstraint = trimmed.match(/(?:constraint\s+\S+\s+)?foreign\s+key\s*\(([^)]+)\)\s+references\s+([`"\[\]\w.]+)\s*\(([^)]+)\)/i);
      if (tableConstraint) {
        const sourceColumns = tableConstraint[1].split(',').map(identifier);
        const targetColumns = tableConstraint[3].split(',').map(identifier);
        sourceColumns.forEach((column, index) => inlineRefs.push({ field: column, table: identifier(tableConstraint[2]), column: targetColumns[index] || targetColumns[0] }));
        continue;
      }
      if (/^(constraint|unique\s*\(|check\s*\(|exclude\s*\(|primary\s+key|foreign\s+key)/i.test(trimmed)) continue;
      const column = trimmed.match(/^([`"\[\]\w]+)\s+([\w]+(?:\s*\([^)]*\))?(?:\s+unsigned)?(?:\s+without\s+time\s+zone)?)/i);
      if (!column) {
        warnings.push(warning('unsupported-sql', `Skipped unsupported table definition: ${trimmed.slice(0, 80)}.`));
        continue;
      }
      const fieldName = identifier(column[1]);
      const definition = trimmed.slice(column[0].length);
      const inlineRef = definition.match(/references\s+([`"\[\]\w.]+)\s*\(([^)]+)\)/i);
      if (inlineRef) inlineRefs.push({ field: fieldName, table: identifier(inlineRef[1]), column: identifier(inlineRef[2]) });
      fields.push({
        id: fieldId(name, fieldName), name: fieldName, type: column[2],
        nullable: !/\bnot\s+null\b/i.test(definition),
        default: definition.match(/\bdefault\s+([^\s,]+)/i)?.[1] ?? null,
        primaryKey: /\bprimary\s+key\b/i.test(definition), unique: /\bunique\b/i.test(definition),
        autoIncrement: /\b(?:auto_increment|serial|identity)\b/i.test(`${column[2]} ${definition}`),
        foreignKey: Boolean(inlineRef), references: inlineRef ? { table: identifier(inlineRef[1]), column: identifier(inlineRef[2]) } : null,
        provenance: source.dialect || 'sql',
      });
    }
    const table: ErdTable = { id: tableId(name, schema), name, schema, fields, provenance: source.dialect || 'sql' };
    tables.push(table); tableByName.set(name.toLowerCase(), table);
    for (const ref of inlineRefs) {
      const sourceField = fields.find((field) => fieldMatches(field, ref.field));
      const oneToOne = Boolean(sourceField?.unique || sourceField?.primaryKey);
      addRef(refs, {
        sourceTable: name,
        sourceColumn: ref.field,
        targetTable: ref.table.split('.').pop() || ref.table,
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
    if (!alter) continue;
    const sourceTable = identifier(alter[1]).split('.').pop() || identifier(alter[1]);
    const targetTable = identifier(alter[3]).split('.').pop() || identifier(alter[3]);
    const sourceColumns = alter[2].split(',').map(identifier); const targetColumns = alter[4].split(',').map(identifier);
    sourceColumns.forEach((sourceColumn, index) => {
      const sourceField = tableByName.get(sourceTable.toLowerCase())?.fields.find((field) => fieldMatches(field, sourceColumn));
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
  if (candidate?.tables && candidate?.refs) {
    const tables = (candidate.tables as any[]).map((item) => ({
      id: item.id || tableId(item.name, item.schema), name: identifier(item.name), schema: item.schema || item.schemaName || null,
      fields: (item.fields || item.columns || []).map((field: any) => ({
        id: field.id || fieldId(item.name, field.name), name: identifier(field.name), type: normaliseType(field.type), nullable: field.nullable !== false && field.not_null !== true,
        default: field.default ?? field.dbdefault ?? null, primaryKey: Boolean(field.primaryKey ?? field.primary_key ?? field.pk), unique: Boolean(field.unique), indexed: Boolean(field.indexed), autoIncrement: Boolean(field.autoIncrement ?? field.increment), foreignKey: Boolean(field.foreignKey || field.references), references: field.references || null, provenance: field.provenance || 'json',
      })), provenance: item.provenance || 'json',
    }));
    const refs = (candidate.refs as any[]).map((item) => ({ id: item.id || `ref:${stableId(item.sourceTable, item.sourceColumn, item.targetTable, item.targetColumn)}`, sourceTable: item.sourceTable || item.source?.table, sourceColumn: item.sourceColumn || item.source?.column, targetTable: item.targetTable || item.target?.table, targetColumn: item.targetColumn || item.target?.column, cardinality: item.cardinality || '1:N', sourceMany: Boolean(item.sourceMany), targetMany: Boolean(item.targetMany), description: item.description, provenance: item.provenance || 'json' }));
    const warnings = [...(candidate.warnings || [])].map((item: any) => typeof item === 'string' ? warning('source-warning', item) : item);
    applyRefs(tables, refs, warnings);
    return { tables, refs, warnings, source: { kind: 'json', ...source } };
  }
  if (Array.isArray(value?.nodes) && Array.isArray(value?.edges)) return fromLegacy(value as SchemaVisualizationResponse, source);
  throw new Error('JSON must contain normalized tables and refs, or legacy nodes and edges.');
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
  return JSON.stringify({ tables: schema.tables, refs: schema.refs, warnings: schema.warnings, source: schema.source }, null, 2);
}
