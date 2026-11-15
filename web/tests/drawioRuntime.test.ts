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

test('Draw.io schema search selects and reveals the chosen matching column', () => {
  const actions = new Map<string, () => void>();
  const parent = { id: 'root' };
  const table = { id: 'table-users', value: 'users' };
  const first = { id: 'column-name', value: 'name: text' };
  const second = { id: 'column-username', value: 'username: text' };
  const children = new Map([[parent, [table]], [table, [first, second]]]);
  const model = { getValue: (cell: typeof table) => cell.value, getChildCount: (cell: typeof table) => children.get(cell)?.length || 0,
    getChildAt: (cell: typeof table, i: number) => children.get(cell)![i] };
  let selected: unknown, revealed: unknown;
  const graph = { getModel: () => model, getDefaultParent: () => parent,
    setSelectionCell: (cell: unknown) => { selected = cell; }, scrollCellToVisible: (cell: unknown) => { revealed = cell; } };
  const answers = ['name', '2'];
  const ui = { editor: { graph }, actions: { get: () => null, addAction: (name: string, action: () => void) => actions.set(name, action) },
    menus: { get: () => ({ funct: () => {} }), addMenuItems: () => {} } };
  runInNewContext(readFileSync(new URL('../drawio/props.js', import.meta.url), 'utf8'), {
    Draw: { loadPlugin: (load: (ui: unknown) => void) => load(ui) }, mxResources: { parse: () => {} },
    window: { opener: {}, prompt: () => answers.shift(), alert: () => {}, addEventListener: () => {} },
    document: { referrer: 'https://app.example/' }, URL,
  });
  actions.get('schemaFind')!();
  assert.equal(selected, second);
  assert.equal(revealed, second);
});

test('Draw.io exchanges selection by stable ID with the connected host', () => {
  const actions = new Map<string, () => void>(), sent: Array<{ data: string; origin: string }> = [], alerts: string[] = [];
  const table = { id: 'table-table%3Aapp.users' }, column = { id: 'column-col%3Aemail' };
  let current: typeof table | typeof column = column, revealed: unknown, receive: (event: unknown) => void = () => {}, focused = 0;
  const opener = { postMessage: (data: string, origin: string) => sent.push({ data, origin }), focus: () => { focused++; } };
  const model = { getParent: () => table, getCell: (id: string) => id === table.id ? table : null };
  const graph = { getModel: () => model, getSelectionCell: () => current, setSelectionCell: (cell: typeof table) => { current = cell; }, scrollCellToVisible: (cell: unknown) => { revealed = cell; } };
  const ui = { editor: { graph }, actions: { get: () => null, addAction: (name: string, action: () => void) => actions.set(name, action) }, menus: { get: () => ({ funct: () => {} }), addMenuItems: () => {} } };
  runInNewContext(readFileSync(new URL('../drawio/props.js', import.meta.url), 'utf8'), {
    Draw: { loadPlugin: (load: (ui: unknown) => void) => load(ui) }, mxResources: { parse: () => {} },
    window: { opener, alert: (message: string) => alerts.push(message), addEventListener: (_: string, handler: (event: unknown) => void) => { receive = handler; } },
    document: { referrer: 'https://app.example/' }, URL,
  });
  actions.get('schemaShowInApp')!();
  assert.deepEqual(JSON.parse(sent[0].data), { event: 'schema-selection', kind: 'table', id: 'table:app.users' });
  assert.equal(sent[0].origin, 'https://app.example');
  assert.equal(focused, 1);
  const data = JSON.stringify({ action: 'schema-reveal-selection', kind: 'table', id: 'table:app.users' });
  receive({ source: {}, origin: 'https://app.example', data });
  receive({ source: opener, origin: 'https://evil.example', data });
  assert.equal(revealed, undefined);
  receive({ source: opener, origin: 'https://app.example', data });
  assert.equal(current, table);
  assert.equal(revealed, table);
  receive({ source: opener, origin: 'https://app.example', data: JSON.stringify({ action: 'schema-reveal-selection', kind: 'table', id: 'missing' }) });
  assert.match(alerts[0], /not present/);
});

test('schema export downloads the current editor model only after a matching host response', async () => {
  const actions = new Map<string, () => Promise<void>>();
  const sent: { message: string; origin: string }[] = [];
  const alerts: string[] = [];
  let receive: (event: unknown) => void = () => {};
  let downloaded = '';
  let allowDbml = false;
  const opener = { postMessage: (message: string, origin: string) => sent.push({ message, origin }) };
  const graph = { getModel: () => ({}) };
  const ui = { editor: { graph }, actions: { get: () => null, addAction: (name: string, action: () => Promise<void>) => actions.set(name, action) },
    menus: { get: () => ({ funct: () => {} }), addMenuItems: () => {} } };
  class FakeURL extends URL { static createObjectURL() { return 'blob:test'; } static revokeObjectURL() {} }
  runInNewContext(readFileSync(new URL('../drawio/props.js', import.meta.url), 'utf8'), {
    Draw: { loadPlugin: (load: (ui: unknown) => void) => load(ui) }, mxResources: { parse: () => {} },
    mxUtils: { getXml: () => '<mxGraphModel/>' }, mxCodec: class { encode() { return {}; } },
    crypto: { randomUUID: () => 'request-1' }, URL: FakeURL, Blob, TextEncoder, setTimeout, clearTimeout,
    window: { opener, confirm: () => allowDbml, alert: (message: string) => alerts.push(message), addEventListener: (_: string, handler: (event: unknown) => void) => { receive = handler; } },
    document: { referrer: 'https://app.example/', createElement: () => ({ click() { downloaded = this.download; }, download: '', href: '' }) },
  });
  const result = actions.get('schemaExport')!();
  assert.deepEqual(JSON.parse(sent[0].message), { requestId: 'request-1', xml: '<mxGraphModel/>', event: 'schema-export-json' });
  assert.equal(sent[0].origin, 'https://app.example');
  const respond = (origin: string, source: unknown, action: string) => receive({ origin, source, data: JSON.stringify({ action, requestId: 'request-1', ok: true, content: '{}' }) });
  respond('https://evil.example', opener, 'schema-export-result');
  respond('https://app.example', {}, 'schema-export-result');
  respond('https://app.example', opener, 'schema-migration-result');
  assert.equal(downloaded, '');
  respond('https://app.example', opener, 'schema-export-result');
  await result;
  assert.equal(downloaded, 'schema.json');
  actions.get('schemaExportDbml')!();
  assert.equal(sent.length, 1);
  allowDbml = true;
  const dbml = actions.get('schemaExportDbml')!();
  assert.equal(JSON.parse(sent[1].message).event, 'schema-export-dbml');
  respond('https://app.example', opener, 'schema-export-result');
  await dbml;
  assert.equal(downloaded, 'schema.dbml');
  assert.deepEqual(alerts, []);
});
