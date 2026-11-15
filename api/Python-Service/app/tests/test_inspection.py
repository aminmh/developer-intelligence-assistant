"""Tests for the inspection service package.

The PHP exporter is never invoked — ``inspection.cli`` functions are
monkeypatched with fixture payloads; Neo4j calls are absorbed by a fake graph
recording upserts. Queries run over the fake graph's in-memory rows so the
whole Tier-2..4 mapping logic is exercised deterministically.
"""

from __future__ import annotations

import inspection.cli as php_cli
from inspection.graph import node_key
from inspection.service import InspectionService, sym_collection_for


def _fixture_defs() -> list[dict]:
    return [
        {
            "kind": "interface", "name": "Payable", "fqn": "App\\Contracts\\Payable",
            "file": "/p/src/Contracts/Payable.php", "line": 3, "end_line": 3,
            "visibility": None, "static": None, "abstract": None, "owner": None,
            "relations": [],
        },
        {
            "kind": "method", "name": "pay", "fqn": "App\\Contracts\\Payable::pay",
            "file": "/p/src/Contracts/Payable.php", "line": 3, "end_line": 3,
            "visibility": "public", "static": False, "abstract": False,
            "owner": "App\\Contracts\\Payable", "relations": None,
        },
        {
            "kind": "class", "name": "Invoice", "fqn": "App\\Models\\Invoice",
            "file": "/p/src/Models/Invoice.php", "line": 5, "end_line": 9,
            "visibility": None, "static": None, "abstract": None, "owner": None,
            "relations": [{"kind": "implements", "target": "App\\Contracts\\Payable"}],
        },
        {
            "kind": "method", "name": "__construct", "fqn": "App\\Models\\Invoice::__construct",
            "file": "/p/src/Models/Invoice.php", "line": 6, "end_line": 6,
            "visibility": "public", "static": False, "abstract": False,
            "owner": "App\\Models\\Invoice", "relations": None,
        },
        {
            "kind": "method", "name": "pay", "fqn": "App\\Models\\Invoice::pay",
            "file": "/p/src/Models/Invoice.php", "line": 7, "end_line": 7,
            "visibility": "public", "static": False, "abstract": False,
            "owner": "App\\Models\\Invoice", "relations": None,
        },
        {
            "kind": "class", "name": "PaymentService", "fqn": "App\\Services\\PaymentService",
            "file": "/p/src/Services/PaymentService.php", "line": 4, "end_line": 12,
            "visibility": None, "static": None, "abstract": None, "owner": None,
            "relations": [],
        },
        {
            "kind": "method", "name": "charge", "fqn": "App\\Services\\PaymentService::charge",
            "file": "/p/src/Services/PaymentService.php", "line": 7, "end_line": 12,
            "visibility": "public", "static": False, "abstract": False,
            "owner": "App\\Services\\PaymentService", "relations": None,
        },
        {
            "kind": "method", "name": "log", "fqn": "App\\Services\\PaymentService::log",
            "file": "/p/src/Services/PaymentService.php", "line": 14, "end_line": 15,
            "visibility": "private", "static": True, "abstract": False,
            "owner": "App\\Services\\PaymentService", "relations": None,
        },
    ]


def _fixture_refs() -> list[dict]:
    return [
        {"kind": "new", "file": "/p/src/Services/PaymentService.php", "line": 8,
         "scope": "App\\Services\\PaymentService::charge", "to_fqn": "App\\Models\\Invoice",
         "to_member": None, "unresolved": False},
        {"kind": "static_call", "file": "/p/src/Services/PaymentService.php", "line": 9,
         "scope": "App\\Services\\PaymentService::charge", "to_fqn": "App\\Services\\PaymentService",
         "to_member": "log", "unresolved": False},
        # unresolved: target method exists but the receiver class is unknown
        {"kind": "method_call", "file": "/p/src/Services/PaymentService.php", "line": 10,
         "scope": "App\\Services\\PaymentService::charge", "to_fqn": None,
         "to_member": "pay", "unresolved": True},
    ]


class FakeGraph:
    """In-memory stand-in for InspectionGraph.

    Writer methods record upserts; ``_get_store()`` returns a fake store whose
    ``structured_query`` interprets the four read cyphers over the recorded
    nodes/edges so SymbolGraphQuery exercises its full mapping logic.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.relations: list[tuple[str, str, str]] = []
        self.links: list[tuple[str, str, dict]] = []
        self.available = True

    def upsert_nodes(self, rows: list[dict]) -> int:
        for r in rows:
            self.nodes[r["id"]] = r["properties"]
        return len(rows)

    def upsert_declaration_relations(self, rows: list[dict]) -> int:
        for r in rows:
            if r["source_id"] in self.nodes and r["target_id"] in self.nodes:
                self.relations.append((r["source_id"], r["target_id"], r["kind"]))
        return len(self.relations)

    def upsert_references(self, rows: list[dict]) -> int:
        for r in rows:
            if r["source_id"] in self.nodes and r["target_id"] in self.nodes:
                self.links.append((r["source_id"], r["target_id"], r))
        return len(self.links)

    def existing_ids(self, ids: list[str]) -> set[str]:
        return {i for i in ids if i in self.nodes}

    def _get_store(self) -> _FakeStore:
        return _FakeStore(self)


class _FakeStore:
    """Structured-query stand-in executing the SymbolGraphQuery cyphers."""

    def __init__(self, fake: FakeGraph) -> None:
        self._fake = fake

    def structured_query(self, cypher: str, param_map: dict | None = None) -> list[dict]:
        p = param_map or {}
        collection = p.get("collection", "")
        nodes = {i: d for i, d in self._fake.nodes.items() if d.get("collection") == collection}

        def rows(matches: list[str]) -> list[dict]:
            return [{"n": self._fake.nodes[i]} for i in matches]

        if "fqn: $fqn" in cypher:
            return rows([i for i, d in nodes.items() if d.get("fqn") == p.get("fqn")])
        if "name: $name" in cypher:
            return rows([i for i, d in nodes.items() if d.get("name") == p.get("name")])
        target = p.get("target_id")
        if "DECLARES" in cypher:
            kinds = {"implements", "extends"}
            impl = [
                s for (s, t, k) in self._fake.relations if t == target and k in kinds
            ]
            return rows(impl)
        if "caller" in cypher and "REFERENCES" in cypher:
            out = []
            for s, t, rel in self._fake.links:
                if t == target:
                    out.append(
                        {"n": self._fake.nodes[s],
                         "rkind": rel.get("kind"),
                         "file": rel.get("file"),
                         "line": rel.get("line"),
                         "to_member": rel.get("to_member")}
                    )
            return out
        if "[r:REFERENCES]->(t" in cypher:
            out = []
            for s, t, rel in self._fake.links:
                if s == p.get("source_id"):
                    out.append(
                        {"n": self._fake.nodes[t],
                         "rkind": rel.get("kind"),
                         "file": rel.get("file"),
                         "line": rel.get("line"),
                         "to_member": rel.get("to_member")}
                    )
            return out
        if "<-[r:REFERENCES]-(c" in cypher:
            out = []
            for s, t, rel in self._fake.links:
                if t == p.get("source_id"):
                    out.append(
                        {"n": self._fake.nodes[s],
                         "rkind": rel.get("kind"),
                         "file": rel.get("file"),
                         "line": rel.get("line"),
                         "to_member": rel.get("to_member")}
                    )
            return out
        return []


def _service(monkeypatch, fake: FakeGraph) -> InspectionService:
    svc = InspectionService(graph=fake)  # type: ignore[arg-type]
    monkeypatch.setattr(php_cli, "inspect_symbols", lambda _d, _e=None: _fixture_defs())
    monkeypatch.setattr(php_cli, "inspect_references", lambda _d, _e=None: _fixture_refs())
    return svc


def _index(monkeypatch, fake: FakeGraph | None = None) -> tuple[InspectionService, FakeGraph]:
    fake = fake or FakeGraph()
    svc = _service(monkeypatch, fake)
    counts = svc.index("/p", "Shop")
    return svc, fake, counts


class TestServiceIndex:
    def test_collection_naming(self):
        assert sym_collection_for("Archive.zip") == "sym_Archive_zip"
        assert sym_collection_for("") == "sym_unnamed"

    def test_index_counts_and_graph_shape(self, monkeypatch):
        _svc, fake, counts = _index(monkeypatch)
        assert counts["collection"] == "sym_Shop"
        assert counts["indexed"] is True
        assert counts["definitions"] == 8
        assert len(fake.nodes) == 8
        # implements edge materialized (Invoice -> Payable both present)
        assert counts["relations"] == 1
        # 2 of 3 refs linkable (method_call to unknown receiver skipped)
        assert counts["links"] == 2
        assert counts["unlinked"] == 1

    def test_snapshot_index_skips_ownerless_migration_methods(self, monkeypatch):
        from telemetry.snapshot import Snapshot

        fake = FakeGraph()
        _service(monkeypatch, fake)
        definitions = _fixture_defs() + [
            {
                "kind": "class", "name": "", "fqn": "",
                "file": "/p/database/migrations/2025_01_01_create_orders.php",
                "line": 8, "end_line": 30,
            },
            {
                "kind": "method", "name": "up", "fqn": "::up",
                "file": "/p/database/migrations/2025_01_01_create_orders.php",
                "line": 12, "end_line": 18, "owner": "", "relations": [],
            },
            {
                "kind": "method", "name": "down", "fqn": "::down",
                "file": "/p/database/migrations/2025_01_01_create_orders.php",
                "line": 20, "end_line": 24, "owner": "", "relations": [],
            },
        ]
        snapshot = Snapshot("pizza", "a" * 40, "b" * 64)

        counts = InspectionService(graph=fake).index(
            "/p", "pizza", exported={"definitions": definitions, "references": []},
            snapshot=snapshot,
        )

        assert counts["indexed"] is True
        assert counts["nodes"] == 8
        assert len(fake.nodes) == 8
        assert all("symbol_identity_error" not in node for node in fake.nodes.values())

    def test_def_keys_disjoint_from_chunk_ids(self):
        key = node_key("method", "App\\Services\\PaymentService::charge", "charge")
        assert len(key) == 40  # sha1 hex

    def test_index_when_graph_down(self, monkeypatch):
        class Dead(FakeGraph):
            def upsert_nodes(self, rows):
                return 0

        fake = Dead()
        _service(monkeypatch, fake)
        counts = fake and _service(monkeypatch, fake).index("/p", "Shop")
        assert counts["indexed"] is False


class TestResolveAndRelations:
    def test_resolve_by_fqn_prefers_class(self, monkeypatch):
        svc, _fake = _index(monkeypatch)[:2]
        rows = svc.resolve_symbol("sym_Shop", "App\\Contracts\\Payable")
        assert rows and rows[0]["kind"] == "interface"
        assert rows[0]["name"] == "Payable"

    def test_resolve_method_symbol(self, monkeypatch):
        svc, _ = _index(monkeypatch)[:2]
        rows = svc.resolve_symbol("sym_Shop", "App\\Services\\PaymentService::charge")
        assert rows and rows[0]["kind"] == "method"

    def test_find_implementations(self, monkeypatch):
        svc, _ = _index(monkeypatch)[:2]
        impl = svc.find_implementations("sym_Shop", "App\\Contracts\\Payable")
        assert [i["fqn"] for i in impl] == ["App\\Models\\Invoice"]

    def test_find_references_returns_callers(self, monkeypatch):
        svc, _ = _index(monkeypatch)[:2]
        refs = svc.find_references("sym_Shop", "App\\Services\\PaymentService::log")
        assert len(refs) == 1
        assert refs[0]["fqn"] == "App\\Services\\PaymentService::charge"
        assert refs[0]["ref"]["kind"] == "static_call"
        assert refs[0]["ref"]["line"] == 9

    def test_unknown_symbol_empty(self, monkeypatch):
        svc, _ = _index(monkeypatch)[:2]
        assert svc.resolve_symbol("sym_Shop", "Nope\\Missing") == []
        assert svc.find_implementations("sym_Shop", "Nope\\Missing") == []


class TestCallHierarchy:
    def test_charge_callees_and_callers(self, monkeypatch):
        svc, _ = _index(monkeypatch)[:2]
        hier = svc.call_hierarchy("sym_Shop", "App\\Services\\PaymentService::charge")
        sorted(c["fqn"] for c in hier["callees"])
        # new Invoice(...) links to the class node? No — links materialize
        # method→def; new targets class def, so charge callees = class node
        assert hier["callers"] == []
        assert hier["callees"]  # Invoice class (new-site) is a callee node

    def test_log_has_caller(self, monkeypatch):
        svc, _ = _index(monkeypatch)[:2]
        hier = svc.call_hierarchy("sym_Shop", "App\\Services\\PaymentService::log")
        assert [c["fqn"] for c in hier["callers"]] == [
            "App\\Services\\PaymentService::charge"
        ]


class TestTierReads:
    def test_method_and_skeleton_delegate(self, monkeypatch):
        captured = {}

        def fake_method(file, name):
            captured["method"] = (file, name)
            return {"name": name, "source": "<?php …"}

        monkeypatch.setattr(php_cli, "inspect_method", fake_method)
        svc = InspectionService(graph=FakeGraph())  # type: ignore[arg-type]
        assert svc.method("/p/A.php", "charge") == {"name": "charge", "source": "<?php …"}


def _typed_fixture_defs() -> list[dict]:
    """Typed-receiver world: User::pay + Order::run linkable via method_call."""
    return [
        {"kind": "class", "name": "User", "fqn": "App\\Models\\User", "file": "/p/User.php",
         "line": 1, "end_line": 5, "visibility": None, "static": None, "abstract": None,
         "owner": None, "relations": []},
        {"kind": "method", "name": "pay", "fqn": "App\\Models\\User::pay", "file": "/p/User.php",
         "line": 3, "end_line": 3, "visibility": "public", "static": False, "abstract": False,
         "owner": "App\\Models\\User", "relations": None},
        {"kind": "class", "name": "Order", "fqn": "App\\Services\\Order", "file": "/p/Order.php",
         "line": 1, "end_line": 9, "visibility": None, "static": None, "abstract": None,
         "owner": None, "relations": []},
        {"kind": "method", "name": "run", "fqn": "App\\Services\\Order::run", "file": "/p/Order.php",
         "line": 4, "end_line": 8, "visibility": "public", "static": False, "abstract": False,
         "owner": "App\\Services\\Order", "relations": None},
    ]


def _typed_fixture_refs() -> list[dict]:
    return [
        # Receiver type known from a typed param → link + via recorded.
        {"kind": "method_call", "file": "/p/Order.php", "line": 5,
         "scope": "App\\Services\\Order::run", "to_fqn": "App\\Models\\User",
         "to_member": "pay", "unresolved": False, "via": "param"},
        # Receiver type known but the method is inherited → honest unlink.
        {"kind": "method_call", "file": "/p/Order.php", "line": 6,
         "scope": "App\\Services\\Order::run", "to_fqn": "App\\Models\\User",
         "to_member": "save", "unresolved": False, "via": "param"},
        # Receiver type unknown → unresolved stays unlinked.
        {"kind": "method_call", "file": "/p/Order.php", "line": 7,
         "scope": "App\\Services\\Order::run", "to_fqn": None,
         "to_member": "magic", "unresolved": True},
    ]


class TestTypedReceiverLinking:
    """method_call edges now carry a resolved receiver (via) when inferable."""

    def test_links_typed_method_call_and_tracks_via(self, monkeypatch):
        svc = InspectionService(graph=FakeGraph())  # type: ignore[arg-type]
        monkeypatch.setattr(php_cli, "inspect_symbols", lambda _d, _e=None: _typed_fixture_defs())
        monkeypatch.setattr(php_cli, "inspect_references", lambda _d, _e=None: _typed_fixture_refs())
        counts = svc.index("/p", "Typed")

        assert counts["links"] == 1  # pay() links; inherited save() and magic() do not
        assert counts["unlinked"] == 2
        assert counts["via"] == {"param": 1}

    def test_exclude_forwarded_to_exporter(self, monkeypatch):
        svc = InspectionService(graph=FakeGraph())  # type: ignore[arg-type]
        seen: dict = {}

        def syms(_d, exclude=None):
            seen["symbols"] = exclude
            return _typed_fixture_defs()

        def refs(_d, exclude=None):
            seen["references"] = exclude
            return _typed_fixture_refs()

        monkeypatch.setattr(php_cli, "inspect_symbols", syms)
        monkeypatch.setattr(php_cli, "inspect_references", refs)
        svc.index("/p", "Typed", exclude=["tests", "coverage"])
        assert seen["symbols"] == ["tests", "coverage"]
        assert seen["references"] == ["tests", "coverage"]

    def test_index_without_exclude_passes_none(self, monkeypatch):
        svc = InspectionService(graph=FakeGraph())  # type: ignore[arg-type]
        monkeypatch.setattr(php_cli, "inspect_symbols", lambda _d, _e=None: _typed_fixture_defs())
        monkeypatch.setattr(php_cli, "inspect_references", lambda _d, _e=None: _typed_fixture_refs())
        counts = svc.index("/p", "Typed")
        assert counts["links"] == 1
