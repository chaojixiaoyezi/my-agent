from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_chat_module_import_does_not_load_full_agent_core() -> None:
    """Fast chat dispatch must not import SimpleAgent before route selection."""
    script = (
        "import sys; import agent_py_agent.cli.chat; "
        "assert 'agent_py_agent.agent.core' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_gateway_chat_client_resolves_owner_runtime_without_full_agent(
    tmp_path,
    monkeypatch,
) -> None:
    """Gateway client should share canonical owner paths without constructing SimpleAgent."""
    from agent_py_agent.cli.bootstrap import DEFAULT_CONFIG
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG_LAYERS", raising=False)
    args = SimpleNamespace(config=str(DEFAULT_CONFIG), workspace_root=str(workspace))

    client = make_gateway_chat_client(args)

    assert client.gateway_client_only is True
    assert client.root == workspace.resolve()
    assert client.home_paths.owner_home_dir == home / "owners" / "local" / "main"
    assert str(client.config.gateway_workspace).startswith(str(client.home_paths.owner_workspace_dir))
    assert not hasattr(client, "full_agent")
    with pytest.raises(AttributeError):
        _ = client.conversation_store


def test_gateway_chat_clients_in_different_projects_share_one_owner_service(
    tmp_path,
    monkeypatch,
) -> None:
    """Different TUI cwd values must share transport but retain project-scoped state."""
    from agent_py_agent.cli.bootstrap import DEFAULT_CONFIG
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client

    home = tmp_path / "home"
    first_workspace = tmp_path / "project-a"
    second_workspace = tmp_path / "project-b"
    first_workspace.mkdir()
    second_workspace.mkdir()
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG_LAYERS", raising=False)

    first = make_gateway_chat_client(
        SimpleNamespace(config=str(DEFAULT_CONFIG), workspace_root=str(first_workspace))
    )
    second = make_gateway_chat_client(
        SimpleNamespace(config=str(DEFAULT_CONFIG), workspace_root=str(second_workspace))
    )

    expected_gateway = (
        home
        / "owners"
        / "local"
        / "main"
        / "workspace"
        / "runtime"
        / "services"
        / "gateway"
    )
    assert first.config.gateway_workspace == str(expected_gateway)
    assert second.config.gateway_workspace == str(expected_gateway)
    assert first.config.local_store_path != second.config.local_store_path
    assert first.root == first_workspace.resolve()
    assert second.root == second_workspace.resolve()


def test_gateway_chat_client_posts_typed_session_lifecycle(monkeypatch, tmp_path) -> None:
    """Lightweight close should use Gateway's structured lifecycle endpoint."""
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    captured = {}

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"disposition": "memory_curator_request"}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = SimpleNamespace(gateway_port=18420)
    client = GatewayChatClientAgent(
        SimpleNamespace(),
        config,
        tmp_path,
        [tmp_path],
        SimpleNamespace(),
    )

    assert client.request_session_lifecycle("sess-test", event="close") is True
    assert captured["url"] == "http://127.0.0.1:18420/ask"
    assert captured["payload"] == {
        "kind": "session_lifecycle",
        "event": "close",
        "user_id": "local-agent",
        "channel": "chat",
        "conversation_id": "sess-test",
    }
    assert captured["timeout"] == 1.0


def test_gateway_chat_client_memory_and_history_stay_on_typed_http(monkeypatch, tmp_path) -> None:
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    captured: list[tuple[str, dict]] = []

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.body).encode()

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode())
        captured.append((request.full_url, payload))
        if request.full_url.endswith("/client/memory"):
            return Response({"ok": True, "records": [{"content": "saved"}]})
        return Response(
            {
                "ok": True,
                "turns": [{"user_message": "问", "assistant_message": "答"}],
                "load_errors": [],
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = GatewayChatClientAgent(
        SimpleNamespace(),
        SimpleNamespace(gateway_port=18420),
        tmp_path,
        [tmp_path],
        SimpleNamespace(),
    )

    memory = client.request_memory(
        operation="remember",
        session_id="sess-test",
        content="saved",
        limit=1,
    )
    history = client.request_chat_history("sess-test", max_turns=3)

    assert memory["records"] == [{"content": "saved"}]
    assert history["turns"] == [{"user_message": "问", "assistant_message": "答"}]
    assert captured[0][0].endswith("/client/memory")
    assert captured[0][1]["conversation_id"] == "sess-test"
    assert captured[1][0].endswith("/client/history")


def test_gateway_chat_client_posts_correlated_active_turn_input(monkeypatch, tmp_path) -> None:
    from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions
    from agent_py_agent.cli.chat_client_context import (
        ActiveTurnInputDelivery,
        GatewayChatClientAgent,
    )

    captured: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "request_id": "gwreq-msg-active-1",
                    "status": "steered",
                    "disposition": "active_turn_input",
                    "delivery_status": "accepted",
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = GatewayChatClientAgent(
        SimpleNamespace(),
        SimpleNamespace(gateway_port=18420),
        tmp_path,
        [tmp_path],
        SimpleNamespace(),
    )

    result = client.request_active_turn_input(
        "sess-steer",
        message="把导出格式改成 JSON",
        message_id="steer-client-1",
        expected_turn_id="gwreq-active-1",
        execution_options=GatewayAskExecutionOptions(
            inject=("遵守项目规范",),
            prompt_files=("spec.md",),
            save=False,
            resume_context=True,
            tool_approval=True,
            rich_transcript=True,
        ),
    )
    assert result.delivery is ActiveTurnInputDelivery.ACCEPTED
    assert result.request_id == "gwreq-msg-active-1"
    assert captured == {
        "url": "http://127.0.0.1:18420/ask",
        "payload": {
            "goal": "把导出格式改成 JSON",
            "user_id": "local-agent",
            "channel": "chat",
            "conversation_id": "sess-steer",
            "metadata": {
                "message_id": "steer-client-1",
                "expected_turn_id": "gwreq-active-1",
            },
            "workspace": {
                "cwd": str(tmp_path.resolve()),
                "roots": [str(tmp_path.resolve())],
            },
            "inject": ["遵守项目规范"],
            "prompt_files": ["spec.md"],
            "save": False,
            "include_prompt": False,
            "resume_context": True,
            "client_capabilities": {
                "tool_approval": True,
                "rich_transcript": True,
            },
        },
        "timeout": 2.0,
    }


def test_gateway_chat_client_rejects_non_steer_control_result(monkeypatch, tmp_path) -> None:
    from agent_py_agent.cli.chat_client_context import (
        ActiveTurnInputDelivery,
        GatewayChatClientAgent,
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"ok": False, "kind": "steer", "delivery_status": "rejected"}
            ).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda _request, timeout: Response())
    client = GatewayChatClientAgent(
        SimpleNamespace(),
        SimpleNamespace(gateway_port=18420),
        tmp_path,
        [tmp_path],
        SimpleNamespace(),
    )

    result = client.request_active_turn_input(
        "sess-steer",
        message="刚好结束时不要插错任务",
        message_id="steer-client-race",
        expected_turn_id="gwreq-finished",
    )
    assert result.delivery is ActiveTurnInputDelivery.REJECTED


def test_gateway_chat_client_preserves_unknown_transport_result(monkeypatch, tmp_path) -> None:
    from agent_py_agent.cli.chat_client_context import (
        ActiveTurnInputDelivery,
        GatewayChatClientAgent,
    )

    def fail_urlopen(_request, timeout):
        del timeout
        raise TimeoutError("response lost after server commit")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    client = GatewayChatClientAgent(
        SimpleNamespace(),
        SimpleNamespace(gateway_port=18420),
        tmp_path,
        [tmp_path],
        SimpleNamespace(),
    )

    result = client.request_active_turn_input(
        "sess-steer",
        message="保持同一条消息继续对账",
        message_id="steer-client-unknown",
        expected_turn_id="gwreq-active-unknown",
    )
    assert result.delivery is ActiveTurnInputDelivery.UNKNOWN
