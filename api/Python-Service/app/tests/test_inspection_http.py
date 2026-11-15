"""Endpoint + auto-inspection tests (pure functions; no FastAPI app needed)."""

from __future__ import annotations

from inspection import auto as auto_mod
from inspection import http as http_mod
from inspection.http import (
    inspect_attributes,
    inspect_call_hierarchy,
    inspect_implementations,
    inspect_index,
    inspect_method,
    inspect_references,
    inspect_skeleton,
    inspect_symbol,
)
from inspection.service import sym_collection_for


class FakeInspection:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def index(self, project_dir: str, stem: str, exclude: list[str] | None = None) -> dict:
        self.calls.append(("index", project_dir, stem))
        return {"collection": sym_collection_for(stem), "indexed": True, "nodes": 3}

    def skeleton(self, file_path: str) -> dict:
        self.calls.append(("skeleton", file_path))
        return {"file": file_path, "namespace": "App", "declarations": []}

    def attributes(self, file_path: str, class_name: str | None) -> list[dict]:
        self.calls.append(("attributes", file_path, class_name))
        return []

    def method(self, file_path: str, method_name: str) -> dict | None:
        self.calls.append(("method", file_path, method_name))
        return None if method_name == "ghost" else {"name": method_name, "source": "<?php …"}

    def resolve_symbol(self, collection: str, symbol: str) -> list[dict]:
        self.calls.append(("resolve", collection, symbol))
        return [{"kind": "method", "name": "charge", "fqn": "App\\S::charge"}]

    def find_implementations(self, collection: str, fqn: str) -> list[dict]:
        self.calls.append(("impl", collection, fqn))
        return [{"kind": "class", "name": "Invoice", "fqn": "App\\Models\\Invoice"}]

    def find_references(self, collection: str, fqn: str) -> list[dict]:
        self.calls.append(("refs", collection, fqn))
        return []

    def call_hierarchy(self, collection: str, fqn: str) -> dict:
        self.calls.append(("hier", collection, fqn))
        return {"callees": [], "callers": []}

    def list_projects(self) -> list[dict]:
        self.calls.append(("projects",))
        return [{"collection": "sym_Shop", "stem": "Shop", "definitions": 8}]

    def list_definitions(self, collection: str, **filters) -> dict:
        self.calls.append(("definitions", collection, filters))
        return {"items": [], "total": 0, "offset": filters.get("offset", 0), "limit": filters.get("limit", 100)}


class TestHttpEndpoints:
    def test_projects_and_definitions(self, monkeypatch):
        fake = FakeInspection()
        monkeypatch.setattr(http_mod, "_service", lambda: fake)
        inspect_projects = http_mod.inspect_projects()
        assert inspect_projects
        assert inspect_projects[0]["stem"] == "Shop"
        result = http_mod.inspect_definitions(
            stem="Shop", search="comment", kind="class", file="Services", offset=20, limit=50
        )
        assert result["total"] == 0
        assert fake.calls[-1] == (
            "definitions", "sym_Shop",
            {"search": "comment", "kind": "class", "file": "Services", "offset": 20, "limit": 50},
        )

    def test_index_validates(self, monkeypatch):
        fake = FakeInspection()
        monkeypatch.setattr(http_mod, "_service", lambda: fake)
        out = inspect_index({"project_dir": "/p", "stem": "Shop"})
        assert out["indexed"] is True
        assert fake.calls == [("index", "/p", "Shop")]

    def test_missing_field_400(self, monkeypatch):
        fake = FakeInspection()
        monkeypatch.setattr(http_mod, "_service", lambda: fake)
        try:
            inspect_index({"stem": "Shop"})
        except Exception as exc:  # noqa: BLE001 — HTTPException duck check
            assert exc.__class__.__name__ == "HTTPException"
            assert exc.status_code == 400
        else:  # pragma: no cover
            raise AssertionError("expected HTTPException")

    def test_collection_from_stem(self, monkeypatch):
        fake = FakeInspection()
        monkeypatch.setattr(http_mod, "_service", lambda: fake)
        inspect_symbol({"stem": "Archive.zip", "symbol": "Payable"})
        kind, collection, symbol = fake.calls[0]
        assert (kind, collection, symbol) == ("resolve", "sym_Archive_zip", "Payable")

    def test_explicit_collection_wins(self, monkeypatch):
        fake = FakeInspection()
        monkeypatch.setattr(http_mod, "_service", lambda: fake)
        inspect_implementations({"collection": "sym_Shop", "fqn": "X"})
        assert fake.calls[0][1] == "sym_Shop"

    def test_unknown_method_404(self, monkeypatch):
        fake = FakeInspection()
        monkeypatch.setattr(http_mod, "_service", lambda: fake)
        try:
            inspect_method({"file": "/p/A.php", "method": "ghost"})
        except Exception as exc:  # noqa: BLE001
            assert exc.status_code == 404
        else:  # pragma: no cover
            raise AssertionError("expected HTTPException")

    def test_skeleton_attributes_passthrough(self, monkeypatch):
        fake = FakeInspection()
        monkeypatch.setattr(http_mod, "_service", lambda: fake)
        assert inspect_skeleton({"file": "/p/A.php"})["file"] == "/p/A.php"
        assert inspect_attributes({"file": "/p/A.php", "class": "C"}) == []
        assert fake.calls == [
            ("skeleton", "/p/A.php"),
            ("attributes", "/p/A.php", "C"),
        ]

    def test_query_helpers_forward(self, monkeypatch):
        fake = FakeInspection()
        monkeypatch.setattr(http_mod, "_service", lambda: fake)
        inspect_references({"collection": "sym_S", "fqn": "A::b"})
        inspect_call_hierarchy({"collection": "sym_S", "fqn": "A::b"})
        assert fake.calls[0][0] == "refs"
        assert fake.calls[1][0] == "hier"


class FakeAutoService:
    """Auto-inspection backing service recording operations."""

    def __init__(self) -> None:
        self.indexed: set[str] = set()
        self.calls: list[str] = []

    def index(self, project_dir: str, stem: str, exclude: list[str] | None = None) -> dict:
        self.calls.append(f"index:{stem}")
        self.indexed.add(stem)
        return {"collection": sym_collection_for(stem), "indexed": True, "nodes": 1}

    def resolve_symbol(self, collection: str, symbol: str) -> list[dict]:
        self.calls.append(f"resolve:{symbol}")
        if symbol == "App\\Pay\\Charge::run":
            return [{"kind": "method", "name": "run", "fqn": "App\\Pay\\Charge::run"}]
        return []

    def call_hierarchy(self, collection: str, fqn: str) -> dict:
        self.calls.append(f"hier:{fqn}")
        return {
            "callees": [{"fqn": "App\\Pay\\Gateway::post", "ref": {"file": "/p/g.php", "line": 9}}],
            "callers": [{"fqn": "App\\Http\\PayController::store", "ref": {"file": "/p/c.php", "line": 4}}],
        }

    def method(self, file_path: str, method_name: str) -> dict | None:
        self.calls.append(f"method:{method_name}")
        return {"name": method_name, "source": "public function run(): void {}"}


def _result(file: str, cls: str, name: str) -> dict:
    return {"payload": {"file": file, "class": cls, "name": name}}


class TestAutoGrounding:
    def test_no_results_none(self):
        assert auto_mod.build_inspection_grounding([], "/p", "S") is None

    def test_grounding_lines_and_budget(self):
        fake = FakeAutoService()
        results = [
            _result("src/Pay/Charge.php", "App\\Pay\\Charge", "run"),
            _result("src/Http/PayController.php", "App\\Http\\PayController", "store"),
        ]
        md = auto_mod.build_inspection_grounding(
            results, "/p", "S", service=fake  # type: ignore[arg-type]
        )
        assert md and md.startswith("Symbol graph inspection")
        assert "App\\Pay\\Charge::run" in md
        assert "App\\Pay\\Gateway::post" in md          # callee hop
        assert "PayController::store" in md             # caller hop
        assert "public function run(): void {}" in md   # method body read
        assert any(c.startswith("method:run") for c in fake.calls)

    def test_candidate_without_symbol_skipped(self):
        fake = FakeAutoService()
        md = auto_mod.build_inspection_grounding(
            [_result("src/X.php", "No\\Match", "zap")], "/p", "S", service=fake
        )
        assert md is None  # nothing resolvable → honest None

    def test_index_failure_none(self):
        class Failing(FakeAutoService):
            def index(self, project_dir: str, stem: str, exclude: list[str] | None = None) -> dict:
                raise RuntimeError("neo4j down")

        assert auto_mod.build_inspection_grounding(
            [_result("a.php", "A", "b")], "/p", "S", service=Failing()  # type: ignore[arg-type]
        ) is None

    def test_method_abs_path_only_inside_dir(self, tmp_path):
        # A file physically outside the project dir must not be readable.
        project = tmp_path / "proj"
        project.mkdir()
        inside = project / "in.php"
        inside.write_text("<?php class A {}")
        outside = tmp_path / "outside.php"
        outside.write_text("<?php class B {}")
        assert auto_mod._abs(str(project), str(outside)) == ""
        assert auto_mod._abs(str(project), str(inside)) == str(inside)
        # Relative traversal attempts resolve nowhere inside the root.
        assert auto_mod._abs(str(project), "../../outside.php") == ""
