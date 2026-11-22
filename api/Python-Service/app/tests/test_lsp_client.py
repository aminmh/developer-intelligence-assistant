"""Unit tests for the generic LSP client framing and dispatch logic."""

from lsp.client import LspClient, encode_message


def test_encode_message_length_header():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    msg = encode_message(payload)
    header, body = msg.split(b"\r\n\r\n", 1)
    length = int(header.split(b":", 1)[1].strip())
    assert header.startswith(b"Content-Length:")
    assert len(body) == length


def test_spawn_command_uses_interpreter():
    client = LspClient("server-bin", args=["--stdio"], language_id="py", interpreter="python3")
    cmd = client._spawn_command()
    assert cmd == ["python3", "server-bin", "--stdio"]


def test_spawn_command_without_interpreter():
    client = LspClient("server-bin", args=["language-server"], language_id="php")
    assert client._spawn_command() == ["server-bin", "language-server"]


def test_dispatch_response_correlates_by_id():
    client = LspClient("fake")
    queue = []
    client._pending[7] = queue
    written = []

    def fake_write(msg):
        written.append(msg)

    client._write = fake_write  # type: ignore[method-assign]
    client._dispatch({"jsonrpc": "2.0", "id": 7, "result": {"capabilities": {}}})
    assert queue and queue[0]["result"] == {"capabilities": {}}


def test_dispatch_publish_diagnostics_stores_per_uri():
    client = LspClient("fake")
    written = []

    def fake_write(msg):
        written.append(msg)

    client._write = fake_write  # type: ignore[method-assign]
    client._dispatch(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": "file:///tmp/x.py",
                "diagnostics": [{"message": "boom", "severity": 1}],
            },
        }
    )
    assert "file:///tmp/x.py" in client._diagnostics
    assert client._diagnostics["file:///tmp/x.py"][0]["diagnostics"][0]["message"] == "boom"
    assert "_t" in client._diagnostics["file:///tmp/x.py"][0]


def test_dispatch_acks_server_requests():
    client = LspClient("fake")
    written = []

    def fake_write(msg):
        written.append(msg)

    client._write = fake_write  # type: ignore[method-assign]
    client._dispatch({"jsonrpc": "2.0", "id": 98, "method": "window/workDoneProgress/create", "params": {}})
    client._dispatch({"jsonrpc": "2.0", "id": 99, "method": "workspace/configuration", "params": {}})
    assert written[0]["id"] == 98 and written[0]["result"] is None
    # workspace/configuration answers with one entry per requested item (LSP spec: an array).
    assert written[1]["id"] == 99 and written[1]["result"] == []


# ─── Workspace mode (index-time driver) ──────────────────────────────────────

def _scripted(client, responses):
    """Answer each written request from `responses[method]` synchronously."""
    written = []

    def fake_write(msg):
        written.append(msg)
        if "id" in msg and msg.get("method") in responses:
            client._dispatch({"jsonrpc": "2.0", "id": msg["id"], **responses[msg["method"]]})

    client._write = fake_write  # type: ignore[method-assign]
    client._proc = type("P", (), {"poll": lambda self: None})()
    return written


def test_workspace_configuration_gets_one_null_per_item():
    client = LspClient("fake")
    written = []
    client._write = written.append  # type: ignore[method-assign]
    client._dispatch({"jsonrpc": "2.0", "id": 3, "method": "workspace/configuration",
                      "params": {"items": [{"section": "a"}, {"section": "b"}]}})
    assert written == [{"jsonrpc": "2.0", "id": 3, "result": [None, None]}]


def test_references_sends_position_without_declaration():
    client = LspClient("fake", root_uri="file:///src")
    written = _scripted(client, {"textDocument/references": {"result": [{"uri": "file:///src/a.php"}]}})
    assert client.references("file:///src/b.php", 4, 7, timeout=1) == [{"uri": "file:///src/a.php"}]
    request = written[-1]
    assert request["params"]["position"] == {"line": 4, "character": 7}
    assert request["params"]["context"] == {"includeDeclaration": False}


def test_implementation_single_location_becomes_list():
    client = LspClient("fake", root_uri="file:///src")
    _scripted(client, {"textDocument/implementation": {"result": {"uri": "file:///src/x.php"}}})
    assert client.implementations("file:///src/y.php", 1, 1, timeout=1) == [{"uri": "file:///src/x.php"}]


def test_error_response_raises_connection_error():
    import pytest
    from lsp.client import LspConnectionError

    client = LspClient("fake", root_uri="file:///src")
    _scripted(client, {"textDocument/documentSymbol": {"error": {"code": -32603, "message": "Unknown document"}}})
    with pytest.raises(LspConnectionError, match="Unknown document"):
        client.document_symbols("file:///src/a.php", timeout=1)


def test_timeout_cancels_request_and_raises_timeout_error():
    import pytest
    from lsp.client import LspTimeoutError

    client = LspClient("fake", root_uri="file:///src")
    written = _scripted(client, {})
    with pytest.raises(LspTimeoutError):
        client.references("file:///src/a.php", 0, 0, timeout=0.05)
    rid = written[0]["id"]
    assert written[-1] == {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": rid}}
    assert rid not in client._pending


def test_supports_reads_provider_capabilities():
    client = LspClient("fake")
    client.capabilities = {"referencesProvider": True, "implementationProvider": False}
    assert client.supports("references")
    assert not client.supports("implementation")
    assert not client.supports("callHierarchy")


def test_workspace_initialize_passes_options():
    client = LspClient("fake", root_uri="file:///src", initialization_options={"composer.enable": False})
    written = _scripted(client, {"initialize": {"result": {"capabilities": {"referencesProvider": True}}}})
    client._proc = None  # not started yet

    def fake_spawn():
        client._proc = type("P", (), {"poll": lambda self: None})()

    client._spawn = fake_spawn  # type: ignore[method-assign]
    assert client.start() == {"referencesProvider": True}
    init = next(m for m in written if m.get("method") == "initialize")
    assert init["params"]["initializationOptions"] == {"composer.enable": False}
    assert init["params"]["rootUri"] == "file:///src"
    assert init["params"]["workspaceFolders"] == [{"uri": "file:///src", "name": "workspace"}]
