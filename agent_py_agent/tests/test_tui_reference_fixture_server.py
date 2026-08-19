from __future__ import annotations

import http.client
import json
import threading

from scripts.tui_reference_fixture_server import FixtureConfig, make_server


def _request(port: int, method: str, path: str, payload: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    body = None if payload is None else json.dumps(payload)
    headers = {} if body is None else {"Content-Type": "application/json"}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, response.getheader("Content-Type"), data


def test_models_and_count_tokens_are_anthropic_shaped() -> None:
    server = make_server("127.0.0.1", 0, FixtureConfig(event_delay_ms=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, _, raw = _request(port, "GET", "/v1/models")
        assert status == 200
        models = json.loads(raw)
        assert models["data"][0]["type"] == "model"
        status, _, raw = _request(port, "POST", "/v1/messages/count_tokens", {"messages": []})
        assert status == 200
        assert json.loads(raw)["input_tokens"] >= 1
    finally:
        server.shutdown()
        server.server_close()


def test_markdown_stream_has_ordered_terminal_event() -> None:
    server = make_server("127.0.0.1", 0, FixtureConfig(event_delay_ms=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, content_type, raw = _request(
            port,
            "POST",
            "/v1/messages?beta=true",
            {
                "model": "fixture-model",
                "stream": True,
                "messages": [{"role": "user", "content": "TUI_FIXTURE_MARKDOWN"}],
            },
        )
        text = raw.decode()
        assert status == 200
        assert content_type == "text/event-stream"
        assert text.index("event: message_start") < text.index("event: content_block_start")
        assert "Fixture 标题" in text
        assert text.rstrip().endswith('data: {"type": "message_stop"}')
    finally:
        server.shutdown()
        server.server_close()


def test_permission_scenario_uses_structured_tool_result_transition(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    server = make_server(
        "127.0.0.1",
        0,
        FixtureConfig(default_scenario="permission", event_delay_ms=0, audit_path=audit_path),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base = {"model": "fixture-model", "stream": False}
        status, _, raw = _request(
            port,
            "POST",
            "/v1/messages",
            {**base, "messages": [{"role": "user", "content": "run fixture"}]},
        )
        first = json.loads(raw)
        assert status == 200
        assert first["stop_reason"] == "tool_use"
        assert first["content"][0]["name"] == "Bash"
        status, _, raw = _request(
            port,
            "POST",
            "/v1/messages",
            {
                **base,
                "messages": [
                    {"role": "user", "content": "run fixture"},
                    {"role": "assistant", "content": first["content"]},
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_tui_fixture", "content": "ok"}],
                    },
                ],
            },
        )
        second = json.loads(raw)
        assert status == 200
        assert second["stop_reason"] == "end_turn"
        audits = [json.loads(line) for line in audit_path.read_text().splitlines()]
        assert audits[-1]["has_tool_result"] is True
        assert "messages" not in audits[-1]
    finally:
        server.shutdown()
        server.server_close()


def test_non_loopback_listener_is_rejected() -> None:
    try:
        make_server("0.0.0.0", 0, FixtureConfig())
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("non-loopback fixture must be rejected")


def test_latest_scenario_marker_wins_in_a_resumed_conversation() -> None:
    server = make_server("127.0.0.1", 0, FixtureConfig(event_delay_ms=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, _, raw = _request(
            port,
            "POST",
            "/v1/messages",
            {
                "model": "fixture-model",
                "stream": False,
                "messages": [
                    {"role": "user", "content": "TUI_FIXTURE_MARKDOWN"},
                    {"role": "assistant", "content": "old response"},
                    {"role": "user", "content": "TUI_FIXTURE_THINKING"},
                ],
            },
        )
        response = json.loads(raw)
        assert status == 200
        assert response["content"][0]["type"] == "thinking"
    finally:
        server.shutdown()
        server.server_close()


def test_terminal_control_scenario_carries_real_untrusted_bytes() -> None:
    server = make_server("127.0.0.1", 0, FixtureConfig(event_delay_ms=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, _, raw = _request(
            port,
            "POST",
            "/v1/messages",
            {
                "model": "fixture-model",
                "stream": False,
                "messages": [
                    {"role": "user", "content": "TUI_FIXTURE_TERMINAL_CONTROL"}
                ],
            },
        )
        response = json.loads(raw)
        text = response["content"][0]["text"]
        assert status == 200
        assert "CONTROL_START" in text and "CONTROL_END" in text
        assert "\x1b[2J" in text
        assert "\x1b]52;c;c2VjcmV0\x07" in text
        assert "\x9b31m" in text
    finally:
        server.shutdown()
        server.server_close()
