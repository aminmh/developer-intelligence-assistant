export const MAX_LAYOUT_BYTES = 4 * 1024 * 1024;
const byteLength = (value: string) => new TextEncoder().encode(value).length;
const rejectedLayoutMemory = new Map<string, { xml: string; stored: boolean }>();
function warnUnsavedRejectedLayouts(event: BeforeUnloadEvent): void {
  event.preventDefault();
  event.returnValue = '';
}

export function cacheRejectedLayout(key: string, xml: string, stored = false): void {
  const wasEmpty = rejectedLayoutMemory.size === 0;
  if (xml) rejectedLayoutMemory.set(key, { xml, stored });
  else rejectedLayoutMemory.delete(key);
  if (typeof window !== 'undefined') {
    if (wasEmpty && rejectedLayoutMemory.size) window.addEventListener('beforeunload', warnUnsavedRejectedLayouts);
    else if (!wasEmpty && !rejectedLayoutMemory.size) window.removeEventListener('beforeunload', warnUnsavedRejectedLayouts);
  }
}

export function cachedRejectedLayout(key: string): string {
  return rejectedLayoutMemory.get(key)?.xml || '';
}

export function rejectedLayoutStored(key: string): boolean {
  return rejectedLayoutMemory.get(key)?.stored ?? false;
}

export function restoreRejectedLayouts(storage: Pick<Storage, 'length' | 'key' | 'getItem'>): void {
  const prefix = 'schema-draft-recovery:';
  const suffix = ':rejected-layout';
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index);
    if (!key?.startsWith(prefix) || !key.endsWith(suffix)) continue;
    const id = key.slice(prefix.length, -suffix.length);
    const xml = storage.getItem(key);
    if (id && xml) cacheRejectedLayout(id, xml, true);
  }
}

if (typeof window !== 'undefined') {
  try { restoreRejectedLayouts(window.sessionStorage); } catch { /* Storage can be unavailable. */ }
}

export function storeRejectedLayout(storage: Pick<Storage, 'setItem' | 'removeItem'>, key: string, xml: string): boolean {
  try {
    if (xml) storage.setItem(key, xml);
    else storage.removeItem(key);
    return true;
  } catch { return false; }
}

export function mayReplaceRejectedLayout(xml: string, recoveryAvailable: boolean, confirmDiscard: () => boolean): boolean {
  return !xml || recoveryAvailable || confirmDiscard();
}

export function editorUrl(raw: string, hostOrigin: string): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error('Configure a valid Draw.io origin before opening the editor.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash || url.origin === hostOrigin) throw new Error('Draw.io must use a separate HTTP(S) origin without a path.');
  if (url.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw new Error('Draw.io requires HTTPS outside local development.');
  url.search = new URLSearchParams({ embed: '1', proto: 'json', spin: '1', modified: 'unsavedChanges', saveAndExit: '1', p: 'props' }).toString();
  return url;
}

export function validLayout(xml: string): boolean {
  if (!xml || byteLength(xml) > MAX_LAYOUT_BYTES || /<!DOCTYPE/i.test(xml)) return false;
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  return !doc.querySelector('parsererror') && ['mxfile', 'mxGraphModel'].includes(doc.documentElement?.tagName);
}

export type EditorEvent = { event: 'init' | 'exit' | 'oversize' } | { event: 'save' | 'invalid'; xml: string; exit: boolean };
export type SchemaSelection = { kind: 'table' | 'relationship'; id: string };
export function readEditorSelection(event: Pick<MessageEvent, 'origin' | 'source' | 'data'>, origin: string, popup: Window): SchemaSelection | null {
  if (event.origin !== origin || event.source !== popup || typeof event.data !== 'string' || event.data.length > 2048 || byteLength(event.data) > 2048) return null;
  let message: unknown;
  try { message = JSON.parse(event.data); } catch { return null; }
  if (!message || typeof message !== 'object' || Array.isArray(message)) return null;
  const data = message as Record<string, unknown>;
  if (data.event !== 'schema-selection' || (data.kind !== 'table' && data.kind !== 'relationship') || typeof data.id !== 'string' || !data.id.trim() || data.id.length > 256 || /[\u0000-\u001f]/.test(data.id)) return null;
  return { kind: data.kind, id: data.id };
}
export function readEditorEvent(event: Pick<MessageEvent, 'origin' | 'source' | 'data'>, origin: string, popup: Window, validate: (xml: string) => boolean = validLayout): EditorEvent | null {
  if (event.origin !== origin || event.source !== popup || typeof event.data !== 'string') return null;
  if (event.data.length > MAX_LAYOUT_BYTES * 2 || byteLength(event.data) > MAX_LAYOUT_BYTES * 2) return { event: 'oversize' };
  let data: unknown;
  try { data = JSON.parse(event.data); } catch { return null; }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const message = data as Record<string, unknown>;
  if (message.event === 'init' || message.event === 'exit') return { event: message.event };
  if (message.event === 'save' && typeof message.xml === 'string' && (message.exit === undefined || typeof message.exit === 'boolean')) {
    if (message.xml.length > MAX_LAYOUT_BYTES || byteLength(message.xml) > MAX_LAYOUT_BYTES) return { event: 'oversize' };
    return { event: validate(message.xml) ? 'save' : 'invalid', xml: message.xml, exit: message.exit === true };
  }
  return null;
}
