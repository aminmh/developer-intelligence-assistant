"""Hermetic tests for the index-aware grounding planner (rag.context_plan)."""

from __future__ import annotations

import os

import pytest

from rag.context_plan import build_index_plan


@pytest.fixture(scope="module", autouse=True)
def _hermetic_analysis_store(tmp_path_factory):
    """Keep the analysis_store singleton out of /app/data.

    The ``TestDefaultProviderPath`` tests drive the REAL lazy default providers;
    importing ``analysis.analysis_store`` constructs its module-level singleton
    against ``SETTINGS_DB_PATH`` (default ``/app/data/settings.db``). Redirect
    the env to a temp dir BEFORE that first import so running this file
    standalone never touches ``/app/data``; the module is cached afterwards so
    every later lazy import (analysis_service, context_plan probes) reuses the
    temp-backed singleton. Restored at teardown.
    """
    db_env = os.environ.get("SETTINGS_DB_PATH")
    dir_env = os.environ.get("ANALYSIS_DIR")
    tmp = tmp_path_factory.mktemp("settings-db")
    os.environ["SETTINGS_DB_PATH"] = str(tmp / "settings.db")
    os.environ["ANALYSIS_DIR"] = str(tmp / "analyses")
    try:
        import analysis.analysis_store  # noqa: F401

        yield
    finally:
        if db_env is None:
            os.environ.pop("SETTINGS_DB_PATH", None)
        else:
            os.environ["SETTINGS_DB_PATH"] = db_env
        if dir_env is None:
            os.environ.pop("ANALYSIS_DIR", None)
        else:
            os.environ["ANALYSIS_DIR"] = dir_env


class _FakeRows:
    def __init__(self, *rows):
        self.rows = list(rows)

    def __call__(self, filename):
        return list(self.rows)


class _FakeCollections:
    def __init__(self, *names):
        self.names = list(names)

    def __call__(self):
        return list(self.names)


class _FakeSym:
    def __init__(self, value=False):
        self.value = bool(value)

    def __call__(self, stem):
        return self.value


def _down():
    raise RuntimeError("probe down")


def _plan(route, filename=None, collection=None, rows=None, cols=None,
          sym=False, down=()):
    """Call build_index_plan with hermetic fakes for every store probe."""
    down = {down} if isinstance(down, str) else set(down or ())
    kwargs = {"route": route, "filename": filename, "collection": collection}
    if "rows" in down:
        kwargs["analysis_store_provider"] = _down
    else:
        kwargs["analysis_store_provider"] = _FakeRows(*(rows or []))
    if "cols" in down:
        kwargs["qdrant_list_provider"] = _down
    else:
        kwargs["qdrant_list_provider"] = _FakeCollections(*(cols or []))
    if "sym" in down:
        kwargs["sym_provider"] = _down
    else:
        kwargs["sym_provider"] = _FakeSym(sym)
    return build_index_plan(**kwargs)


def _has_note(plan, fragment):
    return any(fragment in note for note in plan["notes"])


class TestChatMode:
    def test_chat_without_project_context_is_chat(self):
        plan = _plan("chat")
        assert plan["mode"] == "chat"
        assert plan["collections"] == []
        assert plan["stem"] is None
        assert plan["has_sym"] is False

    def test_chat_with_filename_stays_chat_no_collections(self):
        plan = _plan("chat", filename="Archive.zip", cols=["chunk_code_Archive"])
        assert plan["mode"] == "chat"
        assert plan["collections"] == []
        assert plan["stem"] is None
        assert _has_note(plan, "index use skipped")

    def test_chat_with_collection_stays_chat_no_collections(self):
        plan = _plan("chat", collection="chunk_code_Archive",
                     cols=["chunk_code_Archive"])
        assert plan["mode"] == "chat"
        assert plan["collections"] == []
        assert _has_note(plan, "index use skipped")


class TestGlobalAndOffModes:
    def test_knowledge_without_project_context_is_global(self):
        plan = _plan("knowledge")
        assert plan["mode"] == "global"
        assert plan["collections"] == []
        assert plan["stem"] is None

    def test_search_without_project_context_is_global(self):
        plan = _plan("search")
        assert plan["mode"] == "global"
        assert plan["collections"] == []

    def test_any_project_route_without_context_is_global(self):
        for route in ("code_explain", "code_refactor", "debug",
                      "structure_analyze", "index_code"):
            assert _plan(route)["mode"] == "global"
        assert _plan("sandbox_analyze")["mode"] == "off"

    def test_unknown_route_without_context_is_off(self):
        plan = _plan("future_route")
        assert plan["mode"] == "off"
        assert plan["notes"] == ["no route/project context"]

    def test_unknown_route_with_filename_plans_project(self):
        # The anchored filename is the caller's project-grounding decision: an
        # unknown route name still gets plan discovery, never a silent off.
        plan = _plan("future_route", filename="x.zip",
                     rows=[{"kind": "code_index"}], cols=["chunk_code_x"])
        assert plan["mode"] == "project"
        assert plan["stem"] == "x"
        assert plan["collections"] == ["chunk_code_x"]
        assert plan["saved_kinds"] == ["code_index"]

    def test_unknown_route_with_pinned_collection_plans_project(self):
        plan = _plan("future_route", collection="chunk_code_x",
                     cols=["chunk_code_x", "proj_knowledge_x"])
        assert plan["mode"] == "project"
        assert plan["stem"] == "x"
        assert plan["collections"] == ["chunk_code_x", "proj_knowledge_x"]

    def test_unknown_route_filename_no_indexes_is_honest_project(self):
        plan = _plan("future_route", filename="x.zip")
        assert plan["mode"] == "project"
        assert plan["collections"] == []
        assert _has_note(plan, "no persisted indexes for project")


class TestFilenameBranch:
    def test_knowledge_filename_knowledge_saved_only(self):
        plan = _plan("knowledge", filename="x.zip",
                     rows=[{"kind": "structure"}],
                     cols=["proj_knowledge_x"])
        assert plan["mode"] == "project"
        assert plan["stem"] == "x"
        assert plan["collections"] == ["proj_knowledge_x"]
        assert plan["saved_kinds"] == ["structure"]
        assert plan["has_sym"] is False

    def test_knowledge_filename_both_collections_orders_knowledge_first(self):
        plan = _plan("knowledge", filename="x.zip",
                     rows=[{"kind": "structure"}],
                     cols=["proj_knowledge_x", "chunk_code_x"])
        assert plan["mode"] == "project"
        assert plan["collections"] == ["proj_knowledge_x", "chunk_code_x"]

    def test_saved_rows_exist_but_no_qdrant_collections_note(self):
        """Rows ARE authority: partial state (rows up, Qdrant empty, sym down)
        gets a distinct honest note, never 'no persisted indexes for project'."""
        plan = _plan("code_explain", filename="x.zip",
                     rows=[{"kind": "structure"}], cols=[])
        assert plan["mode"] == "project"
        assert plan["collections"] == []
        assert plan["saved_kinds"] == ["structure"]
        assert plan["has_sym"] is False
        assert _has_note(plan, "saved rows exist but no Qdrant collections found")
        assert not _has_note(plan, "no persisted indexes for project")

    def test_saved_rows_exist_no_qdrant_but_sym_present_note(self):
        plan = _plan("code_explain", filename="x.zip",
                     rows=[{"kind": "structure"}], cols=[], sym=True)
        assert _has_note(plan, "saved rows exist but no Qdrant collections found")
        assert _has_note(plan, "sym graph present")

    def test_saved_rows_exist_but_qdrant_collection_grounds_normally(self):
        plan = _plan("code_explain", filename="x.zip",
                     rows=[{"kind": "structure"}], cols=["chunk_code_x"])
        assert plan["collections"] == ["chunk_code_x"]
        assert not _has_note(plan, "no Qdrant collections")

    def test_filename_code_collection_ordered_before_knowledge(self):
        plan = _plan("code_explain", filename="x.zip",
                     rows=[{"kind": "code_index"}],
                     cols=["proj_knowledge_x", "chunk_code_x"])
        assert plan["collections"] == ["chunk_code_x", "proj_knowledge_x"]
        assert plan["saved_kinds"] == ["code_index"]

    def test_stem_derivation_from_archive_zip(self):
        plan = _plan("code_explain", filename="Archive.zip",
                     rows=[{"kind": "structure"}],
                     cols=["chunk_code_Archive", "proj_knowledge_Archive"])
        assert plan["stem"] == "Archive"
        assert plan["collections"] == ["chunk_code_Archive",
                                       "proj_knowledge_Archive"]
        assert plan["saved_kinds"] == ["structure"]

    def test_search_filename_is_project_pinned(self):
        plan = _plan("search", filename="foo.zip",
                     rows=[{"kind": "code_index"}], cols=["chunk_code_foo"],
                     sym=True)
        assert plan["mode"] == "project"
        assert plan["collections"] == ["chunk_code_foo"]
        assert plan["has_sym"] is True

    def test_sym_graph_detected(self):
        plan = _plan("debug", filename="x.zip",
                     rows=[{"kind": "analysis"}], cols=["chunk_code_x"],
                     sym=True)
        assert plan["has_sym"] is True
        assert plan["stem"] == "x"

    def test_saved_kinds_distinct_and_ordered(self):
        plan = _plan("debug", filename="x.zip",
                     rows=[{"kind": "structure"}, {"kind": "code_index"},
                           {"kind": "structure"}, {"kind": "analysis"}],
                     cols=["chunk_code_x"])
        assert plan["saved_kinds"] == ["structure", "code_index", "analysis"]

    def test_no_saved_rows_but_code_index_grounds(self):
        plan = _plan("code_explain", filename="y.zip", cols=["chunk_code_y"])
        assert plan["mode"] == "project"
        assert plan["collections"] == ["chunk_code_y"]
        assert plan["saved_kinds"] == []
        assert _has_note(plan, "no saved rows")

    def test_no_rows_and_no_collections_is_honest_project_empty(self):
        plan = _plan("debug", filename="y.zip")
        assert plan["mode"] == "project"
        assert plan["collections"] == []
        assert _has_note(plan, "no persisted indexes for project")

    def test_sym_only_project_never_says_no_persisted_index(self):
        plan = _plan("code_explain", filename="y.zip", sym=True)
        assert plan["mode"] == "project"
        assert plan["collections"] == []
        assert plan["has_sym"] is True
        assert _has_note(plan, "sym graph present")
        assert not _has_note(plan, "no persisted indexes for project")

    def test_stores_down_filename_degrades_project_empty(self):
        plan = _plan("code_explain", filename="x.zip",
                     down=("rows", "cols", "sym"))
        assert plan["mode"] == "project"
        assert plan["stem"] == "x"
        assert plan["collections"] == []
        assert plan["saved_kinds"] == []
        assert plan["has_sym"] is False
        assert _has_note(plan, "stores unreachable")


class TestPinnedCollectionBranch:
    def test_pinned_code_joins_existing_knowledge_sibling(self):
        plan = _plan("code_explain", collection="chunk_code_x",
                     cols=["chunk_code_x", "proj_knowledge_x"])
        assert plan["mode"] == "project"
        assert plan["stem"] == "x"
        assert plan["collections"] == ["chunk_code_x", "proj_knowledge_x"]

    def test_knowledge_route_keeps_pinned_code_with_knowledge(self):
        plan = _plan("knowledge", collection="chunk_code_x",
                     cols=["chunk_code_x", "proj_knowledge_x"])
        assert plan["collections"] == ["chunk_code_x", "proj_knowledge_x"]

    def test_knowledge_force_respects_collection_existence(self):
        plan = _plan("knowledge", collection="chunk_code_x",
                     cols=["chunk_code_x"])
        assert plan["collections"] == ["chunk_code_x"]

    def test_pinned_knowledge_joins_existing_code_collection(self):
        plan = _plan("code_explain", collection="proj_knowledge_x",
                     cols=["proj_knowledge_x", "chunk_code_x"])
        assert plan["stem"] == "x"
        assert plan["collections"] == ["proj_knowledge_x", "chunk_code_x"]

    def test_pinned_collection_dedupes_and_fixes_order(self):
        plan = _plan("knowledge", collection="chunk_code_x",
                     cols=["proj_knowledge_x", "chunk_code_x",
                           "proj_knowledge_x"])
        assert plan["collections"] == ["chunk_code_x", "proj_knowledge_x"]

    def test_pinned_sym_collection_is_not_a_qdrant_collection(self):
        # sym_<stem> is Neo4j-only: never listed as a searchable collection even
        # when pinned — only the existing code sibling remains.
        plan = _plan("code_explain", collection="sym_Zeta",
                     cols=["chunk_code_Zeta"])
        assert plan["stem"] == "Zeta"
        assert plan["collections"] == ["chunk_code_Zeta"]

    def test_pinned_sym_collection_alone_yields_empty_qdrant_with_sym(self):
        plan = _plan("code_explain", collection="sym_Zeta",
                     cols=[], sym=True)
        assert plan["mode"] == "project"
        assert plan["stem"] == "Zeta"
        assert plan["collections"] == []
        assert plan["has_sym"] is True

    def test_pinned_collection_with_filename_reads_saved_kinds(self):
        plan = _plan("knowledge", filename="x.zip", collection="chunk_code_x",
                     rows=[{"kind": "structure"}],
                     cols=["chunk_code_x", "proj_knowledge_x"])
        assert plan["saved_kinds"] == ["structure"]
        # Knowledge route + filename + both collections ⇒ knowledge first.
        assert plan["collections"] == ["proj_knowledge_x", "chunk_code_x"]

    def test_pinned_stores_down_keeps_pinned_collection(self):
        plan = _plan("code_explain", collection="chunk_code_x",
                     down=("rows", "cols", "sym"))
        assert plan["mode"] == "project"
        assert plan["collections"] == ["chunk_code_x"]
        assert _has_note(plan, "stores unreachable")


class TestDefaultProviderPath:
    """REAL default-provider path under hermetic conditions.

    The three lazy default providers (analysis_store list_by_filename,
    llamaindex list_qdrant_collections, sym_collection_exists) run — so the
    lazy import path executes — but their store bodies are fakes patched at the
    module level. Covers review finding #1 end-to-end: a raising default
    provider degrades into notes instead of raising out of ``build_index_plan``.
    """

    def _patch_store_modules(self, monkeypatch, rows, cols, sym):
        import analysis.analysis_store as store_mod
        import index.llamaindex_layer as lli

        class _FakeStore:
            def __init__(self, stored):
                self.stored = list(stored or [])

            def list_by_filename(self, filename):
                return list(self.stored)

        monkeypatch.setattr(store_mod, "analysis_store", _FakeStore(rows))
        monkeypatch.setattr(lli, "list_qdrant_collections", lambda: list(cols))
        monkeypatch.setattr(lli, "sym_collection_exists", lambda stem: bool(sym))

    def test_default_providers_hermetic_happy_path(self, monkeypatch):
        self._patch_store_modules(
            monkeypatch, [{"kind": "code_index"}],
            ["chunk_code_x", "proj_knowledge_x"], False,
        )
        plan = build_index_plan(route="code_explain", filename="x.zip")
        assert plan["mode"] == "project"
        assert plan["stem"] == "x"
        assert plan["collections"] == ["chunk_code_x", "proj_knowledge_x"]
        assert plan["saved_kinds"] == ["code_index"]

    def test_default_providers_raising_degrades_not_raise(self, monkeypatch):
        import analysis.analysis_store as store_mod
        import index.llamaindex_layer as lli

        def _boom_rows(filename):
            raise RuntimeError("rows store down")

        def _boom_cols():
            raise RuntimeError("qdrant list down")

        def _boom_sym(stem):
            raise RuntimeError("sym probe down")

        monkeypatch.setattr(
            store_mod, "analysis_store",
            type("_DownStore", (), {"list_by_filename": staticmethod(_boom_rows)})(),
        )
        monkeypatch.setattr(lli, "list_qdrant_collections", _boom_cols)
        monkeypatch.setattr(lli, "sym_collection_exists", _boom_sym)

        plan = build_index_plan(route="code_explain", filename="x.zip")
        assert plan["mode"] == "project"
        assert plan["collections"] == []
        assert plan["saved_kinds"] == []
        assert _has_note(plan, "stores unreachable")


class TestTotalNeverRaises:
    def test_empty_route_and_context_never_raises(self):
        plan = _plan("")
        assert plan["mode"] == "off"

    @pytest.mark.parametrize("route", ["chat", "search", "knowledge"])
    def test_blank_filename_treated_as_no_context(self, route):
        plan = _plan(route, filename="   ")
        assert plan["mode"] in ("chat", "global")

    def test_non_string_filename_never_raises(self):
        plan = build_index_plan(route="code_explain", filename=123,
                                analysis_store_provider=_FakeRows(),
                                qdrant_list_provider=_FakeCollections(),
                                sym_provider=_FakeSym())
        assert plan["mode"] == "global"
