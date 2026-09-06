"""Hermetic Phase-3 answer-path tests (main._answer_context).

Non-chat prompts with a project context (filename/collection) ground from the
plan's persisted indexes via CodePropertyGraph().hybrid_retrieve while keeping
``extra_context`` structure text in the prompt; chat never grounds from project
indexes and the no-project legacy paths stay byte-identical. All store/LLM
touches are fakes — no daemon/Qdrant/Neo4j/LLM contact.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import index.llamaindex_layer as lli
import main as main_module
from rag import answer


def _fake_complete(prompt, system=None, temperature=None, top_p=None,
                   max_tokens=2048, provider="local"):
    return "the answer", "fake-model", provider


def _fake_stream_complete():
    def _gen(prompt, system=None, temperature=None, top_p=None,
             max_tokens=2048, provider="local"):
        yield "", "fake-model", provider
        yield "Hello ", "fake-model", provider
        yield "world", "fake-model", provider

    return _gen


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Fake every external boundary the /answer path can reach."""
    traces: list[dict] = []

    monkeypatch.setattr(
        lli, "record_retrieval_trace",
        lambda trace_id, payload: traces.append(dict(payload)),
    )
    monkeypatch.setattr(main_module, "_resolve_project_profile",
                        lambda body, query: None)
    monkeypatch.setattr(answer.settings, "lsp_validation_enabled", False)
    monkeypatch.setattr(answer, "complete_with_model", _fake_complete)
    monkeypatch.setattr(answer, "stream_complete_with_model",
                        _fake_stream_complete())
    return traces


class FakeGraph:
    """Capturing stand-in for CodePropertyGraph().hybrid_retrieve."""

    def __init__(self):
        self.calls: list[dict] = []
        self.outcomes: dict[str | None, tuple[list, list[str]]] = {}

    def set_outcome(self, collection, results, searched=None):
        self.outcomes[collection] = (
            list(results),
            list(searched) if searched is not None else ([collection] if collection else []),
        )

    def hybrid_retrieve(self, *, query, collection_name=None, limit=10,
                        class_name=None, file=None, trace_id=None,
                        graph_legs=None, join_sibling=True):
        self.calls.append({
            "query": query,
            "collection_name": collection_name,
            "limit": limit,
            "class_name": class_name,
            "file": file,
            "trace_id": trace_id,
            "graph_legs": list(graph_legs) if graph_legs is not None else None,
            "join_sibling": join_sibling,
        })
        results, searched = self.outcomes.get(
            collection_name, ([], ["global"] if collection_name is None else [])
        )
        return {
            "results": [dict(r) for r in results],
            "searched_collections": list(searched),
        }


@pytest.fixture()
def fake_graph(monkeypatch):
    graph = FakeGraph()
    monkeypatch.setattr(lli, "CodePropertyGraph", lambda: graph)
    return graph


def _hit(node_id, collection, score=0.9, code="return 1;", name="n",
         file="f.php", class_fqn="App\\X"):
    return {
        "collection": collection,
        "score": float(score),
        "payload": {
            "node_id": node_id,
            "type": "method",
            "name": name,
            "method_name": name,
            "class": class_fqn.split("\\")[-1],
            "class_fqn": class_fqn,
            "file": file,
            "start_line": 1,
            "end_line": 2,
            "code": code,
        },
        "via_graph": False,
    }


def _sym_hit(graph_key, collection, score=0.5, *, fqn="App\\X::n", kind="method",
             file="f.php", line=7):
    """Sym-evidence row: Neo4j-only, NO node_id — only a graph_key identity."""
    return {
        "collection": collection,
        "score": float(score),
        "payload": {
            "graph_sym": True,
            "via_graph": True,
            "collection": "sym_x",
            "kind": kind,
            "name": fqn.split("::")[-1],
            "fqn": fqn,
            "file": file,
            "line": line,
            "to_fqn": "App\\Y",
            "to_member": "y",
            "rel": "REFERENCES",
            "rel_kind": "method_call",
            "graph_key": graph_key,
        },
        "via_graph": True,
        "graph_leg": "sym",
    }


def _plan(collections, *, has_sym=False, stem="x", route="search",
          notes=None):
    return {
        "mode": "project",
        "route": route,
        "stem": stem,
        "collections": list(collections),
        "saved_kinds": ["structure"],
        "has_sym": has_sym,
        "notes": notes or [],
    }


def _client():
    return TestClient(main_module.app)


def _post_answer(body):
    return _client().post("/answer", json=body)


def _post_stream(body):
    resp = _client().post("/answer/stream", json=body)
    events = [
        json.loads(line[len("data: "):])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    return resp, events


def _patch_plan(monkeypatch, plan):
    monkeypatch.setattr(main_module, "_index_plan_for",
                        lambda body, query, route: plan)


# --- project context + non-chat retrieves from the plan ----------------------


def test_non_chat_filename_plan_non_empty_retrieves_once(monkeypatch, fake_graph,
                                                        _hermetic):
    _patch_plan(monkeypatch, _plan(["chunk_code_x"]))
    fake_graph.set_outcome(
        "chunk_code_x", [_hit("n1", "chunk_code_x", 0.9)],
        searched=["chunk_code_x"],
    )

    resp = _post_answer({
        "query": "how is checkout wired?",
        "route": "search",
        "filename": "x.zip",
        "extra_context": "STRUCT: route -> controller",
        "preview_mode": "inline",
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "the answer"
    assert len(data["matches"]) == 1
    assert len(fake_graph.calls) == 1
    call = fake_graph.calls[0]
    assert call["collection_name"] == "chunk_code_x"
    assert call["graph_legs"] == ["calls", "has_knowledge"]
    # Per-collection plan calls disable hybrid_retrieve's internal sibling join
    # (the planner enumerates every collection explicitly — each is searched once).
    assert call["join_sibling"] is False
    # Structure text is never dropped when indexes are retrieved.
    assert "STRUCT: route -> controller" in data["context_preview"]
    # Aggregate trace carries the union of searched collections.
    project_trace = next(t for t in _hermetic if t.get("mode") == "project")
    assert project_trace["searched_collections"] == ["chunk_code_x"]


def test_knowledge_route_filename_searches_both_plan_collections(
        monkeypatch, fake_graph, _hermetic):
    _patch_plan(monkeypatch,
                _plan(["proj_knowledge_x", "chunk_code_x"], route="knowledge"))
    fake_graph.set_outcome(
        "proj_knowledge_x",
        [_hit("kn:route", "proj_knowledge_x", 0.95,
              code="POST /checkout -> PaymentController::pay")],
    )
    fake_graph.set_outcome(
        "chunk_code_x",
        [_hit("code:pay", "chunk_code_x", 0.9, code="function pay() {}")],
    )

    resp = _post_answer({
        "query": "how is checkout wired?",
        "route": "knowledge",
        "filename": "x.zip",
        "extra_context": "STRUCT: routes table",
        "preview_mode": "inline",
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "the answer"
    assert [c["collection_name"] for c in fake_graph.calls] == [
        "proj_knowledge_x", "chunk_code_x"
    ]
    assert all(c["graph_legs"] == ["calls", "has_knowledge"]
               for c in fake_graph.calls)
    # Each plan collection is searched exactly once (sibling join disabled — the
    # plan already enumerated the sibling as its own collection).
    assert all(c["join_sibling"] is False for c in fake_graph.calls)
    assert "STRUCT: routes table" in data["context_preview"]
    assert len(data["matches"]) == 2


def test_filename_plan_empty_skips_retrieval_honours_extra_context(
        monkeypatch, fake_graph):
    _patch_plan(monkeypatch, _plan([], route="search"))
    resp = _post_answer({
        "query": "anything?",
        "route": "search",
        "filename": "x.zip",
        "extra_context": "STRUCT: nothing persisted",
        "preview_mode": "inline",
    })

    assert resp.status_code == 200
    data = resp.json()
    assert fake_graph.calls == []
    assert data["matches"] == []
    assert "STRUCT: nothing persisted" in data["context_preview"]


# --- graph legs ---------------------------------------------------------------


def test_graph_legs_include_sym_when_plan_has_sym(monkeypatch, fake_graph):
    _patch_plan(monkeypatch, _plan(["chunk_code_x"], has_sym=True, stem="x"))
    fake_graph.set_outcome("chunk_code_x",
                           [_hit("n1", "chunk_code_x", 0.9)])
    resp = _post_answer({
        "query": "how is this wired?",
        "route": "search",
        "filename": "x.zip",
    })
    assert resp.status_code == 200
    assert fake_graph.calls[0]["graph_legs"] == [
        "calls", "has_knowledge", "sym"
    ]


def test_graph_legs_omit_sym_without_has_sym(monkeypatch, fake_graph):
    _patch_plan(monkeypatch, _plan(["chunk_code_x"], has_sym=False))
    fake_graph.set_outcome("chunk_code_x",
                           [_hit("n1", "chunk_code_x", 0.9)])
    _post_answer({"query": "q", "route": "search", "filename": "x.zip"})
    assert fake_graph.calls[0]["graph_legs"] == ["calls", "has_knowledge"]


def test_results_deduped_by_node_id_keep_best_score(monkeypatch, fake_graph):
    _patch_plan(monkeypatch,
                _plan(["chunk_code_x", "proj_knowledge_x"], route="search"))
    fake_graph.set_outcome(
        "chunk_code_x",
        [_hit("shared", "chunk_code_x", 0.7, code="low")],
    )
    fake_graph.set_outcome(
        "proj_knowledge_x",
        [_hit("shared", "proj_knowledge_x", 0.95, code="high")],
    )
    resp = _post_answer({
        "query": "q", "route": "search", "filename": "x.zip",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["matches"]) == 1
    assert data["matches"][0]["score"] == 0.95
    assert data["matches"][0]["chunk"]["id"] == "shared"
    assert fake_graph.calls[0]["collection_name"] == "chunk_code_x"
    assert fake_graph.calls[1]["collection_name"] == "proj_knowledge_x"


# --- chat keeps today's behavior ---------------------------------------------


def test_chat_with_filename_never_retrieves(fake_graph, _hermetic):
    resp = _post_answer({
        "query": "hi", "route": "chat", "filename": "x.zip",
    })
    assert resp.status_code == 200
    assert fake_graph.calls == []
    data = resp.json()
    assert data["answer"] == "the answer"
    assert data["matches"] == []
    assert next(t for t in _hermetic if t.get("mode") == "chat_no_index")


def test_chat_with_filename_keeps_extra_context(fake_graph):
    resp = _post_answer({
        "query": "hi", "route": "chat", "filename": "x.zip",
        "extra_context": "STRUCT: chat context",
        "preview_mode": "inline",
    })
    assert resp.status_code == 200
    assert fake_graph.calls == []
    assert "STRUCT: chat context" in resp.json()["context_preview"]


def test_legacy_explicit_context_early_return_still_active(fake_graph, _hermetic):
    """No filename/collection + extra_context → explicit short circuit."""
    resp = _post_answer({
        "query": "hi", "route": "knowledge", "extra_context": "STRUCT",
        "preview_mode": "inline",
    })
    assert resp.status_code == 200
    assert fake_graph.calls == []
    assert next(t for t in _hermetic if t.get("mode") == "explicit_context")
    assert "STRUCT" in resp.json()["context_preview"]


def test_legacy_chat_no_project_keeps_global_retrieval(fake_graph):
    """No filename/collection + no extra_context → today's global hybrid."""
    resp = _post_answer({"query": "hi", "route": "chat"})
    assert resp.status_code == 200
    assert len(fake_graph.calls) == 1
    call = fake_graph.calls[0]
    assert call["collection_name"] is None
    assert call["graph_legs"] is None


# --- streaming parity ---------------------------------------------------------


def test_answer_stream_project_retrieval_parity(monkeypatch, fake_graph):
    _patch_plan(monkeypatch, _plan(["chunk_code_x"]))
    fake_graph.set_outcome("chunk_code_x",
                           [_hit("n1", "chunk_code_x", 0.9)])
    resp, events = _post_stream({
        "query": "how is checkout wired?",
        "route": "search",
        "filename": "x.zip",
        "extra_context": "STRUCT: stream",
    })
    assert resp.status_code == 200
    assert len(fake_graph.calls) == 1
    assert fake_graph.calls[0]["graph_legs"] == ["calls", "has_knowledge"]
    types = [e["type"] for e in events]
    assert "error" not in types
    assert "delta" in types
    done = events[-1]
    assert done["type"] == "done"
    assert "world" in done["answer"]


# --- review findings ----------------------------------------------------------
# M1/M2 combined: two-collection plans collapse duplicate evidence and search
# each distinct collection exactly once.


def test_two_collection_plan_dedupes_node_and_sym_evidence(monkeypatch,
                                                           fake_graph):
    _patch_plan(monkeypatch,
                _plan(["chunk_code_x", "proj_knowledge_x"], route="search"))
    graph_key = "sym|method|App\\X::n|REFERENCES|App\\Y|f.php|7"
    fake_graph.set_outcome("chunk_code_x", [
        _hit("shared", "chunk_code_x", 0.7, code="low"),
        _sym_hit(graph_key, "chunk_code_x", 0.5),
    ])
    fake_graph.set_outcome("proj_knowledge_x", [
        _hit("shared", "proj_knowledge_x", 0.95, code="high"),
        _sym_hit(graph_key, "proj_knowledge_x", 0.6),
    ])

    resp = _post_answer({
        "query": "q", "route": "search", "filename": "x.zip",
    })
    assert resp.status_code == 200
    data = resp.json()
    ids = [m["chunk"]["id"] for m in data["matches"]]
    # Node-id duplicate + identical sym-evidence duplicate each collapse once.
    assert ids.count("shared") == 1
    sym_matches = [m for m in data["matches"] if m["chunk"]["id"] != "shared"]
    assert len(sym_matches) == 1
    # Best score is kept for the collapsed sym evidence.
    assert sym_matches[0]["score"] == 0.6
    # Exactly one per-collection retrieval (each distinct qdrant collection
    # searched once — the sibling join is disabled because the plan enumerated
    # both collections explicitly).
    assert [c["collection_name"] for c in fake_graph.calls] == [
        "chunk_code_x", "proj_knowledge_x"
    ]
    assert all(c["join_sibling"] is False for c in fake_graph.calls)


def test_project_trace_graph_available_true_when_graph_results(
        monkeypatch, fake_graph, _hermetic):
    _patch_plan(monkeypatch, _plan(["chunk_code_x"]))
    fake_graph.set_outcome("chunk_code_x", [
        _hit("n1", "chunk_code_x", 0.9),
        _sym_hit("sym|method|App\\X::n|REFERENCES|App\\Y|f.php|7",
                 "chunk_code_x", 0.5),
    ])
    resp = _post_answer({
        "query": "q", "route": "search", "filename": "x.zip",
    })
    assert resp.status_code == 200
    project_trace = next(t for t in _hermetic if t.get("mode") == "project")
    assert project_trace["graph_available"] is True


# --- M3: pinned collection on chat / unknown routes ---------------------------


def test_unknown_route_with_pinned_collection_retrieves_legacy(fake_graph):
    """Unknown route + pinned collection must never silently zero out."""
    fake_graph.set_outcome("chunk_code_x",
                           [_hit("n1", "chunk_code_x", 0.9)])
    resp = _post_answer({
        "query": "q", "route": "future_route", "collection": "chunk_code_x",
    })
    assert resp.status_code == 200
    assert len(fake_graph.calls) == 1
    call = fake_graph.calls[0]
    assert call["collection_name"] == "chunk_code_x"
    # Exact old behavior: one call, default legs, sibling join preserved.
    assert call["graph_legs"] is None
    assert call["join_sibling"] is True
    assert resp.json()["matches"][0]["chunk"]["id"] == "n1"


def test_pinned_collection_with_chat_route_retrieves_legacy(fake_graph):
    """Chat blocks NEW planned index use, but pinned retrieval is legacy."""
    fake_graph.set_outcome("chunk_code_x",
                           [_hit("n1", "chunk_code_x", 0.9)])
    resp = _post_answer({
        "query": "q", "route": "chat", "collection": "chunk_code_x",
    })
    assert resp.status_code == 200
    assert len(fake_graph.calls) == 1
    assert fake_graph.calls[0]["collection_name"] == "chunk_code_x"
    assert fake_graph.calls[0]["graph_legs"] is None
    assert resp.json()["matches"][0]["chunk"]["id"] == "n1"


def test_unknown_route_with_filename_uses_plan_discovery(monkeypatch,
                                                         fake_graph):
    _patch_plan(monkeypatch, _plan(["chunk_code_x"]))
    fake_graph.set_outcome("chunk_code_x",
                           [_hit("n1", "chunk_code_x", 0.9)])
    resp = _post_answer({
        "query": "q", "route": "future_route", "filename": "x.zip",
    })
    assert resp.status_code == 200
    assert len(fake_graph.calls) == 1
    call = fake_graph.calls[0]
    assert call["collection_name"] == "chunk_code_x"
    assert call["graph_legs"] == ["calls", "has_knowledge"]
    assert call["join_sibling"] is False


# --- m4: non-string extra_context stays ignored -------------------------------


def test_answer_context_drops_non_string_extra_context(fake_graph):
    """list/dict extra_context is coerced away (legacy) — never forwarded."""
    _, _, extra = main_module._answer_context(
        {"query": "q", "route": "chat", "extra_context": ["a", "b"]},
        "q",
        None,
    )
    assert extra is None
    assert len(fake_graph.calls) == 1
    assert fake_graph.calls[0]["collection_name"] is None


# --- m5: planner exception fallback -------------------------------------------


def test_index_plan_for_returns_off_dict_when_planner_raises(monkeypatch):
    import rag.context_plan as cplan

    def _boom(*, route, filename=None, collection=None):
        raise RuntimeError("planning boom")

    monkeypatch.setattr(cplan, "build_index_plan", _boom)
    plan = main_module._index_plan_for({"filename": "x.zip"}, "q", "search")
    assert plan["mode"] == "off"
    assert plan["route"] == "search"
    assert plan["collections"] == []
    assert plan["saved_kinds"] == []
    assert plan["has_sym"] is False
    assert plan["notes"] == ["plan error"]
