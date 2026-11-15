import { MAX_LAYOUT_BYTES } from './drawioBridge';

type PreviewRequest = { event: 'schema-migration-preview'; requestId: string; xml: string; framework: 'laravel' | 'symfony'; database: 'mysql' | 'postgresql'; timestamp: string };
type DownloadRequest = { event: 'schema-migration-download'; requestId: string; xml: string; confirmed: boolean };
export type DrawioMigrationRequest = PreviewRequest | DownloadRequest;
export type DrawioSchemaExportRequest = { event: 'schema-export-json' | 'schema-export-dbml'; requestId: string; xml: string };

function readRequest(event: Pick<MessageEvent, 'origin' | 'source' | 'data'>, origin: string, popup: Window): Record<string, unknown> | null {
  if (event.origin !== origin || event.source !== popup || typeof event.data !== 'string' || event.data.length > MAX_LAYOUT_BYTES * 2 || new TextEncoder().encode(event.data).length > MAX_LAYOUT_BYTES * 2) return null;
  let data: Record<string, unknown>;
  try { data = JSON.parse(event.data); } catch { return null; }
  if (!data || typeof data !== 'object' || Array.isArray(data) || typeof data.requestId !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(data.requestId) || typeof data.xml !== 'string' || data.xml.length > MAX_LAYOUT_BYTES || new TextEncoder().encode(data.xml).length > MAX_LAYOUT_BYTES) return null;
  return data;
}

export function readSchemaExportRequest(event: Pick<MessageEvent, 'origin' | 'source' | 'data'>, origin: string, popup: Window): DrawioSchemaExportRequest | null {
  const data = readRequest(event, origin, popup);
  return data && (data.event === 'schema-export-json' || data.event === 'schema-export-dbml') ? data as DrawioSchemaExportRequest : null;
}

export function readMigrationRequest(event: Pick<MessageEvent, 'origin' | 'source' | 'data'>, origin: string, popup: Window): DrawioMigrationRequest | null {
  const data = readRequest(event, origin, popup);
  if (!data) return null;
  if (data.event === 'schema-migration-preview' && (data.framework === 'laravel' || data.framework === 'symfony') && (data.database === 'mysql' || data.database === 'postgresql') && typeof data.timestamp === 'string' && /^\d{4}_\d{2}_\d{2}_\d{6}$/.test(data.timestamp)) return data as PreviewRequest;
  if (data.event === 'schema-migration-download' && typeof data.confirmed === 'boolean') return data as DownloadRequest;
  return null;
}

export type DrawioInspectionRequest = { event: 'schema-inspect'; requestId: string; xml: string; target: string; consent: true };
export function readInspectionRequest(event: Pick<MessageEvent, 'origin' | 'source' | 'data'>, origin: string, popup: Window): DrawioInspectionRequest | null {
  const data = readRequest(event, origin, popup);
  return data && data.event === 'schema-inspect' && data.consent === true && typeof data.target === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(data.target) ? data as DrawioInspectionRequest : null;
}
