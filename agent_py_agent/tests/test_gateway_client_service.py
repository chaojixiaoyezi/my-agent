from __future__ import annotations

import json
import socket
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts import client_service
from agent_py_agent.agent.gateway_parts.client_service import (
    execute_gateway_client_memory,
    read_gateway_client_history,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _paths(root: Path) -> GatewayPaths:
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "state.json",
        heartbeat=root / "heartbeat.json",
        stop_request=root / "stop.request",
        log=root / "gateway.log",
        inbox=root / "pending",
        processing=root / "processing",
        done=root / "done",
        failed=root / "failed",
        responses=root / "responses",
        history=root / "history.jsonl",
    )


def _post_json(port: int, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=2.0) as response:
        return json.loads(response.read().decode())


def test_gateway_client_memory_uses_resolved_owner_agent(monkeypatch) -> None:
    records = [SimpleNamespace(entry_id="m-1", kind="fact", role="user", content="one")]

    class Agent:
        memory = SimpleNamespace(all=lambda: records)

        def recall(self, query, limit):
            assert (query, limit) == ("needle", 3)
            return records

        def remember(self, content, *, kind):
            assert (content, kind) == ("saved", "fact")
            return SimpleNamespace(entry_id="m-2", kind=kind, role="user", content=content)

    owner_agent = Agent()
    monkeypatch.setattr(
        client_service,
        "resolve_gateway_scope_agent",
        lambda _base, _scope: owner_agent,
    )
    scope = GatewayControlScope("local-agent", "chat", "sess-test")

    recent = execute_gateway_client_memory(object(), scope=scope, operation="recent", limit=3)
    search = execute_gateway_client_memory(
        object(),
        scope=scope,
        operation="search",
        query="needle",
        limit=3,
    )
    saved = execute_gateway_client_memory(
        object(),
        scope=scope,
        operation="remember",
        content="saved",
    )

    assert recent.ok is True and recent.records[0]["entry_id"] == "m-1"
    assert search.ok is True and search.records[0]["content"] == "one"
    assert saved.ok is True and saved.records[0]["entry_id"] == "m-2"


def test_gateway_client_history_returns_only_complete_foreground_turns(monkeypatch) -> None:
    rows = [
        SimpleNamespace(
            role="user",
            content="first",
            metadata={"gateway_request_id": "req-1"},
        ),
        SimpleNamespace(
            role="assistant",
            content="answer",
            metadata={"gateway_request_id": "req-1"},
        ),
        SimpleNamespace(
            role="user",
            content="incomplete",
            metadata={"gateway_request_id": "req-2"},
        ),
        SimpleNamespace(
            role="assistant",
            content="background",
            metadata={
                "gateway_request_id": "req-3",
                "reason": "audit_finding",
                "background_delivery_reason": "scheduled",
            },
        ),
    ]

    class Store:
        def resolve_thread_report(self, **kwargs):
            assert kwargs["channel_conversation_id"] == "sess-test"
            return SimpleNamespace(thread_id="thread-1"), None

        def recent_messages_report(self, thread_id, *, limit):
            assert thread_id == "thread-1"
            assert limit == 16
            return rows, []

    owner_agent = SimpleNamespace(conversation_store=Store())
    monkeypatch.setattr(
        client_service,
        "resolve_gateway_scope_agent",
        lambda _base, _scope: owner_agent,
    )

    result = read_gateway_client_history(
        object(),
        scope=GatewayControlScope("local-agent", "chat", "sess-test"),
        max_turns=2,
    )

    assert result.ok is True
    assert result.thread_id == "thread-1"
    assert result.turns == (
        {
            "request_id": "req-1",
            "user_message": "first",
            "assistant_message": "answer",
        },
    )


def test_gateway_http_routes_client_memory_and_history(monkeypatch, tmp_path) -> None:
    from agent_py_agent.agent.gateway_parts import http_handlers

    memory_result = SimpleNamespace(
        to_dict=lambda: {"ok": True, "operation": "recent", "records": []}
    )
    history_result = SimpleNamespace(
        to_dict=lambda: {"ok": True, "thread_id": "thread-1", "turns": []}
    )
    monkeypatch.setattr(
        http_handlers,
        "execute_gateway_client_memory",
        lambda *_args, **_kwargs: memory_result,
    )
    monkeypatch.setattr(
        http_handlers,
        "read_gateway_client_history",
        lambda *_args, **_kwargs: history_result,
    )
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        _paths(tmp_path),
        params=GatewayHTTPServerParams(agent=object()),
    )
    server.start()
    try:
        common = {
            "user_id": "local-agent",
            "channel": "chat",
            "conversation_id": "sess-test",
        }
        memory = _post_json(port, "/client/memory", {**common, "operation": "recent"})
        history = _post_json(port, "/client/history", {**common, "limit": 3})
    finally:
        server.stop()

    assert memory == {"ok": True, "operation": "recent", "records": []}
    assert history == {"ok": True, "thread_id": "thread-1", "turns": []}
