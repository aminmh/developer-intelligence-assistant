import json
import sys
import types

import rag.query_normalizer as query_normalizer
import rag.router as router
import index.embeddings as embeddings
import urllib.request


def test_nfkc_unicode_normalization():
    assert query_normalizer.normalize_query("Ｈｅｌｌｏ　Ｗｏｒｌｄ") == "hello world"


def test_lowercasing():
    assert query_normalizer.normalize_query("EXPLAIN this CODE") == "explain this code"


def test_whitespace_collapse():
    assert query_normalizer.normalize_query("  what   does\n\tthis  do  ") == "what does this do"


def test_edge_punctuation_stripped_but_question_mark_kept():
    assert query_normalizer.normalize_query("...why is this failing??") == "why is this failing??"


def test_jargon_expansion():
    assert query_normalizer.normalize_query("how does sc work") == "how does ski school work"
    assert query_normalizer.normalize_query("explain bsps") == "explain booking schedule planning service"


def test_no_partial_jargon_match():
    assert query_normalizer.normalize_query("search for sc") == "search for ski school"


def test_empty_input():
    assert query_normalizer.normalize_query("") == ""


def test_encoder_invokes_normalize_query(monkeypatch):
    called = []
    monkeypatch.setattr(router, "normalize_query", lambda t: called.append(t) or t)

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"embedding": [0.1, 0.2]}).encode()

    monkeypatch.setattr(router.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    # Force the Ollama embedding path — the mock replies in Ollama's native
    # /api/embeddings shape (not the OpenAI-compatible /v1/embeddings shape).
    monkeypatch.setattr(embeddings.settings, "embedding_base_url", "")

    enc = router.OllamaEncoder()
    enc(["Hello  World"])

    assert called == ["Hello  World"]


# ── typo correction ──────────────────────────────────────────────────────────


def _install_fake_symspellpy(monkeypatch):
    mod = types.ModuleType("symspellpy")

    class Verbosity:
        CLOSEST = 2

    mod.Verbosity = Verbosity
    monkeypatch.setitem(sys.modules, "symspellpy", mod)


class _FakeSuggestion:
    def __init__(self, term, count, distance=0):
        self.term = term
        self.count = count
        self.distance = distance


class _FakeSymSpell:
    def __init__(self, results):
        self._results = results  # token -> list[_FakeSuggestion]

    def lookup(self, token, verbosity, max_edit_distance, include_unknown):
        return self._results.get(token, [])


def test_typo_known_token_untouched(monkeypatch):
    _install_fake_symspellpy(monkeypatch)
    sym = _FakeSymSpell({})
    known = {"ski", "school", "instructor"}
    monkeypatch.setattr(query_normalizer, "_get_corrector", lambda: (sym, known))

    tokens, corrections = query_normalizer._correct_typos(["ski", "instructor"])

    assert tokens == ["ski", "instructor"]
    assert corrections == []


def test_typo_single_candidate_corrected(monkeypatch):
    _install_fake_symspellpy(monkeypatch)
    sym = _FakeSymSpell({"skii": [_FakeSuggestion("ski", 5)]})
    monkeypatch.setattr(query_normalizer, "_get_corrector", lambda: (sym, set()))

    tokens, corrections = query_normalizer._correct_typos(["skii"])

    assert tokens == ["ski"]
    assert corrections == [("skii", "ski")]


def test_typo_ambiguous_untouched(monkeypatch):
    _install_fake_symspellpy(monkeypatch)
    sym = _FakeSymSpell(
        {"skx": [_FakeSuggestion("ski", 5), _FakeSuggestion("sky", 5)]}
    )
    monkeypatch.setattr(query_normalizer, "_get_corrector", lambda: (sym, set()))

    tokens, corrections = query_normalizer._correct_typos(["skx"])

    assert tokens == ["skx"]
    assert corrections == []


def test_typo_low_frequency_untouched(monkeypatch):
    _install_fake_symspellpy(monkeypatch)
    sym = _FakeSymSpell({"skii": [_FakeSuggestion("ski", 1)]})  # below threshold 2
    monkeypatch.setattr(query_normalizer, "_get_corrector", lambda: (sym, set()))

    tokens, corrections = query_normalizer._correct_typos(["skii"])

    assert tokens == ["skii"]
    assert corrections == []


def test_normalize_typo_disabled_by_default(monkeypatch):
    monkeypatch.setattr(query_normalizer.settings, "enable_typo_correction", False)
    called = []
    monkeypatch.setattr(
        query_normalizer, "_correct_typos", lambda t: (called.append(True) or t, [])
    )

    assert query_normalizer.normalize_query("hello world") == "hello world"
    assert called == []


def test_normalize_typo_enabled_applies(monkeypatch):
    monkeypatch.setattr(query_normalizer.settings, "enable_typo_correction", True)
    monkeypatch.setattr(
        query_normalizer,
        "_correct_typos",
        lambda t: (["fixed"], [("broken", "fixed")]),
    )
    monkeypatch.setattr(query_normalizer, "_log_corrections", lambda *a: None)

    assert query_normalizer.normalize_query("broken") == "fixed"
