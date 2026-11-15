"""Tests for the model-callable tool registry, validation, limits, and execution."""

import socket
import time
from types import SimpleNamespace

import pytest

import infra.tool_executor as tool_executor
from agent_tools.builtins import PROJECT_STRUCTURE
from agent_tools.executor import bound_output, execute_call
from agent_tools.limits import RunBudget, ToolLimits, effective_timeout, load_limits
from agent_tools.registry import ToolRegistry
from agent_tools.types import (
    EFFECT_READ_ONLY,
    EFFECT_SIDE_EFFECTING,
    USER_WORKFLOWS,
    ToolCallRejected,
    ToolContext,
    ToolDefinition,
    ToolDefinitionError,
)
from agent_tools.validation import (
    normalize_effect,
    validate_arguments,
    validate_input_schema,
    validate_name,
    validate_workflows,
)
from infra.tool_store import ToolStore

SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string", "maxLength": 10}},
    "required": ["id"],
    "additionalProperties": False,
}


def _rest_record(name="get_item", **config):
    base = {"method": "GET", "url": "https://api.example.com/items/{{id}}", "inputSchema": SCHEMA}
    base.update(config)
    return {"id": f"id-{name}", "type": "rest", "name": name, "description": "Fetch an item.",
            "config": base, "enabled": True, "deleted_at": None}


def _definition(name="echo", handler=None, workflows=USER_WORKFLOWS, **kwargs):
    return ToolDefinition(
        name=name, description="Echo.", input_schema={"type": "object"}, effect=EFFECT_READ_ONLY,
        source="builtin", workflows=frozenset(workflows), handler=handler or (lambda a, c: a), **kwargs,
    )


CTX = ToolContext(workflow="search")


# --- validation -----------------------------------------------------------------

@pytest.mark.parametrize("name", ["", "has space", "a" * 65, "semi;colon", None])
def test_invalid_tool_names_are_rejected(name):
    with pytest.raises(ToolDefinitionError):
        validate_name(name)


def test_unknown_effect_defaults_to_side_effecting():
    assert normalize_effect(None) == EFFECT_SIDE_EFFECTING
    assert normalize_effect("maybe") == EFFECT_SIDE_EFFECTING
    assert normalize_effect(EFFECT_READ_ONLY) == EFFECT_READ_ONLY


@pytest.mark.parametrize("schema", [None, [], {"type": "string"}, {"type": "object", "properties": 3}])
def test_invalid_schemas_are_rejected(schema):
    with pytest.raises(ToolDefinitionError):
        validate_input_schema(schema)


def test_workflows_default_to_all_and_reject_unknown():
    assert validate_workflows(None) == USER_WORKFLOWS
    assert validate_workflows(["search"]) == frozenset({"search"})
    with pytest.raises(ToolDefinitionError):
        validate_workflows(["deploy"])


def test_arguments_are_parsed_and_validated():
    assert validate_arguments(SCHEMA, '{"id": "a1"}') == {"id": "a1"}
    with pytest.raises(ToolCallRejected) as missing:
        validate_arguments(SCHEMA, "{}")
    assert missing.value.code == "invalid_arguments"
    with pytest.raises(ToolCallRejected):
        validate_arguments(SCHEMA, '{"id": "a1", "extra": 1}')
    with pytest.raises(ToolCallRejected):
        validate_arguments(SCHEMA, "{not json")
    with pytest.raises(ToolCallRejected):
        validate_arguments(SCHEMA, "[1]")
    with pytest.raises(ToolCallRejected) as big:
        validate_arguments({"type": "object"}, '{"x": "' + "y" * 40000 + '"}')
    assert big.value.code == "arguments_too_large"


# --- registry -------------------------------------------------------------------

def test_registry_exposes_builtins_and_valid_rest_tools():
    registry = ToolRegistry(managed_loader=lambda: [_rest_record()])

    names = [t.name for t in registry.tools_for("search")]

    assert names == ["api_contract_check", "api_contract_generate", "api_contract_operation", "api_contract_operations", "code_outline", "code_read", "code_repo_map",
                     "code_stats", "code_structural_search", "code_symbol_lookup",
                     "code_text_search", "frontend_ui_map", "get_item", "project_structure"]
    rest = next(t for t in registry.tools_for("search") if t.name == "get_item")
    assert rest.effect == EFFECT_SIDE_EFFECTING  # no effect configured
    assert "url" not in str(rest.model_schema())


def test_registry_never_exposes_chunk_code():
    registry = ToolRegistry(managed_loader=lambda: [])

    assert "chunk_code" not in {t.name for t in registry.tools_for("code_explain")}


def test_registry_skips_tools_it_cannot_expose():
    records = [
        dict(_rest_record("no_schema"), config={"url": "https://api.example.com"}),
        {"id": "m", "type": "mcp", "name": "docs", "description": "d", "config": {}, "enabled": True},
        {"id": "s", "type": "shell", "name": "routes", "description": "d", "config": {}, "enabled": True},
        _rest_record("project_structure"),
    ]
    registry = ToolRegistry(managed_loader=lambda: records)

    assert "no_schema" not in {t.name for t in registry.tools_for("search")}
    assert {"docs", "routes"}.isdisjoint(t.name for t in registry.tools_for("search"))
    reasons = {s.name: s.reason for s in registry.skipped_tools()}
    assert "inputSchema" in reasons["no_schema"]
    assert "runner" in reasons["docs"] and "runner" in reasons["routes"]
    assert "already used" in reasons["project_structure"]
    with pytest.raises(ToolCallRejected) as exc:
        registry.resolve_call("search", "docs", "{}")
    assert exc.value.code == "tool_unavailable"


def test_registry_respects_workflow_allowlists():
    registry = ToolRegistry(managed_loader=lambda: [_rest_record(workflows=["debug"])])

    assert "get_item" not in {t.name for t in registry.tools_for("search")}
    assert "get_item" in {t.name for t in registry.tools_for("debug")}
    assert registry.tools_for("not-a-workflow") == []
    with pytest.raises(ToolCallRejected) as exc:
        registry.resolve_call("search", "get_item", '{"id": "1"}')
    assert exc.value.code == "not_allowed"


def test_disabling_a_tool_rejects_stale_calls():
    records = [_rest_record()]
    registry = ToolRegistry(managed_loader=lambda: records)
    definition, args = registry.resolve_call("search", "get_item", '{"id": "1"}')
    assert definition.name == "get_item" and args == {"id": "1"}

    records[0] = dict(records[0], enabled=False)

    with pytest.raises(ToolCallRejected) as exc:
        registry.resolve_call("search", "get_item", '{"id": "1"}')
    assert exc.value.code == "tool_disabled"
    assert "get_item" not in {t.name for t in registry.tools_for("search")}


def test_deleted_and_unknown_tools_are_rejected():
    registry = ToolRegistry(managed_loader=lambda: [dict(_rest_record(), deleted_at="2026-11-10")])

    with pytest.raises(ToolCallRejected) as exc:
        registry.resolve_call("search", "get_item", "{}")
    assert exc.value.code == "unknown_tool"


def test_resolve_validates_arguments_before_execution():
    called = []
    strict = ToolDefinition(
        name="echo", description="Echo.", input_schema=SCHEMA, effect=EFFECT_READ_ONLY, source="builtin",
        workflows=USER_WORKFLOWS, handler=lambda a, c: called.append(a),
    )
    registry = ToolRegistry(builtins=[strict], managed_loader=lambda: [])

    with pytest.raises(ToolCallRejected):
        registry.resolve_call("search", "echo", "{}")
    assert called == []


# --- limits and execution ------------------------------------------------------------

def test_limits_load_config_and_cap_at_ceilings(tmp_path):
    path = tmp_path / "o.yml"
    path.write_text("agent_tools:\n  timeout_seconds: 9999\n  max_output_bytes: 100\n  max_calls_per_run: -1\n")

    limits = load_limits(path)

    assert limits.timeout_seconds == 300.0
    assert limits.max_output_bytes == 100
    assert limits.max_calls_per_run == 12
    assert load_limits(tmp_path / "missing.yml") == ToolLimits()


def test_tool_timeout_cannot_exceed_global_limit():
    assert effective_timeout(ToolLimits(timeout_seconds=30), 120) == 30
    assert effective_timeout(ToolLimits(timeout_seconds=30), 5) == 5
    assert effective_timeout(ToolLimits(timeout_seconds=30), None) == 30


def test_execute_call_returns_bounded_output():
    definition = _definition(handler=lambda a, c: "é" * 100)

    result = execute_call(definition, {}, CTX, ToolLimits(max_output_bytes=11))

    assert result["ok"] is True and result["truncated"] is True
    assert len(result["output"].encode("utf-8")) <= 11


def test_execute_call_reports_failures_and_timeouts_without_raising():
    def boom(a, c):
        raise RuntimeError("database exploded")

    failed = execute_call(_definition(handler=boom), {}, CTX, ToolLimits())
    assert failed["ok"] is False and failed["error"]["code"] == "tool_failed"

    slow = execute_call(_definition(handler=lambda a, c: time.sleep(0.5)), {}, CTX, ToolLimits(timeout_seconds=0.05))
    assert slow["ok"] is False and slow["error"]["code"] == "timeout"


def test_run_budget_stops_after_limit():
    budget = RunBudget(2)
    definition = _definition()

    results = [execute_call(definition, {}, CTX, ToolLimits(), budget) for _ in range(3)]

    assert [r["ok"] for r in results] == [True, True, False]
    assert results[2]["error"]["code"] == "tool_call_limit"


def test_bound_output_keeps_short_text():
    assert bound_output("abc", 10) == ("abc", False)


def test_project_structure_needs_a_workspace():
    result = execute_call(PROJECT_STRUCTURE, {}, ToolContext(workflow="search", workspace_path=None), ToolLimits())

    assert result["ok"] is False and result["error"]["code"] == "no_source"


# --- managed tool store ------------------------------------------------------------

def test_tool_store_validates_agent_fields(tmp_path):
    store = ToolStore(str(tmp_path / "tools.db"))

    saved = store.save({"type": "rest", "name": "t", "config": {
        "url": "https://a.example.com", "inputSchema": SCHEMA, "effect": "read_only",
        "workflows": ["search", "debug"], "allowedHosts": "A.example.com, b.example.com"}})

    assert saved["config"]["allowedHosts"] == ["a.example.com", "b.example.com"]
    assert saved["config"]["workflows"] == ["debug", "search"]
    with pytest.raises(ValueError):
        store.save({"type": "rest", "name": "t2", "config": {"effect": "dangerous"}})
    with pytest.raises(ValueError):
        store.save({"type": "rest", "name": "t3", "config": {"inputSchema": {"type": "string"}}})
    with pytest.raises(ValueError):
        store.save({"type": "rest", "name": "t4", "config": {"workflows": ["deploy"]}})
    legacy = store.save({"type": "shell", "name": "t5", "config": {"command": "php artisan route:list"}})
    assert legacy["config"] == {"command": "php artisan route:list"}


# --- REST adapter host checks ------------------------------------------------------

def _resolve_to(monkeypatch, mapping):
    def fake_getaddrinfo(host, port):
        if host not in mapping:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in mapping[host]]

    monkeypatch.setattr(tool_executor.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "::1", "fd00::1", "::ffff:10.0.0.1"])
def test_private_and_special_addresses_are_blocked(monkeypatch, ip):
    _resolve_to(monkeypatch, {"api.example.com": [ip]})

    assert tool_executor._blocked_host("api.example.com") is True


def test_public_172_addresses_are_allowed(monkeypatch):
    _resolve_to(monkeypatch, {"api.example.com": ["172.67.1.1"]})

    assert tool_executor._blocked_host("api.example.com") is False


def test_a_name_with_any_private_address_is_blocked(monkeypatch):
    _resolve_to(monkeypatch, {"api.example.com": ["93.184.216.34", "10.0.0.1"]})

    assert tool_executor._blocked_host("api.example.com") is True


def test_unresolvable_and_internal_names_are_blocked(monkeypatch):
    _resolve_to(monkeypatch, {})

    assert tool_executor._blocked_host("nowhere.example.com") is True
    assert tool_executor._blocked_host("localhost") is True
    assert tool_executor._blocked_host("host.docker.internal") is True


def _fake_requests(monkeypatch, status=200, payload=None):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=status, ok=200 <= status < 300, text="{}", json=lambda: payload or {})

    monkeypatch.setattr(tool_executor.requests, "request", fake_request)
    return calls


def test_rest_uses_template_host_when_allowlist_empty_and_escapes_arguments(monkeypatch):
    _resolve_to(monkeypatch, {"api.example.com": ["93.184.216.34"]})
    calls = _fake_requests(monkeypatch, payload={"ok": 1})
    record = _rest_record()

    result = tool_executor.execute_tool(record, arguments={"id": "../admin?x=1"})

    assert result == {"success": True, "status_code": 200, "result": {"ok": 1}}
    assert calls[0][1] == "https://api.example.com/items/..%2Fadmin%3Fx%3D1"
    assert calls[0][2]["allow_redirects"] is False


def test_rest_blocks_hosts_outside_allowlist(monkeypatch):
    _resolve_to(monkeypatch, {"evil.example.com": ["93.184.216.34"]})
    calls = _fake_requests(monkeypatch)
    record = _rest_record(url="https://{{host}}/x", allowedHosts=["api.example.com"])

    result = tool_executor.execute_tool(record, arguments={"host": "evil.example.com"})

    assert result["success"] is False and calls == []


def test_rest_does_not_follow_redirects(monkeypatch):
    _resolve_to(monkeypatch, {"api.example.com": ["93.184.216.34"]})
    _fake_requests(monkeypatch, status=302)

    result = tool_executor.execute_tool(_rest_record(), arguments={"id": "1"})

    assert result["success"] is False and "redirect" in result["error"]
