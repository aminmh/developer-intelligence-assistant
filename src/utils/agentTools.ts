import type { ToolCallView } from '../types';

/** Merge `tool_completed` events or a finished answer's `tool_calls` into the calls shown so far. */
export function mergeToolCalls(current: ToolCallView[] | undefined, finished: unknown): ToolCallView[] {
  const byId = new Map((current ?? []).map((call) => [call.id, { ...call }]));
  for (const raw of Array.isArray(finished) ? finished : []) {
    if (!raw || typeof raw !== 'object') continue;
    const item = raw as Record<string, any>;
    const id = String(item.id ?? `${item.name}-${byId.size}`);
    const known = byId.get(id);
    byId.set(id, {
      id,
      name: String(item.name ?? known?.name ?? 'tool'),
      arguments: known?.arguments,
      status: item.status === 'success' ? 'success' : 'error',
      durationMs: typeof item.duration_ms === 'number' ? item.duration_ms : known?.durationMs ?? null,
      error: item.error ?? null,
      truncated: Boolean(item.truncated),
    });
  }
  return [...byId.values()];
}

const ERROR_LABELS: Record<string, string> = {
  no_source: 'no project source',
  not_found: 'file not found',
  invalid_path: 'path outside the project',
  excluded_path: 'excluded path',
  invalid_arguments: 'invalid arguments',
  tool_call_limit: 'tool call limit reached',
  timeout: 'timed out',
  binary_file: 'binary file',
  too_large: 'file too large',
  unavailable: 'tool not installed',
  tool_failed: 'failed',
};

/** Plain-words label for a tool error code. */
export function describeToolError(code: string | null | undefined): string {
  return code ? ERROR_LABELS[code] ?? code.replace(/_/g, ' ') : '';
}

const TOOL_LABELS: Record<string, string> = {
  code_text_search: 'Searched',
  code_structural_search: 'Matched pattern',
  code_read: 'Read',
  code_outline: 'Outlined',
  code_symbol_lookup: 'Looked up',
  code_stats: 'Counted code',
  project_structure: 'Listed structure',
};

/** Short human description of one tool call, e.g. `Read routes/auth.php:1-100`. */
export function describeToolCall(call: Pick<ToolCallView, 'name' | 'arguments'>): string {
  const args = (call.arguments ?? {}) as Record<string, unknown>;
  const label = TOOL_LABELS[call.name] ?? call.name;
  const text = (value: unknown) => (typeof value === 'string' ? value : value === undefined ? '' : String(value));
  switch (call.name) {
    case 'code_text_search':
    case 'code_structural_search':
      return `${label} “${text(args.pattern)}”${args.path_glob ? ` in ${text(args.path_glob)}` : ''}`;
    case 'code_read': {
      const range = args.start_line ? `:${text(args.start_line)}${args.end_line ? `-${text(args.end_line)}` : ''}` : '';
      return `${label} ${text(args.path)}${range}`;
    }
    case 'code_outline':
      return `${label} ${text(args.path)}`;
    case 'code_symbol_lookup':
      return `${label} ${text(args.relation || 'definition')} of ${text(args.symbol)}`;
    case 'project_structure':
      return args.depth ? `${label} (depth ${text(args.depth)})` : label;
    default:
      return label;
  }
}

// A citation is a code span holding `path/to/file.ext:line` or `:start-end`.
// Paths are limited to safe characters, so they can sit in an attribute as-is.
const CITATION = /<code>((?:[\w@.-]+\/)*[\w@.-]+\.[A-Za-z0-9]{1,10}):(\d{1,6})(?:-(\d{1,6}))?<\/code>/g;

/** Turn `<code>path:line</code>` spans of rendered answer HTML into clickable citations. */
export function linkCitations(html: string): string {
  return html.replace(CITATION, (_match, path: string, line: string, end?: string) => {
    const label = `${path}:${line}${end ? `-${end}` : ''}`;
    return `<a href="#" class="code-citation" data-cite-path="${path}" data-cite-line="${line}" title="Show ${label}"><code>${label}</code></a>`;
  });
}

/** User-facing workflows a tool may be allowed in (router intents), with labels. */
export const TOOL_WORKFLOWS: Array<{ id: string; label: string }> = [
  { id: 'chat', label: 'Chat' },
  { id: 'search', label: 'Search' },
  { id: 'code_explain', label: 'Explain code' },
  { id: 'debug', label: 'Debug' },
  { id: 'code_refactor', label: 'Refactor' },
  { id: 'knowledge', label: 'Knowledge' },
  { id: 'structure_analyze', label: 'Structure analysis' },
];

/** Starting argument schema for a REST tool: one required string per `{{placeholder}}` in the URL. */
export function schemaFromUrlTemplate(url: string): Record<string, unknown> {
  const names = [...new Set([...String(url).matchAll(/\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g)].map((m) => m[1]))];
  return {
    type: 'object',
    properties: Object.fromEntries(names.map((name) => [name, { type: 'string', description: `Value for {{${name}}}.` }])),
    required: names,
    additionalProperties: false,
  };
}

/** Parse the schema editor text: empty means "no schema"; otherwise it must be a JSON object schema. */
export function parseSchemaText(text: string): { schema: Record<string, unknown> | null; error: string | null } {
  if (!text.trim()) return { schema: null, error: null };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (err: any) {
    return { schema: null, error: `Argument schema is not valid JSON: ${err?.message ?? 'parse error'}` };
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { schema: null, error: 'Argument schema must be a JSON object.' };
  }
  if ((value as Record<string, unknown>).type !== 'object') {
    return { schema: null, error: 'Argument schema must have "type": "object".' };
  }
  return { schema: value as Record<string, unknown>, error: null };
}
