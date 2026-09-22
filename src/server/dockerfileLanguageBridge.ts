import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { StreamMessageReader, StreamMessageWriter, createMessageConnection, MessageConnection } from 'vscode-jsonrpc/node';

export type Position = { line: number; character: number };
export type Range = { start: Position; end: Position };
export type DockerDiagnostic = { severity?: number; message: string; range: Range; source?: string; code?: string | number };
export type TextEdit = { range: Range; newText: string };

const MAX_DOCUMENT_LENGTH = 200_000;
const MAX_SESSIONS = 32;
const REQUEST_TIMEOUT_MS = 8_000;
const SESSION_ID = /^[A-Za-z0-9_-]{8,100}$/;

type Session = { id: string; uri: string; version: number; text: string; process: ChildProcessWithoutNullStreams; connection: MessageConnection; diagnostics: DockerDiagnostic[] };

const sessions = new Map<string, Session>();
const require = createRequire(import.meta.url);
const languageServerBin = require.resolve('dockerfile-language-server-nodejs/bin/docker-langserver');

function validText(value: unknown): value is string {
  return typeof value === 'string' && value.length <= MAX_DOCUMENT_LENGTH && !value.includes('\0');
}

function validPosition(value: any): value is Position {
  return Number.isInteger(value?.line) && value.line >= 0 && value.line < 1_000_000 && Number.isInteger(value?.character) && value.character >= 0 && value.character < 1_000_000;
}

async function request(session: Session, method: string, params: any): Promise<any> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Dockerfile language server timed out.')), REQUEST_TIMEOUT_MS);
    session.connection.sendRequest(method, params).then((result) => { clearTimeout(timer); resolve(result); }, (error) => { clearTimeout(timer); reject(error); });
  });
}

async function startSession(id: string, uri: string, text: string, version: number): Promise<Session> {
  if (sessions.size >= MAX_SESSIONS) throw new Error('Too many Dockerfile editor sessions.');
  const child = spawn(process.execPath, [languageServerBin, '--stdio'], { stdio: 'pipe' });
  const connection = createMessageConnection(new StreamMessageReader(child.stdout), new StreamMessageWriter(child.stdin));
  const session: Session = { id, uri, version, text, process: child, connection, diagnostics: [] };
  child.on('error', () => { if (sessions.get(id) === session) sessions.delete(id); });
  child.on('exit', () => { if (sessions.get(id) === session) sessions.delete(id); });
  connection.onNotification('textDocument/publishDiagnostics', (params: any) => {
    if (params?.uri === session.uri) session.diagnostics = Array.isArray(params.diagnostics) ? params.diagnostics.slice(0, 500) : [];
  });
  connection.onRequest('workspace/configuration', () => [null]);
  connection.listen();
  try {
    await request(session, 'initialize', { processId: process.pid, rootUri: null, capabilities: { textDocument: { completion: { completionItem: { snippetSupport: false } }, formatting: {} } }, workspaceFolders: null });
    connection.sendNotification('initialized', {});
    connection.sendNotification('textDocument/didOpen', { textDocument: { uri, languageId: 'dockerfile', version, text } });
    sessions.set(id, session);
    await new Promise((resolve) => setTimeout(resolve, 30));
    return session;
  } catch (error) {
    connection.dispose(); child.kill();
    throw error;
  }
}

export async function openDocument(input: { sessionId?: string; uri?: string; version?: number; text: string }) {
  if (!validText(input.text)) throw new Error('Dockerfile text is invalid or too large.');
  const id = input.sessionId || randomUUID().replaceAll('-', '_');
  if (!SESSION_ID.test(id)) throw new Error('Invalid editor session identifier.');
  const uri = typeof input.uri === 'string' && /^dockerfile:\/\/[A-Za-z0-9._/-]{1,160}$/.test(input.uri) ? input.uri : `dockerfile://${id}`;
  const version = Number.isInteger(input.version) && (input.version as number) > 0 ? input.version as number : 1;
  let session = sessions.get(id);
  if (!session) session = await startSession(id, uri, input.text, version);
  else {
    session.uri = uri; session.version = version; session.text = input.text;
    session.connection.sendNotification('textDocument/didChange', { textDocument: { uri, version }, contentChanges: [{ text: input.text }] });
    await new Promise((resolve) => setTimeout(resolve, 30));
  }
  return { sessionId: id, diagnostics: session.diagnostics };
}

export async function complete(input: { sessionId: string; position: Position }) {
  const session = sessions.get(input.sessionId);
  if (!session || !validPosition(input.position)) throw new Error('Unknown editor session or invalid position.');
  const result = await request(session, 'textDocument/completion', { textDocument: { uri: session.uri }, position: input.position });
  return Array.isArray(result) ? result : (result?.items || []);
}

export async function format(input: { sessionId: string }) {
  const session = sessions.get(input.sessionId);
  if (!session) throw new Error('Unknown editor session.');
  const result = await request(session, 'textDocument/formatting', { textDocument: { uri: session.uri }, options: { tabSize: 2, insertSpaces: true } });
  return Array.isArray(result) ? result : [];
}

export function closeSession(id: string): boolean {
  if (!SESSION_ID.test(id)) return false;
  const session = sessions.get(id); if (!session) return false;
  sessions.delete(id); session.connection.dispose(); session.process.kill(); return true;
}

export function closeAllSessions() { for (const id of [...sessions.keys()]) closeSession(id); }
export function bridgeStatus() { return { available: true, sessions: sessions.size }; }
