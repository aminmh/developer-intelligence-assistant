"""Regression tests for the semantic router (embedding + LLM adjudicator).

The router's response is an additive superset dict
``{route, confidence, source, reason, needs_clarification, candidate_routes?}``;
these tests pin the shape and the escalation logic. Embedding/LLM calls are
always faked — no live LM Studio dependency.
"""

import json
import uuid

import rag.router as rt


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


class _FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def _hit(score, route):
    return type("Hit", (), {"score": score, "payload": {"route": route}})()


def _llm_result(
    route="search",
    confidence=0.8,
    reason="adjudicated",
    needs_clarification=False,
    candidate_routes=None,
):
    return {
        "route": route,
        "confidence": confidence,
        "reason": reason,
        "needs_clarification": needs_clarification,
        "candidate_routes": [] if candidate_routes is None else candidate_routes,
    }


def _install_llm_response(monkeypatch, parsed):
    payload = json.dumps({"response": json.dumps(parsed)}).encode()
    monkeypatch.setattr(rt.urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload))


# --- feedback loop ---


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


# --- ambiguity helpers ---


def test_is_ambiguous_low_score():
    assert rt._is_ambiguous(0.70, [("a", 0.70)]) is True


def test_is_ambiguous_clear_high_score():
    assert rt._is_ambiguous(0.90, [("a", 0.90), ("b", 0.50)]) is False


def test_is_ambiguous_close_margin():
    assert rt._is_ambiguous(0.80, [("a", 0.80), ("b", 0.78)]) is True


# --- LLM adjudicator parsing ---


def test_llm_select_route_valid_json_returns_dict(monkeypatch):
    _install_llm_response(monkeypatch, {"route": "debug"})

    result = rt._llm_select_route("fix this bug", ["debug", "search"])

    assert result is not None
    assert result["route"] == "debug"
    # Model omitted confidence => None; classify substitutes the embedding top
    # score when the value is not a number.
    assert result["confidence"] is None
    assert result["needs_clarification"] is False
    assert result["candidate_routes"] == []
    assert result["reason"] == ""


def test_llm_select_route_unknown_returns_none(monkeypatch):
    _install_llm_response(monkeypatch, "no route here")

    assert rt._llm_select_route("fix this bug", ["debug", "search"]) is None


def test_llm_select_route_text_scan_fallback(monkeypatch):
    # Non-JSON prose that mentions a route name still resolves via text scan.
    _install_llm_response(monkeypatch, "i think code_explain fits best")

    result = rt._llm_select_route("what does this do", ["debug", "code_explain"])

    assert result is not None
    assert result["route"] == "code_explain"


def test_llm_select_route_bad_route_name_returns_none(monkeypatch):
    # A JSON route outside the enum, with no known name in the raw text to scan,
    # is a validation failure => None (classify then falls back to embeddings).
    _install_llm_response(monkeypatch, {"route": "not_a_route"})

    assert rt._llm_select_route("explain this", ["debug", "code_explain"]) is None


def test_llm_select_route_bad_route_valid_name_in_reason_returns_none(monkeypatch):
    # Valid JSON naming an unknown route is a validation failure => None, even
    # when `reason` mentions a real route name: no text-scan of the raw JSON.
    _install_llm_response(
        monkeypatch,
        {"route": "bogus", "reason": "use code_explain next time"},
    )

    assert rt._llm_select_route("explain this", ["debug", "code_explain"]) is None


def test_llm_select_route_clamps_confidence_and_drops_extra_fields(monkeypatch):
    long_reason = "r" * 500
    _install_llm_response(
        monkeypatch,
        {
            "route": "debug",
            "confidence": 1.7,
            "reason": long_reason,
            "needs_clarification": "yes",  # non-bool must not be trusted
            # Duplicate "debug" inside the first-3 window — only order-preserving
            # dict.fromkeys dedupe (then [:3]) yields ["debug","chat","search"].
            "candidate_routes": ["debug", "chat", "bogus", "debug", "search"],
            "extra_field": 123,
        },
    )

    result = rt._llm_select_route("fix this bug", ["debug", "search"])

    assert result["route"] == "debug"
    assert result["confidence"] == 1.0
    assert len(result["reason"]) <= 300
    assert result["needs_clarification"] is False
    assert result["candidate_routes"] == ["debug", "chat", "search"]
    assert "extra_field" not in result


def test_llm_select_route_urlopen_exception_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(rt.urllib.request, "urlopen", boom)

    assert rt._llm_select_route("fix this bug", ["debug", "search"]) is None


# --- classify pipeline ---


def _prepare_classify(monkeypatch, ranked):
    monkeypatch.setattr(rt, "encoder", _FakeEncoder())
    monkeypatch.setattr(rt, "_feedback_override", lambda v: None)
    monkeypatch.setattr(rt, "router", object())
    monkeypatch.setattr(rt, "_top_route_scores", lambda v: ranked)


def test_classify_router_unavailable(monkeypatch):
    monkeypatch.setattr(rt, "_init_router", lambda: False)

    result = rt.classify("hello")

    assert result["route"] == "chat"
    assert result["confidence"] == 0.0
    assert result["reason"] == "router unavailable"
    assert result["needs_clarification"] is True


def test_classify_no_route_candidates(monkeypatch):
    _prepare_classify(monkeypatch, [])

    result = rt.classify("hello there")

    assert result["route"] == "chat"
    assert result["confidence"] == 0.0
    assert result["reason"] == "no route candidates"
    assert result["needs_clarification"] is True


def test_classify_light_only_clear_fast_path_no_fallback(monkeypatch):
    _prepare_classify(monkeypatch, [("knowledge", 0.9), ("search", 0.4)])
    calls = {"llm": 0}
    monkeypatch.setattr(
        rt,
        "_llm_select_route",
        lambda q, candidates, context_note="": calls.__setitem__(
            "llm", calls["llm"] + 1
        )
        or _llm_result(),
    )
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("how is auth wired")

    assert result["route"] == "knowledge"
    assert result["confidence"] == 0.9
    assert result["source"] == "embedding"
    assert result["source"] != "llm_fallback"
    assert result["reason"] is None
    assert result["needs_clarification"] is False
    assert calls["llm"] == 0


def test_classify_heavy_candidate_escalates_to_llm(monkeypatch):
    _prepare_classify(monkeypatch, [("code_explain", 0.82), ("search", 0.5)])
    calls = {"llm": 0}

    def fake_llm(query, candidates, context_note=""):
        calls["llm"] += 1
        return _llm_result(route="code_explain", confidence=0.75, reason="explain intent")

    monkeypatch.setattr(rt, "_llm_select_route", fake_llm)
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("what does this class do")

    assert calls["llm"] == 1
    assert result["route"] == "code_explain"
    assert result["source"] == "llm_fallback"
    assert result["reason"] == "explain intent"
    assert result["confidence"] == 0.75
    assert result["needs_clarification"] is False
    # Model echoed no candidates => the embedding pre-filter substitutes in.
    assert result["candidate_routes"] == ["code_explain", "search"]


def test_classify_model_echo_of_candidate_routes_wins(monkeypatch):
    _prepare_classify(monkeypatch, [("code_explain", 0.82), ("search", 0.5)])
    monkeypatch.setattr(
        rt,
        "_llm_select_route",
        lambda q, candidates, context_note="": _llm_result(
            route="code_explain", confidence=0.75, candidate_routes=["debug"]
        ),
    )
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("what does this class do")

    assert result["route"] == "code_explain"
    assert result["source"] == "llm_fallback"
    # A NON-empty model echo wins over the embedding candidates.
    assert result["candidate_routes"] == ["debug"]


def test_classify_heavy_at_second_position_escalates_to_llm(monkeypatch):
    # A clear light top-1 still escalates when a heavy route sits in the top 3.
    _prepare_classify(
        monkeypatch,
        [("chat", 0.92), ("code_refactor", 0.6), ("knowledge", 0.3)],
    )
    calls = {"llm": 0}

    def fake_llm(query, candidates, context_note=""):
        calls["llm"] += 1
        return _llm_result(
            route="code_refactor", confidence=0.6, reason="refactor intent"
        )

    monkeypatch.setattr(rt, "_llm_select_route", fake_llm)
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("make this cleaner")

    assert calls["llm"] == 1
    assert result["source"] == "llm_fallback"
    assert result["route"] == "code_refactor"


def test_classify_heavy_beyond_top3_does_not_escalate(monkeypatch):
    # A heavy route at position 4+ is outside the adjudication window.
    _prepare_classify(
        monkeypatch,
        [
            ("chat", 0.92),
            ("knowledge", 0.5),
            ("search", 0.3),
            ("code_explain", 0.28),
        ],
    )
    calls = {"llm": 0}
    monkeypatch.setattr(
        rt,
        "_llm_select_route",
        lambda q, candidates, context_note="": calls.__setitem__(
            "llm", calls["llm"] + 1
        )
        or _llm_result(),
    )
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("hello there")

    assert calls["llm"] == 0
    assert result["source"] == "embedding"
    assert result["route"] == "chat"
    assert result["confidence"] == 0.92


def test_classify_llm_omits_confidence_substitutes_embedding_top_score(monkeypatch):
    _prepare_classify(monkeypatch, [("code_explain", 0.81), ("search", 0.4)])
    monkeypatch.setattr(
        rt,
        "_llm_select_route",
        lambda q, candidates, context_note="": _llm_result(
            route="code_explain", confidence=None, reason="explain intent"
        ),
    )
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("what does this class do")

    assert result["route"] == "code_explain"
    assert result["source"] == "llm_fallback"
    assert result["confidence"] == 0.81


def test_classify_ambiguous_triggers_fallback_with_additive_keys(monkeypatch):
    _prepare_classify(monkeypatch, [("debug", 0.80), ("search", 0.78)])
    monkeypatch.setattr(
        rt,
        "_llm_select_route",
        lambda q, candidates, context_note="": _llm_result(
            route="search",
            confidence=0.8,
            reason="debug and search are close; location wording wins",
            needs_clarification=True,
        ),
    )
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("find this thing")

    assert result["route"] == "search"
    assert result["source"] == "llm_fallback"
    assert result["reason"] == "debug and search are close; location wording wins"
    assert result["needs_clarification"] is True


def test_classify_llm_none_falls_back_to_embedding(monkeypatch):
    _prepare_classify(monkeypatch, [("code_refactor", 0.8), ("chat", 0.4)])
    monkeypatch.setattr(rt, "_llm_select_route", lambda q, candidates, context_note="": None)
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("make this cleaner")

    assert result["route"] == "code_refactor"
    assert result["confidence"] == 0.8
    assert result["source"] == "embedding"
    assert result["reason"] == "llm adjudication unavailable"
    assert result["needs_clarification"] is False


def test_classify_low_embedding_score_llm_none_routes_chat(monkeypatch):
    _prepare_classify(monkeypatch, [("code_explain", 0.2), ("chat", 0.1)])
    monkeypatch.setattr(rt, "_llm_select_route", lambda q, candidates, context_note="": None)
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("hmm what even")

    assert result["route"] == "chat"
    assert result["confidence"] == 0.2
    assert result["source"] == "embedding"
    assert result["reason"] == "llm adjudication unavailable"
    assert result["needs_clarification"] is True


def test_classify_llm_transport_error_falls_back_to_embedding(monkeypatch):
    _prepare_classify(monkeypatch, [("code_explain", 0.85), ("search", 0.3)])

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(rt.urllib.request, "urlopen", boom)
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("what does this function do")

    assert result["route"] == "code_explain"
    assert result["source"] == "embedding"
    assert result["reason"] == "llm adjudication unavailable"
    assert result["confidence"] == 0.85


def test_classify_feedback_override_shape(monkeypatch):
    monkeypatch.setattr(rt, "encoder", _FakeEncoder())
    monkeypatch.setattr(rt, "_feedback_override", lambda v: "search")
    monkeypatch.setattr(rt, "router", object())

    result = rt.classify("find where save is defined")

    assert result["route"] == "search"
    assert result["confidence"] == 1.0
    assert result.get("source") == "feedback"
    assert result["reason"] == "user correction match"
    assert result["needs_clarification"] is False


def test_classify_named_service_uses_code_explain_boundary(monkeypatch):
    _prepare_classify(monkeypatch, [("knowledge", 0.70), ("code_explain", 0.54)])
    monkeypatch.setattr(rt, "_llm_select_route", lambda *a, **k: None)

    result = rt.classify("how CommentService works?")

    assert result["route"] == "code_explain"
    assert result["source"] == "deterministic_symbol"
    assert result["confidence"] == 1.0


def test_log_fallback_writes_contract_fields(tmp_path, monkeypatch):
    # Real disk write: pin the JSON line contract — final_confidence arrives as
    # the 6th positional arg and llm_confidence/final_confidence both serialize.
    monkeypatch.setattr(rt.settings, "log_dir", str(tmp_path))

    rt._log_fallback(
        "query",
        "code_explain",
        0.8,
        [("code_explain", 0.8), ("search", 0.5)],
        {
            "route": "debug",
            "confidence": 0.7,
            "reason": "r",
            "needs_clarification": False,
            "candidate_routes": ["debug"],
        },
        0.7,
    )

    log_path = tmp_path / "router-fallback.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["query"] == "query"
    assert entry["embedding_route"] == "code_explain"
    assert entry["llm_route"] == "debug"
    assert entry["llm_confidence"] == 0.7
    assert entry["final_confidence"] == 0.7
    assert entry["final_choice"] == "debug"
    assert entry["needs_clarification"] is False
    assert entry["candidate_routes"] == ["debug"]


def test_classify_app_route_question_embeds_knowledge_not_chat(monkeypatch):
    # "which route is for appreciations" embeds as knowledge; chat placing
    # second must NOT win on the embedding fast path.
    _prepare_classify(
        monkeypatch, [("knowledge", 0.82), ("chat", 0.6), ("search", 0.5)]
    )
    calls = {"llm": 0}
    monkeypatch.setattr(
        rt,
        "_llm_select_route",
        lambda q, candidates, context_note="": calls.__setitem__(
            "llm", calls["llm"] + 1
        )
        or _llm_result(),
    )
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("which route is for appreciations")

    assert result["route"] == "knowledge"
    assert result["source"] == "embedding"
    assert calls["llm"] == 0


def test_classify_endpoint_explain_wording_escapes_knowledge(monkeypatch):
    # "what does this endpoint do" ranks knowledge first but code_explain sits
    # in the top 3 (heavy) => escalation; the adjudicator must keep the
    # explain-single-snippet intent instead of defaulting to knowledge.
    _prepare_classify(
        monkeypatch,
        [("knowledge", 0.8), ("code_explain", 0.78), ("chat", 0.4)],
    )
    calls = {"llm": 0}

    def fake_llm(query, candidates, context_note=""):
        calls["llm"] += 1
        return _llm_result(route="code_explain", confidence=0.7)

    monkeypatch.setattr(rt, "_llm_select_route", fake_llm)
    monkeypatch.setattr(rt, "_log_fallback", lambda *a: None)

    result = rt.classify("what does this endpoint do")

    assert calls["llm"] == 1
    assert result["route"] == "code_explain"
    assert result["source"] == "llm_fallback"
    assert result["confidence"] == 0.7
