/** Construct a presentation-only starting diagram from a persisted structured draft. */
export function createDrawioDiagram(value: unknown): string {
  const schema = value as Schema;
  if (!Array.isArray(schema?.tables) || !Array.isArray(schema.relationships)) throw new Error('The saved schema is invalid.');
  const escape = (text: string) => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;');
  const id = (kind: string, value: string) => `${kind}-${encodeURIComponent(value)}`;
  // Labels stay "name: type" because they are parsed back; keys show through style and a note below the table.
  const foreignColumns = new Set(schema.relationships.flatMap(relation => relation.source_column_ids));
  const tableName = (tableId: string) => schema.tables.find(table => table.id === tableId)?.name ?? '?';
  const columnName = (columnId: string) => schema.tables.flatMap(table => table.columns).find(column => column.id === columnId)?.name ?? '?';
  const noteLines = (table: Schema['tables'][number]) => {
    const names = (ids: string[]) => ids.map(columnName).join(', ');
    return [
      ...(table.keys ?? []).map(key => `${key.kind === 'primary' ? 'PK' : 'UQ'} ${names(key.column_ids)}`),
      ...(table.indexes ?? []).map(index => `${index.unique ? 'UQ ' : ''}INDEX ${index.name} (${names(index.column_ids)})`),
      ...schema.relationships.filter(relation => relation.source_table_id === table.id)
        .map(relation => `FK ${names(relation.source_column_ids)} -> ${tableName(relation.target_table_id)}(${names(relation.target_column_ids)})`),
    ];
  };
  const primary = new Set(schema.tables.flatMap(table => (table.keys ?? []).filter(key => key.kind === 'primary').flatMap(key => key.column_ids)));
  // Related tables sit next to each other: walk the relationship graph from the most connected table.
  const links = new Map(schema.tables.map(table => [table.id, new Set<string>()]));
  for (const relation of schema.relationships) { links.get(relation.source_table_id)?.add(relation.target_table_id); links.get(relation.target_table_id)?.add(relation.source_table_id); }
  const ordered: Schema['tables'] = [];
  const seen = new Set<string>();
  for (const start of [...schema.tables].sort((a, b) => (links.get(b.id)?.size ?? 0) - (links.get(a.id)?.size ?? 0))) {
    const queue = [start.id];
    while (queue.length) {
      const next = queue.shift() as string;
      if (seen.has(next)) continue;
      seen.add(next);
      const table = schema.tables.find(item => item.id === next);
      if (table) ordered.push(table);
      queue.push(...[...(links.get(next) ?? [])].sort((a, b) => (links.get(b)?.size ?? 0) - (links.get(a)?.size ?? 0)));
    }
  }
  const perRow = Math.min(6, Math.max(3, Math.ceil(Math.sqrt(ordered.length))));
  const longest = Math.max(...ordered.flatMap(table => [(table.namespace ? table.namespace.length + 1 : 0) + table.name.length + 4, ...table.columns.map(column => column.name.length + column.type.length + 8), ...noteLines(table).map(line => line.length * 0.85)]), 20);
  const width = Math.min(380, Math.max(220, Math.round(longest * 7.2 + 24)));
  const gap = 90;
  const heights = ordered.map(table => 32 + 26 * table.columns.length + (noteLines(table).length ? 10 + 16 * noteLines(table).length : 0));
  const rowTop: number[] = [40];
  heights.forEach((height, index) => { const row = Math.floor(index / perRow); rowTop[row + 1] = Math.max(rowTop[row + 1] ?? 0, rowTop[row] + height + gap); });
  const cells = ordered.flatMap((table, index) => {
    const tableId = id('table', table.id);
    const height = 32 + 26 * table.columns.length;
    const title = table.namespace ? `${table.namespace}.${table.name}` : table.name;
    const x = 40 + (index % perRow) * (width + gap);
    const y = rowTop[Math.floor(index / perRow)];
    const header = `<mxCell id="${escape(tableId)}" value="${escape(title)}" style="swimlane;fontStyle=1;fontSize=13;startSize=32;horizontal=1;rounded=1;arcSize=6;fillColor=#1e3a5f;fontColor=#ffffff;strokeColor=#1e3a5f;swimlaneFillColor=#ffffff;shadow=1;collapsible=0;" vertex="1" parent="1"><mxGeometry x="${x}" y="${y}" width="${width}" height="${height}" as="geometry"/></mxCell>`;
    const columns = table.columns.map((column, row) => {
      const isPrimary = primary.has(column.id), isForeign = foreignColumns.has(column.id);
      const emphasis = (isPrimary ? 1 : 0) + (isForeign ? 2 : 0);
      const style = `text;align=left;verticalAlign=middle;spacingLeft=10;spacingRight=8;whiteSpace=nowrap;overflow=hidden;fontSize=12;fillColor=${row % 2 ? '#f1f5f9' : '#ffffff'};strokeColor=none;fontColor=${isPrimary ? '#b45309' : isForeign ? '#1d4ed8' : '#1f2937'};${emphasis ? `fontStyle=${emphasis};` : ''}`;
      return `<mxCell id="${escape(id('column', column.id))}" value="${escape(`${column.name}: ${column.type}`)}" style="${style}" vertex="1" parent="${escape(tableId)}"><mxGeometry y="${32 + row * 26}" width="${width}" height="26" as="geometry"/></mxCell>`;
    });
    const lines = noteLines(table);
    const note = lines.length
      ? [`<mxCell id="${escape(`keys-${encodeURIComponent(table.id)}`)}" value="${escape(lines.join('\n')).replace(/\n/g, '&#xa;')}" style="text;align=left;verticalAlign=top;spacingLeft=8;spacingTop=4;fontSize=10;fontColor=#475569;whiteSpace=nowrap;overflow=hidden;fillColor=#f8fafc;strokeColor=#cbd5e1;rounded=1;arcSize=8;" vertex="1" parent="1"><mxGeometry x="${x}" y="${y + height + 6}" width="${width}" height="${10 + 16 * lines.length}" as="geometry"/></mxCell>`]
      : [];
    return [header, ...columns, ...note];
  });
  // The foreign-key table is the "many" end; the referenced table is the "one" end.
  for (const relation of schema.relationships) cells.push(`<mxCell id="${escape(id('relationship', relation.id))}" style="edgeStyle=entityRelationEdgeStyle;html=1;startArrow=ERmany;startFill=0;endArrow=ERone;endFill=0;strokeColor=#64748b;strokeWidth=1.5;" edge="1" parent="1" source="${escape(id('table', relation.source_table_id))}" target="${escape(id('table', relation.target_table_id))}"><mxGeometry relative="1" as="geometry"/></mxCell>`);
  const metadata: unknown[] = schema.tables.flatMap(table => table.columns.map(column => ({ id: column.id, nullable: column.nullable ?? false, default: column.default ?? null, auto_increment: column.auto_increment ?? null, ...(Object.hasOwn(column, 'default_expression') ? { default_expression: column.default_expression } : {}), ...(Object.hasOwn(column, 'identity') ? { identity: column.identity } : {}), ...(Object.hasOwn(column, 'collation') ? { collation: column.collation } : {}), ...(Object.hasOwn(column, 'mysql_definition') ? { mysql_definition: column.mysql_definition } : {}) })));
  metadata.push({ kind: 'constraints-v1' }, ...schema.tables.map(table => ({ table_id: table.id, keys: table.keys, indexes: table.indexes, ...(Object.hasOwn(table, 'inspection_metadata') ? { inspection_metadata: table.inspection_metadata } : {}) })), { kind: 'relationships-v1' }, ...schema.relationships.map(relationship => ({ relationship })));
  cells.push(`<mxCell id="schema-metadata" value="${escape(JSON.stringify(metadata))}" vertex="1" visible="0" parent="1"><mxGeometry width="1" height="1" as="geometry"/></mxCell>`);
  return `<mxfile><diagram name="Schema"><mxGraphModel grid="1" gridSize="10" background="#ffffff"><root><mxCell id="0"/><mxCell id="1" parent="0"/>${cells.join('')}</root></mxGraphModel></diagram></mxfile>`;
}

/** Preserve the existing layout unless the user explicitly agrees to replace it. */
export function rebuildDrawioDiagram(existing: string, savedSchema: unknown, confirmReplacement: () => boolean): string {
  return confirmReplacement() ? createDrawioDiagram(savedSchema) : existing;
}

/** Add property metadata to a validated legacy layout without changing its geometry or annotations. */
export function initializeDrawioMetadata(xml: string, schema: unknown): string {
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  const existing = doc.querySelectorAll('mxCell[id="schema-metadata"]');
  if (existing.length > 1) throw new Error('Duplicate schema metadata.');
  if (existing.length) {
    schemaFromDrawio(xml, schema);
    const entries = JSON.parse(existing[0].getAttribute('value') ?? '');
    const saved = schema as Schema;
    let changed = false;
    if (!entries.some((entry: { kind?: string }) => entry.kind === 'constraints-v1')) {
      entries.push({ kind: 'constraints-v1' }, ...saved.tables.map(table => ({ table_id: table.id, keys: table.keys, indexes: table.indexes, ...(Object.hasOwn(table, 'inspection_metadata') ? { inspection_metadata: table.inspection_metadata } : {}) }))); changed = true;
    }
    if (!entries.some((entry: { kind?: string }) => entry.kind === 'relationships-v1')) {
      entries.push({ kind: 'relationships-v1' }, ...saved.relationships.map(relationship => ({ relationship }))); changed = true;
    }
    if (!changed) return xml;
    existing[0].setAttribute('value', JSON.stringify(entries));
    return new XMLSerializer().serializeToString(doc);
  }
  if (doc.querySelector('parsererror') || doc.querySelectorAll('mxGraphModel').length !== 1) throw new Error('The diagram must contain one uncompressed page.');
  const root = doc.querySelector('mxGraphModel > root');
  const generated = new DOMParser().parseFromString(createDrawioDiagram(schema), 'application/xml');
  const metadata = generated.querySelector('mxCell[id="schema-metadata"]');
  if (!root || !metadata) throw new Error('Cannot initialize schema metadata.');
  root.appendChild(doc.importNode(metadata, true));
  return new XMLSerializer().serializeToString(doc);
}

type MySQLDefinition = { unsigned: boolean; charset: string | null; collation: string | null; comment: string | null; default_kind: 'none' | 'null' | 'literal'; other_attributes_absent: boolean };
type Column = { id: string; name: string; type: string; nullable: boolean; default: string | number | boolean | null; auto_increment: boolean | null; default_expression?: string | null; identity?: string | null; collation?: string | null; mysql_definition?: MySQLDefinition | null };
type Key = { id: string; name: string | null; kind: 'primary' | 'unique'; column_ids: string[] };
type Index = { id: string; name: string; unique: boolean; column_ids: string[] };
type Relationship = { id: string; name: string | null; source_table_id: string; source_column_ids: string[]; target_table_id: string; target_column_ids: string[]; on_delete: 'cascade' | 'restrict' | 'set null' | 'no action' | 'set default' | null; on_update: 'cascade' | 'restrict' | 'set null' | 'no action' | 'set default' | null };
type Schema = { tables: Array<{ id: string; name: string; namespace: string | null; columns: Column[]; inspection_metadata?: string[]; keys: Key[]; indexes: Index[] }>; relationships: Relationship[] };

/** Read only explicitly identified schema cells; all other shapes are annotations. */
export function schemaFromDrawio(xml: string, original: unknown, requireMetadata = false): unknown {
  const schema = structuredClone(original) as Schema;
  if (!Array.isArray(schema?.tables) || !Array.isArray(schema.relationships)) throw new Error('The saved schema is invalid.');
  const originalColumnIds = new Set(schema.tables.flatMap(table => table.columns.map(column => column.id)));
  const originalRelationshipIds = new Set(schema.relationships.map(relation => relation.id));
  const originalNonConstraintIds = new Set([...schema.tables.flatMap(table => [table.id, ...table.columns.map(column => column.id)]), ...originalRelationshipIds]);
  const originalNonRelationshipIds = new Set(schema.tables.flatMap(table => [table.id, ...table.columns.map(column => column.id), ...table.keys.map(key => key.id), ...table.indexes.map(index => index.id)]));
  const originalConstraints = new Map<string, { table: string; kind: 'key' | 'index' }>(schema.tables.flatMap(table => [
    ...table.keys.map(key => [key.id, { table: table.id, kind: 'key' }] as const),
    ...table.indexes.map(index => [index.id, { table: table.id, kind: 'index' }] as const),
  ]));
  const document = new DOMParser().parseFromString(xml, 'application/xml');
  if (document.querySelector('parsererror') || document.querySelectorAll('mxGraphModel').length !== 1) throw new Error('The diagram must contain one uncompressed page.');
  const cells = new Map<string, Element>();
  for (const cell of document.querySelectorAll('mxCell')) {
    const wrapper = cell.parentElement;
    if (wrapper && wrapper.tagName !== 'root' && [wrapper.getAttribute('id'), cell.getAttribute('id')].some(id => /^(?:(table|column|relationship)-|schema-metadata$)/.test(id || ''))) {
      throw new Error('Schema shapes with custom Draw.io data cannot be saved yet. Remove the custom data and retry; the last valid draft is preserved.');
    }
    const id = cell.getAttribute('id');
    if (!id) continue;
    if (cells.has(id)) throw new Error(`Duplicate diagram ID: ${id}`);
    cells.set(id, cell);
  }
  const metadataCell = cells.get('schema-metadata');
  if (!metadataCell && requireMetadata) throw new Error('The diagram is missing its schema metadata. The last valid draft is preserved.');
  const metadata = new Map<string, { nullable: boolean; default: Column['default']; auto_increment: boolean | null; default_expression?: string | null; identity?: string | null; collation?: string | null; mysql_definition?: MySQLDefinition | null }>();
  const constraints = new Map<string, { keys: Key[]; indexes: Index[]; inspection_metadata?: string[] }>();
  let constraintsVersion = false;
  let relationshipsVersion = false;
  const relationshipMetadata = new Map<string, Relationship>();
  const identifier = (v: unknown): v is string => typeof v === 'string' && !!v.trim() && v.length <= 256;
  if (metadataCell) {
    if (metadataCell.getAttribute('vertex') !== '1' || metadataCell.getAttribute('parent') !== '1' || metadataCell.getAttribute('visible') !== '0') throw new Error('Invalid schema metadata shape.');
    let entries: unknown;
    try { entries = JSON.parse(metadataCell.getAttribute('value') ?? ''); } catch { throw new Error('Invalid column metadata.'); }
    if (!Array.isArray(entries)) throw new Error('Invalid column metadata.');
    for (const raw of entries) {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid column metadata.');
      const data = raw as Record<string, unknown>;
      if (data.kind === 'constraints-v1' && Object.keys(data).join(',') === 'kind') {
        if (constraintsVersion) throw new Error('Duplicate constraints metadata version.');
        constraintsVersion = true; continue;
      }
      if (data.kind === 'relationships-v1' && Object.keys(data).join(',') === 'kind') {
        if (relationshipsVersion) throw new Error('Duplicate relationship metadata version.');
        relationshipsVersion = true; continue;
      }
      if ('relationship' in data) {
        if (Object.keys(data).join(',') !== 'relationship' || !data.relationship || typeof data.relationship !== 'object' || Array.isArray(data.relationship)) throw new Error('Invalid relationship metadata.');
        const rel = data.relationship as Record<string, unknown>;
        const columns = (value: unknown): value is string[] => Array.isArray(value) && value.length > 0 && value.every(identifier) && new Set(value).size === value.length;
        const action = (value: unknown) => value === null || ['cascade', 'restrict', 'set null', 'no action', 'set default'].includes(value as string);
        if (Object.keys(rel).sort().join(',') !== 'id,name,on_delete,on_update,source_column_ids,source_table_id,target_column_ids,target_table_id' || !identifier(rel.id) || relationshipMetadata.has(rel.id) || !(rel.name === null || identifier(rel.name)) || !identifier(rel.source_table_id) || !identifier(rel.target_table_id) || !columns(rel.source_column_ids) || !columns(rel.target_column_ids) || rel.source_column_ids.length !== rel.target_column_ids.length || !action(rel.on_delete) || !action(rel.on_update)) throw new Error('Invalid or duplicate relationship metadata.');
        relationshipMetadata.set(rel.id, rel as Relationship); continue;
      }
      if ('table_id' in data) {
        if (Object.keys(data).filter(key => key !== 'inspection_metadata').sort().join(',') !== 'indexes,keys,table_id' || ('inspection_metadata' in data && !(Array.isArray(data.inspection_metadata) && data.inspection_metadata.every(value => typeof value === 'string'))) || !identifier(data.table_id) || constraints.has(data.table_id) || !Array.isArray(data.keys) || !Array.isArray(data.indexes)) throw new Error('Invalid or duplicate table constraints metadata.');
        const ids = new Set<string>();
        let primary = 0;
        for (const [kind, items] of [['key', data.keys], ['index', data.indexes]] as const) for (const item of items) {
          if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('Invalid constraint metadata.');
          const entry = item as Record<string, unknown>;
          const fields = kind === 'key' ? 'column_ids,id,kind,name' : 'column_ids,id,name,unique';
          if (Object.keys(entry).sort().join(',') !== fields || !identifier(entry.id) || ids.has(entry.id) || !Array.isArray(entry.column_ids) || !entry.column_ids.length || !entry.column_ids.every(identifier) || new Set(entry.column_ids).size !== entry.column_ids.length) throw new Error('Invalid or duplicate constraint metadata.');
          if (kind === 'key') {
            if (!(entry.name === null || identifier(entry.name)) || (entry.kind !== 'primary' && entry.kind !== 'unique')) throw new Error('Invalid key metadata.');
            if (entry.kind === 'primary' && ++primary > 1) throw new Error('A table can have only one primary key.');
          } else if (!identifier(entry.name) || typeof entry.unique !== 'boolean') throw new Error('Invalid index metadata.');
          ids.add(entry.id);
        }
        constraints.set(data.table_id, { keys: data.keys as Key[], indexes: data.indexes as Index[], ...('inspection_metadata' in data ? { inspection_metadata: data.inspection_metadata as string[] } : {}) }); continue;
      }
      const keys = Object.keys(data).filter(key => !['collation', 'mysql_definition', 'default_expression', 'identity'].includes(key)).sort().join(',');
      const definition = data.mysql_definition as Record<string, unknown> | null | undefined;
      const validDefinition = definition == null || (typeof definition === 'object' && !Array.isArray(definition) && Object.keys(definition).sort().join(',') === 'charset,collation,comment,default_kind,other_attributes_absent,unsigned' && typeof definition.unsigned === 'boolean' && typeof definition.other_attributes_absent === 'boolean' && ['none', 'null', 'literal'].includes(definition.default_kind as string) && [definition.charset, definition.collation, definition.comment].every(value => value === null || (typeof value === 'string' && value.length <= 256)));
      if (keys !== 'auto_increment,default,id,nullable' || typeof data.id !== 'string' || !data.id.trim() || data.id.length > 256 || metadata.has(data.id) || typeof data.nullable !== 'boolean' || !(data.auto_increment === null || typeof data.auto_increment === 'boolean') || !validDefinition || ['default_expression', 'identity'].some(key => key in data && !(data[key] === null || (typeof data[key] === 'string' && (data[key] as string).length <= 65536))) || ('collation' in data && !(data.collation === null || identifier(data.collation))) || !(data.default === null || typeof data.default === 'string' || typeof data.default === 'boolean' || (typeof data.default === 'number' && Number.isFinite(data.default)))) throw new Error('Invalid or duplicate column metadata.');
      metadata.set(data.id, { nullable: data.nullable, default: data.default as Column['default'], auto_increment: data.auto_increment as boolean | null, ...('default_expression' in data ? { default_expression: data.default_expression as string | null } : {}), ...('identity' in data ? { identity: data.identity as string | null } : {}), ...('collation' in data ? { collation: data.collation as string | null } : {}), ...('mysql_definition' in data ? { mysql_definition: definition as MySQLDefinition | null } : {}) });
    }
  }
  if ((constraints.size && !constraintsVersion) || (requireMetadata && !constraintsVersion)) throw new Error('Missing constraints metadata version.');
  if ((relationshipMetadata.size && !relationshipsVersion) || (requireMetadata && !relationshipsVersion)) throw new Error('Missing relationship metadata version.');
  const metadataByCellId = new Map([...metadata.keys()].map(id => [`column-${encodeURIComponent(id)}`, id]));
  const otherIds = new Set([...schema.tables.flatMap(table => [table.id, ...table.keys.map(key => key.id), ...table.indexes.map(index => index.id)]), ...schema.relationships.map(relation => relation.id)]);
  const cellId = (kind: string, id: string) => `${kind}-${encodeURIComponent(id)}`;
  const expected = new Set<string>();
  const get = (kind: string, id: string) => {
    const key = cellId(kind, id); expected.add(key);
    const cell = cells.get(key);
    if (!cell) throw new Error(`Missing ${kind} ${id}. Deleting schema shapes is not supported in Draw.io yet.`);
    return cell;
  };
  const valid = (value: string, owner: string) => {
    if (!value.trim() || value.length > 256 || /[\r\n<>]/.test(value)) throw new Error(`Invalid ${owner}. Use plain text between 1 and 256 characters.`);
    return value;
  };
  // Only IDs created by the bundled plugin can introduce schema entities.
  const newId = (id: string, kind: string) => new RegExp(`^${kind}-(new-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$`).exec(id)?.[1];
  if (relationshipsVersion) {
    schema.relationships = [...relationshipMetadata.values()].filter(relation => cells.has(cellId('relationship', relation.id)));
    for (const relation of schema.relationships) {
      if (originalNonRelationshipIds.has(relation.id) || (!originalRelationshipIds.has(relation.id) && !/^new-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(relation.id))) throw new Error(`Invalid relationship identity: ${relation.id}.`);
    }
  }
  const removedTables = schema.tables.filter(table => !cells.has(cellId('table', table.id)));
  if (removedTables.some(table => schema.relationships.some(r => r.source_table_id === table.id || r.target_table_id === table.id))) throw new Error('Remove relationships in the structured schema editor before deleting their tables.');
  schema.tables = schema.tables.filter(table => cells.has(cellId('table', table.id)));
  for (const [id, cell] of cells) {
    const value = newId(id, 'table');
    if (value && !schema.tables.some(table => table.id === value)) {
      if (cell.getAttribute('vertex') !== '1' || cell.getAttribute('parent') !== '1') throw new Error('Invalid new table shape.');
      schema.tables.push({ id: value, name: valid(cell.getAttribute('value') ?? '', 'table name'), namespace: null, columns: [], keys: [], indexes: [] });
    }
  }
  const names = new Set<string>();
  for (const table of schema.tables) {
    const cell = get('table', table.id);
    if (cell.getAttribute('vertex') !== '1' || cell.getAttribute('parent') !== '1') throw new Error(`Invalid table shape: ${table.name}.`);
    const title = cell.getAttribute('value') ?? '';
    const prefix = table.namespace ? `${table.namespace}.` : '';
    if (!title.startsWith(prefix)) throw new Error(`Changing the namespace of ${table.name} is not supported in Draw.io.`);
    const newName = title.slice(prefix.length);
    if (newName !== table.name) table.name = valid(newName, 'table name');
    const nameKey = JSON.stringify([table.namespace, table.name]);
    if (names.has(nameKey)) throw new Error(`Duplicate table name: ${table.name}.`);
    names.add(nameKey);
    const tableConstraints = constraints.get(table.id);
    if (constraintsVersion && !tableConstraints) throw new Error(`Missing constraints metadata for table ${table.name}.`);
    if (tableConstraints) {
      for (const [kind, entries] of [['key', tableConstraints.keys], ['index', tableConstraints.indexes]] as const) for (const entry of entries) {
        const previous = originalConstraints.get(entry.id);
        if (previous ? previous.table !== table.id || previous.kind !== kind : originalNonConstraintIds.has(entry.id) || !/^new-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(entry.id)) throw new Error(`Invalid constraint identity: ${entry.id}.`);
      }
      table.keys = tableConstraints.keys; table.indexes = tableConstraints.indexes;
      if ('inspection_metadata' in tableConstraints) table.inspection_metadata = tableConstraints.inspection_metadata;
    }
    const removedColumns = table.columns.filter(column => !cells.has(cellId('column', column.id)));
    for (const column of removedColumns) {
      if ([...(table.keys || []), ...(table.indexes || [])].some(item => item.column_ids.includes(column.id)) || schema.relationships.some(r => r.source_column_ids.includes(column.id) || r.target_column_ids.includes(column.id))) throw new Error(`Remove references to column ${column.name} in the structured schema editor before deleting it.`);
    }
    table.columns = table.columns.filter(column => cells.has(cellId('column', column.id)));
    for (const [id, item] of cells) {
      const metadataId = metadataByCellId.get(id);
      const value = newId(id, 'column') || (metadataId && !originalColumnIds.has(metadataId) ? metadataId : undefined);
      if (value && item.getAttribute('parent') === cellId('table', table.id) && !table.columns.some(column => column.id === value)) {
        if (otherIds.has(value) || originalColumnIds.has(value)) throw new Error(`Duplicate schema ID: ${value}.`);
        const label = item.getAttribute('value') ?? '';
        const separator = label.indexOf(': ');
        if (separator < 0 || label.indexOf(': ', separator + 2) >= 0) throw new Error('Invalid new column label; use name: type.');
        table.columns.push({ id: value, name: valid(label.slice(0, separator), 'column name'), type: valid(label.slice(separator + 2), 'column type'), nullable: false, default: null, auto_increment: false });
      }
    }
    const activeColumnIds = new Set(table.columns.map(column => column.id));
    if ([...table.keys, ...table.indexes].some(entry => entry.column_ids.some(id => !activeColumnIds.has(id)))) throw new Error(`Invalid constraint column reference in ${table.name}.`);
    const columnNames = new Set<string>();
    for (const column of table.columns) {
      const item = get('column', column.id);
      if (item.getAttribute('vertex') !== '1' || item.getAttribute('parent') !== cellId('table', table.id)) throw new Error(`Moving column ${column.name} to another table is not supported.`);
      const label = item.getAttribute('value') ?? '';
      if (label !== `${column.name}: ${column.type}`) {
        if (column.name.includes(': ') || column.type.includes(': ')) throw new Error(`Column ${column.name} has an ambiguous label. Edit it in the structured schema controls.`);
        const separator = label.indexOf(': ');
        if (separator < 0 || label.indexOf(': ', separator + 2) >= 0) throw new Error(`Invalid column label in ${table.name}; use name: type.`);
        column.name = valid(label.slice(0, separator), 'column name');
        column.type = valid(label.slice(separator + 2), 'column type');
      }
      if (columnNames.has(column.name)) throw new Error(`Duplicate column name in ${table.name}: ${column.name}.`);
      columnNames.add(column.name);
    }
  }
  const schemaIds = new Set<string>();
  for (const id of [...schema.tables.flatMap(table => [table.id, ...table.columns.map(column => column.id), ...table.keys.map(key => key.id), ...table.indexes.map(index => index.id)]), ...schema.relationships.map(relation => relation.id)]) {
    if (schemaIds.has(id)) throw new Error(`Duplicate schema ID: ${id}.`);
    schemaIds.add(id);
  }
  for (const relation of schema.relationships) {
    const cell = get('relationship', relation.id);
    if (cell.getAttribute('edge') !== '1' || cell.getAttribute('source') !== cellId('table', relation.source_table_id) || cell.getAttribute('target') !== cellId('table', relation.target_table_id) || (cell.getAttribute('value') ?? '') !== '') throw new Error(`Invalid connector for relationship ${relation.id}. Use the relationship editor.`);
    const source = schema.tables.find(table => table.id === relation.source_table_id);
    const target = schema.tables.find(table => table.id === relation.target_table_id);
    if (!source || !target || relation.source_column_ids.some(id => !source.columns.some(column => column.id === id)) || relation.target_column_ids.some(id => !target.columns.some(column => column.id === id))) throw new Error(`Invalid table or column reference in relationship ${relation.id}.`);
  }
  if (metadataCell) {
    for (const table of schema.tables) for (const column of table.columns) {
      const data = metadata.get(column.id);
      if (!data) throw new Error(`Missing metadata for column ${column.name}.`);
      column.nullable = data.nullable; column.default = data.default; column.auto_increment = data.auto_increment;
      if ('default_expression' in data) column.default_expression = data.default_expression;
      if ('identity' in data) column.identity = data.identity;
      if ('collation' in data) column.collation = data.collation;
      if ('mysql_definition' in data) column.mysql_definition = data.mysql_definition;
    }
  }
  for (const id of cells.keys()) if (/^(table|column|relationship)-/.test(id) && !expected.has(id)) throw new Error(`Unknown schema shape ${id}. Rebuild the diagram or remove the unsupported shape.`);
  return schema;
}
