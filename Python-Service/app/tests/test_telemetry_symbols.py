from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from telemetry.symbols import from_chunk, normalize
from telemetry.snapshot import Snapshot, prepare_chunks, storage_id

FIXTURES = Path(__file__).parents[2] / "php/tests/telemetry"
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
            with pytest.raises(ValueError, match="Ambiguous"):
                prepare_chunks([chunk, chunk], snapshot)
    assert len(ids) == 4
    assert storage_id("ab", "a" * 40, "c") != storage_id("a", "a" * 40, "bc")


@pytest.mark.parametrize("kwargs", [
    {"commit_sha": "abcdef"}, {"project_id": "../escape"}, {"dirty": True},
    {"source_archive_sha256": "bad"}, {"telemetry_schema_version": "future"},
])
def test_invalid_snapshot_metadata_rejected(kwargs):
    with pytest.raises(ValueError):
        Snapshot(**({"project_id": "one", "commit_sha": "a" * 40,
                     "source_archive_sha256": "b" * 64} | kwargs))
