from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from agent_py_agent.agent.tooling.lsp_client import LspTool

_FAKE_LSP = r'''
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii").partition(":")
        headers[key.lower().strip()] = value.strip()
    length = int(headers["content-length"])
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

def send(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {"hoverProvider": True}}})
    elif method == "textDocument/hover":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"contents": "fake hover"}})
    elif method == "textDocument/didOpen":
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"diagnostics": [{"message": "fake diagnostic"}]}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": request_id, "result": None})
    elif method == "exit":
        break
'''


def _payload(result):
    assert result.ok, result.output
    return json.loads(result.output)


def _fake_tool(tmp_path: Path) -> tuple[LspTool, Path]:
    server = tmp_path / "fake_lsp.py"
    server.write_text(_FAKE_LSP, encoding="utf-8")
    source = tmp_path / "app.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    tool = LspTool(
        tmp_path,
        [tmp_path],
        "",
        {"python": {"command": sys.executable, "args": [str(server)], "timeout": 3}},
    )
    return tool, source


def test_lsp_tool_runs_real_json_rpc_server(tmp_path: Path) -> None:
    tool, source = _fake_tool(tmp_path)

    status = _payload(tool.execute({"action": "status"}))
    assert status["configured"] == ["python"]

    hover = _payload(
        tool.execute(
            {
                "action": "request",
                "server": "python",
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": source.as_uri()},
                    "position": {"line": 0, "character": 2},
                },
            }
        )
    )
    assert hover["result"] == {"contents": "fake hover"}

    opened = _payload(
        tool.execute(
            {
                "action": "open_document",
                "server": "python",
                "path": "app.py",
                "language_id": "python",
            }
        )
    )
    assert opened["status"] == "opened"

    deadline = time.time() + 3
    notifications = []
    while time.time() < deadline and not notifications:
        notifications = _payload(
            tool.execute({"action": "diagnostics", "server": "python"})
        )["notifications"]
        time.sleep(0.02)
    assert notifications[0]["params"]["diagnostics"][0]["message"] == "fake diagnostic"

    closed = _payload(tool.execute({"action": "close", "server": "python"}))
    assert closed["status"] == "closed"


def test_lsp_tool_rejects_unconfigured_server(tmp_path: Path) -> None:
    result = LspTool(tmp_path, [tmp_path], "", {}).execute(
        {
            "action": "request",
            "server": "python",
            "method": "textDocument/hover",
            "params": {},
        }
    )

    assert result.ok is False
    assert result.error_code == "TOOL_UNAVAILABLE"


def test_lsp_open_document_stays_in_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    tool = LspTool(
        tmp_path,
        [tmp_path],
        "",
        {"python": {"command": sys.executable, "args": ["-c", "pass"]}},
    )

    result = tool.execute(
        {
            "action": "open_document",
            "server": "python",
            "path": str(outside),
            "language_id": "python",
        }
    )

    assert result.ok is False
    assert result.error_code == "PATH_OUTSIDE_WORKSPACE"
