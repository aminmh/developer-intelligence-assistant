"""Tests for infra/providers — protocol surface + thin adapters + registry.

Adapters are thin; behavior lives in the wrapped modules (covered by their own
tests). Here we verify: registration/lazy singleton semantics, override, and
that each adapter forwards to the right implementation with settings honored
at call time (no capture at construction)."""

from __future__ import annotations

import infra.providers as pv
from infra.providers import (
    DocRetriever,
    DocRetrieverAdapter,
    EmbeddingAdapter,
    EmbeddingProvider,
    LlmAdapter,
    LlmProvider,
    SandboxBackend,
    SandboxBackendAdapter,
    provider,
)


def _reset_registry():
    pv._instances.clear()


def test_protocol_contracts_hold():
    assert isinstance(LlmAdapter(), LlmProvider)
    assert isinstance(EmbeddingAdapter(), EmbeddingProvider)
    assert isinstance(DocRetrieverAdapter(), DocRetriever)
    assert isinstance(SandboxBackendAdapter(), SandboxBackend)


def test_registry_lazy_singleton():
    _reset_registry()
    a = provider("llm")
    b = provider("llm")
    assert a is b
    assert isinstance(a, LlmProvider)


def test_unknown_provider_raises():
    _reset_registry()
    try:
        provider("does-not-exist")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_register_override():
    _reset_registry()

    class FakeLlm:
        def complete(self, *a, **k):
            return ("", "", "")

        def stream(self, *a, **k):
            return iter(())

    pv.register("llm", FakeLlm())
    assert isinstance(provider("llm"), FakeLlm)
    assert provider("llm") is provider("llm")


def test_named_accessors_resolve():
    _reset_registry()
    assert pv.llm() is provider("llm")
    assert pv.embeddings() is provider("embeddings")
    assert pv.doc_retriever() is provider("doc_retriever")
    assert pv.sandbox() is provider("sandbox")


class TestLlmAdapter:
    def test_complete_forwards_provider_flag(self, monkeypatch):
        _reset_registry()
        captured = {}

        def fake_complete(prompt, system=None, temperature=None, top_p=None,
                          max_tokens=2048, provider="local"):
            captured.update(prompt=prompt, system=system, temp=temperature,
                            topp=top_p, tokens=max_tokens, provider=provider)
            return ("answer", "model-x", provider)

        monkeypatch.setattr("runtime.llm.complete_with_model", fake_complete)
        out = pv.llm().complete("hi", system="sys", temperature=0.2, max_tokens=99,
                                provider="remote")
        assert out == ("answer", "model-x", "remote")
        assert captured == {"prompt": "hi", "system": "sys", "temp": 0.2,
                            "topp": None, "tokens": 99, "provider": "remote"}

    def test_stream_returns_iterator(self, monkeypatch):
        _reset_registry()

        def fake_stream(prompt, system=None, temperature=None, top_p=None,
                        provider="local"):
            yield ("d1", "m", provider)
            yield ("d2", "m", provider)

        monkeypatch.setattr("runtime.llm.stream_complete_with_model", fake_stream)
        chunks = list(pv.llm().stream("q", temperature=0.0, provider="remote"))
        assert chunks == [("d1", "m", "remote"), ("d2", "m", "remote")]


class TestEmbeddingAdapter:
    def test_forwards_texts(self, monkeypatch):
        _reset_registry()
        captured = {}

        def fake_embed(texts):
            captured["texts"] = texts
            return [[0.1]] * len(texts)

        monkeypatch.setattr("index.embeddings.embed_texts", fake_embed)
        out = pv.embeddings().embed(["a", "b"])
        assert out == [[0.1], [0.1]]
        assert captured["texts"] == ["a", "b"]


class TestDocRetrieverAdapter:
    def test_forwards_packages_query_and_flags(self, monkeypatch):
        _reset_registry()
        captured = {}

        def fake_retrieve(packages, query, enabled=None, strict=None):
            captured.update(packages=packages, query=query, enabled=enabled,
                            strict=strict)
            return {"ok": True, "chunks": ["c"], "provider": "context7",
                    "provenance": [], "warning": None}

        monkeypatch.setattr("rag.doc_retrieval.retrieve_documentation", fake_retrieve)
        out = pv.doc_retriever().retrieve({"pkg": {}}, "how?", enabled=True,
                                          strict=False)
        assert out["chunks"] == ["c"]
        assert captured == {"packages": {"pkg": {}}, "query": "how?",
                            "enabled": True, "strict": False}


class TestSandboxBackendAdapter:
    def test_status_delegates(self, monkeypatch):
        _reset_registry()
        monkeypatch.setattr(
            "runtime.sandbox_runtime.sandbox_available",
            lambda docker_host=None: {"available": True},
        )
        assert pv.sandbox().status() == {"available": True}

    def test_analyze_and_run_command_delegate_to_executor(self, monkeypatch):
        _reset_registry()
        calls = []

        class FakeExecutor:
            def analyze(self, workspace_path, project_id, framework="php",
                        probes=None):
                calls.append(("analyze", workspace_path, project_id, framework,
                              probes))
                return {"staged": True}

            def run_command(self, project_id, command, framework="php", cwd=None):
                calls.append(("run_command", project_id, command, framework, cwd))
                return {"success": True, "exit_code": 0, "stdout": "ok"}

        monkeypatch.setattr("sandbox.executor.default_executor",
                            lambda: FakeExecutor())
        assert pv.sandbox().analyze("/ws", "Archive", framework="laravel",
                                    probes=["routes"]) == {"staged": True}
        assert pv.sandbox().run_command("Archive", "php -v") == {
            "success": True, "exit_code": 0, "stdout": "ok"}
        assert calls == [
            ("analyze", "/ws", "Archive", "laravel", ["routes"]),
            ("run_command", "Archive", "php -v", "php", None),
        ]
