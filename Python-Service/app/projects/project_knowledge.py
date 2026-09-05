"""Project-knowledge grounding — framework-resolution renderer.

Turns a **canonical project profile** (dict) into the compact grounding block
that is injected into the answer prompt. This is the single abstract entry
point for "framework knowledge" so main-app modules (``answer.py`` and up)
never touch framework specifics.

Canonical profile schema (see also ``project_profile.py`` — plugins fill these
keys in their ``analyze()``; new frameworks inherit grounding for free):

    framework: str, version: str|None, language: str|None
    routes:      [{method, uri, action, name, middleware[], file}]
    schema:      [{table, columns: [{name, type, nullable, default,
                  unique, indexed, foreign: {table, column}|None}]}]
    models:      [{name, file, table, relations: [{method, type, target}]}]
    controllers: [{name, file, methods[]}]
    config:      {stem: {key: value|{...}}}         (values env:KEY-safe)
    wiring:      {providers[], provider_count, registration_site,
                  bindings[], middleware{aliases, groups, global}}
    behaviors:   {jobs[], events[], listeners[], notifications[],
                  schedule[], dispatch_sites{}, event_sites{}}
    frontend:    {views[], components[], route_view_links[],
                  controller_view_links[]}
    errors:      {custom_exceptions[], throw_hotspots[], catch_hotspots[]}
    packages:    {payment_gateways[], payment_absent[],
                  notable_dependencies[]}

A renderer must not assume how a value was produced (migrations vs. models vs.
ORM metadata) — only these shapes. Anything framework-specific (e.g. dropping
Eloquent timestamp columns) is normalized away by the producing plugin.
"""

from __future__ import annotations

import re

MAX_PROJECT_ROUTES = 40
MAX_PROJECT_TABLES = 16
MAX_PROJECT_COLS_PER_TABLE = 14
MAX_PROJECT_MODELS = 12
MAX_PROJECT_CONTROLLERS = 12
MAX_PROJECT_CONFIG_LINES = 30
MAX_PROJECT_ITEMS = 12
MAX_PROJECT_CHARS = 3500

_CANONICAL_KEYS = (
    "framework", "version", "language", "routes", "schema", "models",
    "controllers", "config", "wiring", "behaviors", "frontend", "errors",
    "packages",
)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def _join_bits(items: list, limit: int = MAX_PROJECT_ITEMS) -> str:
    """Comma-join with an overflow marker."""
    if not items:
        return ""
    if len(items) > limit:
        items = items[:limit] + ["…"]
    return ", ".join(str(i) for i in items)


# --- per-section builders (each returns list[str], [] when absent) ---------


def _render_schema_lines(schema: list) -> list[str]:
    """Canonical schema slice — already merged/normalized by the plugin.

    Each record: {table, columns: [{name, type, nullable, default, unique,
    indexed, foreign{table, column}|None}]}.
    """
    if not schema:
        return []
    lines = [f"Schema tables ({len(schema)}):"]
    for rec in schema[:MAX_PROJECT_TABLES]:
        cols = rec.get("columns") or []
        shown = []
        for c in cols[:MAX_PROJECT_COLS_PER_TABLE]:
            nm = c.get("name") or "?"
            ty = c.get("type") or ""
            col = f"{nm}:{ty}" if ty else nm
            fk = c.get("foreign")
            if fk and fk.get("table"):
                col += f"->{fk['table']}"
                if fk.get("column"):
                    col += f".{fk['column']}"
            shown.append(col)
        if len(cols) > len(shown):
            shown.append(f"+{len(cols) - len(shown)} more cols")
        lines.append(f"  {rec.get('table', '?')}({', '.join(shown)})")
    if len(schema) > MAX_PROJECT_TABLES:
        lines.append(f"  … and {len(schema) - MAX_PROJECT_TABLES} more tables")
    return lines


def _render_config_lines(config: dict) -> list[str]:
    """Flatten shallow config tree to ``file.key[.sub] = value`` lines.

    Values are already secret-safe (env refs mapped to ``env:KEY`` by the
    extracting plugin). Only scalar leaves render.
    """
    if not config:
        return []
    out: list[str] = []
    for stem, tree in sorted((config or {}).items()):
        if not isinstance(tree, dict):
            out.append(f"{stem} = {tree}")
            continue
        for k, v in tree.items():
            if isinstance(v, dict):
                for sub, val in v.items():
                    if not isinstance(val, dict):
                        out.append(f"{stem}.{k}.{sub} = {val}")
            else:
                out.append(f"{stem}.{k} = {v}")
        if len(out) >= MAX_PROJECT_CONFIG_LINES:
            out.append("… (more config keys)")
            break
    return out[: MAX_PROJECT_CONFIG_LINES + 1]


def _class_leaf(target: str) -> str:
    """Strip leading namespace for a readable class name."""
    if isinstance(target, str) and "\\" in target:
        return target.rsplit("\\", 1)[-1]
    return str(target)


def _route_lines(routes: list) -> list[str]:
    if not routes:
        return []
    lines = [f"Routes ({len(routes)} total):"]
    for r in routes[:MAX_PROJECT_ROUTES]:
        name = f" [name: {r['name']}]" if r.get("name") else ""
        mw = f" [middleware: {', '.join(r['middleware'])}]" if r.get("middleware") else ""
        lines.append(
            f"  {r.get('method', '?')} {r.get('uri', '/')} -> {r.get('action', '?')}{name}{mw}"
        )
    if len(routes) > MAX_PROJECT_ROUTES:
        lines.append(f"  … and {len(routes) - MAX_PROJECT_ROUTES} more")
    return lines


def _model_lines(models: list) -> list[str]:
    if not models:
        return []
    lines = [f"Models ({len(models)}):"]
    for m in models[:MAX_PROJECT_MODELS]:
        table = f", table={m.get('table')}" if m.get("table") else ""
        rels = m.get("relations") or []
        rel_txt = ""
        if rels:
            rel_bits = [
                f"{r.get('type', '?')}->{_class_leaf(r.get('target', '?'))}"
                for r in rels[:6]
            ]
            rel_txt = f" [{' '.join(rel_bits)}]"
        name = m.get("name") or (m.get("file") or "?").replace(".php", "")
        lines.append(f"  {name}{table}{rel_txt}")
    if len(models) > MAX_PROJECT_MODELS:
        lines.append(f"  … and {len(models) - MAX_PROJECT_MODELS} more")
    return lines


def _controller_lines(controllers: list) -> list[str]:
    if not controllers:
        return []
    ctrl_bits = []
    for c in controllers[:MAX_PROJECT_CONTROLLERS]:
        methods = c.get("methods") or []
        label = c.get("name", "?")
        if methods:
            label += f"({', '.join(methods[:8])})"
        ctrl_bits.append(label)
    return ["Controllers: " + ", ".join(ctrl_bits)]


def _wiring_lines(wiring: dict) -> list[str]:
    if not wiring:
        return []
    lines = []
    providers = wiring.get("providers") or []
    if providers:
        bits = []
        for p in providers[: MAX_PROJECT_ITEMS]:
            flag = ""
            if p.get("register_has_code") or p.get("boot_has_code"):
                flag = "*"
            bits.append(p.get("name", "?") + flag)
        site = wiring.get("registration_site")
        lines.append(
            f"Providers ({len(providers)}): {_join_bits(bits)}"
            + (f" [registered via {site}]" if site else "")
        )
    bindings = wiring.get("bindings") or []
    if bindings:
        for b in bindings[:6]:
            lines.append(
                f"  {b.get('type', 'bind')}: {b.get('abstract', '?')} -> "
                f"{b.get('concrete', '?')} ({b.get('file', '')})"
            )
        if len(bindings) > 6:
            lines.append(f"  … and {len(bindings) - 6} more bindings")
    mw = wiring.get("middleware") or {}
    if mw.get("aliases"):
        lines.append("Middleware aliases: " + _join_bits(mw["aliases"], 10))
    groups = mw.get("groups") or {}
    if groups:
        for gname, entries in list(groups.items())[:4]:
            if isinstance(entries, dict):
                entries = [f"{k}:{v}" for k, v in entries.items()]
            lines.append(f"  group {gname}: {_join_bits(entries, 8)}")
    if mw.get("global"):
        lines.append("Global middleware: " + _join_bits(mw["global"], 8))
    return lines


def _behavior_lines(behaviors: dict) -> list[str]:
    if not behaviors:
        return []
    lines = []
    jobs = behaviors.get("jobs") or []
    if jobs:
        lines.append(
            "Jobs: "
            + _join_bits(
                [f"{j.get('name', '?')}{'*' if j.get('queueable') else ''}" for j in jobs]
            )
        )
    events = behaviors.get("events") or []
    if events:
        lines.append("Events: " + _join_bits([e.get("name", "?") for e in events]))
    listeners = behaviors.get("listeners") or []
    if listeners:
        bits = []
        for lsn in listeners[: MAX_PROJECT_ITEMS]:
            h = lsn.get("handles")
            bits.append(f"{lsn.get('name', '?')}" + (f"←{h}" if h else ""))
        lines.append("Listeners: " + _join_bits(bits))
    notifications = behaviors.get("notifications") or []
    if notifications:
        lines.append(
            "Notifications: "
            + _join_bits([n.get("name", "?") for n in notifications])
        )
    schedule = behaviors.get("schedule") or []
    if schedule:
        lines.append(
            "Scheduled: "
            + _join_bits(
                [f"{s.get('task', '?')} ({s.get('frequency', '')})" for s in schedule]
            )
        )
    dispatch = behaviors.get("dispatch_sites") or {}
    if dispatch:
        lines.append(
            "Dispatch sites: "
            + _join_bits([f"{k}×{v}" for k, v in sorted(dispatch.items())], 8)
        )
    return lines


def _frontend_lines(frontend: dict) -> list[str]:
    if not frontend:
        return []
    lines = []
    views = frontend.get("views") or []
    if views:
        lines.append(
            f"Views ({len(views)}): "
            + _join_bits([v.get("name", "?") for v in views], 20)
        )
    components = frontend.get("components") or []
    if components:
        lines.append(
            "Components: "
            + _join_bits([f"{c.get('name', '?')}:{c.get('kind', '')}" for c in components], 10)
        )
    links = frontend.get("route_view_links") or []
    if links:
        lines.append(
            "Routes rendering views: "
            + _join_bits([f"{l.get('uri', '?')}→{l.get('view', '?')}" for l in links], 8)
        )
    return lines


def _error_lines(errors: dict) -> list[str]:
    if not errors:
        return []
    lines = []
    custom = errors.get("custom_exceptions") or []
    if custom:
        bits = []
        for e in custom[: MAX_PROJECT_ITEMS]:
            ex = f" ({e.get('extends', '?')})" if e.get("extends") else ""
            bits.append(e.get("name", "?") + ex)
        lines.append(f"Custom exceptions ({len(custom)}): {_join_bits(bits)}")
    hotspots = errors.get("throw_hotspots") or []
    if hotspots:
        lines.append(
            "Throw hotspots: "
            + _join_bits([f"{h.get('file', '?')}×{h.get('throws', 0)}" for h in hotspots], 6)
        )
    return lines


def _packages_lines(packages: dict) -> list[str]:
    if not packages:
        return []
    lines = []
    gateways = packages.get("payment_gateways") or []
    if gateways:
        lines.append(
            "Payment SDK(s): "
            + _join_bits(
                [
                    f"{g.get('gateway', '?')} "
                    f"({g.get('version') or g.get('required') or '?'})"
                    for g in gateways
                ]
            )
        )
    else:
        absent = packages.get("payment_absent") or []
        lines.append(
            "Payment SDK(s): none installed"
            + (f" (searched: {', '.join(absent)})" if absent else "")
        )
    notable = packages.get("notable_dependencies") or []
    if notable:
        lines.append(
            "Notable deps: "
            + _join_bits(
                [f"{d.get('package', '?')} {d.get('required', '')}".strip() for d in notable],
                12,
            )
        )
    return lines


# --- slice selection -------------------------------------------------------

_SLICE_PATTERNS: list[tuple[str, str]] = [
    # tag → regex (lowercased query). These are question-intent words shared by
    # all frameworks; section membership below is framework-neutral too.
    (
        "api",
        (
            r"api|endpoint|route|router|web|pay|payment|checkout|stripe|mollie|"
            r"paypal|razorpay|paystack|braintree|square|adyen|gateway|billing|"
            r"purchase|subscription|invoice|refund|order"
        ),
    ),
    (
        "data",
        (
            r"table|schema|migration|model|eloquent|database|\bdb\b|column|"
            r"stored|storage|foreign key|relation|field"
        ),
    ),
    (
        "config",
        (
            r"config|setting|configured|mail|queue|provider|bind|binding|"
            r"container|wiring|wired|service provider|cache|session"
        ),
    ),
    (
        "behavior",
        (
            r"\bjob\b|event|listener|notification|schedule|scheduled|background|"
            r"queueable|cron|dispatched"
        ),
    ),
    (
        "frontend",
        (
            r"view|blade|page|frontend|component|template|homepage|\bui\b|"
            r"login page"
        ),
    ),
    ("errors", r"exception|throw|error|handler|fail|fails|catch|crash|500"),
]

_DEFAULT_SLICE = ["routes", "schema", "models", "controllers", "config"]

_SECTION_ORDER = [
    "routes",
    "schema",
    "models",
    "controllers",
    "config",
    "wiring",
    "packages",
    "behaviors",
    "frontend",
    "errors",
]

_SECTION_RENDERERS = {
    "routes": lambda p: _route_lines(p.get("routes") or []),
    "schema": lambda p: _render_schema_lines(p.get("schema") or []),
    "models": lambda p: _model_lines(p.get("models") or []),
    "controllers": lambda p: _controller_lines(p.get("controllers") or []),
    "config": lambda p: _render_config_lines(p.get("config") or {}),
    "wiring": lambda p: _wiring_lines(p.get("wiring") or {}),
    "packages": lambda p: _packages_lines(p.get("packages") or {}),
    "behaviors": lambda p: _behavior_lines(p.get("behaviors") or {}),
    "frontend": lambda p: _frontend_lines(p.get("frontend") or {}),
    "errors": lambda p: _error_lines(p.get("errors") or {}),
}

_SLICE_MEMBERS = {
    "api": ["routes", "controllers", "packages"],
    "data": ["schema", "models"],
    "config": ["config", "wiring"],
    "behavior": ["behaviors", "wiring"],
    "frontend": ["frontend", "routes"],
    "errors": ["errors"],
}


def _select_sections(profile: dict, query: str | None) -> list[str]:
    """Pick profile sections by question intent; default when no hints."""
    q = (query or "").lower()
    if not q:
        return list(_DEFAULT_SLICE)
    chosen: set[str] = set()
    for tag, pattern in _SLICE_PATTERNS:
        if re.search(pattern, q):
            chosen.update(_SLICE_MEMBERS[tag])
    if not chosen:
        return list(_DEFAULT_SLICE)
    return [s for s in _SECTION_ORDER if s in chosen]


def _short_summary(profile: dict) -> str:
    """Compact project one-liner derived from canonical data counts."""
    parts: list[str] = []
    routes = profile.get("routes") or []
    if routes:
        parts.append(f"{len(routes)} route(s)")
    tables = profile.get("schema") or []
    if tables:
        parts.append(f"{len(tables)} table(s)")
    models = profile.get("models") or []
    if models:
        parts.append(f"{len(models)} model(s)")
    wiring = profile.get("wiring") or {}
    if (wiring.get("provider_count") or 0) > 0:
        parts.append(f"{wiring['provider_count']} provider(s)")
    bindings = wiring.get("bindings") or []
    if bindings:
        parts.append(f"{len(bindings)} binding(s)")
    gateways = (profile.get("packages") or {}).get("payment_gateways") or []
    if gateways:
        parts.append(
            "payment: " + ", ".join(g.get("gateway", "?") for g in gateways)
        )
    else:
        absent = (profile.get("packages") or {}).get("payment_absent") or []
        if absent:
            parts.append("no payment SDK")
    return ", ".join(parts) or "n/a"


def profile_summary(profile: dict) -> str:
    """Public accessor for the compact project one-liner (counts)."""
    return _short_summary(profile)


def build_grounding(
    profile: dict | None, query: str | None = None
) -> str | None:
    """Render the canonical project profile into a grounding block for the
    prompt, selecting the slice most relevant to ``query`` (compact core when
    no hints). None when no profile. Never raises on unknown/extra profile
    keys — only ``_CANONICAL_KEYS`` are read.
    """
    if not profile or not isinstance(profile, dict):
        return None
    framework = (profile.get("framework") or "unknown").title()
    version = profile.get("version")
    lines = [
        f"Framework: {framework}" + (f" {version}" if version else ""),
        f"Project: {_short_summary(profile)}",
    ]
    for section in _select_sections(profile, query):
        lines.extend(_SECTION_RENDERERS[section](profile))
    text = "\n".join(lines)
    return _cap(text, MAX_PROJECT_CHARS)
