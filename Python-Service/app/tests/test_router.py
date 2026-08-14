import json
import uuid

import pytest

import router as rt


class _FakeEncoder:
    def __call__(self, docs):
        return [[0.1, 0.2, 0.3, 0.4] for _ in docs]


class _FakeQdrant:
    def __init__(self, existing=None, hits=None):
        self.existing = existing or []
        self.hits = hits or []
        self.upserts = []
        self.searches = []

    def get_collections(self):
        class Col:
            def __init__(self, name):
                self.name = name

        return type("Resp", (), {"collections": [Col(n) for n in self.existing]})()

    def create_collection(self, **kwargs):
        pass

    def upsert(self, collection_name, wait, points):
        self.upserts.append((collection_name, points))

    def search(self, collection_name, query_vector, limit):
        self.searches.append((collection_name, query_vector, limit))
        return self.hits


def _hit(score, route):
    return type("Hit", (), {"score": score, "payload": {"route": route}})()


# --- Step 2: feedback loop ---


def test_submit_feedback_upserts(monkeypatch):
    monkeypatch.setattr(rt, "encoder", _FakeEncoder())
    fake = _FakeQdrant(existing=[rt.FEEDBACK_COLLECTION])
    monkeypatch.setattr(rt, "_get_qdrant", lambda: fake)

    result = rt.submit_feedback("how do I fix this bug", "debug")

    assert result["success"] is True
    assert result["route"] == "debug"
    assert len(fake.upserts) == 1

    collection, points = fake.upserts[0]
    assert collection == rt.FEEDBACK_COLLECTION
    assert len(points) == 1
    assert points[0].id == str(uuid.uuid5(uuid.NAMESPACE_URL, "how do I fix this bug"))
    assert points[0].payload["route"] == "debug"
    assert points[0].payload["source"] == "feedback"
    assert points[0].payload["original_query"] == "how do I fix this bug"
    assert "timestamp" in points[0].payload


def test_submit_feedback_rejects_unknown_route(monkeypatch):
    monkeypatch.setattr(rt, "encoder", _FakeEncoder())
    fake = _FakeQdrant(existing=[rt.FEEDBACK_COLLECTION])
    monkeypatch.setattr(rt, "_get_qdrant", lambda: fake)

    result = rt.submit_feedback("query", "not_a_route")
    assert result["success"] is False
    assert fake.upserts == []


def test_feedback_override_triggers_above_threshold(monkeypatch):
    fake = _FakeQdrant(existing=[rt.FEEDBACK_COLLECTION], hits=[_hit(0.98, "debug")])
    monkeypatch.setattr(rt, "_get_qdrant", lambda: fake)

    assert rt._feedback_override([0.1, 0.2, 0.3, 0.4]) == "debug"


def test_feedback_override_ignores_below_threshold(monkeypatch):
    fake = _FakeQdrant(existing=[rt.FEEDBACK_COLLECTION], hits=[_hit(0.75, "debug")])
    monkeypatch.setattr(rt, "_get_qdrant", lambda: fake)

    assert rt._feedback_override([0.1, 0.2, 0.3, 0.4]) is None


# --- Step 3: hybrid fallback ---


def test_is_ambiguous_low_score():
    assert rt._is_ambiguous(0.70, [("a", 0.70)]) is True


def test_is_ambiguous_clear_high_score():
    assert rt._is_ambiguous(0.90, [("a", 0.90), ("b", 0.50)]) is False


def test_is_ambiguous_close_margin():
    assert rt._is_ambiguous(0.80, [("a", 0.80), ("b", 0.78)]) is True


def test_llm_select_route_valid_json(monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"response": json.dumps({"route": "debug"})}).encode()

    monkeypatch.setattr(rt.urllib.request, "urlopen", lambda *a, **k: FakeResp())

    assert rt._llm_select_route("fix this bug") == "debug"


def test_llm_select_route_unknown_returns_none(monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"response": "no route here"}).encode()

    monkeypatch.setattr(rt.urllib.request, "urlopen", lambda *a, **k: FakeResp())

    assert rt._llm_select_route("fix this bug") is None


# --- Step 4: no breakage of fast path + override/fallback wiring ---


def _fake_layer(result):
    return type("Layer", (), {"__call__": lambda self, prompt: result})()


def test_classify_fast_path_no_fallback(monkeypatch):
    monkeypatch.setattr(rt, "encoder", _FakeEncoder())
    monkeypatch.setattr(rt, "_feedback_override", lambda v: None)
    monkeypatch.setattr(
        rt,
        "router",
        _fake_layer(type("R", (), {"name": "debug", "similarity_score": 0.85})()),
    )
    monkeypatch.setattr(
        rt, "_top_route_scores", lambda v: [("debug", 0.85), ("search", 0.40)]
    )
    calls = {"llm": 0}
    monkeypatch.setattr(rt, "_llm_select_route", lambda q: calls.__setitem__("llm", calls["llm"] + 1) or "debug")
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("debug this function")

    assert result["route"] == "debug"
    assert result["confidence"] == 0.85
    assert result.get("source") != "llm_fallback"
    assert calls["llm"] == 0


def test_classify_ambiguous_triggers_fallback(monkeypatch):
    monkeypatch.setattr(rt, "encoder", _FakeEncoder())
    monkeypatch.setattr(rt, "_feedback_override", lambda v: None)
    monkeypatch.setattr(
        rt,
        "router",
        _fake_layer(type("R", (), {"name": "debug", "similarity_score": 0.80})()),
    )
    monkeypatch.setattr(
        rt, "_top_route_scores", lambda v: [("debug", 0.80), ("search", 0.78)]
    )
    monkeypatch.setattr(rt, "_llm_select_route", lambda q: "search")
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("find this thing")

    assert result["route"] == "search"
    assert result.get("source") == "llm_fallback"


def test_classify_feedback_override(monkeypatch):
    monkeypatch.setattr(rt, "encoder", _FakeEncoder())
    monkeypatch.setattr(rt, "_feedback_override", lambda v: "search")
    monkeypatch.setattr(
        rt, "router", _fake_layer(type("R", (), {"name": "chat", "similarity_score": 0.5})())
    )

    result = rt.classify("find where save is defined")

    assert result["route"] == "search"
    assert result["confidence"] == 1.0
    assert result.get("source") == "feedback"
