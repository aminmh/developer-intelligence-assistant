"""Knowledge-context builder — turns a saved analysis (canonical project
profile + optional sandbox runtime probes) into the durable "full context for
the LLM":

  1. ``knowledge_chunks`` — embeddable text chunks (one per route/table/model/
     controller/config leaf/provider/binding/job/event/listener/view/exception/
     package + one per runtime probe). Each chunk is a dict in the SAME shape
     ``CodePropertyGraph.ingest`` already consumes, so persisting reuses the
     existing vector (Qdrant) + graph (Neo4j) + typo-dictionary pipeline.
  2. ``grounding_md`` — the human-readable grounding block (full default
     profile slice + runtime section) stored as an artifact.

Chunk identity is DETERMINISTIC per project + kind + name: node ids derive
from ``node_id_for`` via the class_fqn/name convention below, so re-saving the
same project MERGEs the same graph nodes / vector points instead of duplicating.

Only canonical profile keys are read (see project_knowledge.py); secrets never
enter (config values are already env:KEY-safe by the extracting plugin).
"""

from __future__ import annotations

from typing import Any

from projects.project_knowledge import build_grounding, profile_summary

_KNOWN_KINDS = (
    "route", "table", "model", "controller", "config", "provider", "binding",
    "job", "event", "listener", "notification", "schedule", "view", "component",
    "exception", "package", "middleware", "routeview", "dep", "runtime",
)
_RUNTIME_KIND = "runtime_probe"


def _kn_id(project: str, kind: str, name: str) -> tuple[str, str]:
    """Return (class_fqn, name) pair that yields a stable node id.

    Mirrors indexer.node_id_for(fqn, name): id = sha1(f"{fqn}::{name}").
    project is a sanitized short label (filename stem, e.g. "Archive").
    """
    return f"kn:{kind}:{project}", (name or "unnamed")[:120]


def _chunk(
    project: str,
    kind: str,
    name: str,
    text: str,
    *,
    file: str | None = None,
) -> dict[str, Any]:
    fqn, node_name = _kn_id(project, kind, name)
    chunk: dict[str, Any] = {
        "class_fqn": fqn,
        "class": fqn,
        "name": node_name,
        "method_name": None,
        "file": file or "",
        "type": kind,
        "code": text[:3000],
        "has_docblock": False,
        "calls": [],
    }
    return chunk


# --- per-section renderers (each → [(name, text, file)]) --------------------


def _routes_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    for r in profile.get("routes") or []:
        name = r.get("name") or f"{r.get('method', '?')} {r.get('uri', '/')}"
        lines = [
            f"HTTP {r.get('method', '?')} {r.get('uri', '/')}",
            f"action: {r.get('action', '?')}",
            f"name: {name}",
        ]
        if r.get("middleware"):
            lines.append("middleware: " + ", ".join(r["middleware"]))
        lines.append(f"source: {r.get('source', 'static')}")
        out.append((f"route:{name}", "\n".join(lines), r.get("file") or "routes/*"))
    return out


def _schema_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    schema = profile.get("schema") or []
    for rec in schema[:60]:
        table = rec.get("table", "?")
        lines = [f"TABLE {table}"]
        for c in (rec.get("columns") or [])[:40]:
            nm = c.get("name", "?")
            ty = c.get("type", "")
            col = f"  {nm}: {ty}" if ty else f"  {nm}"
            fk = c.get("foreign")
            if fk and fk.get("table"):
                col += f" -> {fk['table']}" + (
                    f".{fk['column']}" if fk.get("column") else ""
                )
            if c.get("unique"):
                col += " [unique]"
            if c.get("nullable"):
                col += " [nullable]"
            lines.append(col)
        out.append((f"table:{table}", "\n".join(lines), "database/migrations"))
    return out


def _model_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    for m in profile.get("models") or []:
        name = m.get("name") or (m.get("file") or "?")
        lines = [f"MODEL {name}"]
        if m.get("table"):
            lines.append(f"  table: {m['table']}")
        for rel in (m.get("relations") or [])[:12]:
            lines.append(
                f"  {rel.get('method', '?')} -> {rel.get('type', '?')} "
                f"{rel.get('target', '?')}"
            )
        out.append((f"model:{name}", "\n".join(lines), m.get("file")))
    return out


def _controller_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    for c in profile.get("controllers") or []:
        name = c.get("name", "?")
        methods = ", ".join((c.get("methods") or [])[:30])
        out.append(
            (
                f"controller:{name}",
                f"CONTROLLER {name}\n  methods: {methods}",
                c.get("file"),
            )
        )
    return out


def _config_items(profile: dict) -> list[tuple[str, str, str | None]]:
    """Config leaves as ``config('file.key') = value`` items.

    Defense-in-depth: values under secret-class keys are always rendered
    ``[redacted]`` here — independent of the extracting plugin's env:KEY
    handling — so inline literal secrets in config files can never reach the
    persisted knowledge store.
    """
    out = []
    config = profile.get("config") or {}
    secret_hint = (
        "secret", "password", "token", "api_key", "apikey", "private",
        "credential", "client_secret", "access_key",
    )

    def _redact(key: str, value: Any) -> Any:
        low = key.lower()
        return "[redacted]" if any(h in low for h in secret_hint) else value

    for stem, tree in sorted(config.items()):
        if not isinstance(tree, dict):
            out.append((f"config:{stem}", f"{stem} = {tree}", f"config/{stem}.php"))
            continue
        for k, v in tree.items():
            if isinstance(v, dict):
                for sub, val in v.items():
                    if not isinstance(val, dict):
                        val = _redact(sub, val)
                        out.append(
                            (f"config:{stem}.{k}.{sub}",
                             f"config('{stem}.{k}.{sub}') = {val}",
                             f"config/{stem}.php")
                        )
            else:
                v = _redact(k, v)
                out.append(
                    (f"config:{stem}.{k}", f"config('{stem}.{k}') = {v}",
                     f"config/{stem}.php")
                )
    return out


def _wiring_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    wiring = profile.get("wiring") or {}
    for p in wiring.get("providers") or []:
        name = p.get("name", "?")
        flags = []
        if p.get("register_has_code") or p.get("boot_has_code"):
            flags.append("has register/boot code")
        site = p.get("registered_via") or wiring.get("registration_site")
        if site:
            flags.append(f"registered via {site}")
        out.append(
            (f"provider:{name}",
             f"SERVICE PROVIDER {name}\n  " + "; ".join(flags) if flags else
             f"SERVICE PROVIDER {name} (declared only)",
             p.get("file"))
        )
    for b in wiring.get("bindings") or []:
        detail = (
            f"BINDING {b.get('type', 'bind')}: {b.get('abstract', '?')} "
            f"-> {b.get('concrete', '?')}"
        )
        out.append(
            (f"binding:{b.get('abstract', '?')}",
             detail,
             b.get("file"))
        )
    mw = wiring.get("middleware") or {}
    for alias, entries in (mw.get("aliases") or {}).items():
        out.append(
            (f"middleware:{alias}",
             f"MIDDLEWARE ALIAS {alias} -> {entries}",
             mw.get("source"))
        )
    return out


def _behavior_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    behaviors = profile.get("behaviors") or {}
    for kind, files_attr in (
        ("job", "jobs"),
        ("event", "events"),
        ("notification", "notifications"),
    ):
        for item in behaviors.get(files_attr) or []:
            name = item.get("name", "?")
            queue = "* queueable" if item.get("queueable") else ""
            out.append(
                (f"{kind}:{name}", f"{kind.upper()} {name}{queue}", item.get("file"))
            )
    for lsn in behaviors.get("listeners") or []:
        name = lsn.get("name", "?")
        h = f" handles {lsn['handles']}" if lsn.get("handles") else ""
        out.append((f"listener:{name}", f"LISTENER {name}{h}", lsn.get("file")))
    for s in behaviors.get("schedule") or []:
        out.append(
            (f"schedule:{s.get('task', '?')}",
             f"SCHEDULED {s.get('task', '?')} ({s.get('frequency', '')})",
             "routes/console.php")
        )
    return out


def _frontend_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    frontend = profile.get("frontend") or {}
    for v in frontend.get("views") or []:
        name = v.get("name", "?")
        out.append((f"view:{name}", f"VIEW {name}", v.get("file")))
    for c in frontend.get("components") or []:
        name = c.get("name", "?")
        out.append(
            (f"component:{name}",
             f"COMPONENT {name} ({c.get('kind', '')})", c.get("file"))
        )
    for l in frontend.get("route_view_links") or []:
        detail = (
            f"ROUTE {l.get('method', 'GET')} {l.get('uri', '?')} renders view "
            f"{l.get('view', '?')}"
        )
        out.append(
            (f"routeview:{l.get('uri', '?')}", detail, None)
        )
    return out


def _error_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    errors = profile.get("errors") or {}
    for e in errors.get("custom_exceptions") or []:
        name = e.get("name", "?")
        ext = f" extends {e.get('extends', '?')}" if e.get("extends") else ""
        out.append((f"exception:{name}", f"CUSTOM EXCEPTION {name}{ext}", e.get("file")))
    return out


def _package_items(profile: dict) -> list[tuple[str, str, str | None]]:
    out = []
    packages = profile.get("packages") or {}
    gateways = packages.get("payment_gateways") or []
    if gateways:
        for g in gateways:
            ver = g.get("version") or g.get("required") or "?"
            detail = (
                f"PAYMENT SDK {g.get('gateway', '?')} {ver} "
                f"(package {g.get('package', '?')})"
            )
            out.append(
                (f"package:{g.get('gateway', '?')}", detail, "composer.lock")
            )
    elif packages.get("payment_absent"):
        out.append(
            ("package:none",
             "NO PAYMENT SDK INSTALLED (searched: "
             + ", ".join(packages["payment_absent"]) + ")",
             "composer.json")
        )
    for d in packages.get("notable_dependencies") or []:
        out.append(
            (f"dep:{d.get('package', '?')}",
             f"DEPENDENCY {d.get('package', '?')} {d.get('required', '')}".strip(),
             "composer.json")
        )
    return out


_SECTION_BUILDERS = (
    _routes_items,
    _schema_items,
    _model_items,
    _controller_items,
    _config_items,
    _wiring_items,
    _behavior_items,
    _frontend_items,
    _error_items,
    _package_items,
)


def _runtime_items(runtime: dict[str, Any] | None) -> list[tuple[str, str, str | None]]:
    """One chunk per sandbox probe (runtime truth: artisan route:list etc.)."""
    out = []
    if not runtime:
        return out
    for probe in runtime.get("probes") or []:
        name = probe.get("name", "probe")
        status = probe.get("status", "unknown")
        lines = [f"RUNTIME PROBE {name} [{status}]",
                 f"command: {' '.join(probe.get('command') or [])}",
                 f"image: {probe.get('image') or '?'}",
                 f"exit_code: {probe.get('exit_code')}"]
        body = probe.get("stdout") or ""
        if body:
            lines.append("output:")
            lines.append(body[:1800])
        if probe.get("stderr"):
            lines.append("stderr: " + str(probe["stderr"])[:500])
        out.append((f"runtime:{name}", "\n".join(lines), None))
    return out


def build_knowledge_chunks(
    profile: dict[str, Any],
    project: str,
    runtime: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Render the profile (+ runtime probes) into ingest-ready chunk dicts.

    ``project`` is a short stable label (sanitized filename stem). Order is
    deterministic so re-runs produce identical chunk lists.
    """
    items: list[tuple[str, str, str | None]] = []
    for builder in _SECTION_BUILDERS:
        items.extend(builder(profile))
    items.extend(_runtime_items(runtime))
    chunks = [
        _chunk(project, kind_of(name), name, text, file=file)
        for name, text, file in items
    ]
    return chunks


def kind_of(name: str) -> str:
    """Recover the canonical kind from a 'kind:name' item label.

    Runtime-probe items carry the ``runtime:`` label but land in the
    distinct ``runtime_probe`` namespace so probes never collide with
    scheduled tasks.
    """
    head = name.split(":", 1)[0]
    if head == "runtime":
        return _RUNTIME_KIND
    return head if head in _KNOWN_KINDS else "knowledge"


def build_grounding_md(
    profile: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    filename: str = "",
) -> str:
    """Assemble the durable human-readable grounding block artifact."""
    base = build_grounding(profile) or "Project summary: "
    sections = [base]
    if runtime:
        rows = runtime.get("probes") or []
        if rows:
            lines = [f"Runtime probes ({len(rows)}):"]
            for p in rows:
                lines.append(
                    f"  {p.get('name', '?')} [{p.get('status', '?')}] "
                    f"exit={p.get('exit_code')}: {' '.join(p.get('command') or [])}"
                )
            sections.append("\n".join(lines))
    if filename:
        sections.insert(0, f"Analysis project: {filename}\n")
    return "\n\n".join(sections)


def analysis_summary(profile: dict[str, Any]) -> str:
    """Compact one-liner for the session row (header reuse)."""
    summary = profile_summary(profile)
    framework = (profile.get("framework") or "unknown").title()
    version = profile.get("version")
    return f"{framework}{(' ' + version) if version else ''} — {summary}"
