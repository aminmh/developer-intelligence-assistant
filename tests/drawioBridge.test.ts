import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cacheRejectedLayout, cachedRejectedLayout, editorUrl, MAX_LAYOUT_BYTES, mayReplaceRejectedLayout, readEditorEvent, rejectedLayoutStored, restoreRejectedLayouts, storeRejectedLayout } from '../src/utils/drawioBridge';
import { createDrawioDiagram, rebuildDrawioDiagram } from '../src/utils/drawioDiagram';

test('editor URL enforces a separate secure origin and embed protocol', () => {
  const url = editorUrl('http://localhost:18081', 'http://localhost:3000');
  assert.equal(url.origin, 'http://localhost:18081');
  assert.equal(url.searchParams.get('proto'), 'json');
  for (const input of ['http://localhost:3000', 'https://example.com/path', 'http://remote.example', 'javascript:alert(1)', 'https://user:pass@example.com']) assert.throws(() => editorUrl(input, 'http://localhost:3000'));
});

test('bridge accepts only bounded messages from the expected origin and window', () => {
  const popup = {} as Window;
  const origin = 'https://drawio.example';
  const event = (data: string, source: Window = popup, sender = origin) => ({ data, source, origin: sender });
  const xml = '<mxfile/>';
  const validate = (value: string) => value === xml;
  assert.deepEqual(readEditorEvent(event(JSON.stringify({ event: 'save', xml, exit: true })), origin, popup, validate), { event: 'save', xml, exit: true });
  assert.equal(readEditorEvent(event(JSON.stringify({ event: 'save', xml }), {} as Window), origin, popup, validate), null);
  assert.equal(readEditorEvent(event(JSON.stringify({ event: 'save', xml }), popup, 'https://evil.example'), origin, popup, validate), null);
  assert.deepEqual(readEditorEvent(event(JSON.stringify({ event: 'save', xml: '<bad/>' })), origin, popup, validate), { event: 'invalid', xml: '<bad/>', exit: false });
  assert.equal(readEditorEvent(event(JSON.stringify({ event: 'save', xml, exit: 'yes' })), origin, popup, validate), null);
  assert.equal(readEditorEvent(event('{'), origin, popup, validate), null);
  assert.deepEqual(readEditorEvent(event('x'.repeat(MAX_LAYOUT_BYTES * 2 + 1)), origin, popup, validate), { event: 'oversize' });
  assert.deepEqual(readEditorEvent(event(JSON.stringify({ event: 'save', xml: 'x'.repeat(MAX_LAYOUT_BYTES + 1) })), origin, popup, validate), { event: 'oversize' });
  assert.equal(readEditorEvent(event('x'.repeat(MAX_LAYOUT_BYTES * 2 + 1), {} as Window), origin, popup, validate), null);
  assert.equal(readEditorEvent(event(JSON.stringify({ event: 'unknown' })), origin, popup, validate), null);
});

test('rejected XML recovery reports storage failure and keeps bounded XML intact', () => {
  const items = new Map<string, string>();
  const storage = { setItem: (key: string, value: string) => { items.set(key, value); }, removeItem: (key: string) => { items.delete(key); } };
  const xml = '<!DOCTYPE invalid><mxfile/>';
  assert.equal(storeRejectedLayout(storage, 'draft', xml), true);
  assert.equal(items.get('draft'), xml);
  assert.equal(storeRejectedLayout(storage, 'draft', ''), true);
  assert.equal(items.has('draft'), false);
  assert.equal(storeRejectedLayout({ ...storage, setItem: () => { throw new Error('quota'); } }, 'draft', xml), false);
});

test('switching drafts requires explicit discard when rejected XML has no stored copy', () => {
  let asked = 0;
  const reject = () => { asked++; return false; };
  assert.equal(mayReplaceRejectedLayout('<mxfile/>', false, reject), false);
  assert.equal(asked, 1);
  assert.equal(mayReplaceRejectedLayout('<mxfile/>', false, () => true), true);
  assert.equal(mayReplaceRejectedLayout('<mxfile/>', true, reject), true);
  assert.equal(mayReplaceRejectedLayout('', false, reject), true);
  assert.equal(asked, 1);
});

test('failed storage keeps rejected XML accessible across component lifetimes', () => {
  cacheRejectedLayout('draft-memory-test', '<mxfile/>');
  assert.equal(cachedRejectedLayout('other-draft'), '');
  assert.equal(cachedRejectedLayout('draft-memory-test'), '<mxfile/>');
  cacheRejectedLayout('draft-memory-test', '');
  assert.equal(cachedRejectedLayout('draft-memory-test'), '');
});

test('in-memory rejected XML installs an unload guard until every cached draft is cleared', () => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  let listener: ((event: BeforeUnloadEvent) => void) | undefined;
  let removals = 0;
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    addEventListener: (_name: string, callback: typeof listener) => { listener = callback; },
    removeEventListener: (_name: string, callback: typeof listener) => { assert.equal(callback, listener); removals++; },
  } });
  try {
    cacheRejectedLayout('first', '<mxfile/>');
    cacheRejectedLayout('second', '<mxfile/>', true);
    assert.equal(rejectedLayoutStored('second'), true);
    assert.ok(listener);
    let prevented = false;
    const event = { preventDefault: () => { prevented = true; }, returnValue: 'unchanged' } as unknown as BeforeUnloadEvent;
    listener(event);
    assert.equal(prevented, true);
    assert.equal(event.returnValue, '');
    cacheRejectedLayout('first', '');
    assert.equal(removals, 0);
    cacheRejectedLayout('second', '');
    assert.equal(removals, 1);
  } finally {
    if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow);
    else delete (globalThis as { window?: unknown }).window;
  }
});

test('restored session storage retains rejected XML and its stored status', () => {
  const values = new Map([['schema-draft-recovery:restored:rejected-layout', '<mxfile/>'], ['unrelated', 'value']]);
  restoreRejectedLayouts({ length: values.size, key: (index) => [...values.keys()][index] ?? null, getItem: (key) => values.get(key) ?? null });
  assert.equal(cachedRejectedLayout('restored'), '<mxfile/>');
  assert.equal(rejectedLayoutStored('restored'), true);
  cacheRejectedLayout('restored', '');
});

test('starting diagram escapes schema labels and references stable IDs', () => {
  const xml = createDrawioDiagram({ tables: [{ id: 'users:1', name: '<users>', namespace: null, columns: [{ id: 'id:1', name: 'id&name', type: 'int' }] }], relationships: [] });
  assert.match(xml, /table-users%3A1/);
  assert.match(xml, /column-id%3A1/);
  assert.match(xml, /&lt;users&gt;/);
  assert.match(xml, /id&amp;name/);
});

test('rebuilding a diagram preserves the old layout on cancellation and uses the saved schema on confirmation', () => {
  const existing = '<mxfile>hand-placed annotations</mxfile>';
  const saved = { tables: [{ id: 't', name: 'saved_table', namespace: null, columns: [] }], relationships: [] };
  assert.equal(rebuildDrawioDiagram(existing, saved, () => false), existing);
  const rebuilt = rebuildDrawioDiagram(existing, saved, () => true);
  assert.match(rebuilt, /saved_table/);
  assert.doesNotMatch(rebuilt, /hand-placed annotations/);
});
