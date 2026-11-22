"""Deterministic index-aware grounding planner (Phase 1).

Given the semantic route plus an optional project filename/collection a
question is anchored to, decide WHICH existing persisted indexes it should be
grounded from:

  * Qdrant collections ``chunk_code_<stem>`` / ``proj_knowledge_<stem>``,
  * the Neo4j ``sym_<stem>`` Symbol graph (presence only, ``has_sym``).

Saved ``analysis_sessions`` rows are the authority for what was persisted;
guarded Qdrant/Neo4j probes confirm what still exists. The ``knowledge`` route
gets a knowledge-first ordering: when a filename anchors the question and BOTH
``chunk_code_<stem>`` and ``proj_knowledge_<stem>`` exist, the knowledge
collection is returned FIRST (structure facts are the primary evidence there;
the code collection still joins as secondary evidence).

Route handling: ``chat`` never uses project indexes (mode ``chat``); the known
project routes ground from indexes whenever a project context exists. Unknown
route names WITH a project context (filename and/or pinned collection) are
treated as project questions — the client's decision to anchor to an uploaded
project overrides route classification, so plan discovery still happens instead
of a silent empty result. Only an unknown route with NO project context is
``off``.

``collections`` is strictly the Qdrant-searchable set (``chunk_code_<stem>`` /
``proj_knowledge_<stem>``). Pinned ``sym_<stem>`` names never appear there —
sym nodes are Neo4j-only with no Qdrant payloads — their presence is carried by
``has_sym`` instead.

The planner is TOTAL and NEVER raises: every store touch — including the lazy
stem/prefix derivations that import ``analysis.analysis_service`` (whose module
import constructs the store's ``AnalysisStore``) or the heavy
``index.llamaindex_layer`` module — goes through guarded probes that return a
safe default + degraded flag on any failure. A failed derivation yields
``stem=None`` / empty collections with a note entry, never a raise. Pure string
helpers never trigger imports: the collection prefixes are local constants that
mirror ``index.llamaindex_layer`` naming. It performs no retrieval and opens no
store connection at import time — all app imports are lazy.
"""

from __future__ import annotations

# Routes that may ground from persisted project indexes (chat never does).
_PROJECT_ROUTES = frozenset(
    {
        "search",
        "knowledge",
        "code_explain",
        "code_refactor",
        "debug",
        "structure_analyze",
        "index_code",
    }
)

# Public mirror of the known project-route set. ``main._answer_context`` uses it
# BEFORE planning to route a pinned collection on a chat/unknown route straight
# to the legacy pinned retrieval (no plan). Imported lazily — this module stays
# import-light (no heavy app imports at module scope).
PROJECT_ROUTES = frozenset(_PROJECT_ROUTES)

# Collection prefixes — local pure constants mirroring
# ``index.llamaindex_layer`` naming so planning never imports that heavy module
# (top-level qdrant/embedding imports) just to read a string.
_CODE_PREFIX = "chunk_code_"
_KNOWLEDGE_PREFIX = "proj_knowledge_"
_SYM_PREFIX = "sym_"


# --- lazy app imports (always invoked behind a guarded probe) --------------


def _default_saved_rows(filename: str) -> list[dict]:
    from analysis.analysis_store import analysis_store

    return analysis_store.list_by_filename(filename)


def _default_qdrant_collections() -> list[str]:
    from index.llamaindex_layer import list_qdrant_collections

    return list_qdrant_collections()


def _default_sym_exists(stem: str) -> bool:
    from index.llamaindex_layer import sym_collection_exists

    return sym_collection_exists(stem)


# --- small pure helpers (never import) --------------------------------------


def _clean_text(value) -> str | None:
    """None for missing/blank values; surrounding whitespace trimmed."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _distinct_kinds(rows: list[dict]) -> list[str]:
    """Order-preserving distinct ``kind`` values across saved rows."""
    kinds: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def _code_collection(stem: str) -> str:
    return f"{_CODE_PREFIX}{stem}"


def _knowledge_collection(stem: str) -> str:
    return f"{_KNOWLEDGE_PREFIX}{stem}"


def _stem_from_collection(collection: str) -> str | None:
    """Project stem embedded in a collection name (pure string strip). None when
    the name carries no known project prefix."""
    for prefix in (_CODE_PREFIX, _KNOWLEDGE_PREFIX, _SYM_PREFIX):
        if collection.startswith(prefix):
            rest = collection[len(prefix) :].strip()
            if rest:
                return rest
    return None


def _knowledge_first(
    route: str,
    filename: str | None,
    stem: str | None,
    collections: list[str],
) -> list[str]:
    """Knowledge-first ordering for ``knowledge`` questions anchored to a file.

    ``knowledge`` + filename + both code/knowledge collections existing ⇒ the
    knowledge collection leads (structure text is the primary evidence); the
    code collection still joins after it. Every other route keeps its
    deterministic code-first order.
    """
    ordered = list(collections)
    if route == "knowledge" and filename is not None and stem is not None:
        code_col = _code_collection(stem)
        proj_col = _knowledge_collection(stem)
        if code_col in ordered and proj_col in ordered:
            ordered = [proj_col] + [c for c in ordered if c != proj_col]
    return ordered


def _make_plan(
    mode: str,
    route: str,
    stem: str | None,
    collections: list[str],
    saved_kinds: list[str],
    has_sym: bool,
    notes: list[str],
) -> dict:
    return {
        "mode": mode,
        "route": route,
        "stem": stem,
        "collections": list(collections),
        "saved_kinds": list(saved_kinds),
        "has_sym": bool(has_sym),
        "notes": list(notes),
    }


# --- guarded derivations -----------------------------------------------------


def _derive_stem(filename: str) -> tuple[str | None, bool]:
    """Project stem from a filename; never raises.

    Deriving imports ``analysis.analysis_service``, whose module scope opens
    the store's ``AnalysisStore`` (SQLite) — a cold-start DB failure must read
    as ``(None, True)`` (degraded), not raise into the planner.
    """
    try:
        from analysis.analysis_service import project_stem

        stem = project_stem(filename)
        return (stem or None), False
    except Exception:  # noqa: BLE001 — planning must never raise
        return None, True


def _derive_pinned_stem(collection: str) -> tuple[str | None, bool]:
    """Stem for a pinned collection: prefix strip when possible (pure), else the
    guarded filename-style derivation."""
    stem = _stem_from_collection(collection)
    if stem is not None:
        return stem, False
    return _derive_stem(collection)


# --- store probes (guarded; degraded flags feed the notes) ------------------


def _probe_collections(cols_provider) -> tuple[set[str], bool]:
    try:
        return {str(c) for c in (cols_provider() or [])}, False
    except Exception:  # noqa: BLE001 — planning must never raise
        return set(), True


def _probe_sym(sym_provider, stem: str) -> tuple[bool, bool]:
    try:
        return bool(sym_provider(stem)), False
    except Exception:  # noqa: BLE001 — planning must never raise
        return False, True


def _probe_saved_rows(rows_provider, filename: str) -> tuple[list[dict], bool]:
    try:
        return list(rows_provider(filename) or []), False
    except Exception:  # noqa: BLE001 — planning must never raise
        return [], True


# --- branch planners --------------------------------------------------------


def _pinned_plan(
    route: str,
    collection: str,
    filename: str | None,
    rows_provider,
    cols_provider,
    sym_provider,
) -> dict:
    """Plan for a caller-pinned collection (trusted anchor)."""
    notes: list[str] = []
    degraded = False

    stem, stem_down = _derive_pinned_stem(collection)
    degraded = degraded or stem_down

    saved_kinds: list[str] = []
    if filename is not None:
        rows, rows_down = _probe_saved_rows(rows_provider, filename)
        saved_kinds = _distinct_kinds(rows)
        degraded = degraded or rows_down

    existing: set[str] = set()
    has_sym = False
    if stem is not None:
        existing, qdrant_down = _probe_collections(cols_provider)
        has_sym, sym_down = _probe_sym(sym_provider, stem)
        degraded = degraded or qdrant_down or sym_down

    code_col = _code_collection(stem) if stem is not None else None
    proj_col = _knowledge_collection(stem) if stem is not None else None

    def _add_if_exists(candidate: str | None) -> None:
        if (
            candidate
            and candidate != collection
            and candidate not in result
            and candidate in existing
        ):
            result.append(candidate)

    result: list[str] = [collection]
    if stem is not None and (
        collection == code_col or collection.startswith(_CODE_PREFIX)
    ):
        # Pinned code collection joins its persisted-knowledge sibling.
        _add_if_exists(proj_col)
    elif stem is not None and (
        collection == proj_col or collection.startswith(_KNOWLEDGE_PREFIX)
    ):
        # Pinned knowledge collection joins its code sibling.
        _add_if_exists(code_col)
    else:
        _add_if_exists(code_col)
        _add_if_exists(proj_col)
    # Qdrant-searchable collections only: a pinned ``sym_<stem>`` name has no
    # Qdrant payload (Neo4j-only) so it must never be searched as a vector
    # collection. Its presence is still reflected in ``has_sym``.
    result = [c for c in result if not c.startswith(_SYM_PREFIX)]
    result = _knowledge_first(route, filename, stem, result)

    if degraded:
        notes.append("stores unreachable (degraded)")
    elif filename is not None and not saved_kinds:
        notes.append("no saved rows")
    return _make_plan("project", route, stem, result, saved_kinds, has_sym, notes)


def _filename_plan(
    route: str,
    filename: str,
    rows_provider,
    cols_provider,
    sym_provider,
) -> dict:
    """Plan grounded on an uploaded project filename."""
    notes: list[str] = []
    degraded = False

    stem, stem_down = _derive_stem(filename)
    degraded = degraded or stem_down

    rows, rows_down = _probe_saved_rows(rows_provider, filename)
    saved_kinds = _distinct_kinds(rows)
    degraded = degraded or rows_down

    existing: set[str] = set()
    has_sym = False
    if stem is not None:
        existing, qdrant_down = _probe_collections(cols_provider)
        has_sym, sym_down = _probe_sym(sym_provider, stem)
        degraded = degraded or qdrant_down or sym_down

    if stem is None:
        collections: list[str] = []
    else:
        code_col = _code_collection(stem)
        proj_col = _knowledge_collection(stem)
        # Deterministic order: code vectors first, persisted knowledge sibling
        # second — only collections that actually exist are returned. The
        # knowledge route re-orders via ``_knowledge_first`` when a filename is
        # present and both collections exist.
        collections = _knowledge_first(
            route,
            filename,
            stem,
            [c for c in (code_col, proj_col) if c in existing],
        )

    if degraded:
        notes.append("stores unreachable (degraded)")
    else:
        if not saved_kinds:
            notes.append("no saved rows")
        if not collections:
            if saved_kinds:
                # Rows ARE authority: they describe work that completed, but the
                # vectors they produced are gone (or the store never held them).
                note = "saved rows exist but no Qdrant collections found"
                if has_sym:
                    note += " (sym graph present)"
                notes.append(note)
            elif has_sym:
                notes.append("no qdrant vectors; sym graph present")
            else:
                notes.append("no persisted indexes for project")
    return _make_plan(
        "project", route, stem, collections, saved_kinds, has_sym, notes
    )


# --- public entry point ----------------------------------------------------


def build_index_plan(
    *,
    route: str,
    filename: str | None = None,
    collection: str | None = None,
    analysis_store_provider=None,
    qdrant_list_provider=None,
    sym_provider=None,
) -> dict:
    """Decide which persisted indexes a question should ground from.

    Keyword-only; total and never raises. Returned dict always has keys:
    ``mode`` (chat|project|global|off), ``route``, ``stem``, ``collections``,
    ``saved_kinds``, ``has_sym``, ``notes``. Providers are injectable for
    hermetic tests and default to the real guarded lazy store helpers. ``route
    == "knowledge"`` + filename + both collections existing orders the
    knowledge collection first. ``collections`` holds only Qdrant-searchable
    names (sym_* never included). Unknown route names with a project context
    still plan (the anchored project is authoritative); chat never uses
    indexes.
    """
    route = _clean_text(route) or ""
    filename = _clean_text(filename)
    collection = _clean_text(collection)
    rows_provider = analysis_store_provider or _default_saved_rows
    cols_provider = qdrant_list_provider or _default_qdrant_collections
    sym_exists = sym_provider or _default_sym_exists

    if route == "chat":
        notes: list[str] = []
        if filename is not None or collection is not None:
            notes.append("chat route with project context: index use skipped")
        return _make_plan("chat", route, None, [], [], False, notes)

    if filename is None and collection is None:
        if route not in _PROJECT_ROUTES:
            return _make_plan(
                "off", route, None, [], [], False, ["no route/project context"]
            )
        return _make_plan(
            "global", route, None, [], [], False,
            ["no project filename/collection; global fallback"],
        )

    # A project context (filename and/or pinned collection) is the caller's
    # decision to ground from an uploaded project — plan discovery runs for the
    # known routes AND for unknown route names alike (chat was excluded above).
    # An unknown route alone never zeroes out a pinned/filename retrieval.
    if collection is not None:
        return _pinned_plan(
            route, collection, filename, rows_provider, cols_provider, sym_exists
        )
    return _filename_plan(route, filename, rows_provider, cols_provider, sym_exists)
