"""Phase-2 hybrid graph-leg tests.

``hybrid_retrieve`` keeps its vector + CALLS default and adds opt-in legs
(``has_knowledge`` / ``sym``). No daemon/Qdrant/Neo4j contact — a fake qdrant
and an in-memory fake graph store (structured_query matcher) drive the whole
merge/rank path deterministically.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import index.llamaindex_layer as lli

# --- fake stores ------------------------------------------------------------


class _Hit:
    def __init__(self, node_id, score, payload):
        self.id = node_id
        self.score = score
        self.payload = payload


class _Rec:
    def __init__(self, point_id, payload):
        self.id = point_id
        self.payload = payload


class FakeQdrant:
    """Tiny in-memory qdrant: collections, scoped points, search/retrieve."""

    def __init__(self):
        self.collections: set[str] = set()
        self._payloads: dict[tuple[str, str], dict] = {}
        self._hits: dict[str, list[_Hit]] = {}
        self.retrieve_calls: list[tuple[str, list]] = []
        self.search_error = False

    def add_point(self, collection, payload):
        stored = dict(payload)
        stored["collection"] = collection
        self.collections.add(collection)
        self._payloads[(collection, stored["node_id"])] = stored

    def set_hits(self, collection, node_scores):
        self._hits[collection] = [
            _Hit(node_id, float(score), dict(self._payloads[(collection, node_id)]))
            for node_id, score in node_scores
        ]

    def get_collections(self):
        return SimpleNamespace(
            collections=[
                SimpleNamespace(name=n) for n in sorted(self.collections)
            ]
        )

    def search(self, collection_name, query_vector, limit=5, query_filter=None):
        if self.search_error:
            raise RuntimeError("simulated qdrant search failure")
        return list(self._hits.get(collection_name, []))[:limit]

    def retrieve(self, collection_name, ids):
        self.retrieve_calls.append((collection_name, list(ids)))
        out = []
        for pid in ids:
            for (col, node_id), pl in self._payloads.items():
                if col == collection_name and lli._point_id_for(node_id) == str(pid):
                    out.append(_Rec(pid, dict(pl)))
                    break
        return out


class FakeStore:
    """In-memory graph store whose structured_query runs the read cyphers.

    Handles the three Phase-2 read cyphers (CALLS neighbours, HAS_KNOWLEDGE
    links, sym evidence) with the same collection/edge semantics as Neo4j.
    """

    def __init__(self):
        self.calls_edges: list[dict] = []
        self.knowledge_edges: list[tuple[str, str]] = []
        self.sym_nodes: dict[str, dict] = {}
        self.sym_edges: list[dict] = []
        self.fail = False

    def add_calls_edge(self, src, tgt, collection):
        self.calls_edges.append({"src": src, "tgt": tgt, "collection": collection})

    def add_knowledge_edge(self, chunk_id, knowledge_id):
        self.knowledge_edges.append((chunk_id, knowledge_id))

    def add_sym_node(self, kind, name, fqn, file="", line=None, collection="sym_A"):
        node_id = f"sym:{kind}:{fqn}"
        self.sym_nodes[node_id] = {
            "id": node_id, "kind": kind, "name": name, "fqn": fqn,
            "file": file, "line": line, "collection": collection,
        }
        return node_id

    def add_sym_edge(self, src, tgt, rel="REFERENCES", rel_kind="method_call",
                     file="", line=None, to_member=None):
        self.sym_edges.append({
            "src": src, "tgt": tgt, "rel": rel, "rel_kind": rel_kind,
            "file": file, "line": line, "to_member": to_member,
        })

    def structured_query(self, cypher, param_map=None):
        if self.fail:
            raise RuntimeError("neo4j down")
        p = param_map or {}
        if "HAS_KNOWLEDGE" in cypher:
            return self._q_has_knowledge(p)
        if "CALLS" in cypher:
            return self._q_calls(p)
        if ":Symbol" in cypher:
            return self._q_sym(p)
        return []

    def _q_calls(self, p):
        ids = set(p.get("ids") or [])
        collection = p.get("collection") or ""
        out = []
        for e in self.calls_edges:
            if e["src"] not in ids and e["tgt"] not in ids:
                continue
            # Edge is scoped out only when a collection is requested and the
            # edge names a different one; legacy (None) edges always match.
            if collection and e["collection"] is not None and e["collection"] != collection:
                continue
            src, tgt = (e["src"], e["tgt"]) if e["src"] in ids else (e["tgt"], e["src"])
            out.append({"src": src, "tgt": tgt, "rel": "CALLS", "collection": e["collection"]})
        return out

    def _q_has_knowledge(self, p):
        ids = set(p.get("ids") or [])
        out = []
        for chunk_id, knowledge_id in self.knowledge_edges:
            if chunk_id in ids:
                out.append({"src": chunk_id, "tgt": knowledge_id, "rel": "HAS_KNOWLEDGE"})
            elif knowledge_id in ids:
                out.append({"src": knowledge_id, "tgt": chunk_id, "rel": "HAS_KNOWLEDGE"})
        return out

    def _q_sym(self, p):
        candidates = set(p.get("candidates") or [])
        collection = p.get("collection") or ""
        cap = int(p.get("cap") or 12)
        anchors = {
            node_id
            for node_id, props in self.sym_nodes.items()
            if props.get("collection") == collection
            and (props.get("fqn") in candidates or props.get("name") in candidates)
        }
        out = []
        for e in self.sym_edges:
            src_anchor = e["src"] in anchors
            tgt_anchor = e["tgt"] in anchors
            if not (src_anchor or tgt_anchor):
                continue
            anchor = e["src"] if src_anchor else e["tgt"]
            other = e["tgt"] if src_anchor else e["src"]
            anchor_props = self.sym_nodes[anchor]
            other_props = self.sym_nodes[other]
            out.append({
                "src": anchor,
                "src_fqn": anchor_props.get("fqn", ""),
                "src_kind": anchor_props.get("kind", ""),
                "rel": e.get("rel"),
                "rel_kind": e.get("rel_kind"),
                "ref_file": e.get("file"),
                "ref_line": e.get("line"),
                "to_member": e.get("to_member"),
                "id": other,
                "kind": other_props.get("kind", ""),
                "name": other_props.get("name", ""),
                "fqn": other_props.get("fqn", ""),
                "file": other_props.get("file", ""),
                "line": other_props.get("line"),
                "end_line": other_props.get("end_line"),
                "owner": other_props.get("owner"),
            })
        return out[:cap]


# --- helpers ----------------------------------------------------------------


def _payload(node_id, *, name="", class_fqn="", code="", file="f.php", kind="method"):
    return {
        "node_id": node_id,
        "type": kind,
        "name": name,
        "method_name": name,
        "class": class_fqn.split("\\")[-1] if class_fqn else "",
        "class_fqn": class_fqn,
        "file": file,
        "start_line": 1,
        "end_line": 2,
        "code": code,
    }


def _retrieve(monkeypatch, qdrant, store, *, sym_exists=None, **kwargs):
    """Run hybrid_retrieve against fakes; returns (out, captured trace).

    ``sym_exists`` patches the guarded sym-collection probe (left untouched when
    None — callers may install their own guard or rely on leg short-circuiting).
    """
    traces = []

    monkeypatch.setattr(lli.CodePropertyGraph, "_get_qdrant", lambda self: qdrant)
    monkeypatch.setattr(
        lli.CodePropertyGraph, "_get_embed_model",
        lambda self: SimpleNamespace(get_query_embedding=lambda q: [0.1] * 4),
    )
    monkeypatch.setattr(lli.CodePropertyGraph, "_get_graph_store", lambda self: store)
    monkeypatch.setattr(lli, "record_retrieval_trace",
                        lambda trace_id, trace: traces.append(trace))
    if sym_exists is not None:
        monkeypatch.setattr(lli, "sym_collection_exists", lambda stem: sym_exists)
    out = lli.CodePropertyGraph().hybrid_retrieve(**kwargs)
    return out, (traces[-1] if traces else {})


def _by_node(results):
    return {
        (r.get("payload") or {}).get("node_id"): r
        for r in results
        if (r.get("payload") or {}).get("node_id")
    }


def test_hybrid_retrieve_distinguishes_missing_collection_and_search_failure(monkeypatch):
    store = FakeStore()
    missing, _ = _retrieve(
        monkeypatch, FakeQdrant(), store,
        query="search", collection_name="chunk_code_missing", graph_legs=[],
    )
    assert missing["mode"] == "no_results"
    assert missing["failure_code"] == "collection_missing"

    qdrant = FakeQdrant()
    qdrant.collections.add("chunk_code_present")
    qdrant.search_error = True
    failed, _ = _retrieve(
        monkeypatch, qdrant, store,
        query="search", collection_name="chunk_code_present", graph_legs=[],
    )
    assert failed["mode"] == "vector_only"
    assert failed["failure_code"] == "qdrant_search_failed"


# --- default = legacy CALLS only --------------------------------------------


class TestDefaultLegacy:
    def test_default_graph_legs_matches_calls_only(self, monkeypatch):
        store = FakeStore()
        src = _payload("code:Service", name="charge", class_fqn="App\\Service",
                       code="function charge() {}")
        nbr = _payload("code:Helper", name="helper", class_fqn="App\\Service",
                       code="function helper() {}")
        store.add_calls_edge("code:Service", "code:Helper", "chunk_code_A")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("chunk_code_A", nbr)
        qdrant.set_hits("chunk_code_A", [("code:Service", 0.9)])

        default_out, default_trace = _retrieve(
            monkeypatch, qdrant, store,
            query="charge?", collection_name="chunk_code_A", graph_legs=None,
        )
        explicit_out, _ = _retrieve(
            monkeypatch, qdrant, store,
            query="charge?", collection_name="chunk_code_A", graph_legs=["calls"],
        )

        assert default_out == explicit_out
        assert default_trace["legs"] == ["calls"]
        by_node = _by_node(default_out["results"])
        assert by_node["code:Helper"]["via_graph"] is True
        assert by_node["code:Helper"]["graph_leg"] == "calls"
        # graph neighbours carry a stable graph_key identity (merge key when a
        # payload has no qdrant node_id).
        assert by_node["code:Helper"]["payload"]["graph_key"] == (
            "calls|chunk_code_A|code:Helper"
        )
        # graph neighbours rank just below the weakest vector hit
        assert by_node["code:Helper"]["score"] == 0.89

    def test_default_does_not_run_opt_in_legs(self, monkeypatch):
        store = FakeStore()
        src = _payload("code:Service", name="charge", class_fqn="App\\Service", code="x")
        store.add_knowledge_edge("code:Service", "kn:route:checkout")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("proj_knowledge_A", _payload(
            "kn:route:checkout", name="route:checkout", file="routes.php"))
        qdrant.set_hits("chunk_code_A", [("code:Service", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="charge?", collection_name="chunk_code_A",
        )
        by_node = _by_node(out["results"])
        assert "kn:route:checkout" not in by_node
        assert "has_knowledge_count" not in trace
        assert "sym_count" not in trace

    def test_unknown_leg_name_fails_fast(self, monkeypatch):
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", _payload("code:S"))
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])
        try:
            _retrieve(
                monkeypatch, qdrant, FakeStore(),
                query="q", collection_name="chunk_code_A", graph_legs=["bogus"],
            )
        except ValueError as exc:
            assert "bogus" in str(exc)
        else:
            raise AssertionError("expected ValueError for unknown graph leg")


# --- scoped CALLS ------------------------------------------------------------


class TestScopedCalls:
    def test_same_collection_edge_returned(self, monkeypatch):
        store = FakeStore()
        src = _payload("code:S", name="charge", class_fqn="App\\S", code="a")
        nbr = _payload("code:T", name="target", class_fqn="App\\S", code="b")
        store.add_calls_edge("code:S", "code:T", "chunk_code_A")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("chunk_code_A", nbr)
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])

        out, _ = _retrieve(
            monkeypatch, qdrant, store,
            query="q", collection_name="chunk_code_A",
        )
        assert "code:T" in _by_node(out["results"])

    def test_cross_collection_edge_ignored(self, monkeypatch):
        # Same FQN indexed in two projects (shared node id); the pinned project
        # must NOT see edges owned by the other project's collection.
        store = FakeStore()
        src = _payload("code:S", name="charge", class_fqn="App\\S", code="a")
        foreign = _payload("code:X", name="other", class_fqn="App\\S", code="c")
        store.add_calls_edge("code:S", "code:X", "chunk_code_B")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("chunk_code_B", src)
        qdrant.add_point("chunk_code_B", foreign)
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="q", collection_name="chunk_code_A",
        )
        assert "code:X" not in _by_node(out["results"])
        assert trace["graph_neighbor_count"] == 0

    def test_legacy_collectionless_edge_still_returned(self, monkeypatch):
        store = FakeStore()
        src = _payload("code:S", name="charge", class_fqn="App\\S", code="a")
        legacy = _payload("code:L", name="legacy", class_fqn="App\\S", code="b")
        store.add_calls_edge("code:S", "code:L", None)
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("chunk_code_A", legacy)
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])

        out, _ = _retrieve(
            monkeypatch, qdrant, store,
            query="q", collection_name="chunk_code_A",
        )
        assert "code:L" in _by_node(out["results"])


# --- sibling join control -----------------------------------------------------
#
# hybrid_retrieve auto-joins a chunk_code_<stem> with its proj_knowledge_<stem>
# sibling by default (legacy single-call callers). join_sibling=False restricts
# the search to the pinned collection so an index-aware caller that enumerated
# every collection explicitly searches each one exactly once.


class TestJoinSibling:
    def _world(self):
        store = FakeStore()
        src = _payload("code:S", name="charge", class_fqn="App\\S", code="a")
        kn = _payload("kn:route:checkout", name="route:checkout",
                      file="routes.php", class_fqn="kn:route:A")
        store.add_knowledge_edge("code:S", "kn:route:checkout")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("proj_knowledge_A", kn)
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])
        qdrant.set_hits("proj_knowledge_A", [("kn:route:checkout", 0.8)])
        return store, qdrant

    def test_default_joins_knowledge_sibling(self, monkeypatch):
        store, qdrant = self._world()
        out, _ = _retrieve(
            monkeypatch, qdrant, store,
            query="checkout?", collection_name="chunk_code_A",
            graph_legs=["calls", "has_knowledge"],
        )
        assert out["searched_collections"] == [
            "chunk_code_A", "proj_knowledge_A"
        ]
        assert "kn:route:checkout" in _by_node(out["results"])

    def test_join_sibling_false_searches_only_the_pinned_collection(
            self, monkeypatch):
        store, qdrant = self._world()
        out, _ = _retrieve(
            monkeypatch, qdrant, store,
            query="checkout?", collection_name="chunk_code_A",
            graph_legs=["calls", "has_knowledge"],
            join_sibling=False,
        )
        # One distinct vector collection searched — the sibling is searched by
        # its own separate plan call instead of being double-scanned here.
        assert out["searched_collections"] == ["chunk_code_A"]
        # The sibling's knowledge point is still reachable through the
        # HAS_KNOWLEDGE leg (graph rehydration) — but only as a via_graph row,
        # never as a duplicate vector hit from a second vector search.
        kn_rows = [
            r for r in out["results"]
            if (r.get("payload") or {}).get("node_id") == "kn:route:checkout"
        ]
        assert len(kn_rows) == 1
        assert kn_rows[0]["via_graph"] is True


# --- has_knowledge leg -------------------------------------------------------


class TestHasKnowledgeLeg:
    def test_code_hit_links_knowledge_payload(self, monkeypatch):
        store = FakeStore()
        src = _payload("code:Service", name="charge", class_fqn="App\\Service", code="a")
        kn = _payload("kn:route:checkout", name="route:checkout",
                      file="routes.php", class_fqn="kn:route:A")
        store.add_knowledge_edge("code:Service", "kn:route:checkout")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("proj_knowledge_A", kn)
        qdrant.set_hits("chunk_code_A", [("code:Service", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="checkout wiring?", collection_name="chunk_code_A",
            graph_legs=["has_knowledge"],
        )
        result = _by_node(out["results"])["kn:route:checkout"]
        assert result["via_graph"] is True
        assert result["graph_leg"] == "has_knowledge"
        assert result["collection"] == "proj_knowledge_A"
        assert result["payload"]["name"] == "route:checkout"
        assert trace["has_knowledge_count"] == 1

    def test_knowledge_hit_reverse_links_code(self, monkeypatch):
        store = FakeStore()
        kn = _payload("kn:route:checkout", name="route:checkout",
                      file="routes.php", class_fqn="kn:route:A")
        src = _payload("code:Service", name="charge", class_fqn="App\\Service", code="a")
        store.add_knowledge_edge("code:Service", "kn:route:checkout")
        qdrant = FakeQdrant()
        qdrant.add_point("proj_knowledge_A", kn)
        qdrant.add_point("chunk_code_A", src)
        qdrant.set_hits("proj_knowledge_A", [("kn:route:checkout", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="checkout wiring?", collection_name="proj_knowledge_A",
            graph_legs=["has_knowledge"],
        )
        result = _by_node(out["results"])["code:Service"]
        assert result["via_graph"] is True
        assert result["graph_leg"] == "has_knowledge"
        assert result["collection"] == "chunk_code_A"
        assert trace["has_knowledge_count"] == 1

    def test_code_without_knowledge_sibling_skips_leg(self, monkeypatch):
        store = FakeStore()
        src = _payload("code:S", name="charge", class_fqn="App\\S", code="a")
        store.add_knowledge_edge("code:S", "kn:x")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="q", collection_name="chunk_code_A",
            graph_legs=["has_knowledge"],
        )
        assert "kn:x" not in _by_node(out["results"])
        assert "has_knowledge_count" not in trace


# --- sym leg -----------------------------------------------------------------


class TestSymLeg:
    def _sym_world(self):
        store = FakeStore()
        store.add_sym_node("class", "Invoice", "App\\Pay\\Invoice",
                           file="/p/Invoice.php", line=1)
        invoice = "sym:class:App\\Pay\\Invoice"
        payable = store.add_sym_node("interface", "Payable", "App\\Contracts\\Payable",
                                     file="/p/Payable.php", line=2)
        charge = store.add_sym_node("method", "charge", "App\\Svc\\Payment::charge",
                                    file="/p/PaymentService.php", line=5)
        store.add_sym_edge(invoice, payable, rel="DECLARES", rel_kind="implements")
        store.add_sym_edge(charge, invoice, rel="REFERENCES", rel_kind="new",
                           file="/p/PaymentService.php", line=7)
        return store

    def test_sym_leg_returns_declares_and_references(self, monkeypatch):
        store = self._sym_world()
        hit = _payload("code:Invoice", name="Invoice", class_fqn="App\\Pay\\Invoice",
                       file="/p/Invoice.php", code="class Invoice {}")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", hit)
        qdrant.set_hits("chunk_code_A", [("code:Invoice", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="invoice?", collection_name="chunk_code_A", graph_legs=["sym"],
            sym_exists=True,
        )
        sym_results = [r for r in out["results"] if r.get("graph_leg") == "sym"]
        assert len(sym_results) == 2
        assert trace["sym_count"] == 2
        for result in sym_results:
            assert result["via_graph"] is True
            payload = result["payload"]
            assert payload["graph_sym"] is True
            assert payload["via_graph"] is True
            assert "code" not in payload
            assert payload["collection"] == "sym_A"
            assert payload["fqn"] and payload["file"] is not None
            # Sym rows are Neo4j-only (no node_id); the graph_key is the stable
            # cross-call identity the answer-path merge dedupes on.
            assert payload["graph_key"].startswith("sym|")
            assert "|App\\" in payload["graph_key"]
        # Neo4j-only leg: no qdrant point rehydration happens.
        assert qdrant.retrieve_calls == []

    def test_sym_leg_caps_evidence(self, monkeypatch):
        store = FakeStore()
        store.add_sym_node("class", "Bus", "App\\Bus", file="/p/Bus.php", line=1)
        bus = "sym:class:App\\Bus"
        for i in range(15):
            target = store.add_sym_node("method", f"m{i}", f"App\\Bus::m{i}",
                                        file="/p/Bus.php", line=10 + i)
            store.add_sym_edge(bus, target, rel="REFERENCES", rel_kind="method_call")
        hit = _payload("code:Bus", name="Bus", class_fqn="App\\Bus", code="class Bus {}")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", hit)
        qdrant.set_hits("chunk_code_A", [("code:Bus", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="bus?", collection_name="chunk_code_A", graph_legs=["sym"],
            sym_exists=True,
        )
        sym_results = [r for r in out["results"] if r.get("graph_leg") == "sym"]
        assert trace["sym_count"] <= 12
        assert len(sym_results) <= 12

    def test_sym_leg_requires_existing_sym_collection(self, monkeypatch):
        store = FakeStore()
        hit = _payload("code:Invoice", name="Invoice", class_fqn="App\\Pay\\Invoice", code="x")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", hit)
        qdrant.set_hits("chunk_code_A", [("code:Invoice", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="invoice?", collection_name="chunk_code_A", graph_legs=["sym"],
            sym_exists=False,
        )
        assert all(r.get("graph_leg") != "sym" for r in out["results"])
        assert "sym_count" not in trace

    def test_sym_leg_no_candidates_returns_nothing(self, monkeypatch):
        store = FakeStore()
        store.add_sym_node("class", "Other", "App\\Other", file="/p/O.php", line=1)
        hit = _payload("code:Invoice", name="Invoice", class_fqn="App\\Pay\\Invoice", code="x")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", hit)
        qdrant.set_hits("chunk_code_A", [("code:Invoice", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="invoice?", collection_name="chunk_code_A", graph_legs=["sym"],
            sym_exists=True,
        )
        assert all(r.get("graph_leg") != "sym" for r in out["results"])
        assert trace["sym_count"] == 0


# --- merge / ranking / guards ------------------------------------------------


class TestMergeRanking:
    def test_same_node_from_two_legs_deduped(self, monkeypatch):
        store = FakeStore()
        src = _payload("code:S", name="charge", class_fqn="App\\S", code="a")
        shared = _payload("code:K", name="payment", class_fqn="App\\S", code="b")
        store.add_calls_edge("code:S", "code:K", "chunk_code_A")
        # Same linked node reachable through both the CALLS and knowledge legs.
        store.add_knowledge_edge("code:S", "code:K")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("chunk_code_A", shared)
        qdrant.add_point("proj_knowledge_A", dict(shared))
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="q", collection_name="chunk_code_A",
            graph_legs=["calls", "has_knowledge"],
        )
        by_node = _by_node(out["results"])
        # Exactly one result for the shared node id.
        assert [r for r in out["results"] if (r.get("payload") or {}).get("node_id") == "code:K"] == [by_node["code:K"]]
        assert by_node["code:K"]["graph_leg"] == "calls"
        # The knowledge leg found nothing new — nothing was double-appended.
        assert trace["has_knowledge_count"] == 0

    def test_graph_and_sym_results_rank_below_vectors(self, monkeypatch):
        store = FakeStore()
        src = _payload("code:Bus", name="Bus", class_fqn="App\\Bus", code="class Bus {}")
        nbr = _payload("code:Wheel", name="Wheel", class_fqn="App\\Bus", code="b")
        store.add_calls_edge("code:Bus", "code:Wheel", "chunk_code_A")
        store.add_sym_node("class", "Bus", "App\\Bus", file="/p/Bus.php", line=1)
        bus = "sym:class:App\\Bus"
        store.add_sym_node("interface", "Drivable", "App\\Contracts\\Drivable",
                           file="/p/D.php", line=2)
        drivable = "sym:interface:App\\Contracts\\Drivable"
        store.add_sym_edge(bus, drivable, rel="DECLARES", rel_kind="implements")
        qdrant = FakeQdrant()
        qdrant.add_point("chunk_code_A", src)
        qdrant.add_point("chunk_code_A", nbr)
        qdrant.set_hits("chunk_code_A", [("code:Bus", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="bus?", collection_name="chunk_code_A",
            graph_legs=["calls", "sym"], sym_exists=True,
        )
        graph_results = [r for r in out["results"] if r.get("via_graph")]
        vector_results = [r for r in out["results"] if not r.get("via_graph")]
        assert vector_results and all(r["score"] > 0.8 for r in vector_results)
        assert graph_results and all(r["score"] < 0.9 for r in graph_results)
        # Sorted results lead with vectors, then graph evidence.
        assert out["results"][0]["via_graph"] is False
        assert trace["graph_context_count"] >= 2


class TestGuards:
    def test_store_absent_keeps_vector_only(self, monkeypatch):
        qdrant = FakeQdrant()
        hit = _payload("code:S", name="charge", class_fqn="App\\S", code="a")
        qdrant.add_point("chunk_code_A", hit)
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])

        def _fail_if_called(stem):
            raise AssertionError("sym probe must not run without a store")

        monkeypatch.setattr(lli, "sym_collection_exists", _fail_if_called)
        out, trace = _retrieve(
            monkeypatch, qdrant, None,
            query="q", collection_name="chunk_code_A",
            graph_legs=["calls", "has_knowledge", "sym"],
        )
        assert out["results"] and all(not r.get("via_graph") for r in out["results"])
        assert trace["graph_fallback"] is True
        assert "legs" not in trace

    def test_store_raises_degrades_to_vector_only(self, monkeypatch):
        store = FakeStore()
        store.fail = True
        qdrant = FakeQdrant()
        hit = _payload("code:S", name="charge", class_fqn="App\\S", code="a")
        qdrant.add_point("chunk_code_A", hit)
        qdrant.set_hits("chunk_code_A", [("code:S", 0.9)])

        out, trace = _retrieve(
            monkeypatch, qdrant, store,
            query="q", collection_name="chunk_code_A",
        )
        assert out["results"] and all(not r.get("via_graph") for r in out["results"])
        assert trace["graph_neighbor_count"] == 0

    def test_signature_keeps_legacy_callers_and_optional_kwarg(self):
        params = inspect.signature(lli.CodePropertyGraph.hybrid_retrieve).parameters
        assert "graph_legs" in params
        assert params["graph_legs"].default is None
        assert "join_sibling" in params
        assert params["join_sibling"].default is True
        # Existing keyword call shape stays valid (positional params unchanged).
        assert list(params)[:5] == ["self", "query", "collection_name", "limit", "class_name"]

    def test_module_default_legs_stay_calls_only(self):
        assert lli._DEFAULT_GRAPH_LEGS == ["calls"]
