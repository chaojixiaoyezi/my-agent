from __future__ import annotations

"""gateway client regression tests."""

import json

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
