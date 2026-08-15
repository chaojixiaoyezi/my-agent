from __future__ import annotations

"""gateway client regression tests."""

import json
import threading
import time
from types import SimpleNamespace

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayPaths,
    gateway_paths,
    gateway_response_path,
    read_json_file,
    write_gateway_request,
)
from agent_py_agent.agent.gateway_parts.request_worker import (
    GatewayAskParams,
    _process_gateway_requests,
    submit_gateway_ask,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli import gateway_client
from agent_py_agent.cli.chat_parts.gateway_client import poll_gateway_chunks


def test_default_gateway_entry_can_reach_chat_handler():
    """默认 gateway 入口必须能找到 chat 处理函数。"""

    assert callable(gateway_client.cmd_chat)


def test_submit_gateway_ask_uses_collision_safe_ids(tmp_path, monkeypatch):
    """Concurrent chat submits must not overwrite requests created in the same millisecond."""

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
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.request_worker.time.time", lambda: 1234567890.123)

    ids = [
        submit_gateway_ask(paths, params=GatewayAskParams(prompt=f"message {index}", save=False))[0]
        for index in range(20)
    ]

    assert len(set(ids)) == len(ids)
    assert len(list(paths.inbox.glob("*.json"))) == len(ids)


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

    result = gateway_client._poll_gateway_response(
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
    payload = result.payload

    assert payload["response"] == "DONE"
    assert result.streamed_text is False
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

    result = gateway_client._poll_gateway_response(
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
    payload = result.payload

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

    consumed = gateway_client._stream_chunk_lines(chunk_path, Spinner())

    captured = capsys.readouterr()
    assert consumed == 2
    assert captured.out == "VISIBLE"
    assert "gateway stream chunk load_error" in captured.err


def test_gateway_stream_reads_archived_chunk_file(tmp_path, capsys):
    class Spinner:
        def stop(self) -> None:
            pass

    processing = tmp_path / "requests" / "processing"
    done = tmp_path / "requests" / "done"
    done.mkdir(parents=True)
    chunk_path = processing / "req.chunks.jsonl"
    archived_chunk_path = done / "req.chunks.jsonl"
    archived_chunk_path.write_text(json.dumps({"text": "ARCHIVED"}) + "\n", encoding="utf-8")

    state = gateway_client.GatewayStreamState()
    consumed = gateway_client._stream_chunk_lines(chunk_path, Spinner(), state)

    captured = capsys.readouterr()
    assert consumed == 1
    assert captured.out == "ARCHIVED"


def test_typed_commentary_is_visible_but_does_not_hide_final_response(tmp_path, capsys):
    class Spinner:
        def stop(self) -> None:
            pass

    chunk_path = tmp_path / "req.chunks.jsonl"
    chunk_path.write_text(
        json.dumps({"kind": "assistant_commentary", "text": "我先检查。"}) + "\n",
        encoding="utf-8",
    )
    state = gateway_client.GatewayStreamState()

    gateway_client._stream_chunk_lines(chunk_path, Spinner(), state)

    assert capsys.readouterr().out == "我先检查。"
    assert state.chunks_printed == 1
    assert state.visible_chunks == 0


def test_typed_progress_obeys_verbose_level_and_never_hides_final(tmp_path, capsys):
    class Spinner:
        def stop(self) -> None:
            pass

    chunk_path = tmp_path / "req.chunks.jsonl"
    chunk_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "tool_progress",
                        "verbose_level": "off",
                        "text": "HIDDEN",
                    }
                ),
                json.dumps(
                    {
                        "kind": "tool_progress",
                        "verbose_level": "on",
                        "text": "VISIBLE",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    state = gateway_client.GatewayStreamState()

    gateway_client._stream_chunk_lines(chunk_path, Spinner(), state)

    assert capsys.readouterr().out == "VISIBLE"
    assert state.chunks_printed == 2
    assert state.visible_chunks == 0


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


def test_chat_gateway_poll_uses_processing_activity_as_inactivity_lease(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    processing_path = tmp_path / "req.json"
    response_path = tmp_path / "response.json"
    processing_path.write_text("{}", encoding="utf-8")

    def active_request() -> None:
        time.sleep(0.04)
        processing_path.write_text('{"heartbeat": 1}', encoding="utf-8")
        time.sleep(0.04)
        processing_path.write_text('{"heartbeat": 2}', encoding="utf-8")
        time.sleep(0.04)
        response_path.write_text(
            json.dumps({"ok": True, "response": "finished after initial deadline"}),
            encoding="utf-8",
        )

    worker = threading.Thread(target=active_request)
    worker.start()
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            time.time() + 0.05,
            lambda _chunk: False,
            [0],
            activity_paths=(processing_path,),
            inactivity_timeout_seconds=0.15,
        )
    )
    worker.join(timeout=1)

    assert response["response"] == "finished after initial deadline"


def test_chat_commentary_does_not_claim_terminal_response_stream(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    response_path = tmp_path / "response.json"
    chunk_path.write_text(
        json.dumps({"kind": "assistant_commentary", "text": "我先检查。"}) + "\n",
        encoding="utf-8",
    )
    response_path.write_text(
        json.dumps({"ok": True, "response": "检查完成。"}),
        encoding="utf-8",
    )
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

    assert response["response"] == "检查完成。"
    assert seen == ["我先检查。"]
    assert visible_chunks_ref == [0]


def test_chat_gateway_chunk_poll_reads_only_new_tail(tmp_path):
    from agent_py_agent.cli.chat_parts import gateway_client as chat_gateway_client
    from agent_py_agent.cli.chat_parts.gateway_client import ChunkFilePollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    chunk_path.write_text(json.dumps({"text": "first"}) + "\n", encoding="utf-8")
    seen: list[str] = []

    chunks, visible, offset = chat_gateway_client._poll_chunk_file(
        ChunkFilePollRequest(chunk_path, lambda chunk: seen.append(chunk) or True, 0, 0, 0)
    )
    with open(chunk_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"text": "second"}) + "\n")
    chunks, visible, offset = chat_gateway_client._poll_chunk_file(
        ChunkFilePollRequest(chunk_path, lambda chunk: seen.append(chunk) or True, chunks, visible, offset)
    )

    assert seen == ["first", "second"]
    assert chunks == 2
    assert visible == 2


def test_chat_gateway_chunk_poll_reads_archived_tail(tmp_path):
    from agent_py_agent.cli.chat_parts import gateway_client as chat_gateway_client
    from agent_py_agent.cli.chat_parts.gateway_client import ChunkFilePollRequest

    processing = tmp_path / "requests" / "processing"
    done = tmp_path / "requests" / "done"
    done.mkdir(parents=True)
    chunk_path = processing / "req.chunks.jsonl"
    archived_chunk_path = done / "req.chunks.jsonl"
    archived_chunk_path.write_text(json.dumps({"text": "archived"}) + "\n", encoding="utf-8")
    seen: list[str] = []

    chunks, visible, offset = chat_gateway_client._poll_chunk_file(
        ChunkFilePollRequest(chunk_path, lambda chunk: seen.append(chunk) or True, 0, 0, 0)
    )

    assert seen == ["archived"]
    assert chunks == 1
    assert visible == 1
    assert offset > 0


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


def test_gateway_response_poll_state_reads_only_when_file_changes(tmp_path):
    from agent_py_agent.agent.gateway_parts.response_renderer import (
        GatewayResponsePollState,
        read_gateway_response_file_when_ready,
    )

    response_path = tmp_path / "response.json"
    state = GatewayResponsePollState()

    assert (
        read_gateway_response_file_when_ready(
            response_path,
            state=state,
            context="gateway.test.response.read",
        )
        == {}
    )

    response_path.write_text(json.dumps({"ok": True, "response": "first"}), encoding="utf-8")
    assert (
        read_gateway_response_file_when_ready(
            response_path,
            state=state,
            context="gateway.test.response.read",
        )["response"]
        == "first"
    )
    assert (
        read_gateway_response_file_when_ready(
            response_path,
            state=state,
            context="gateway.test.response.read",
        )
        == {}
    )

    response_path.write_text(json.dumps({"ok": True, "response": "second", "extra": "changed"}), encoding="utf-8")
    assert (
        read_gateway_response_file_when_ready(
            response_path,
            state=state,
            context="gateway.test.response.read",
        )["response"]
        == "second"
    )


def test_chat_gateway_poll_consumes_but_does_not_show_invisible_chunks(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    response_path = tmp_path / "response.json"
    chunk_path.write_text(
        json.dumps({"text": "   \n"}) + "\n",
        encoding="utf-8",
    )
    response_path.write_text(json.dumps({"ok": True, "response": "archived response"}), encoding="utf-8")

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

    assert response["response"] == "archived response"
    assert chunks_printed_ref == [1]
    assert visible_chunks_ref == [0]


def test_plain_gateway_commentary_does_not_hide_terminal_response(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts import plain_handlers

    rendered: list[tuple[str, bool]] = []
    monkeypatch.setattr(plain_handlers, "check_gateway_alive", lambda _paths: True)
    monkeypatch.setattr(
        plain_handlers,
        "_make_chunk_handler",
        lambda *_args, **_kwargs: (lambda _chunk: True, [True]),
    )
    monkeypatch.setattr(
        plain_handlers,
        "submit_chat_request",
        lambda *_args, **_kwargs: (
            "req-1",
            tmp_path / "req-1.chunks.jsonl",
            tmp_path / "req-1.json",
        ),
    )

    def poll(request):
        assert request.visible_chunks_ref is not None
        request.visible_chunks_ref[0] = 0  # commentary was visible; terminal was not
        return {"ok": True, "response": "真正的最终回答"}

    monkeypatch.setattr(plain_handlers, "poll_gateway_chunks", poll)
    monkeypatch.setattr(
        plain_handlers,
        "_render_if_needed",
        lambda _ctx, text, streamed: rendered.append((text, streamed)),
    )
    monkeypatch.setattr(plain_handlers, "_print_gateway_timing", lambda *_args: None)
    ctx = plain_handlers.PlainJobContext(
        job=SimpleNamespace(
            user="测试",
            prompt_files=[],
            show_prompt=False,
            inject=[],
            system_task=None,
        ),
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="agent")),
        args=SimpleNamespace(no_save=False, gateway_timeout=1),
        paths=SimpleNamespace(),
        assistant_outputs=[],
        build_history_context=lambda: "",
    )

    text, streamed = plain_handlers._plain_gateway_handle(ctx)

    assert text == "真正的最终回答"
    assert streamed is False
    assert rendered == [("真正的最终回答", False)]


def test_plain_gateway_user_stop_is_silent(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts import plain_handlers

    rendered: list[tuple[str, bool]] = []
    monkeypatch.setattr(plain_handlers, "check_gateway_alive", lambda _paths: True)
    monkeypatch.setattr(
        plain_handlers,
        "_make_chunk_handler",
        lambda *_args, **_kwargs: (lambda _chunk: False, [False]),
    )
    monkeypatch.setattr(
        plain_handlers,
        "submit_chat_request",
        lambda *_args, **_kwargs: (
            "req-stop",
            tmp_path / "req-stop.chunks.jsonl",
            tmp_path / "req-stop.json",
        ),
    )
    monkeypatch.setattr(
        plain_handlers,
        "poll_gateway_chunks",
        lambda _request: {
            "ok": True,
            "status": "interrupted",
            "error_code": "INTERRUPTED",
            "response": "当前任务已停止。",
        },
    )
    monkeypatch.setattr(
        plain_handlers,
        "_render_if_needed",
        lambda _ctx, text, streamed: rendered.append((text, streamed)),
    )
    timing_calls: list[tuple] = []
    monkeypatch.setattr(
        plain_handlers,
        "_print_gateway_timing",
        lambda *_args: timing_calls.append(_args),
    )
    ctx = plain_handlers.PlainJobContext(
        job=SimpleNamespace(
            user="/stop",
            prompt_files=[],
            show_prompt=False,
            inject=[],
            system_task=None,
        ),
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="agent")),
        args=SimpleNamespace(no_save=False, gateway_timeout=1),
        paths=SimpleNamespace(),
        assistant_outputs=[],
        build_history_context=lambda: "",
    )

    text, streamed = plain_handlers._plain_gateway_handle(ctx)

    assert text == ""
    assert streamed is False
    assert rendered == []
    assert timing_calls == []


def test_plain_local_user_stop_is_silent(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts import plain_handlers

    rendered: list[tuple[str, bool]] = []
    result = SimpleNamespace(
        response="当前任务已停止。",
        runtime_status="cancelled",
        runtime_reason="user_stop",
        tool_rounds=0,
        prompt_token_estimate=0,
        memory_resume_context_injected=False,
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(agent_name="agent"),
        run=lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(plain_handlers, "register_interruptible", lambda *_args: nullcontext())
    monkeypatch.setattr(
        plain_handlers,
        "_make_chunk_handler",
        lambda *_args, **_kwargs: (lambda _chunk: False, [False]),
    )
    monkeypatch.setattr(
        plain_handlers,
        "_render_if_needed",
        lambda _ctx, text, streamed: rendered.append((text, streamed)),
    )
    timing_calls: list[tuple] = []
    monkeypatch.setattr(
        plain_handlers,
        "_print_local_timing",
        lambda *_args: timing_calls.append(_args),
    )
    ctx = plain_handlers.PlainJobContext(
        job=SimpleNamespace(
            user="长任务",
            prompt_files=[],
            show_prompt=False,
            inject=[],
            system_task=None,
            request_id="req-local-stop",
        ),
        agent=agent,
        args=SimpleNamespace(no_save=False),
        paths=SimpleNamespace(),
        assistant_outputs=[],
        build_history_context=lambda: "",
    )

    text, streamed = plain_handlers._plain_local_handle(ctx)

    assert text == ""
    assert streamed is False
    assert rendered == []
    assert timing_calls == []


def test_gateway_worker_reports_processing_lease_write_failure(tmp_path, monkeypatch):
    """A lease file write failure must surface as a local gateway failure."""

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
    request_id = "gwreq-lease-write-error"
    write_gateway_request(
        paths,
        {
            "id": request_id,
            "kind": "ask",
            "prompt": "lease write error should be reported",
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

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.request_worker.update_json_file_atomic",
        lambda path, _updater, **_kwargs: fail_processing_lease_write(path, {}),
    )

    processed = _process_gateway_requests(agent, paths)

    assert processed == 1
    assert gateway_response_path(paths, request_id).exists()
    assert (paths.failed / f"{request_id}.json").exists()
    assert not (paths.processing / f"{request_id}.json").exists()
    response = read_json_file(gateway_response_path(paths, request_id))
    assert response["ok"] is False
    assert response["error_code"] == "GATEWAY_REQUEST_PROCESSING_STATE_WRITE_ERROR"
    assert response["processing_state_error"]["context"] == "gateway.worker.processing_state.write"
