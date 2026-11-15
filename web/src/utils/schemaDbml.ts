import type { StructuredSchemaDraft } from '../types';

/** Export representable schema details. DBML is an interchange format, not a draft backup. */
export function structuredSchemaToDbml(schema: StructuredSchemaDraft): string {
  const id = (value: string) => {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(value)) throw new Error(`DBML export cannot represent identifier ${value}. Use schema JSON instead.`);
    return value;
  };
  const qualified = (table: StructuredSchemaDraft['tables'][number]) => table.namespace ? `${id(table.namespace)}.${id(table.name)}` : id(table.name);
  const tables = new Map(schema.tables.map(table => [table.id, table]));
  const columns = (table: StructuredSchemaDraft['tables'][number], ids: string[]) => ids.map(columnId => {
    const column = table.columns.find(item => item.id === columnId);
    if (!column) throw new Error(`Unknown column in table ${table.name}.`);
    return id(column.name);
  });
  const list = (names: string[]) => names.length === 1 ? names[0] : `(${names.join(', ')})`;
  const blocks = schema.tables.map(table => {
    const lines = [`Table ${qualified(table)} {`];
    for (const column of table.columns) {
      if (!/^[A-Za-z][A-Za-z0-9_ ]*(?:\(\s*\d+\s*(?:,\s*\d+\s*)?\))?(?:\[\])?$/.test(column.type)) throw new Error(`DBML export cannot represent the type of ${table.name}.${column.name}. Use schema JSON instead.`);
      const settings = [];
      if (!column.nullable) settings.push('not null');
      if (column.auto_increment === true) settings.push('increment');
      if (table.keys.some(key => key.kind === 'unique' && !key.name && key.column_ids.length === 1 && key.column_ids[0] === column.id)) settings.push('unique');
      if (column.default !== null) {
        const value = typeof column.default === 'string' ? `'${column.default.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'` : String(column.default);
        if (/[\r\n]/.test(value)) throw new Error(`DBML export cannot represent the default of ${table.name}.${column.name}. Use schema JSON instead.`);
        settings.push(`default: ${value}`);
      }
      lines.push(`  ${id(column.name)} ${column.type}${settings.length ? ` [${settings.join(', ')}]` : ''}`);
    }
    const indexes = [
      ...table.keys.filter(key => !(key.kind === 'unique' && !key.name && key.column_ids.length === 1)).map(key => ({ column_ids: key.column_ids, name: key.name, setting: key.kind === 'primary' ? 'pk' : 'unique' })),
      ...table.indexes.map(index => ({ column_ids: index.column_ids, name: index.name, setting: index.unique ? 'unique' : '' })),
    ];
    if (indexes.length) {
      lines.push('  indexes {');
      for (const index of indexes) {
        const settings = [index.setting, index.name ? `name: "${id(index.name)}"` : ''].filter(Boolean);
        if (!index.name && index.setting !== 'pk') throw new Error(`DBML export requires a name for a unique constraint in ${table.name}. Use schema JSON instead.`);
        lines.push(`    ${list(columns(table, index.column_ids))}${settings.length ? ` [${settings.join(', ')}]` : ''}`);
      }
      lines.push('  }');
    }
    lines.push('}');
    return lines.join('\n');
  });
  for (const relationship of schema.relationships) {
    const source = tables.get(relationship.source_table_id), target = tables.get(relationship.target_table_id);
    if (!source || !target) throw new Error('A relationship references a missing table.');
    const settings = [relationship.on_delete ? `delete: ${relationship.on_delete}` : '', relationship.on_update ? `update: ${relationship.on_update}` : ''].filter(Boolean);
    blocks.push(`Ref${relationship.name ? ` ${id(relationship.name)}` : ''}: ${qualified(source)}.${list(columns(source, relationship.source_column_ids))} > ${qualified(target)}.${list(columns(target, relationship.target_column_ids))}${settings.length ? ` [${settings.join(', ')}]` : ''}`);
  }
  return `${blocks.join('\n\n')}\n`;
}
