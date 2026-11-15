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
    client._dispatch({"jsonrpc": "2.0", "id": 99, "method": "workspace/configuration", "params": {}})
    assert written and written[0]["id"] == 99 and written[0]["result"] is None
