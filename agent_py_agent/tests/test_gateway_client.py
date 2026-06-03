from __future__ import annotations

"""gateway client regression tests."""

import json
import time
from types import SimpleNamespace

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayPaths,
    gateway_paths,
    gateway_response_path,
    read_json_file,
    write_gateway_request,
)
from agent_py_agent.agent.gateway_parts import runtime as gateway_runtime
from agent_py_agent.cli import gateway_client
from agent_py_agent.cli.chat_parts.gateway_client import poll_gateway_chunks


def test_default_gateway_entry_can_reach_chat_handler():
    """默认 gateway 入口必须能找到 chat 处理函数。"""

    assert callable(gateway_client.cmd_chat)


def test_gateway_json_polling_suppresses_stream_chunks(tmp_path, capsys):
    """`gateway ask --json` must keep stdout parseable JSON, without streamed text before it."""

    request_id = "gw-json"
    paths = GatewayPaths(
        root=tmp_path,
        pid=tmp_path / "gateway.pid",
        adapter_pid=tmp_path / "adapter.pid",
        state=tmp_path / "state.json",
        heartbeat=tmp_path / "heartbeat.json",
        stop_request=tmp_path / "stop.request",
        log=tmp_path / "gateway.log",
        inbox=tmp_path / "pending",
        processing=tmp_path / "processing",
        done=tmp_path / "done",
        failed=tmp_path / "failed",
        responses=tmp_path / "responses",
        history=tmp_path / "history.jsonl",
    )
    paths.processing.mkdir(parents=True)
    paths.responses.mkdir(parents=True)
    (paths.processing / f"{request_id}.chunks.jsonl").write_text(
        json.dumps({"text": "STREAMED"}) + "\n",
        encoding="utf-8",
    )
    response_path = paths.responses / f"{request_id}.json"
    response_path.write_text(json.dumps({"ok": True, "response": "DONE"}), encoding="utf-8")

    payload = gateway_client._poll_gateway_response(
        gateway_client.GatewayAskContext(
            agent=object(),
            paths=paths,
            request_id=request_id,
            request_path=paths.processing / f"{request_id}.json",
            response_path=response_path,
            timeout=1,
            stream_output=False,
        )
    )

    assert payload["response"] == "DONE"
    assert capsys.readouterr().out == ""


def test_gateway_poll_reports_bad_response_json(tmp_path):
    request_id = "gw-bad-response"
    paths = GatewayPaths(
        root=tmp_path,
        pid=tmp_path / "gateway.pid",
        adapter_pid=tmp_path / "adapter.pid",
        state=tmp_path / "state.json",
        heartbeat=tmp_path / "heartbeat.json",
        stop_request=tmp_path / "stop.request",
        log=tmp_path / "gateway.log",
        inbox=tmp_path / "pending",
        processing=tmp_path / "processing",
        done=tmp_path / "done",
        failed=tmp_path / "failed",
        responses=tmp_path / "responses",
        history=tmp_path / "history.jsonl",
    )
    paths.processing.mkdir(parents=True)
    paths.responses.mkdir(parents=True)
    response_path = paths.responses / f"{request_id}.json"
    response_path.write_text("{bad json", encoding="utf-8")

    payload = gateway_client._poll_gateway_response(
        gateway_client.GatewayAskContext(
            agent=object(),
            paths=paths,
            request_id=request_id,
            request_path=paths.processing / f"{request_id}.json",
            response_path=response_path,
            timeout=0.2,
            stream_output=False,
        )
    )

    assert payload["error_code"] == "GATEWAY_RESPONSE_LOAD_ERROR"
    assert payload["response_load_error"]["context"] == "gateway.cli.response.read"


def test_gateway_result_reports_bad_response_json(tmp_path, monkeypatch, capsys):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    request_id = "gw-result-bad-response"
    paths = gateway_paths(agent)
    paths.responses.mkdir(parents=True, exist_ok=True)
    gateway_response_path(paths, request_id).write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(gateway_client, "make_agent", lambda _args: agent)

    code = gateway_client.cmd_gateway_result(
        SimpleNamespace(request_id=request_id, json=True, show_prompt=False)
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "GATEWAY_RESPONSE_LOAD_ERROR"
    assert payload["response_load_error"]["context"] == "gateway.cli.result.response.read"


def test_gateway_stream_chunk_skips_bad_line_and_continues(tmp_path, capsys):
    class Spinner:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    chunk_path = tmp_path / "req.chunks.jsonl"
    chunk_path.write_text(
        "{bad json\n" + json.dumps({"text": "VISIBLE"}) + "\n",
        encoding="utf-8",
    )

    consumed = gateway_client._stream_chunk_lines(chunk_path, 0, Spinner())

    captured = capsys.readouterr()
    assert consumed == 2
    assert captured.out == "VISIBLE"
    assert "gateway stream chunk load_error" in captured.err


def test_chat_gateway_poll_drains_chunks_when_response_is_ready(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    response_path = tmp_path / "response.json"
    chunk_path.write_text(
        json.dumps({"text": "hello"}) + "\n" + json.dumps({"text": " world"}) + "\n",
        encoding="utf-8",
    )
    response_path.write_text(json.dumps({"ok": True, "response": "hello world"}), encoding="utf-8")
    seen: list[str] = []
    visible_chunks_ref = [0]

    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            9999999999,
            lambda chunk: seen.append(chunk) or True,
            [0],
            visible_chunks_ref,
        )
    )

    assert response["response"] == "hello world"
    assert seen == ["hello", " world"]
    assert visible_chunks_ref == [2]


def test_chat_gateway_poll_skips_bad_chunk_line_and_continues(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    response_path = tmp_path / "response.json"
    chunk_path.write_text(
        "{bad json\n" + json.dumps({"text": "ok"}) + "\n",
        encoding="utf-8",
    )
    response_path.write_text(json.dumps({"ok": True, "response": "done"}), encoding="utf-8")
    seen: list[str] = []
    visible_chunks_ref = [0]
    chunks_printed_ref = [0]

    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            9999999999,
            lambda chunk: seen.append(chunk) or True,
            chunks_printed_ref,
            visible_chunks_ref,
        )
    )

    assert response["response"] == "done"
    assert seen == ["ok"]
    assert chunks_printed_ref == [2]
    assert visible_chunks_ref == [1]


def test_chat_gateway_poll_reports_bad_response_json(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    response_path = tmp_path / "response.json"
    chunk_path.write_text("", encoding="utf-8")
    response_path.write_text("{bad json", encoding="utf-8")

    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            time.time() + 0.2,
            lambda _chunk: False,
            [0],
        )
    )

    assert response["error_code"] == "GATEWAY_RESPONSE_LOAD_ERROR"
    assert response["response_load_error"]["context"] == "gateway.chat.response.read"


def test_chat_gateway_poll_consumes_but_does_not_show_invisible_chunks(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    response_path = tmp_path / "response.json"
    chunk_path.write_text(
        json.dumps({"text": "   \n"}) + "\n",
        encoding="utf-8",
    )
    response_path.write_text(json.dumps({"ok": True, "response": "fallback"}), encoding="utf-8")

    chunks_printed_ref = [0]
    visible_chunks_ref = [0]
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            9999999999,
            lambda _chunk: False,
            chunks_printed_ref,
            visible_chunks_ref,
        )
    )

    assert response["response"] == "fallback"
    assert chunks_printed_ref == [1]
    assert visible_chunks_ref == [0]


def test_gateway_worker_continues_when_processing_lease_write_fails(tmp_path, monkeypatch):
    """A lease file write failure must not strand a user request in processing."""

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    request_id = "gwreq-lease-fallback"
    write_gateway_request(
        paths,
        {
            "id": request_id,
            "kind": "ask",
            "prompt": "lease fallback should still answer",
            "inject": [],
            "prompt_files": [],
            "save": False,
            "include_prompt": False,
            "created_at": 1.0,
            "status": "pending",
            "attempts": 0,
        },
    )

    def fail_processing_lease_write(path, payload):
        if path.name == f"{request_id}.json":
            raise OSError("simulated long-path lease write failure")
        return None

    monkeypatch.setattr(gateway_runtime, "write_json_file_atomic", fail_processing_lease_write)

    processed = gateway_runtime._process_gateway_requests(agent, paths)

    assert processed == 1
    assert gateway_response_path(paths, request_id).exists()
    assert (paths.done / f"{request_id}.json").exists()
    assert not (paths.processing / f"{request_id}.json").exists()
    assert read_json_file(gateway_response_path(paths, request_id))["ok"] is True
