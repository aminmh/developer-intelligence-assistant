"""Static Laravel wiring extractor (DI / providers / middleware).

Builds the "how is the app wired" facts:
  * providers         — classes in ``app/Providers`` + registration site
                       (bootstrap/providers.php L11 style, or config/app.php
                       ``'providers'`` in older apps)
  * provider activity — whether register()/boot() bodies contain real code
  * bindings          — ``$this->app->bind/singleton/scoped(...)`` calls
                       anywhere under app/ (abstract => concrete pairs)
  * middleware        — aliases/groups/global registered in bootstrap/app.php
                       (withMiddleware) or app/Http/Kernel.php (legacy)

Source text only — no runtime. Absence is first-class: an empty provider body
is reported as such, never guessed.
"""

from __future__ import annotations

import re
from typing import Any

_CLASS_RE = re.compile(r"class\s+(\w+)\s+extends\s+ServiceProvider")# Laravel 11+ providers live in bootstrap/providers.php:
#   use App\Providers\AppServiceProvider;
#   return [ AppServiceProvider::class, ];
_BOOTSTRAP_USE_RE = re.compile(r"use\s+([\w\\]+Provider\w*);")
_BOOTSTRAP_RETURN_RE = re.compile(r"return\s*\[(.*?)\];", re.DOTALL)
_CLASS_CONST_RE = re.compile(r"([\w\\]+)::class")
# legacy config/app.php
_APP_PROVIDERS_RE = re.compile(
    r"['\"]providers['\"]\s*=>\s*\[(.*?)\]", re.DOTALL
)

_BIND_RE = re.compile(
    r"\$this->app->(bind|singleton|scoped|instance|bindIf)\s*\("
)
_CONCRETE_RE = re.compile(r"([\w\\]+)::class")

_MIDDLEWARE_ALIAS_RE = re.compile(r"->alias\s*\((.*?)\)", re.DOTALL)
_MIDDLEWARE_USE_RE = re.compile(r"->use\s*\((.*?)\)", re.DOTALL)
_MIDDLEWARE_APPEND_RE = re.compile(r"->(append|prepend)\s*\((.*?)\)", re.DOTALL)
_MIDDLEWARE_GROUP_RE = re.compile(r"->group\s*\(\s*['\"]([\w]+)['\"]\s*,\s*(.*?)\)", re.DOTALL)
_MW_KEYPAIR_RE = re.compile(r"['\"]([\w:.-]+)['\"]\s*=>\s*([\w\\]+)::class")
_MW_NAME_RE = re.compile(r"['\"]([\w:.-]+)['\"]")
_KERNEL_ALIAS_RE = re.compile(
    r"protected\s+\$middlewareAliases\s*=\s*\[(.*?)\];", re.DOTALL
)
_KERNEL_GROUP_RE = re.compile(
    r"protected\s+\$middlewareGroups\s*=\s*\[(.*?)\];", re.DOTALL
)


def _strip_comment(line: str) -> str:
    """Remove // and # comments (keeps strings naive — sufficient for wiring)."""
    for marker in ("//", "#"):
        i = line.find(marker)
        if i != -1:
            line = line[:i]
    return line


def _provider_method_code(source: str, method: str) -> bool:
    """True when register()/boot() body holds statements beyond comments."""
    idx = source.find(f"function {method}(")
    if idx == -1:
        return False
    open_i = source.find("{", idx)
    if open_i == -1:
        return False
    depth = 0
    close_i = -1
    for i in range(open_i, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                close_i = i
                break
    if close_i == -1:
        return False
    body = source[open_i + 1 : close_i]
    cleaned = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    cleaned = "\n".join(_strip_comment(line) for line in cleaned.splitlines() if line.strip())
    return bool(cleaned.strip())


def parse_providers(
    files: list[tuple[str, str]],
    bootstrap_src: str = "",
    app_config_src: str = "",
) -> dict[str, Any]:
    """Providers + which register/boot bodies carry real code.

    ``files`` = [(name, source)] of app/Providers/*.php. ``bootstrap_src`` =
    bootstrap/providers.php content when present (Laravel 11+). Legacy apps
    list providers in config/app.php ``'providers'`` — pass via
    ``app_config_src``.
    """
    registered: list[str] = []
    site = ""
    for src, candidate_site in (
        (bootstrap_src, "bootstrap/providers.php"),
        (app_config_src, "config/app.php"),
    ):
        if not src:
            continue
        ret_block = ""
        rm = _BOOTSTRAP_RETURN_RE.search(src)
        if rm:
            ret_block = rm.group(1)
            consts = _CLASS_CONST_RE.findall(ret_block)
            if consts:
                registered = consts  # fully-qualified ::class entries
                site = candidate_site
                break
        # no return array or no ::class entries — use `use` statements
        use_list = _BOOTSTRAP_USE_RE.findall(src)
        if use_list:
            registered = use_list
            site = candidate_site
            break

    providers: list[dict[str, Any]] = []
    for name, src in files:
        cm = _CLASS_RE.search(src)
        cls = cm.group(1) if cm else name.replace(".php", "")
        providers.append(
            {
                "name": cls,
                "file": f"app/Providers/{name}",
                "registered": cls in {p.split("\\")[-1] for p in registered},
                "register_has_code": _provider_method_code(src, "register"),
                "boot_has_code": _provider_method_code(src, "boot"),
            }
        )
    return {
        "providers": providers,
        "provider_count": len(providers),
        "registration_site": site or None,
    }


def _split_top_args(args: str) -> list[str]:
    """Split ``a, b`` at top-level commas (paren/brace/quote aware)."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    quote = ""
    for ch in args:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


def _abstract_name(raw: str) -> str:
    raw = raw.strip()
    cm = _CONCRETE_RE.search(raw)
    if cm:
        return cm.group(1)
    if raw[:1] in "'\"":
        return raw.strip("'\"")
    return raw


def parse_bindings(app_files: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Find bind/singleton/scoped calls across app/ sources."""
    out: list[dict[str, Any]] = []
    for file, src in app_files:
        for bm in _BIND_RE.finditer(src):
            open_i = src.find("(", bm.end() - 1)
            depth = 0
            close_i = -1
            for i in range(open_i, len(src)):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        close_i = i
                        break
            if close_i == -1:
                continue
            parts = _split_top_args(src[open_i + 1 : close_i])
            if not parts:
                continue
            abstract = _abstract_name(parts[0])
            concrete_raw = parts[1] if len(parts) > 1 else ""
            concrete = "?"
            if "function" in concrete_raw or concrete_raw.startswith("fn"):
                # closure — surface the class it news/returns when obvious
                new_cm = re.search(r"new\s+([\w\\]+)\s*\(", concrete_raw)
                ret_cm = _CONCRETE_RE.search(
                    concrete_raw.rsplit("}", 1)[-1]
                )
                concrete = (
                    (new_cm or ret_cm).group(1)
                    if (new_cm or ret_cm)
                    else "closure"
                )
            else:
                cm = _CONCRETE_RE.search(concrete_raw)
                concrete = cm.group(1) if cm else (concrete_raw.strip("'\"") or "?")
            if concrete == "?":
                continue
            out.append(
                {
                    "type": bm.group(1),
                    "abstract": abstract,
                    "concrete": concrete,
                    "file": file,
                }
            )
    return out


def _mw_entries(block: str) -> dict[str, str]:
    """Parse an alias array block ``['name' => Class::class, ...]``."""
    entries: dict[str, str] = {}
    for km in _MW_KEYPAIR_RE.finditer(block):
        entries[km.group(1)] = km.group(2).lstrip("\\")
    if not entries:
        # bare list of names (use()/append/prepend) — ::class or quoted
        classes = re.findall(r"([\w\\]+)::class", block)
        if classes:
            for c in classes:
                entries[c.lstrip("\\")] = c.lstrip("\\")
        else:
            for nm in _MW_NAME_RE.finditer(block):
                entries[nm.group(1)] = nm.group(1)
    return entries


def parse_middleware(
    bootstrap_app_src: str, kernel_src: str = ""
) -> dict[str, Any]:
    """Middleware registration from bootstrap/app.php (L11) or Kernel (legacy)."""
    result: dict[str, Any] = {
        "aliases": {},
        "groups": {},
        "global": [],
        "source": None,
    }
    if bootstrap_app_src:
        aliases: dict[str, str] = {}
        for am in _MIDDLEWARE_ALIAS_RE.finditer(bootstrap_app_src):
            aliases.update(_mw_entries(am.group(1)))
        groups: dict[str, dict[str, str]] = {}
        for gm in _MIDDLEWARE_GROUP_RE.finditer(bootstrap_app_src):
            groups[gm.group(1)] = _mw_entries(gm.group(2))
        globals_: list[str] = []
        for um in _MIDDLEWARE_USE_RE.finditer(bootstrap_app_src):
            globals_.extend(_mw_entries(um.group(1)).keys())
        for pm in _MIDDLEWARE_APPEND_RE.finditer(bootstrap_app_src):
            globals_.extend(_mw_entries(pm.group(2)).keys())
        if aliases or groups or globals_:
            result.update(
                {
                    "aliases": aliases,
                    "groups": groups,
                    "global": globals_,
                    "source": "bootstrap/app.php",
                }
            )
            return result
    if kernel_src:
        ka = _KERNEL_ALIAS_RE.search(kernel_src)
        kg = _KERNEL_GROUP_RE.search(kernel_src)
        if ka or kg:
            result["source"] = "app/Http/Kernel.php"
            if ka:
                result["aliases"] = _mw_entries(ka.group(1))
            if kg:
                groups = {}
                # nested group blocks:  'web' => [ ... ],  'api' => [ ... ]
                for gm in re.finditer(
                    r"['\"]([\w]+)['\"]\s*=>\s*\[(.*?)\](?:,|\s*\))",
                    kg.group(1),
                    re.DOTALL,
                ):
                    groups[gm.group(1)] = _mw_entries(gm.group(2))
                result["groups"] = groups
    return result


def extract_wiring(
    files: list[tuple[str, str]],
    *,
    bootstrap_providers_src: str = "",
    bootstrap_app_src: str = "",
    app_config_src: str = "",
) -> dict[str, Any]:
    """Aggregate wiring facts for one framework root.

    ``files`` = [(relative_path, source)] for every file under the Laravel
    root. Helper module funcs above are kept public + testable; this one is
    what the plugin calls.
    """
    provider_files = [(n, s) for n, s in files if n.startswith("app/Providers/")]
    bootstrap_src = bootstrap_providers_src
    providers = parse_providers(
        provider_files, bootstrap_src=bootstrap_src, app_config_src=app_config_src
    )
    bindings = parse_bindings(files)
    kernel_src = next((s for n, s in files if n == "app/Http/Kernel.php"), "")
    middleware = parse_middleware(bootstrap_app_src, kernel_src=kernel_src)
    return {
        "providers": providers.get("providers", []),
        "provider_count": providers.get("provider_count", 0),
        "registration_site": providers.get("registration_site"),
        "bindings": bindings,
        "middleware": middleware,
    }
