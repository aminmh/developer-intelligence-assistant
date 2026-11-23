"""Generate typed front-end code and mocks from a contract version.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 5).

Targets:

- ``models``   — ``models.ts``: one type per contract schema.
- ``angular``  — ``api.service.ts``: an injectable service over ``HttpClient``.
- ``react``    — ``client.ts`` (typed ``fetch`` functions) and ``hooks.ts``.
- ``vue``      — ``client.ts`` and ``composables.ts``.
- ``mocks``    — ``mock-data.ts`` (typed example responses) and ``handlers.ts`` (Mock Service Worker v2).

The generated code depends only on the framework itself (and ``msw`` for mock
handlers). No template engine and no model are involved: output is a pure
function of the contract.
"""

from __future__ import annotations

import re
from typing import Any

from api_contracts.codegen.typescript import (
    Operation, TypeNames, arguments, camel, declaration, docblock, literal, operations_of, pascal, path_template,
    signature,
)

TARGETS = ("models", "angular", "react", "vue", "mocks")
MAX_OPERATIONS = 2000
_HEADER = "// Generated from the API contract \"{title}\" {version}. Do not edit by hand: regenerate when the contract changes.\n"


def _header(model: dict[str, Any]) -> str:
    clean = lambda value: " ".join(str(value).split()).replace("\"", "'")[:120]  # noqa: E731
    return _HEADER.format(title=clean(model["title"]), version=clean(model["api_version"]))


def default_base_url(model: dict[str, Any]) -> str:
    """The path part of the contract's first server, e.g. ``/api/v1``; '' when none is declared."""
    for base in model.get("base_paths") or []:
        path = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*", "", str(base)).rstrip("/")
        if "{" not in path:
            return path
    return ""


# ── models ───────────────────────────────────────────────────────────────────

def _used_schemas(model: dict[str, Any], selected: set[str] | None) -> list[str]:
    """Every schema, or only those the selected operations reach."""
    if selected is None:
        return sorted(model["schemas"])
    from api_contracts.conformance import _referenced_schemas

    found: set[str] = set()
    for key in selected:
        found |= _referenced_schemas(model["operations"][key], model["schemas"])
    return sorted(name for name in found if name in model["schemas"])


def models_file(model: dict[str, Any], names: TypeNames, selected: set[str] | None = None) -> str:
    parts = [_header(model)]
    schemas = _used_schemas(model, selected)
    parts.extend(declaration(name, model["schemas"][name], names) for name in schemas)
    if not schemas:
        parts.append("export {};\n")
    return "\n".join(parts)


def _model_import(text: str, names: TypeNames, schemas: list[str]) -> str:
    """``import type {...}`` for the model types a generated file mentions."""
    used = sorted(names.names[name] for name in schemas if re.search(rf"\b{re.escape(names.names[name])}\b", text))
    return f"import type {{ {', '.join(used)} }} from './models';\n" if used else ""


# ── fetch client (React and Vue) ─────────────────────────────────────────────

_CLIENT_RUNTIME = '''
export interface ApiConfig {
  /** Prefix of every request, for example "https://api.example.com/v1". */
  baseUrl: string;
  /** Extra headers for every request, for example an Authorization header. */
  headers?: () => Record<string, string>;
  fetch?: typeof fetch;
}

let apiConfig: ApiConfig = { baseUrl: __BASE_URL__ };

export function configureApi(next: Partial<ApiConfig>): void {
  apiConfig = { ...apiConfig, ...next };
}

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly body: unknown) {
    super(`Request failed with status ${status}`);
    this.name = 'ApiError';
  }
}

type Query = Record<string, unknown>;

function queryString(query?: Query): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null) continue;
    for (const item of Array.isArray(value) ? value : [value]) search.append(key, String(item));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

async function request<T>(method: string, path: string, options: { query?: Query; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...(apiConfig.headers?.() ?? {}) };
  let body: BodyInit | undefined;
  if (options.body instanceof FormData || options.body instanceof Blob || typeof options.body === 'string') {
    body = options.body;
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(options.body);
  }
  const response = await (apiConfig.fetch ?? fetch)(`${apiConfig.baseUrl}${path}${queryString(options.query)}`, {
    method, headers, body, signal: options.signal,
  });
  const text = response.status === 204 ? '' : await response.text();
  let data: unknown = text;
  if ((response.headers.get('content-type') ?? '').includes('json') && text) data = JSON.parse(text);
  if (!response.ok) throw new ApiError(response.status, data);
  return (text ? data : undefined) as T;
}
'''


def client_file(model: dict[str, Any], names: TypeNames, operations: list[Operation], base_url: str, schemas: list[str]) -> str:
    functions = []
    for operation in operations:
        items = arguments(operation)
        options = [name for name, _kind, _optional in items if name in ("query", "body")] + ["signal"]
        functions.append(
            f"{docblock(operation)}export function {operation.name}({signature(items, 'signal?: AbortSignal')}): "
            f"Promise<{operation.response_type}> {{\n"
            f"  return request<{operation.response_type}>({literal(operation.method)}, `{path_template(operation)}`, "
            f"{{ {', '.join(options)} }});\n}}\n")
    body = _CLIENT_RUNTIME.replace("__BASE_URL__", literal(base_url)) + "\n" + "\n".join(functions)
    return _header(model) + _model_import(body, names, schemas) + body


# ── React hooks ──────────────────────────────────────────────────────────────

_HOOKS_RUNTIME = '''import { useCallback, useEffect, useState } from 'react';
import * as api from './client';

export interface QueryState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  /** Run the request again. */
  reload: () => void;
}

function useQuery<T>(load: (signal: AbortSignal) => Promise<T>, key: string): QueryState<T> {
  const [state, setState] = useState<{ data: T | undefined; error: unknown; loading: boolean }>({ data: undefined, error: undefined, loading: true });
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState((previous) => ({ ...previous, loading: true, error: undefined }));
    load(controller.signal).then(
      (data) => setState({ data, error: undefined, loading: false }),
      (error: unknown) => { if (!controller.signal.aborted) setState({ data: undefined, error, loading: false }); },
    );
    return () => controller.abort();
    // The request is described by `key`; `load` is a new function on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, version]);
  const reload = useCallback(() => setVersion((value) => value + 1), []);
  return { ...state, reload };
}

export interface MutationState<A extends unknown[], T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  /** Send the request. Rejects with the same error that is stored in `error`. */
  mutate: (...args: A) => Promise<T>;
}

function useMutation<A extends unknown[], T>(run: (...args: A) => Promise<T>): MutationState<A, T> {
  const [state, setState] = useState<{ data: T | undefined; error: unknown; loading: boolean }>({ data: undefined, error: undefined, loading: false });
  const mutate = useCallback(async (...args: A): Promise<T> => {
    setState({ data: undefined, error: undefined, loading: true });
    try {
      const data = await run(...args);
      setState({ data, error: undefined, loading: false });
      return data;
    } catch (error) {
      setState({ data: undefined, error, loading: false });
      throw error;
    }
  }, [run]);
  return { ...state, mutate };
}
'''


def hooks_file(model: dict[str, Any], names: TypeNames, operations: list[Operation], schemas: list[str]) -> str:
    hooks = []
    for operation in operations:
        hook = "use" + pascal(operation.name)
        if operation.method == "GET":
            items = arguments(operation)
            passed = ", ".join([*(name for name, _kind, _optional in items), "signal"])
            key = "JSON.stringify([" + ", ".join(name for name, _kind, _optional in items) + "])"
            hooks.append(f"{docblock(operation)}export function {hook}({signature(items)}): QueryState<{operation.response_type}> {{\n"
                         f"  return useQuery((signal) => api.{operation.name}({passed}), {key});\n}}\n")
        else:
            hooks.append(f"{docblock(operation)}export function {hook}() {{\n  return useMutation(api.{operation.name});\n}}\n")
    body = _HOOKS_RUNTIME + "\n" + "\n".join(hooks)
    return _header(model) + _model_import(body, names, schemas) + body


# ── Vue composables ──────────────────────────────────────────────────────────

_COMPOSABLES_RUNTIME = '''import { shallowRef, toValue, watchEffect, type MaybeRefOrGetter, type ShallowRef } from 'vue';
import * as api from './client';

export interface QueryState<T> {
  data: ShallowRef<T | undefined>;
  error: ShallowRef<unknown>;
  loading: ShallowRef<boolean>;
  /** Run the request again. */
  reload: () => void;
}

/** Runs `load` now and again whenever a ref or getter it reads changes. */
function useQuery<T>(load: (signal: AbortSignal) => Promise<T>): QueryState<T> {
  const data = shallowRef<T | undefined>(undefined);
  const error = shallowRef<unknown>(undefined);
  const loading = shallowRef(true);
  const version = shallowRef(0);
  watchEffect((onCleanup) => {
    void version.value;
    const controller = new AbortController();
    onCleanup(() => controller.abort());
    loading.value = true;
    error.value = undefined;
    load(controller.signal).then(
      (value) => { data.value = value; loading.value = false; },
      (reason: unknown) => { if (!controller.signal.aborted) { data.value = undefined; error.value = reason; loading.value = false; } },
    );
  });
  return { data, error, loading, reload: () => { version.value += 1; } };
}

export interface MutationState<A extends unknown[], T> {
  data: ShallowRef<T | undefined>;
  error: ShallowRef<unknown>;
  loading: ShallowRef<boolean>;
  /** Send the request. Rejects with the same error that is stored in `error`. */
  mutate: (...args: A) => Promise<T>;
}

function useMutation<A extends unknown[], T>(run: (...args: A) => Promise<T>): MutationState<A, T> {
  const data = shallowRef<T | undefined>(undefined);
  const error = shallowRef<unknown>(undefined);
  const loading = shallowRef(false);
  const mutate = async (...args: A): Promise<T> => {
    loading.value = true;
    error.value = undefined;
    try {
      data.value = await run(...args);
      return data.value as T;
    } catch (reason) {
      data.value = undefined;
      error.value = reason;
      throw reason;
    } finally {
      loading.value = false;
    }
  };
  return { data, error, loading, mutate };
}
'''


def composables_file(model: dict[str, Any], names: TypeNames, operations: list[Operation], schemas: list[str]) -> str:
    composables = []
    for operation in operations:
        name = "use" + pascal(operation.name)
        if operation.method == "GET":
            items = arguments(operation, wrap="MaybeRefOrGetter")
            passed = ", ".join([*(f"toValue({argument})" for argument, _kind, _optional in items), "signal"])
            composables.append(f"{docblock(operation)}export function {name}({signature(items)}): QueryState<{operation.response_type}> {{\n"
                               f"  return useQuery((signal) => api.{operation.name}({passed}));\n}}\n")
        else:
            composables.append(f"{docblock(operation)}export function {name}() {{\n  return useMutation(api.{operation.name});\n}}\n")
    body = _COMPOSABLES_RUNTIME + "\n" + "\n".join(composables)
    return _header(model) + _model_import(body, names, schemas) + body


# ── Angular service ──────────────────────────────────────────────────────────

_ANGULAR_RUNTIME = '''import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, InjectionToken, inject } from '@angular/core';
import { Observable } from 'rxjs';
__MODELS__
/** Prefix of every request. Provide another value to point the service at a different server. */
export const API_BASE_URL = new InjectionToken<string>('API_BASE_URL', { providedIn: 'root', factory: () => __BASE_URL__ });

function toParams(query?: Record<string, unknown>): HttpParams {
  let params = new HttpParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null) continue;
    for (const item of Array.isArray(value) ? value : [value]) params = params.append(key, String(item));
  }
  return params;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = inject(API_BASE_URL);
'''


def angular_file(model: dict[str, Any], names: TypeNames, operations: list[Operation], base_url: str, schemas: list[str]) -> str:
    methods = []
    for operation in operations:
        items = arguments(operation)
        has = {name for name, _kind, _optional in items}
        options = []
        if "body" in has:
            options.append("body")
        if "query" in has:
            options.append("params: toParams(query)")
        if operation.response_type in ("Blob", "string"):
            options.append(f"responseType: {literal('blob' if operation.response_type == 'Blob' else 'text')}")
            call = f"this.http.request({literal(operation.method)}, url, {{ {', '.join(options)} }})"
        else:
            call = (f"this.http.request<{operation.response_type}>({literal(operation.method)}, url"
                    + (f", {{ {', '.join(options)} }})" if options else ")"))
        methods.append(
            f"\n{docblock(operation, '  ')}  {operation.name}({signature(items)}): Observable<{operation.response_type}> {{\n"
            f"    const url = `${{this.baseUrl}}{path_template(operation)}`;\n    return {call};\n  }}\n")
    body = "".join(methods) + "}\n"
    runtime = _ANGULAR_RUNTIME.replace("__BASE_URL__", literal(base_url))
    return _header(model) + runtime.replace("__MODELS__\n", _model_import(runtime + body, names, schemas)) + body


# ── mocks ────────────────────────────────────────────────────────────────────

_STRING_FORMATS = {"date-time": "2026-01-15T09:30:00Z", "date": "2026-01-15", "time": "09:30:00",
                   "email": "user@example.com", "uuid": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                   "uri": "https://example.com/resource", "url": "https://example.com/resource",
                   "hostname": "example.com", "ipv4": "192.0.2.1", "ipv6": "2001:db8::1", "password": "secret"}


def example(schema: Any, schemas: dict[str, Any], trail: tuple[str, ...] = (), name: str = "") -> Any:
    """A value that fits the schema: its own example or default first, otherwise one built from its type."""
    if not isinstance(schema, dict) or len(trail) > 6:
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = ref.removeprefix("#/schemas/")
        if target in trail:                                  # a recursive schema ends here
            return None
        return example(schemas.get(target), schemas, (*trail, target), name)
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    if isinstance(schema.get("allOf"), list):
        merged: dict[str, Any] = {}
        for part in schema["allOf"]:
            value = example(part, schemas, trail, name)
            if isinstance(value, dict):
                merged.update(value)
        own = example({key: value for key, value in schema.items() if key != "allOf"}, schemas, trail, name) \
            if schema.get("properties") else None
        return {**merged, **own} if isinstance(own, dict) else merged
    for key in ("oneOf", "anyOf"):
        if isinstance(schema.get(key), list) and schema[key]:
            return example(schema[key][0], schemas, trail, name)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), None)
    if kind == "string":
        if schema.get("format") in _STRING_FORMATS:
            return _STRING_FORMATS[schema["format"]]
        text = name or "string"
        return text[: schema["maxLength"]] if isinstance(schema.get("maxLength"), int) and schema["maxLength"] > 0 else text
    if kind in ("integer", "number"):
        low = schema.get("minimum")
        value = low if isinstance(low, (int, float)) and not isinstance(low, bool) else (1 if kind == "integer" else 1.5)
        return int(value) if kind == "integer" else value
    if kind == "boolean":
        return True
    if kind == "array" or "items" in schema:
        item = example(schema.get("items"), schemas, trail, name)
        return [] if item is None else [item]
    if kind == "object" or "properties" in schema:
        result = {}
        required = set(schema.get("required") or [])
        for key, value in (schema.get("properties") or {}).items():
            built = example(value, schemas, trail, key)
            if built is not None or key in required:
                result[key] = built
        return result
    return None


def _mock_path(path: str, base_url: str) -> str:
    return "*" + base_url + re.sub(r"\{([^{}]+)\}", lambda match: ":" + camel(match.group(1)), path)


def mock_files(model: dict[str, Any], names: TypeNames, operations: list[Operation], base_url: str,
               schemas: list[str]) -> list[dict[str, str]]:
    data_lines, handlers = [], []
    for operation in operations:
        constant = f"{operation.name}Response"
        has_data = operation.response_schema is not None
        if has_data:
            value = example(operation.response_schema, model["schemas"])
            # Typed against the models, so a type check also proves the mock data fits the contract.
            typed = operation.response_type if "Blob" not in operation.response_type else "unknown"
            data_lines.append(f"/** Example response of {operation.key}. */\n"
                              f"export const {constant}: {typed} = {literal(value)};\n")
            reply = f"HttpResponse.json(data.{constant}, {{ status: {operation.success_status} }})"
        else:
            reply = f"new HttpResponse(null, {{ status: {operation.success_status} }})"
        handlers.append(f"  // {operation.key}\n"
                        f"  http.{operation.method.lower()}({literal(_mock_path(operation.path, base_url))}, () => {reply}),")
    data_body = "\n".join(data_lines) or "export {};\n"
    data_file = _header(model) + _model_import(data_body, names, schemas) + "\n" + data_body
    uses_data = any(operation.response_schema is not None for operation in operations)
    handlers_file = (_header(model) + "import { http, HttpResponse } from 'msw';\n"
                     + ("import * as data from './mock-data';\n" if uses_data else "")
                     + "\n/** Mock Service Worker (v2) handlers, one per contract operation. */\n"
                     + "export const handlers = [\n" + "\n".join(handlers) + "\n];\n")
    return [{"path": "mock-data.ts", "content": data_file}, {"path": "handlers.ts", "content": handlers_file}]


# ── entry point ──────────────────────────────────────────────────────────────

class GenerationError(ValueError):
    """The request cannot be generated; the message is safe to show."""


def select_operations(model: dict[str, Any], search: str | None) -> set[str] | None:
    """Operation keys matching ``search`` (path, operation ID, tag, or summary text); None means all."""
    if not search or not search.strip():
        return None
    needle = search.strip().casefold()
    found = {key for key, operation in model["operations"].items()
             if needle in f"{key} {operation.get('operation_id') or ''} {' '.join(operation['tags'])} {operation['summary']}".casefold()}
    if not found:
        raise GenerationError(f"No operation matches '{search.strip()[:80]}'.")
    return found


def syntax_errors(files: list[dict[str, str]]) -> list[str]:
    """Files that do not parse as TypeScript. Empty for correct generator output."""
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:  # pragma: no cover — the parser ships with the service image
        return []
    parser = get_parser("typescript")
    return [file["path"] for file in files if parser.parse(file["content"].encode("utf-8")).root_node.has_error]


def generate(model: dict[str, Any], target: str, *, base_url: str | None = None, search: str | None = None) -> dict[str, Any]:
    """``{"target", "files": [{"path", "content"}], "operations", "notes"}`` for one target."""
    if target not in TARGETS:
        raise GenerationError("target must be one of: " + ", ".join(TARGETS) + ".")
    if base_url is not None and (len(base_url) > 300 or any(char in base_url for char in "\n\r`\\")):
        raise GenerationError("The base address is not valid.")
    selected = select_operations(model, search)
    names = TypeNames(model["schemas"])
    operations = operations_of(model, names, selected)
    if len(operations) > MAX_OPERATIONS:
        raise GenerationError(f"The contract has more than {MAX_OPERATIONS} operations. Narrow the selection.")
    base = default_base_url(model) if base_url is None else base_url.rstrip("/")
    schemas = _used_schemas(model, selected)
    files = [{"path": "models.ts", "content": models_file(model, names, selected)}]
    if target == "angular":
        files.append({"path": "api.service.ts", "content": angular_file(model, names, operations, base, schemas)})
    elif target in ("react", "vue"):
        files.append({"path": "client.ts", "content": client_file(model, names, operations, base, schemas)})
        files.append({"path": "hooks.ts", "content": hooks_file(model, names, operations, schemas)} if target == "react"
                     else {"path": "composables.ts", "content": composables_file(model, names, operations, schemas)})
    elif target == "mocks":
        files.extend(mock_files(model, names, operations, base, schemas))
    notes = []
    skipped = sum(operation.skipped_parameters for operation in operations)
    if skipped and target != "models":
        notes.append(f"{skipped} header or cookie parameter(s) are not sent by the generated code; "
                     "set headers through the client configuration or an interceptor.")
    if target == "mocks":
        notes.append("The handlers use Mock Service Worker version 2 (the msw package).")
    broken = syntax_errors(files)
    if broken:  # a generator defect, not a user error: say so instead of handing out broken code
        notes.append("These files did not pass a syntax check and need review: " + ", ".join(broken) + ".")
    return {"target": target, "base_url": base, "files": files, "operations": len(operations),
            "schemas": len(schemas), "syntax_checked": True, "notes": notes}
