import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

test('Draw.io shows runtime counts only from its connected host', () => {
  const actions = new Map<string, () => void>();
  const alerts: string[] = [];
  const opener = {};
  const table = { id: 'table-users' };
  const model = { getValue: () => 'public.users', getParent: () => table };
  let receive: (event: unknown) => void = () => {};
  const graph = { getModel: () => model, getSelectionCell: () => table };
  const ui = {
    editor: { graph },
    actions: { get: () => null, addAction: (name: string, action: () => void) => actions.set(name, action) },
    menus: { get: () => ({ funct: () => {} }), addMenuItems: () => {} },
  };
  const context = {
    Draw: { loadPlugin: (load: (ui: unknown) => void) => load(ui) },
    mxResources: { parse: () => {} },
    window: { opener, alert: (message: string) => alerts.push(message), addEventListener: (_event: string, handler: (event: unknown) => void) => { receive = handler; } },
    document: { referrer: 'https://app.example/' }, URL,
  };
  runInNewContext(readFileSync(new URL('../drawio/props.js', import.meta.url), 'utf8'), context);
  const message = (origin: string, source: unknown, counts: unknown) => receive({ origin, source, data: JSON.stringify({ action: 'schema-runtime-counts', counts }) });
  message('https://other.example', opener, { users: 90 });
  actions.get('schemaRuntime')!();
  assert.match(alerts.pop()!, /No observed operations/);
  message('https://app.example', opener, { users: 12 });
  actions.get('schemaRuntime')!();
  assert.match(alerts.pop()!, /12 observed operations/);
  message('https://app.example', {}, { users: 90 });
  actions.get('schemaRuntime')!();
  assert.match(alerts.pop()!, /12 observed operations/);
});
