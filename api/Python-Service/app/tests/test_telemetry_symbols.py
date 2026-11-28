from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from telemetry.symbols import from_chunk, normalize
from telemetry.registry import SnapshotRegistry
from telemetry.snapshot import Snapshot, prepare_chunks, resolve_registered_revision, storage_id

PHP_ROOT = Path(os.environ.get("PHP_CLI_ROOT", Path(__file__).parents[2] / "php"))
FIXTURES = PHP_ROOT / "tests/telemetry"
CASES = json.loads((FIXTURES / "symbol_ids.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.get("name", str(c["input"])))
def test_shared_golden(case):
    if case.get("error"):
        with pytest.raises(ValueError):
            normalize(case["input"])
    else:
        assert normalize(case["input"]).encode() == case["expected"].encode()


def test_php_and_python_golden_outputs_are_byte_identical():
    php = json.loads(subprocess.check_output(["php", str(FIXTURES / "check_symbols.php"), "--json"]))
    python = []
    for case in CASES:
        try:
            python.append({"symbol_id": normalize(case["input"])})
        except ValueError:
            python.append({"error": True})
    assert json.dumps(php, ensure_ascii=False).encode() == json.dumps(python, ensure_ascii=False).encode()


def test_real_exporter_uses_same_contract_for_every_roadmap_row():
    exported = json.loads(subprocess.check_output(["php", str(FIXTURES / "export_fixture.php")]))
    seen = set()
    for chunk in exported["chunks"]:
        if chunk.get("symbol_identity_error"):
            with pytest.raises(ValueError):
                from_chunk(chunk)
        else:
            assert from_chunk(chunk) == chunk.get("symbol_id")
            if chunk.get("symbol_id"):
                seen.add(chunk["type"])
                if chunk["symbol_id"].endswith("::__invoke"):
                    assert normalize({"kind": "controller", "controller": chunk["class_fqn"]}) == chunk["symbol_id"]
                    seen.add("controller")
    for fact in exported["facts"]:
        assert normalize(fact["input"]) == fact["symbol_id"]
        seen.add(fact["input"]["kind"])
    assert {"method", "function", "closure", "controller", "route", "table"} <= seen


def test_scoping_and_ambiguous_closures():
    chunk = {"type": "closure", "file": "routes/web.php", "start_line": 42}
    ids = set()
    for project in ("one", "two"):
        for commit in ("a" * 40, "b" * 40):
            snapshot = Snapshot(project, commit, "c" * 64)
            prepared = prepare_chunks([chunk], snapshot)[0]
            assert prepared["symbol_id"] == "closure@routes/web.php:42"
            ids.add(prepared["storage_id"])
            method = {"type": "method", "class_fqn": "App\\Foo", "method_name": "bar", "file": "src/Foo.php"}
            with pytest.raises(ValueError, match="Ambiguous"):
                prepare_chunks([method, method], snapshot)
    assert len(ids) == 4
    assert storage_id("ab", "a" * 40, "c") != storage_id("a", "a" * 40, "bc")


def test_closures_nested_on_one_line_keep_one_runtime_id():
    # `static fn ($j) => array_map(static fn ($x) => $x->get(), $j)` exports two
    # closures with the same file, start line and source text.
    path = "tests/Unit/ClientIndexSearchTest.php"
    outer = {"type": "arrow_function", "file": path, "start_line": 177, "end_line": 177, "name": "{closure}"}
    snapshot = Snapshot("one", "a" * 40, "c" * 64)
    prepared = prepare_chunks([outer, dict(outer)], snapshot)
    assert [c["symbol_id"] for c in prepared] == [f"closure@{path}:177"]

    # A multi-line closure that opens on the same line as an inner one.
    wide = {**outer, "end_line": 181}
    prepared = prepare_chunks([wide, outer], snapshot)
    assert [c["symbol_id"] for c in prepared] == [f"closure@{path}:177", None]
    assert len({c["storage_id"] for c in prepared}) == 2


def test_anonymous_class_methods_are_indexed_without_runtime_identity():
    chunk = {
        "node_type": "method",
        "type": "method",
        "class_fqn": "(anonymous)",
        "method_name": "up",
        "fq_symbol_name": "(anonymous)::up",
        "file_path": "database/migrations/2025_01_01_000001_create_users_table.php",
        "start_line": 11,
        "end_line": 24,
    }

    assert from_chunk(chunk) is None
    prepared = prepare_chunks([chunk], Snapshot("pizza", "a" * 40, "c" * 64))[0]
    assert prepared["symbol_id"] is None
    assert prepared["storage_id"]
    # In a namespace (test doubles such as `new class extends Repository {}`) the class is Ns\(anonymous).
    namespaced = {**chunk, "class_fqn": "App\\Tests\\Functional\\(anonymous)", "fq_symbol_name": "App\\Tests\\Functional\\(anonymous)::flush",
                  "file_path": "tests/Functional/WebhookTest.php"}
    assert from_chunk(namespaced) is None
    assert prepare_chunks([namespaced], Snapshot("pizza", "a" * 40, "c" * 64))[0]["symbol_id"] is None


def test_indexed_chunks_carry_registered_immutable_revision_id(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_SNAPSHOT_ROOT", str(tmp_path))
    registry = SnapshotRegistry(tmp_path)
    snapshot = Snapshot("pizza", "a" * 40, "c" * 64)
    registry.register(snapshot)

    resolved = resolve_registered_revision(snapshot)
    prepared = prepare_chunks([{"type": "method", "class_fqn": "App\\User", "method_name": "save",
                               "file_path": "app/User.php", "start_line": 8, "end_line": 12}], resolved)[0]

    assert resolved.revision_id == registry.get("pizza", "a" * 40)["revision_id"]
    assert prepared["revision_id"] == resolved.revision_id


@pytest.mark.parametrize("kwargs", [
    {"commit_sha": "abcdef"}, {"project_id": "../escape"}, {"dirty": True},
    {"source_archive_sha256": "bad"}, {"telemetry_schema_version": "future"},
])
def test_invalid_snapshot_metadata_rejected(kwargs):
    with pytest.raises(ValueError):
        Snapshot(**({"project_id": "one", "commit_sha": "a" * 40,
                     "source_archive_sha256": "b" * 64} | kwargs))


def test_a_name_without_a_php_identity_in_another_language_gets_no_symbol_id_instead_of_failing():
    rxjs = {"node_type": "function", "name": "visitorSchoolName$", "namespace": "js_libs_auth_visitor",
            "file_path": "libs/auth/components/visitor-school.ts"}
    assert from_chunk(rxjs) is None
    assert from_chunk({"node_type": "function", "name": "load", "namespace": "", "file_path": "libs/auth/load.ts"}) == "\\load"


def test_an_invalid_identity_in_a_php_file_or_a_chunk_without_a_file_still_fails():
    import pytest

    for chunk in ({"node_type": "function", "name": "bad$name", "namespace": "App", "file_path": "app/Helpers.php"},
                  {"node_type": "function", "name": "bad$name", "namespace": "App"}):
        with pytest.raises(ValueError):
            from_chunk(chunk)


def test_a_repeated_name_outside_php_keeps_the_first_id_and_a_repeat_in_php_still_fails():
    import pytest
    from telemetry.snapshot import Snapshot, prepare_chunks

    snapshot = Snapshot("web", "a" * 40, "b" * 64)
    getter = {"node_type": "method", "class_fqn": "ui\\Palette", "method_name": "color", "file_path": "src/palette.ts", "start_line": 3, "end_line": 5}
    setter = {**getter, "start_line": 7, "end_line": 9}
    prepared = prepare_chunks([getter, setter, dict(setter)], snapshot)

    assert [chunk["symbol_id"] for chunk in prepared] == ["ui\\Palette::color", None]  # the repeat of the same span adds nothing
    php = {**getter, "file_path": "src/Palette.php"}
    with pytest.raises(ValueError, match="Ambiguous duplicate identity"):
        prepare_chunks([php, {**php, "start_line": 7, "end_line": 9}], snapshot)
