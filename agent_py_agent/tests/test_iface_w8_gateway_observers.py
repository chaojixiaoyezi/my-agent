"""IFACE-W8：宿主观察接口、原文件轮询及认证/审批边界的运行时回归。"""

from __future__ import annotations

import inspect
import json
import threading
import time
from dataclasses import fields, replace
from types import SimpleNamespace
from typing import Protocol, get_type_hints

import pytest

from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_client as ask
from agent_py_agent.cli.chat_parts import gateway_client as chat
from agent_py_agent.cli.chat_parts import plain_handlers as plain
from agent_py_agent.cli.chat_parts import tui_worker_paths as tui


class RecordingObserver:
    """不继承协议且布尔值为假，仍是合法的宿主可调用实现。"""

    def __init__(self, observe):
        self.observe = observe
        self.requests = []

    def __bool__(self):
        return False

    def __call__(self, request):
        self.requests.append(request)
        return self.observe(request)


def _kwargs(mode, observer):
    if mode == "omitted":
        return {}
    return {"observer": None if mode == "none" else observer}


def _terminal(paths, request_id="req", **response):
    target = paths.terminal / f"{request_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "schema_version": "gateway_terminal_request.v1",
        "id": request_id,
        "status": response.get("status", "done"),
        "turn_phase": "closed",
        "terminal_response": {"id": request_id, **response},
    }), encoding="utf-8")
    return target


def _chunks(paths, *rows):
    target = paths.processing / "req.chunks.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return target


def _plain_context(paths, agent=None):
    return plain.PlainJobContext(
        job=SimpleNamespace(
            user="观察原请求", inject=[], prompt_files=[], show_prompt=False, system_task=None,
        ),
        agent=agent or SimpleNamespace(config=SimpleNamespace(agent_name="test")),
        args=SimpleNamespace(no_save=True, gateway_timeout=0),
        paths=paths, assistant_outputs=[], build_history_context=lambda: "",
        current_session_id="iface-w8",
    )


def _prepare_plain(monkeypatch, ctx):
    seen, rendered, submits = [], [], []
    monkeypatch.setattr(plain, "check_gateway_alive", lambda _paths: True)
    monkeypatch.setattr(plain, "_make_chunk_handler", lambda *a, **kw: (
        lambda text: seen.append(text) or True, [False],
    ))

    def submit(*args, **kwargs):
        submits.append(kwargs)
        return "req", ctx.paths.processing / "req.chunks.jsonl", ctx.paths.responses / "req.json"

    monkeypatch.setattr(plain, "submit_chat_request", submit)
    monkeypatch.setattr(plain, "_render_if_needed", lambda ctx, text, streamed: rendered.append((text, streamed)))
    monkeypatch.setattr(plain, "_print_gateway_timing", lambda response: None)
    return seen, rendered, submits


class TurnAdapter:
    def __init__(self):
        self.sink = None
        self.cleared = 0
        self.events = []

    def __call__(self, text):
        return True

    def on_gateway_event(self, event):
        self.events.append(event)
        return True

    def configure_gateway_permission_sink(self, sink):
        self.sink = sink

    def clear_gateway_permission_sink(self):
        self.sink = None
        self.cleared += 1


def _tui_context(paths, *, attached=True, agent=None):
    notices, clears = [], []
    runtime = SimpleNamespace(
        set_notice=lambda text, **kw: notices.append((text, kw)),
        clear_notice=lambda **kw: clears.append(kw),
    )
    cfg = SimpleNamespace(
        paths=paths, agent=agent, args=SimpleNamespace(gateway_timeout=0),
        state_lock=threading.Lock(), running_request_id_ref=["local-id"],
        last_token_estimate_ref=[0], stop_event=threading.Event(), tui_runtime=runtime,
        current_session_id="iface-w8",
    )
    job = SimpleNamespace(
        gateway_request_id="req" if attached else "", user="观察原请求", input_media=(),
        prompt_files=[], save=False, show_prompt=False, resume_context=None, system_task=None,
        tool_approval=True, rich_transcript=True,
    )
    return SimpleNamespace(cfg=cfg, job=job, turn_inject=[], turn_adapter=TurnAdapter()), notices, clears


@pytest.mark.parametrize("function", [plain._plain_gateway_handle, tui._worker_gateway_path, ask._poll_gateway_response])
def test_observer_is_optional_keyword_only(function):
    parameters = inspect.signature(function).parameters
    assert "observer" in parameters
    assert parameters["observer"].default is None
    assert parameters["observer"].kind is inspect.Parameter.KEYWORD_ONLY


def test_protocols_express_existing_request_and_result_contracts():
    for module, name, request_type, result_type in (
        (chat, "GatewayChatObserver", chat.GatewayChunkPollRequest, dict[str, object]),
        (ask, "GatewayAskObserver", ask.GatewayPollRequest, ask.GatewayPollResult),
    ):
        protocol = getattr(module, name, None)
        assert protocol is not None
        assert Protocol in protocol.__bases__
        hints = get_type_hints(protocol.__call__)
        assert hints == {"request": request_type, "return": result_type}
    for dto in (chat.ChatRequestContent, chat.GatewayChunkPollRequest, ask.GatewayAskParams, ask.GatewayPollRequest):
        assert not {"observer", "provider", "factory"}.intersection(field.name for field in fields(dto))


@pytest.mark.parametrize("mode", ["omitted", "none", "injected"])
@pytest.mark.parametrize("kind", ["assistant_commentary", "assistant_final"])
def test_plain_keeps_tail_drain_counters_and_terminal_authority(tmp_path, monkeypatch, mode, kind):
    paths = gateway_paths_from_root(tmp_path)
    ctx = _plain_context(paths)
    seen, rendered, submits = _prepare_plain(monkeypatch, ctx)
    chunk = _chunks(paths, {"kind": "model_delta", "text": "私有候选"})
    _terminal(paths, ok=True, response="最终正文")
    original = chat.read_gateway_terminal_response_file_when_ready

    def read_terminal(*args, **kwargs):
        response = original(*args, **kwargs)
        if response:
            with chunk.open("a", encoding="utf-8") as target:
                target.write(json.dumps({"kind": kind, "text": "尾部正文"}) + "\n")
        return response

    monkeypatch.setattr(chat, "read_gateway_terminal_response_file_when_ready", read_terminal)
    observer = RecordingObserver(chat.poll_gateway_chunks)
    if mode == "injected":
        monkeypatch.setattr(plain, "poll_gateway_chunks", lambda request: pytest.fail("不能忽略注入实现"))
    text, streamed = plain._plain_gateway_handle(ctx, **_kwargs(mode, observer))
    assert (text, streamed) == ("最终正文", kind == "assistant_final")
    assert seen == ["尾部正文"]
    assert rendered == [(text, streamed)]
    assert ctx.job.completion_status == "completed" and len(submits) == 1
    if mode == "injected":
        request, = observer.requests
        assert request.terminal_path == paths.terminal / "req.json"
        assert request.chunks_printed_ref == [2]
        assert request.visible_chunks_ref == [int(streamed)]
        assert request.activity_paths == (chunk, paths.inbox / "req.json", paths.processing / "req.json")


@pytest.mark.parametrize("mode", ["omitted", "none", "injected"])
@pytest.mark.parametrize("outcome", ["timeout", "failed", "interrupted"])
def test_plain_keeps_timeout_failure_and_silent_stop(tmp_path, monkeypatch, mode, outcome):
    paths = gateway_paths_from_root(tmp_path)
    ctx = _plain_context(paths)
    _seen, rendered, _submits = _prepare_plain(monkeypatch, ctx)
    if outcome != "timeout":
        _terminal(paths, ok=False, response="原回复", status=outcome,
                  **({"error_code": "INTERRUPTED"} if outcome == "interrupted" else {}))
    else:
        paths.responses.mkdir(parents=True, exist_ok=True)
        (paths.responses / "req.json").write_text('{"ok":true,"response":"孤立投影"}', encoding="utf-8")
    observer = RecordingObserver(chat.poll_gateway_chunks)
    if outcome == "timeout":
        with pytest.raises(TimeoutError, match="request_id=req"):
            plain._plain_gateway_handle(ctx, **_kwargs(mode, observer))
        assert rendered == [] and not hasattr(ctx.job, "completion_status")
    else:
        result = plain._plain_gateway_handle(ctx, **_kwargs(mode, observer))
        assert ctx.job.completion_status == outcome
        assert result == (("", False) if outcome == "interrupted" else ("原回复", False))
        assert rendered == ([] if outcome == "interrupted" else [("原回复", False)])


@pytest.mark.parametrize("mode", ["omitted", "none", "injected"])
def test_tui_waits_same_request_then_cleans_sink_and_notice(tmp_path, monkeypatch, mode):
    paths = gateway_paths_from_root(tmp_path)
    ctx, notices, clears = _tui_context(paths)
    chunk = _chunks(paths, {"kind": "assistant_commentary", "text": "先到"})
    ticks = [0.0]

    def advance(seconds):
        ticks[0] += seconds
        assert ticks[0] < 3, "未按原终态及时退出"
        if ticks[0] >= 1.2 and not (paths.terminal / "req.json").exists():
            with chunk.open("a", encoding="utf-8") as target:
                target.write(json.dumps({"kind": "assistant_final", "text": "后到"}) + "\n")
            _terminal(paths, ok=True, response="后到", current_context_token_estimate=42)

    clock = SimpleNamespace(time=lambda: 100 + ticks[0], monotonic=lambda: ticks[0], sleep=advance)
    monkeypatch.setattr(chat, "time", clock)
    monkeypatch.setattr(tui, "time", clock)
    monkeypatch.setattr(tui, "_submit_new_gateway_job", lambda *a: pytest.fail("不能重新提交"))
    observer = RecordingObserver(chat.poll_gateway_chunks)
    text, recorded, summary = tui._worker_gateway_path(ctx, **_kwargs(mode, observer))
    assert text == "后到" and recorded is False and summary.ok
    assert ctx.cfg.last_token_estimate_ref == [42]
    assert ctx.cfg.running_request_id_ref == ["req"]
    assert [event["text"] for event in ctx.turn_adapter.events] == ["先到", "后到"]
    assert len(notices) == 1 and notices[0][1]["notice_kind"] == "gateway_wait_timeout"
    assert ctx.turn_adapter.sink is None and ctx.turn_adapter.cleared == 1
    assert clears == [{"expected_kind": "gateway_wait_timeout"}]
    if mode == "injected":
        request, = observer.requests
        assert request.chunks_printed_ref == [2] and request.visible_chunks_ref == [1]
        assert request.on_chunk is ctx.turn_adapter
        assert request.is_wait_cancelled.__self__ is ctx.cfg.stop_event


@pytest.mark.parametrize("mode", ["omitted", "none", "injected"])
def test_tui_cancellation_is_local_and_always_cleans_up(tmp_path, mode):
    paths = gateway_paths_from_root(tmp_path)
    ctx, _notices, clears = _tui_context(paths)
    paths.processing.mkdir(parents=True, exist_ok=True)
    active = paths.processing / "req.json"
    active.write_text('{"status":"processing"}', encoding="utf-8")
    ctx.cfg.stop_event.set()
    with pytest.raises(InterruptedError, match="观察已退出"):
        tui._worker_gateway_path(ctx, **_kwargs(mode, RecordingObserver(chat.poll_gateway_chunks)))
    assert active.read_text(encoding="utf-8") == '{"status":"processing"}'
    assert not (paths.terminal / "req.json").exists()
    assert ctx.turn_adapter.sink is None and ctx.turn_adapter.cleared == 1
    assert clears == [{"expected_kind": "gateway_wait_timeout"}]


def _ask_context(paths, *, stream_output=True):
    return ask.GatewayAskContext(
        None, paths, "req", paths.inbox / "req.json", paths.responses / "req.json", 1,
        stream_output=stream_output,
    )


def _spinner(monkeypatch):
    calls = []
    spinner = SimpleNamespace(start=lambda: calls.append("start"), stop=lambda: calls.append("stop"))
    monkeypatch.setattr(ask, "ThinkingSpinner", lambda: spinner)
    return spinner, calls


@pytest.mark.parametrize("mode", ["omitted", "none", "injected"])
@pytest.mark.parametrize("kind", ["assistant_commentary", "assistant_final"])
@pytest.mark.parametrize("stream_output", [True, False])
def test_ask_keeps_tail_drain_display_counters_and_stream_flag(
    tmp_path, monkeypatch, capsys, mode, kind, stream_output,
):
    paths = gateway_paths_from_root(tmp_path)
    chunk = _chunks(paths, {"kind": "model_delta", "text": "私有候选"})
    _terminal(paths, ok=True, response="最终正文")
    spinner, calls = _spinner(monkeypatch)
    original = ask.read_gateway_terminal_response_file_when_ready
    states = []
    flush = ask._flush_stream_chunks

    def read_terminal(*args, **kwargs):
        response = original(*args, **kwargs)
        if response:
            with chunk.open("a", encoding="utf-8") as target:
                target.write(json.dumps({"kind": kind, "text": "尾部正文"}) + "\n")
        return response

    def track_flush(request, state):
        result = flush(request, state)
        states.append((state.chunks_printed, state.visible_chunks, state.chunk_offset))
        return result

    monkeypatch.setattr(ask, "read_gateway_terminal_response_file_when_ready", read_terminal)
    monkeypatch.setattr(ask, "_flush_stream_chunks", track_flush)
    observer = RecordingObserver(ask._wait_for_gateway_response)
    if mode == "injected":
        monkeypatch.setattr(ask, "_wait_for_gateway_response", lambda request: pytest.fail("不能忽略注入实现"))
    result = ask._poll_gateway_response(_ask_context(paths, stream_output=stream_output), **_kwargs(mode, observer))
    assert result.payload["response"] == "最终正文" and result.payload["ok"] is True
    assert result.streamed_text is (stream_output and kind == "assistant_final")
    assert capsys.readouterr().out == ("尾部正文" if stream_output else "")
    assert states == ([(1, 0, states[0][2]), (2, int(kind == "assistant_final"), chunk.stat().st_size)]
                      if stream_output else [(0, 0, 0), (0, 0, 0)])
    assert calls == (["start", "stop", "stop"] if stream_output else [])
    if mode == "injected":
        request, = observer.requests
        assert request.chunk_path == chunk and request.terminal_path == paths.terminal / "req.json"
        assert request.spinner is spinner and request.stream_output is stream_output


@pytest.mark.parametrize("mode", ["omitted", "none", "injected"])
def test_ask_orphan_projection_cannot_finish_wait(tmp_path, monkeypatch, mode):
    paths = gateway_paths_from_root(tmp_path)
    paths.responses.mkdir(parents=True, exist_ok=True)
    (paths.responses / "req.json").write_text('{"ok":true,"response":"孤立投影"}', encoding="utf-8")
    ticks = [0.0]

    def advance(seconds):
        ticks[0] += seconds
        assert ticks[0] < 2, "必须保留原超时上限"

    monkeypatch.setattr(ask, "time", SimpleNamespace(time=lambda: ticks[0], sleep=advance))
    _spinner_object, calls = _spinner(monkeypatch)
    result = ask._poll_gateway_response(
        _ask_context(paths), **_kwargs(mode, RecordingObserver(ask._wait_for_gateway_response)),
    )
    assert result == ask.GatewayPollResult({}, False)
    assert ticks[0] >= 1 and calls == ["start", "stop"]


@pytest.mark.parametrize("caller", ["plain", "tui", "ask"])
def test_custom_observer_can_supply_result_without_local_file_poll(tmp_path, monkeypatch, caller):
    paths = gateway_paths_from_root(tmp_path)
    payload = {"ok": True, "response": "宿主观察结果"}
    observer = RecordingObserver(lambda request: ask.GatewayPollResult(payload, True) if caller == "ask" else payload)
    if caller == "plain":
        ctx = _plain_context(paths)
        _prepare_plain(monkeypatch, ctx)
        monkeypatch.setattr(plain, "poll_gateway_chunks", lambda request: pytest.fail("不得读取本地文件"))
        assert plain._plain_gateway_handle(ctx, observer=observer) == ("宿主观察结果", False)
    elif caller == "tui":
        ctx, _notices, clears = _tui_context(paths)
        monkeypatch.setattr(chat, "poll_gateway_chunks", lambda request: pytest.fail("不得读取本地文件"))
        text, recorded, summary = tui._worker_gateway_path(ctx, observer=observer)
        assert (text, recorded, summary.ok) == ("宿主观察结果", False, True)
        assert ctx.turn_adapter.sink is None and clears
    else:
        _spinner_object, calls = _spinner(monkeypatch)
        monkeypatch.setattr(ask, "_wait_for_gateway_response", lambda request: pytest.fail("不得读取本地文件"))
        assert ask._poll_gateway_response(_ask_context(paths), observer=observer) == ask.GatewayPollResult(payload, True)
        assert calls == ["start", "stop"]
    assert len(observer.requests) == 1
    assert not (paths.terminal / "req.json").exists()


@pytest.mark.parametrize("caller", ["plain", "tui"])
@pytest.mark.parametrize("status", [401, 403])
def test_observer_cannot_bypass_authenticated_submit_rejection(tmp_path, monkeypatch, caller, status):
    from agent_py_agent.agent.gateway_parts import request_http_client

    paths = gateway_paths_from_root(tmp_path)
    posts = []
    agent = SimpleNamespace(
        config=SimpleNamespace(agent_name="test"),
        gateway_request_identity=lambda: {"user_id": "local:alice", "channel": "chat"},
        gateway_request_workspace=lambda: {},
    )
    observer = RecordingObserver(lambda request: pytest.fail("认证/范围拒绝后不可观察"))
    monkeypatch.setattr(request_http_client, "post_gateway_ask", lambda agent, body: posts.append(body) or (status, {}))
    monkeypatch.setattr(chat, "check_gateway_alive", lambda paths: True)
    monkeypatch.setattr(plain, "check_gateway_alive", lambda paths: True)
    with pytest.raises(PermissionError, match="未授权"):
        if caller == "plain":
            plain._plain_gateway_handle(_plain_context(paths, agent), observer=observer)
        else:
            ctx, _notices, _clears = _tui_context(paths, attached=False, agent=agent)
            tui._worker_gateway_path(ctx, observer=observer)
    assert observer.requests == [] and len(posts) == 1
    assert posts[0]["user_id"] == "local:alice"
    assert not {"observer", "provider", "factory"}.intersection(posts[0])
    assert not list(paths.inbox.glob("*.json")) and not (paths.terminal / "req.json").exists()


@pytest.mark.parametrize("decision_kind", ["approved", "denied", "tampered"])
def test_tui_observer_keeps_real_permission_sink_and_exact_binding(tmp_path, decision_kind):
    from agent_py_agent.agent.contracts.tool_approval import (
        ToolApprovalDecision,
        ToolApprovalRequest,
    )
    from agent_py_agent.agent.gateway_parts.permission_bridge import (
        wait_for_gateway_permission_decision,
    )

    paths = gateway_paths_from_root(tmp_path)
    ctx, _notices, _clears = _tui_context(paths)
    approval = ToolApprovalRequest(
        "permission", "req", "run_command", 1, 0, "授权", "synthetic-only",
        {"tool_name": "run_command", "run_id": "run", "operation_id": "operation",
         "idempotency_key": "once", "args_hash": "hash"},
        ({"id": "yes", "label": "允许", "decision": "approved"},
         {"id": "no", "label": "拒绝", "decision": "denied"}),
        expires_at=time.time() + 60,
    )

    def observe(request):
        assert ctx.turn_adapter.sink is not None
        assert request.on_event.__self__ is ctx.turn_adapter
        with pytest.raises(ValueError, match="id mismatch"):
            ctx.turn_adapter.sink(approval, ToolApprovalDecision("different", "approved"))
        decision = "denied" if decision_kind == "denied" else "approved"
        target = ctx.turn_adapter.sink(approval, ToolApprovalDecision("permission", decision))
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["request_id"] == "req" and payload["binding"] == approval.binding
        expected = approval if decision_kind != "tampered" else replace(approval, binding={**approval.binding, "run_id": "other"})
        result = wait_for_gateway_permission_decision(request.chunk_path, expected)
        assert result.decision == ("unavailable" if decision_kind == "tampered" else decision)
        assert result.approved is (decision_kind == "approved")
        assert not target.exists()
        return {"ok": True, "response": "原回复"}

    assert tui._worker_gateway_path(ctx, observer=RecordingObserver(observe))[2].ok
    assert ctx.turn_adapter.sink is None and ctx.turn_adapter.cleared == 1


def test_tui_observer_exception_keeps_original_exception_and_cleans_up(tmp_path):
    ctx, _notices, clears = _tui_context(gateway_paths_from_root(tmp_path))
    error = RuntimeError("synthetic observer failure")

    def observe(request):
        raise error

    with pytest.raises(RuntimeError) as caught:
        tui._worker_gateway_path(ctx, observer=RecordingObserver(observe))
    assert caught.value is error
    assert ctx.turn_adapter.sink is None and ctx.turn_adapter.cleared == 1
    assert clears == [{"expected_kind": "gateway_wait_timeout"}]


def test_injected_observers_do_not_bypass_real_http_authentication(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_http_client
    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
    from agent_py_agent.tests.test_local_ask_owner_transport import _client, _http_case

    # 复用已有真实 Client→HTTP 宿主→Client 夹具，只替换凭据来源，不 mock 认证裁决。
    with _http_case(tmp_path, monkeypatch, 0) as (agent, _gate, server):
        monkeypatch.setattr(request_http_client, "gateway_client_credentials", lambda _agent: GatewayClientCredentials(
            tmp_path, configured_token="synthetic-invalid",
        ))
        monkeypatch.setattr(chat, "check_gateway_alive", lambda paths: True)
        monkeypatch.setattr(plain, "check_gateway_alive", lambda paths: True)
        observer = RecordingObserver(lambda request: pytest.fail("实际宿主认证拒绝后不可观察"))
        client = _client(agent, server)
        before = {p: p.read_bytes() for p in server.paths.inbox.glob("*.json")}
        with pytest.raises(PermissionError, match="未授权"):
            plain._plain_gateway_handle(_plain_context(server.paths, client), observer=observer)
        ctx, _notices, _clears = _tui_context(server.paths, attached=False, agent=client)
        with pytest.raises(PermissionError, match="未授权"):
            tui._worker_gateway_path(ctx, observer=observer)
        assert observer.requests == []
        assert {p: p.read_bytes() for p in server.paths.inbox.glob("*.json")} == before
        assert not list(server.paths.terminal.glob("*.json"))


@pytest.mark.parametrize("caller", ["plain", "tui"])
def test_observer_does_not_skip_gateway_availability_guard(tmp_path, monkeypatch, caller):
    paths = gateway_paths_from_root(tmp_path)
    observer = RecordingObserver(lambda request: pytest.fail("服务不可用时不可观察"))
    monkeypatch.setattr(chat, "check_gateway_alive", lambda paths: False)
    monkeypatch.setattr(plain, "check_gateway_alive", lambda paths: False)
    with pytest.raises(RuntimeError, match="gateway 已停止"):
        if caller == "plain":
            plain._plain_gateway_handle(_plain_context(paths), observer=observer)
        else:
            ctx, _notices, _clears = _tui_context(paths, attached=False)
            tui._worker_gateway_path(ctx, observer=observer)
    assert observer.requests == [] and not list(paths.inbox.glob("*.json"))


@pytest.mark.parametrize("stream_output", [True, False])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_ask_observer_exception_stops_spinner_and_preserves_exception(
    tmp_path, monkeypatch, stream_output, error_type,
):
    _spinner_object, calls = _spinner(monkeypatch)
    error = error_type("synthetic observer failure")

    def observe(request):
        raise error

    with pytest.raises(error_type) as caught:
        ask._poll_gateway_response(
            _ask_context(gateway_paths_from_root(tmp_path), stream_output=stream_output),
            observer=RecordingObserver(observe),
        )
    assert caught.value is error
    assert calls == (["start", "stop"] if stream_output else [])


def test_ask_observer_failure_stops_real_spinner_thread(tmp_path, monkeypatch):
    from agent_py_agent.cli.thinking_spinner import ThinkingSpinner

    spinner = ThinkingSpinner(enabled=True, on_update=lambda frame: None, on_stop=lambda: None)
    monkeypatch.setattr(ask, "ThinkingSpinner", lambda: spinner)
    error = RuntimeError("synthetic observer failure")
    threads = []

    def observe(request):
        assert spinner._thread is not None and spinner._thread.is_alive()
        threads.append(spinner._thread)
        raise error

    try:
        with pytest.raises(RuntimeError) as caught:
            ask._poll_gateway_response(_ask_context(gateway_paths_from_root(tmp_path)), observer=observe)
        assert caught.value is error
        assert not spinner._running and spinner._thread is None
        assert len(threads) == 1 and not threads[0].is_alive()
    finally:
        # 红测也回收真实线程，不将待修缺陷变成测试资源泄漏。
        spinner.stop()
