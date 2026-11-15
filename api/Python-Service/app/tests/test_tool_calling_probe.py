"""Tests for the remote native tool-calling probe and its stored results."""

import json
from types import SimpleNamespace

import runtime.llm as llm
import runtime.tool_calling_probe as probe_module
from runtime.tool_calling_probe import (
    PROBE_TOOL_NAME,
    lookup_probe,
    probe_key,
    probe_tool_calling,
    record_probe,
)


class _MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


def _tool_call(name=PROBE_TOOL_NAME, arguments='{"status": "ok"}', call_id="call_1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _response(tool_calls=None, content=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _ScriptedClient:
    """OpenAI-client stand-in replaying scripted responses or errors in order."""

    def __init__(self, *steps):
        self.chat = self
        self.completions = self
        self.calls = []
        self._steps = list(steps)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_probe_succeeds_after_full_round_trip():
    client = _ScriptedClient(_response([_tool_call()]), _response(content="done"))

    result = probe_tool_calling(client, "m", timeout=5)

    assert result == {"supported": True, "detail": "Native tool calls supported."}
    first, second = client.calls
    assert first["tool_choice"] == "auto"
    assert first["tools"][0]["function"]["name"] == PROBE_TOOL_NAME
    assert first["timeout"] == 5
    assistant, tool = second["messages"][1], second["messages"][2]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert tool == {"role": "tool", "tool_call_id": "call_1", "content": json.dumps({"received": True})}


def test_probe_reports_text_only_reply():
    client = _ScriptedClient(_response(tool_calls=None, content="ok"))

    result = probe_tool_calling(client, "m")

    assert result["supported"] is False
    assert "text" in result["detail"]
    assert len(client.calls) == 1


def test_probe_reports_endpoint_rejecting_tools():
    client = _ScriptedClient(RuntimeError('"auto" tool choice requires --enable-auto-tool-choice'))

    result = probe_tool_calling(client, "m")

    assert result["supported"] is False
    assert "--enable-auto-tool-choice" in result["detail"]


def test_probe_reports_unknown_tool_name():
    result = probe_tool_calling(_ScriptedClient(_response([_tool_call(name="other")])), "m")

    assert result["supported"] is False
    assert "unknown tool" in result["detail"]


def test_probe_reports_invalid_json_arguments():
    result = probe_tool_calling(_ScriptedClient(_response([_tool_call(arguments="{oops")])), "m")

    assert result == {"supported": False, "detail": "The tool arguments were not valid JSON."}


def test_probe_reports_arguments_outside_schema():
    result = probe_tool_calling(_ScriptedClient(_response([_tool_call(arguments='{"status": "bad"}')])), "m")

    assert result["supported"] is False
    assert "schema" in result["detail"]


def test_probe_reports_rejected_tool_result():
    client = _ScriptedClient(_response([_tool_call()]), RuntimeError("tool role not supported"))

    result = probe_tool_calling(client, "m")

    assert result["supported"] is False
    assert "tool result" in result["detail"]


def test_probe_detail_is_bounded():
    result = probe_tool_calling(_ScriptedClient(RuntimeError("x" * 5000)), "m")

    assert len(result["detail"]) < 400


def test_probe_key_normalizes_v1_and_trailing_slash():
    assert probe_key("https://api.example.com/v1/", "m") == probe_key("https://api.example.com", "m")
    assert probe_key("https://api.example.com", "a") != probe_key("https://api.example.com", "b")


def test_record_and_lookup_are_scoped_to_url_and_model():
    store = _MemoryStore()

    record_probe("https://api.example.com/v1", "m", {"supported": True, "detail": "ok"}, store=store)

    entry = lookup_probe("https://api.example.com", "m", store=store)
    assert entry["supported"] is True
    assert entry["checked_at"]
    assert lookup_probe("https://api.example.com", "other", store=store) is None
    assert lookup_probe("https://other.example.com", "m", store=store) is None


def test_record_keeps_only_recent_probes(monkeypatch):
    monkeypatch.setattr(probe_module, "_MAX_STORED_PROBES", 2)
    store = _MemoryStore()
    for model in ("a", "b", "c"):
        record_probe("https://api.example.com", model, {"supported": True, "detail": ""}, store=store)

    assert lookup_probe("https://api.example.com", "a", store=store) is None
    assert lookup_probe("https://api.example.com", "c", store=store) is not None


def _configure_remote(monkeypatch, store):
    monkeypatch.setattr(llm.settings, "llm_remote_base_url", "https://api.example.com/v1")
    monkeypatch.setattr(llm.settings, "llm_remote_model", "remote-model")
    monkeypatch.setattr(llm.settings, "llm_remote_api_key", "sekret")
    monkeypatch.setattr(probe_module, "_store", lambda store_arg=None: store)


def test_test_remote_runs_and_stores_tool_probe(monkeypatch):
    store = _MemoryStore()
    _configure_remote(monkeypatch, store)
    client = _ScriptedClient(_response(content="ok"), _response([_tool_call()]), _response(content="done"))
    monkeypatch.setattr(llm, "_get_remote_client", lambda: client)

    result = llm.test_remote(timeout=5)

    assert result["ok"] is True
    assert result["tool_calling"]["supported"] is True
    assert llm.remote_tool_calling_status()["supported"] is True


def test_test_remote_skips_tool_probe_when_connection_fails(monkeypatch):
    store = _MemoryStore()
    _configure_remote(monkeypatch, store)
    client = _ScriptedClient(RuntimeError("401 unauthorized"))
    monkeypatch.setattr(llm, "_get_remote_client", lambda: client)

    result = llm.test_remote()

    assert result["ok"] is False
    assert result["tool_calling"] is None
    assert len(client.calls) == 1
    assert llm.remote_tool_calling_status() is None


def test_remote_tool_calling_status_is_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_remote_base_url", "")

    assert llm.remote_tool_calling_status() is None
    assert llm.test_remote()["tool_calling"] is None
