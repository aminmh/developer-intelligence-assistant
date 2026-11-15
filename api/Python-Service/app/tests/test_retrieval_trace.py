import json

import index.llamaindex_layer as layer


def test_retrieval_trace_is_cached_and_written(monkeypatch, tmp_path):
    monkeypatch.setattr(layer.settings, "log_dir", str(tmp_path))
    layer._TRACE_CACHE.clear()

    payload = {
        "mode": "hybrid",
        "vector_result_count": 3,
        "graph_context_count": 1,
        "sources": [{"file": "src/Service.php", "via_graph": True}],
    }

    layer.record_retrieval_trace("trace-1", payload)

    cached = layer.get_retrieval_trace("trace-1")
    assert cached is not None
    assert cached["trace_id"] == "trace-1"
    assert cached["mode"] == "hybrid"
    assert cached["graph_context_count"] == 1
    assert cached["timestamp"]

    entries = (tmp_path / "retrieval-traces.jsonl").read_text().splitlines()
    assert len(entries) == 1
    assert json.loads(entries[0])["trace_id"] == "trace-1"


def test_missing_retrieval_trace_returns_none():
    assert layer.get_retrieval_trace("missing-trace") is None


def test_retrieval_trace_can_be_loaded_from_log(monkeypatch, tmp_path):
    monkeypatch.setattr(layer.settings, "log_dir", str(tmp_path))
    layer._TRACE_CACHE.clear()
    trace_path = tmp_path / "retrieval-traces.jsonl"
    trace_path.write_text(
        json.dumps({"trace_id": "persisted", "mode": "vector_only"}) + "\n",
        encoding="utf-8",
    )

    trace = layer.get_retrieval_trace("persisted")

    assert trace == {"trace_id": "persisted", "mode": "vector_only"}
