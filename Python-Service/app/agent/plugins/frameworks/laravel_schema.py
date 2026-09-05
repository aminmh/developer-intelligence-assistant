"""Static Laravel migration parser — database/schema knowledge.

Converts ``database/migrations/*.php`` Blueprint code into a normalized table
schema so answers know the *real* storage shape:

    {"table": "users", "columns": [
        {"name": "id", "type": "bigint", "nullable": False,
         "default": None, "unique": False, "indexed": False,
         "foreign": {"table": null, "column": null}},
        ...
    ]}

Handles the everyday Blueprint DSL:
    $table->id();  increments/bigIncrements, string(col, len), text,
    integer/bigInteger/unsignedBigInteger, boolean, decimal, float,
    dateTime/timestamp, foreignId(col)->constrained(), uuid, json,
    enum(col, [...]), softDeletes(), timestamps()
plus chain modifiers: nullable(), default(x), unique(), index(), after().

Reversible operations (dropIfExists/drop/Schema::drop) are reported but do not
contribute columns; migrations that are purely reversible are tracked by name.
"""

from __future__ import annotations

import re
from typing import Any

_CREATE_RE = re.compile(
    r"Schema::create\s*\(\s*['\"](?P<table>[A-Za-z0-9_.]+)['\"]\s*,\s*function\s*\([^)]*\)\s*\{",
)
_DROP_RE = re.compile(
    r"Schema::(?:dropIfExists|drop)\s*\(\s*['\"](?P<table>[A-Za-z0-9_.]+)['\"]\s*\)"
)
_COLUMN_CALL_RE = re.compile(
    r"\$table\s*->\s*(?P<col>[_A-Za-z][_A-Za-z0-9]*)\s*\((?P<body>[^;]*?)\)",
)
_TYPED_COLUMN_TYPES = {
    "id": "bigint",
    "increments": "int",
    "bigIncrements": "bigint",
    "mediumIncrements": "mediumint",
    "smallIncrements": "smallint",
    "tinyIncrements": "tinyint",
    "string": "varchar",
    "char": "char",
    "text": "text",
    "mediumText": "mediumtext",
    "longText": "longtext",
    "integer": "int",
    "tinyInteger": "tinyint",
    "smallInteger": "smallint",
    "mediumInteger": "mediumint",
    "bigInteger": "bigint",
    "unsignedInteger": "int unsigned",
    "unsignedBigInteger": "bigint unsigned",
    "unsignedTinyInteger": "tinyint unsigned",
    "float": "float",
    "double": "double",
    "decimal": "decimal",
    "boolean": "boolean",
    "date": "date",
    "dateTime": "datetime",
    "timestamp": "timestamp",
    "time": "time",
    "year": "year",
    "json": "json",
    "jsonb": "jsonb",
    "uuid": "uuid",
    "binary": "binary",
    "foreignId": "bigint unsigned",
    "foreignUuid": "uuid",
    "rememberToken": "varchar",
    "softDeletes": None,  # marker column, expanded below
    "softDeletesTz": None,
    "timestamps": None,  # marker
    "timestampsTz": None,
    "nullableTimestamps": None,
    "enum": "enum",
    "set": "set",
    "ipAddress": "varchar",
    "macAddress": "varchar",
    "geometry": "geometry",
}

_CONSTRAINED_RE = re.compile(r"->\s*constrained\s*(?:\(\s*['\"]([^'\"]+)['\"]\s*\))?")
_REFERENCES_RE = re.compile(r"->\s*references\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
_ON_RE = re.compile(r"->\s*on\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
_INDEX_RE = re.compile(r"->\s*(?:unique|index|fullText|spatialIndex)\s*\(")
_NULLABLE_RE = re.compile(r"->\s*nullable\s*\(")
_DEFAULT_STR_RE = re.compile(r"->\s*default\s*\(\s*['\"]([^'\"]*)['\"]\s*\)")
_DEFAULT_LIT_RE = re.compile(r"->\s*default\s*\(\s*(\w+)\s*\)")


def _default_value(tail: str) -> Any:
    m = _DEFAULT_STR_RE.search(tail)
    if m:
        return m.group(1)
    m = _DEFAULT_LIT_RE.search(tail)
    if m:
        lit = m.group(1)
        if lit == "true":
            return True
        if lit == "false":
            return False
        if lit == "null":
            return None
        try:
            return int(lit)
        except ValueError:
            pass
        try:
            return float(lit)
        except ValueError:
            return lit
    return None


def parse_migration(source: str, name: str = "") -> dict[str, Any]:
    """Parse a single migration file's source into schema intent."""
    created: dict[str, Any] = {}
    drops: list[str] = []
    for m in _CREATE_RE.finditer(source):
        table = m.group("table")
        body = source[m.end() :]
        # body ends at the matching '}' — approximate: slice up to '});'
        end = body.find("});")
        body = body[: end if end != -1 else 1200]
        created[table] = _parse_create_body(body, table)
    for m in _DROP_RE.finditer(source):
        drops.append(m.group("table"))
    # Schema::table additions to an existing table
    for m in re.finditer(
        r"Schema::table\s*\(\s*['\"]([A-Za-z0-9_.]+)['\"]\s*,\s*function\s*\([^)]*\)\s*\{",
        source,
    ):
        table = m.group(1)
        body = source[m.end() :]
        end = body.find("});")
        body = body[: end if end != -1 else 1200]
        created.setdefault(table, {"columns": []})
        cols = _parse_create_body(body, table)["columns"]
        created[table]["columns"].extend(cols)

    return {"migration": name, "created": created, "drops": drops}


def _parse_create_body(body: str, table: str) -> dict[str, Any]:
    """Scan Blueprint closure: each ``$table->method(...)`` statement is a
    segment that may include chained modifiers (``->nullable()`` etc.)."""
    columns: list[dict[str, Any]] = []
    stmt_re = re.compile(r"\$table\s*->\s*[_A-Za-z][_A-Za-z0-9]*\s*\(")
    for sm in stmt_re.finditer(body):
        # segment start → end: next $table->, else till ';' (then stop at closing brace/comment)
        seg_end = stmt_re.search(body, sm.end())
        if seg_end:
            segment = body[sm.start() : seg_end.start()]
        else:
            semi = body.find(";", sm.end())
            segment = body[sm.start() : (semi + 1 if semi != -1 else len(body))]

        head = re.match(r"\$table\s*->\s*(?P<col>[_A-Za-z][_A-Za-z0-9]*)\s*\(", segment)
        if not head:
            continue
        col_method = head.group("col")
        open_p = segment.find("(", head.end() - 1)
        close = _match_balanced(segment, open_p)
        args_raw = segment[open_p + 1 : close]
        tail = segment[close:]  # everything after the call: ->unique() etc.

        if col_method in ("timestamps", "timestampsTz"):
            columns.append(_mk("created_at", "timestamp", tail, True, None, True))
            columns.append(_mk("updated_at", "timestamp", tail, True, None, True))
            continue
        if col_method == "nullableTimestamps":
            continue
        if col_method == "softDeletes":
            columns.append(_mk("deleted_at", "timestamp", tail, True, None, True))
            continue
        if col_method == "rememberToken":
            columns.append(_mk("remember_token", "varchar", tail, True, None, True))
            continue
        if col_method == "foreign":
            _attach_foreign(columns, args_raw, tail)
            continue

        base_type = _TYPED_COLUMN_TYPES.get(col_method)
        if base_type is None:
            continue

        args = _split_args(args_raw)
        if col_method == "id":
            name = "id"
        else:
            name = args[0].strip().strip("'\"") if args else None
            if not name:
                continue
        if col_method in ("foreignId", "foreignUuid"):
            plain = name
            if plain.endswith("_id"):
                plain = plain[:-3]
            elif plain.endswith("Id"):
                plain = plain[:-2]
            name = plain

        # foreign key resolution
        foreign = None
        fk_refs = _CONSTRAINED_RE.search(tail) or _REFERENCES_RE.search(tail)
        if fk_refs:
            arg_table = _CONSTRAINED_RE.search(tail)
            ftable = arg_table.group(1) if arg_table and arg_table.group(1) else _default_fk_table(name)
            fcol = None
            refs = _REFERENCES_RE.search(tail)
            if refs:
                fcol = refs.group(1)
            on = _ON_RE.search(tail)
            if on and not ftable:
                ftable = on.group(1)
            foreign = {"table": ftable, "column": fcol}

        columns.append(_mk(name, base_type, tail, foreign=foreign))

    return {"table": table, "columns": columns}


def _default_fk_table(column_name: str) -> str:
    if column_name.endswith("_id"):
        base = column_name[:-3]
    elif column_name.endswith("Id"):
        base = column_name[:-2]
    else:
        base = column_name
    return f"{base}s" if not base.endswith("s") else base


def _mk(
    name: str,
    col_type: str,
    tail: str,
    nullable: bool = False,
    default: Any = None,
    force_ts: bool = False,
    foreign: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not force_ts:
        nullable = bool(_NULLABLE_RE.search(tail)) or nullable
        default = _default_value(tail)
    return {
        "name": name,
        "type": col_type,
        "nullable": nullable,
        "default": default,
        "unique": bool(re.search(r"->\s*unique\s*\(", tail)),
        "indexed": bool(re.search(r"->\s*(?:index|fullText|spatialIndex)\s*\(", tail)),
        "foreign": foreign,
    }


def _attach_foreign(columns: list[dict[str, Any]], args_raw: str, tail: str) -> None:
    """Handle ``$table->foreign('user_id')->references('id')->on('users')`` —
    attach the FK to the matching column by name."""
    args = _split_args(args_raw)
    if not args:
        return
    col_name = args[0].strip().strip("'\"")
    refs = _REFERENCES_RE.search(tail)
    on = _ON_RE.search(tail)
    foreign = {
        "table": on.group(1) if on else None,
        "column": refs.group(1) if refs else None,
    }
    for c in columns:
        if c["name"] == col_name:
            c["foreign"] = foreign
            return
    columns.append(_mk(col_name, "bigint unsigned", tail, foreign=foreign))


def _match_balanced(code: str, open_idx: int) -> int:
    depth = 0
    in_s = in_d = False
    i = open_idx
    while i < len(code):
        ch = code[i]
        if in_d and ch == "\\":
            i += 2
            continue
        if in_s and ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return len(code) - 1


def _split_args(body: str) -> list[str]:
    args: list[str] = []
    depth = 0
    start = 0
    in_s = in_d = False
    i = 0
    while i < len(body):
        ch = body[i]
        if in_d and ch == "\\":
            i += 2
            continue
        if in_s and ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                args.append(body[start:i])
                start = i + 1
        i += 1
    tail = body[start:].strip()
    if tail:
        args.append(tail)
    return args


def parse_migrations_directory(files: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Parse several migration files into a list of schema-intent dicts."""
    return [parse_migration(src, name) for name, src in files]


# Eloquent-convention boilerplate columns (timestamps, soft deletes, tokens).
# Stripped here — the framework plugin owns its conventions, so consumers of
# the canonical schema never need to know about them.
_BOILERPLATE_COLS = {"created_at", "updated_at", "deleted_at", "remember_token"}


def collapse_migrations(migrations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize migration records into the canonical schema slice.

    Consumers of the profile expect the **canonical** shape — a flat, merged
    table list, not framework-specific migration intent:

        [{"table": "users", "columns": [{name, type, nullable, default,
          unique, indexed, foreign{table, column} | None}]}]

    Application order follows the migration list: ``drops`` remove a table,
    ``created``/``Schema::table`` add or extend columns. Eloquent boilerplate
    columns are dropped here (framework convention), not by the renderer.
    """
    tables: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for mig in migrations or []:
        for tname in mig.get("drops") or []:
            if tname in tables:
                del tables[tname]
                order.remove(tname)
        for tname, info in (mig.get("created") or {}).items():
            if tname not in tables:
                tables[tname] = []
                order.append(tname)
            for col in info.get("columns") or []:
                if (col.get("name") or "?") in _BOILERPLATE_COLS:
                    continue
                tables[tname].append(col)
    return [{"table": name, "columns": tables[name]} for name in order]
