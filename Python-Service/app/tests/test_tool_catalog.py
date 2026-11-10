"""Tests for how tools are described to the LLM Tools settings card."""

from fastapi.testclient import TestClient

from agent_tools.catalog import agent_status, builtin_entries, pipeline_entries

SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}


def _record(**overrides):
    record = {"id": "t1", "type": "rest", "name": "get_item", "description": "Get an item.", "enabled": True,
              "deleted_at": None, "config": {"url": "https://api.example.com/{{id}}", "inputSchema": SCHEMA}}
    record.update(overrides)
    return record


def test_builtins_are_read_only_and_callable():
    entries = builtin_entries()

    names = [e["name"] for e in entries]
    assert "code_text_search" in names and "project_structure" in names and names == sorted(names)
    assert all(e["agent"]["callable"] and e["agent"]["effect"] == "read_only" and e["builtin"] for e in entries)
    assert all(e["agent"]["input_schema"]["type"] == "object" for e in entries)


def test_pipeline_tools_are_listed_but_never_offered():
    entries = pipeline_entries({"chunk_code": {"args": {}}, "export_structure": {"description": "Export"}})

    assert [e["id"] for e in entries] == ["pipeline:chunk_code", "pipeline:export_structure"]
    assert not any(e["agent"]["callable"] for e in entries)


def test_managed_tool_status_explains_what_is_missing():
    assert agent_status(_record()) | {"input_schema": None} == {
        "callable": True, "reason": None, "effect": "side_effecting",
        "workflows": sorted(agent_status(_record())["workflows"]), "input_schema": None}
    assert agent_status(_record(config={"url": "https://x.example.com", "effect": "read_only"}))["reason"].startswith(
        "REST tools need an explicit JSON argument schema")
    assert agent_status(_record(enabled=False))["reason"] == "Disabled."
    assert agent_status(_record(name="code_read"))["reason"] == "A built-in tool already uses this name."
    assert "runner" in agent_status(_record(type="mcp"))["reason"]
    assert agent_status(_record(config={**_record()["config"], "workflows": ["debug"]}))["workflows"] == ["debug"]


def test_tools_api_lists_builtins_and_model_access(monkeypatch, tmp_path):
    import main
    from infra.tool_store import ToolStore

    store = ToolStore(str(tmp_path / "tools.db"))
    monkeypatch.setattr(main, "tool_store", store)
    client = TestClient(main.app)

    created = client.post("/tools", json={"type": "rest", "name": "get_item", "description": "Get an item.",
                                          "config": {"url": "https://api.example.com/{{id}}", "inputSchema": SCHEMA,
                                                     "effect": "read_only"}}).json()
    listed = client.get("/tools").json()["tools"]

    assert created["tool"]["agent"]["callable"] is True and created["tool"]["agent"]["effect"] == "read_only"
    by_id = {t["id"]: t for t in listed}
    assert by_id["builtin:code_read"]["agent"]["callable"] is True
    assert by_id["pipeline:chunk_code"]["agent"]["callable"] is False
    assert client.put("/tools/builtin:code_read", json={}).status_code == 400
    assert client.delete("/tools/pipeline:chunk_code").status_code == 400
