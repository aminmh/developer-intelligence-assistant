type Identified = { id: string };
type IdentitySchema = {
  tables: Array<Identified & { columns: Identified[]; keys: Identified[]; indexes: Identified[] }>;
  relationships: Identified[];
};

export function trackSchemaIdentities(schema: unknown, ledger: Map<string, string>) {
  const value = schema as IdentitySchema;
  const identities: Array<[string, string]> = [];
  const record = (id: string, kind: string, owner: string | null = null) => {
    identities.push([id, JSON.stringify([kind, owner])]);
  };
  for (const table of value.tables) {
    record(table.id, 'table');
    for (const column of table.columns) record(column.id, 'column', table.id);
    for (const key of table.keys) record(key.id, 'key', table.id);
    for (const index of table.indexes) record(index.id, 'index', table.id);
  }
  for (const relationship of value.relationships) record(relationship.id, 'relationship');
  for (const [id, identity] of identities) {
    if (ledger.has(id) && ledger.get(id) !== identity) throw new Error(`Schema ID ${id} cannot be reused for a different object or table.`);
  }
  for (const [id, identity] of identities) ledger.set(id, identity);
}
