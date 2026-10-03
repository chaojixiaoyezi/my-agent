"""LLM: tests for model generation boundary behavior inside the tool loop.

给人看的解释：
这里测试的不是某个具体模型厂商，而是 my-agent 调模型的公共边界：
如果后端请求卡住，工具循环必须按 request_timeout 退出，方便父级后续恢复任务。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation_module
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _effective_model_request_timeout_seconds,
    _publish_transport_retry,
    generate_model_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError, ProviderTimeoutError
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.tooling.content_transport_policy import RECOVERY_WRITE_CHUNK_CHARS
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


def test_transport_retry_projection_uses_structured_attempt_event() -> None:
    projected: list[dict[str, object]] = []

    class Sink:
        def write_provider_retry(self, **payload: object) -> bool:
            projected.append(dict(payload))
            return True

    _publish_transport_retry(
        Sink(),
        {
            "status": "failed",
            "retry_scheduled": True,
            "retry_attempt": 2,
            "retry_total": 3,
            "retry_wait_seconds": 5.0,
            "error_type": "URLError",
        },
    )
    _publish_transport_retry(
        Sink(),
        {"status": "failed", "retry_scheduled": False, "error_type": "URLError"},
    )

    assert projected == [
        {
            "scope": "transport",
            "attempt": 2,
            "total": 3,
            "delay_seconds": 5.0,
            "error_type": "URLError",
        }
    ]


class _BlockingBackend:
    name = "blocking-test-backend"

    def __init__(self) -> None:
        self.entered = threading.Event()

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.entered.set()
        time.sleep(0.08)
        return ModelResponse(text="late response", backend=self.name)


class _SubmissionInspectingBackend:
    name = "submission-inspecting-backend"

    def __init__(self, store: ConversationStore, dedupe_key: str) -> None:
        self.store = store
        self.dedupe_key = dedupe_key
        self.observed_status = ""

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        receipt = self.store.guidance.receipt(self.dedupe_key)
        self.observed_status = str(receipt.status if receipt is not None else "")
        assert receipt is not None and receipt.submission_id
        return ModelResponse(text="accepted", backend=self.name)


class _InterruptibleBlockingBackend:
    name = "interruptible-blocking-test-backend"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.closed = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.worker_threads: list[threading.Thread] = []
        self.closed_events: list[threading.Event] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        from agent_py_agent.agent.concurrency.interrupt import (
            is_interrupted,
            register_interrupt_callback,
        )

        del prompt, on_chunk
        worker = threading.current_thread()
        closed = threading.Event()
        self.worker_thread = worker
        self.worker_threads.append(worker)
        self.closed = closed
        self.closed_events.append(closed)
        with register_interrupt_callback(closed.set):
            self.entered.set()
            closed.wait(timeout=5)
            if is_interrupted():
                raise InterruptedError("模型传输已关闭")
        return ModelResponse(text="late response", backend=self.name)


class _LocalResponseTransportBackend:
    name = "local-response-transport-test-backend"
    model_name = "synthetic-model"

    def __init__(self, request_factory, *, owns_stream_timeout: bool):
        self._request_factory = request_factory
        self.stream_enabled = owns_stream_timeout
        self.stream_timeout_is_idle = owns_stream_timeout
        self.worker_threads: list[threading.Thread] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        self.worker_threads.append(threading.current_thread())
        request = self._request_factory()
        if request.url.startswith(("ws://", "wss://")):
            from agent_py_agent.agent.backends.responses_websocket import iter_responses_websocket

            lines = iter_responses_websocket(request)
        else:
            from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter

            lines = post_stream_iter(request)
        try:
            list(lines)
        finally:
            lines.close()
        return ModelResponse(text="", backend=self.name)


def _fake_response_request(api_base: str, timeout: float):
    from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest

    return GatewayRequest(
        api_base=api_base,
        api_key="synthetic-test-token",
        path="/responses",
        payload={"model": "synthetic-model", "input": [], "stream": True},
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        timeout=timeout,
        connect_timeout=0.5,
        first_event_timeout=timeout,
        max_retries=0,
    )


@contextmanager
def _fake_sse_service(tmp_path, *, send_first_event: bool):
    request_seen = threading.Event()
    release_handlers = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body_size = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(body_size)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            if send_first_event:
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"x"}\n\n')
                self.wfile.flush()
            request_seen.set()
            release_handlers.wait(timeout=10.0)

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.block_on_close = False
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = int(server.server_address[1])
    (tmp_path / "fake-sse-port.txt").write_text(str(port), encoding="utf-8")
    try:
        yield f"http://127.0.0.1:{port}", request_seen
    finally:
        release_handlers.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)


# LLM: The fake handler consumes only its local test connection and exits on close/release without contacting a provider.
# 函数用途: 接收假 Responses 请求，可选发送一个首事件，然后等待测试释放。
def _serve_fake_websocket_connection(connection, request_seen, release_handler, send_first_event):
    try:
        connection.recv(timeout=2.0)
    except Exception:
        return
    request_seen.set()
    if send_first_event:
        try:
            connection.send('{"type":"response.output_text.delta","delta":"x"}')
        except Exception:
            return
    while not release_handler.is_set():
        try:
            connection.recv(timeout=0.05)
        except TimeoutError:
            continue
        except Exception:
            return


# LLM: Bind a randomized loopback websocket listener to a tempfile record and always release its handler on exit.
# 函数用途: 构造无需真实供应商、随机端口的 Responses WebSocket 服务。
@contextmanager
def _fake_websocket_service(tmp_path, *, send_first_event: bool = False):
    from websockets.sync.server import serve

    request_seen = threading.Event()
    release_handler = threading.Event()

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    # LLM: Bind this server instance's synchronization events to its handler; it cannot reach another test's sockets.
    # 函数用途: 把本例的释放标记交给本地假服务连接处理器。
    def handler(connection):
        return _serve_fake_websocket_connection(connection, request_seen, release_handler, send_first_event)

    server = serve(handler, sock=listener, open_timeout=1.0, close_timeout=1.0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    (tmp_path / "fake-websocket-port.txt").write_text(str(port), encoding="utf-8")
    try:
        yield f"ws://127.0.0.1:{port}", request_seen
    finally:
        release_handler.set()
        server.shutdown()
        server_thread.join(timeout=2.0)
        listener.close()


class _StreamingLongWriteBackend:
    name = "streaming-long-write-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        parts = [
            '[TOOL_CALL]\n{"tool":"write_file","filesystem":{"path":"site/index.html","content":"',
            "A" * 128,
            "B" * 128,
            "C" * 128,
        ]
        text = ""
        for part in parts:
            self.chunks_emitted += 1
            text += part
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _StreamingRecoveryLongWriteBackend:
    name = "streaming-recovery-long-write-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        parts = [
            '[TOOL_CALL]\n{"tool":"write_file","path":"reports/final.md","content":"',
            "A" * (RECOVERY_WRITE_CHUNK_CHARS + 1),
            "B" * 5000,
        ]
        text = ""
        for part in parts:
            self.chunks_emitted += 1
            text += part
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _StreamingRepeatedToolBackend:
    name = "streaming-repeated-tool-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        parts = [
            '[TOOL_CALL]\n{"tool":"read_file","path":"final.html"}\n[/TOOL_CALL]',
            '\n[TOOL_CALL]\n{"tool":"write_file","action":"append"',
            ',"session_id":"same","chunk_index":3,"content":"duplicate"}\n[/TOOL_CALL]',
        ]
        text = ""
        for part in parts:
            self.chunks_emitted += 1
            text += part
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _StreamingToolThenProseBackend:
    name = "streaming-tool-then-prose-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt
        parts = [
            '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]',
            "\nlate prose",
        ]
        for part in parts:
            self.chunks_emitted += 1
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text="".join(parts), backend=self.name)


class _StreamingTokenBackend:
    name = "streaming-token-test-backend"
    model_name = "test-model"
    max_tokens = 64

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        if on_chunk is not None:
            on_chunk("hello")
        return ModelResponse(text="hello world", backend=self.name)


class _ProviderUsageBackend:
    name = "provider-usage-test-backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        return ModelResponse(
            text="ok",
            backend=self.name,
            usage={
                "input_tokens": 10_000,
                "cache_read_input_tokens": 20_000,
                "cache_creation_input_tokens": 10_000,
                "output_tokens": 10,
            },
        )


class _PlainRuntimeContextTextBackend:
    name = "plain-runtime-context-text-test-backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise RuntimeError(
            "ordinary exception text mentions context length but is not a provider code"
        )


class _ProviderContextWindowBackend:
    name = "provider-context-window-test-backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise ProviderContextWindowError("HTTP 400: prompt too long")


class _StreamingLiteralProtocolMarkerContentBackend:
    name = "streaming-literal-protocol-marker-content-test-backend"

    def __init__(self) -> None:
        self.chunks_emitted = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        payload = {
            "tool": "write_file",
            "path": "outputs/report.md",
            "content": (
                "# 报告\n\n"
                "正文会原样提到 `[TOOL_CALL]` 和 `[/TOOL_CALL]`，"
                "它们只是文档内容，不是新的工具调用。"
            ),
        }
        text = "[TOOL_CALL]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/TOOL_CALL]"
        parts = [
            text[:80],
            text[80:140],
            text[140:],
        ]
        for part in parts:
            self.chunks_emitted += 1
            if on_chunk is not None:
                on_chunk(part)
        return ModelResponse(text=text, backend=self.name)


class _NonStreamingUnclosedLongWriteBackend:
    name = "non-streaming-unclosed-long-write-test-backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        text = '[TOOL_CALL]\n{"tool":"write_file","path":"outputs/report.md","content":"' + (
            "A" * 400
        )
        return ModelResponse(text=text, backend=self.name)


class _TimeoutAwareBackend:
    name = "timeout-aware-test-backend"
    model_name = "test-model"
    max_tokens = 1200

    def __init__(self) -> None:
        self.request_timeout = 1
        self.seen_timeout = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.seen_timeout = self.request_timeout
        if on_chunk is not None:
            on_chunk("ok")
        return ModelResponse(text="ok", backend=self.name)


class _IdleTimeoutOwnedBackend(_TimeoutAwareBackend):
    stream_enabled = True
    stream_timeout_is_idle = True

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.seen_timeout = self.request_timeout
        time.sleep(0.03)
        if on_chunk is not None:
            on_chunk("ok")
        return ModelResponse(text="ok", backend=self.name)


def _tool_loop_params() -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=None,
        request_id="",
        run_id="",
        task_id="",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
    )


def test_provider_boundary_commits_guidance_batch_before_backend_io(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    dedupe_key = "thread/provider-boundary"
    entry = store.guidance.append_once(
        {
            "target_type": "request",
            "target_id": "request-provider-boundary",
            "message": "模型触网前必须整批提交",
            "metadata": {
                "dedupe_key": dedupe_key,
                "expected_turn_id": "request-provider-boundary",
            },
        },
        dedupe_key=dedupe_key,
    )
    assert store.guidance.claim_for_turn(
        entry,
        expected_turn_id="request-provider-boundary",
        attempt_id="attempt-provider-boundary",
    )
    backend = _SubmissionInspectingBackend(store, dedupe_key)
    agent = SimpleNamespace(
        backend=backend,
        conversation_store=store,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )
    params = replace(
        _tool_loop_params(),
        request_id="request-provider-boundary",
        attempt_id="attempt-provider-boundary",
        live_archive_state={
            "_guidance_ack_ids": {entry.guidance_id},
            "_guidance_ack_entries": {entry.guidance_id: entry},
        },
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="continue",
            tool_rounds=0,
        )
    )

    assert response.text == "accepted"
    assert backend.observed_status == "submitted"
    receipt = store.guidance.receipt(dedupe_key)
    assert receipt is not None and receipt.status == "submitted"
    assert params.live_archive_state["_guidance_submission_id"] == receipt.submission_id


def test_model_generate_enforces_request_timeout_when_backend_blocks():
    backend = _InterruptibleBlockingBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=0.01),
        _current_subagent_run_id="",
    )

    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError, match=r"request_timeout=0.01s"):
        generate_model_response(
            ModelGenerateParams(
                agent=agent,
                params=_tool_loop_params(),
                prompt="hello",
                tool_rounds=0,
            )
        )

    assert backend.entered.is_set()
    assert backend.closed.is_set()
    assert backend.worker_threads and all(not worker.is_alive() for worker in backend.worker_threads)
    assert time.monotonic() - started < 0.06


# LLM: Capture a real model generation exception/result from its caller thread without duplicating test-specific branches.
# 函数用途: 在本地传输测试里并行启动模型调用并保存终态。
def _capture_model_call(outcome, agent, prompt):
    try:
        outcome["response"] = generate_model_response(
            ModelGenerateParams(
                agent=agent,
                params=_tool_loop_params(),
                prompt=prompt,
                tool_rounds=0,
            )
        )
    except BaseException as exc:
        outcome["error"] = exc


# LLM: Cleanup only the test's own exact provider workers if a deliberately removed deadline leaves the caller blocked.
# 函数用途: 变异测试或失败时中断并回收本地假服务调用线程。
def _interrupt_model_caller(caller, backend) -> None:
    if not caller.is_alive():
        return
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt

    for worker in backend.worker_threads:
        if worker.ident is not None:
            set_interrupt(True, worker.ident)
    caller.join(timeout=2.0)


# LLM: Assert one failed transport call reached the expected ledger terminal and disappeared from the public in-flight projection.
# 函数用途: 共用单次本地超时测试的线程、账本及 /status 收尾断言。
def _assert_single_transport_timeout(agent, backend, expected_stage) -> None:
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.contracts.model_call_ledger import model_call_inflight_snapshot

    assert len(backend.worker_threads) == 1
    assert not backend.worker_threads[0].is_alive()
    record = model_call_ledger(agent).records()[0]
    assert record.status == "timed_out" and record.timeout_stage == expected_stage
    assert model_call_inflight_snapshot() == (0, 0.0)


# LLM: Verify both existing wall-clock attempts closed through the original ledger with no new identity source.
# 函数用途: 核对回合放弃后两条模型调用都已按 wall_clock 超时收口。
def _assert_wall_clock_ledger(agent) -> None:
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger

    records = model_call_ledger(agent).records()
    assert len(records) == 2
    assert all(record.status == "timed_out" for record in records)
    assert all(record.timeout_stage == "wall_clock" for record in records)


def test_wall_clock_timeout_interrupts_provider_worker_and_closes_ledger():
    backend = _InterruptibleBlockingBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=0.05),
        _current_subagent_run_id="",
    )
    started = time.monotonic()
    error = None
    response = None
    try:
        response = generate_model_response(
            ModelGenerateParams(
                agent=agent,
                params=_tool_loop_params(),
                prompt="hello",
                tool_rounds=0,
            )
        )
    except BaseException as exc:
        error = exc

    try:
        assert isinstance(error, ProviderTimeoutError), (
            f"unexpected result={response!r}, error={error!r}, elapsed={time.monotonic() - started:.3f}, "
            f"closed={backend.closed.is_set()}, worker={backend.worker_thread!r}"
        )
        assert error.stage == "wall_clock"
        assert backend.entered.wait(timeout=0.5)
        assert len(backend.worker_threads) == len(backend.closed_events) == 2
        assert all(event.is_set() for event in backend.closed_events)
        for worker in backend.worker_threads:
            worker.join(timeout=0.5)
            assert not worker.is_alive()
        _assert_wall_clock_ledger(agent)
    finally:
        for event in backend.closed_events:
            event.set()
        for worker in backend.worker_threads:
            worker.join(timeout=1.0)


def test_websocket_first_event_timeout_closes_call_and_ledger(tmp_path):
    with _fake_websocket_service(tmp_path) as (api_base, request_seen):
        backend = _LocalResponseTransportBackend(
            lambda: _fake_response_request(api_base, timeout=1.0),
            owns_stream_timeout=True,
        )
        agent = SimpleNamespace(
            backend=backend,
            config=SimpleNamespace(request_timeout=10.0),
            _current_subagent_run_id="",
        )
        started = time.monotonic()
        outcome: dict[str, object] = {}
        caller = threading.Thread(
            target=_capture_model_call,
            args=(outcome, agent, "ws first event fixture"),
            daemon=True,
        )
        caller.start()
        completed_before_cleanup = False
        try:
            assert request_seen.wait(timeout=2.0)
            caller.join(timeout=3.0)
            completed_before_cleanup = not caller.is_alive()
        finally:
            _interrupt_model_caller(caller, backend)

        assert completed_before_cleanup  # 若首事件期限被删，先取消清理再将该变异判红
        assert not caller.is_alive()
        caught = outcome.get("error")
        assert isinstance(caught, ProviderTimeoutError)
        assert caught.stage == "first_event"
        assert time.monotonic() - started < 7.0  # 包含已有 WebSocket close_timeout 上限
        _assert_single_transport_timeout(agent, backend, "first_event")


# LLM: Drive a real local WebSocket through one valid event and then silence to verify the rolling idle deadline.
# 函数用途: 确认首事件后 WebSocket 空闲超时会退出线程并结清调用账本。
def test_websocket_stream_idle_timeout_after_first_event_closes_call_and_ledger(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.contracts.model_call_ledger import model_call_inflight_snapshot

    with _fake_websocket_service(tmp_path, send_first_event=True) as (api_base, request_seen):
        backend = _LocalResponseTransportBackend(
            lambda: _fake_response_request(api_base, timeout=1.0),
            owns_stream_timeout=True,
        )
        agent = SimpleNamespace(
            backend=backend,
            config=SimpleNamespace(request_timeout=10.0),
            _current_subagent_run_id="",
        )
        with pytest.raises(ProviderTimeoutError) as caught:
            generate_model_response(
                ModelGenerateParams(
                    agent=agent,
                    params=_tool_loop_params(),
                    prompt="websocket idle fixture",
                    tool_rounds=0,
                )
            )

        assert caught.value.stage == "stream_idle"
        assert request_seen.wait(timeout=0.5)
        assert len(backend.worker_threads) == 1
        assert not backend.worker_threads[0].is_alive()
        record = model_call_ledger(agent).records()[0]
        assert record.status == "timed_out" and record.timeout_stage == "stream_idle"
        assert model_call_inflight_snapshot() == (0, 0.0)


def test_sse_first_event_timeout_closes_call_and_ledger(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.contracts.model_call_ledger import model_call_inflight_snapshot

    with _fake_sse_service(tmp_path, send_first_event=False) as (api_base, request_seen):
        backend = _LocalResponseTransportBackend(
            lambda: _fake_response_request(api_base, timeout=1.0),
            owns_stream_timeout=True,
        )
        agent = SimpleNamespace(
            backend=backend,
            config=SimpleNamespace(request_timeout=10.0),
            _current_subagent_run_id="",
        )
        with pytest.raises(ProviderTimeoutError) as caught:
            generate_model_response(
                ModelGenerateParams(
                    agent=agent,
                    params=_tool_loop_params(),
                    prompt="sse first event fixture",
                    tool_rounds=0,
                )
            )

        assert caught.value.stage == "first_event"
        assert request_seen.wait(timeout=0.5)
        assert len(backend.worker_threads) == 1
        assert not backend.worker_threads[0].is_alive()
        record = model_call_ledger(agent).records()[0]
        assert record.status == "timed_out" and record.timeout_stage == "first_event"
        assert model_call_inflight_snapshot() == (0, 0.0)


def test_sse_stream_idle_timeout_after_first_event_closes_call_and_ledger(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.contracts.model_call_ledger import model_call_inflight_snapshot

    with _fake_sse_service(tmp_path, send_first_event=True) as (api_base, request_seen):
        backend = _LocalResponseTransportBackend(
            lambda: _fake_response_request(api_base, timeout=1.0),
            owns_stream_timeout=True,
        )
        agent = SimpleNamespace(
            backend=backend,
            config=SimpleNamespace(request_timeout=10.0),
            _current_subagent_run_id="",
        )
        with pytest.raises(ProviderTimeoutError) as caught:
            generate_model_response(
                ModelGenerateParams(
                    agent=agent,
                    params=_tool_loop_params(),
                    prompt="sse idle fixture",
                    tool_rounds=0,
                )
            )

        assert caught.value.stage == "stream_idle"
        assert request_seen.wait(timeout=0.5)
        assert len(backend.worker_threads) == 1
        assert not backend.worker_threads[0].is_alive()
        record = model_call_ledger(agent).records()[0]
        assert record.status == "timed_out" and record.timeout_stage == "stream_idle"
        assert model_call_inflight_snapshot() == (0, 0.0)


def test_wall_clock_abandonment_closes_real_sse_workers_before_return(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.contracts.model_call_ledger import model_call_inflight_snapshot

    with _fake_sse_service(tmp_path, send_first_event=False) as (api_base, request_seen):
        backend = _LocalResponseTransportBackend(
            lambda: _fake_response_request(api_base, timeout=5.0),
            owns_stream_timeout=False,
        )
        agent = SimpleNamespace(
            backend=backend,
            config=SimpleNamespace(request_timeout=0.05),
            _current_subagent_run_id="",
        )
        started = time.monotonic()
        with pytest.raises(ProviderTimeoutError) as caught:
            generate_model_response(
                ModelGenerateParams(
                    agent=agent,
                    params=_tool_loop_params(),
                    prompt="abandoned sse fixture",
                    tool_rounds=0,
                )
            )

        assert caught.value.stage == "wall_clock"
        assert request_seen.wait(timeout=0.5)
        assert time.monotonic() - started < 2.0
        assert len(backend.worker_threads) == 2  # 保持既有至多一次的 wall_clock 超时重试
        assert all(not worker.is_alive() for worker in backend.worker_threads)
        records = model_call_ledger(agent).records()
        assert len(records) == 2
        assert all(record.status == "timed_out" for record in records)
        assert all(record.timeout_stage == "wall_clock" for record in records)
        assert model_call_inflight_snapshot() == (0, 0.0)


# LLM: Exercise the real Responses WebSocket transport while the turn wall-clock guard abandons its worker;
#   this catches a graceful close handshake that outlives the caller's bounded drain interval.
# 函数用途: 确认回合超时后 WebSocket 读线程在短上界内退出且调用账本已结清。
def test_wall_clock_abandonment_closes_real_websocket_workers_before_return(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.contracts.model_call_ledger import model_call_inflight_snapshot

    with _fake_websocket_service(tmp_path) as (api_base, request_seen):
        backend = _LocalResponseTransportBackend(
            lambda: _fake_response_request(api_base, timeout=5.0),
            owns_stream_timeout=False,
        )
        agent = SimpleNamespace(
            backend=backend,
            config=SimpleNamespace(request_timeout=0.05),
            _current_subagent_run_id="",
        )
        started = time.monotonic()
        with pytest.raises(ProviderTimeoutError) as caught:
            generate_model_response(
                ModelGenerateParams(
                    agent=agent,
                    params=_tool_loop_params(),
                    prompt="abandoned websocket fixture",
                    tool_rounds=0,
                )
            )

        elapsed = time.monotonic() - started
        assert caught.value.stage == "wall_clock"
        assert request_seen.wait(timeout=0.5)
        assert elapsed < 2.0
        assert len(backend.worker_threads) == 2
        assert all(not worker.is_alive() for worker in backend.worker_threads)
        records = model_call_ledger(agent).records()
        assert len(records) == 2
        assert all(record.status == "timed_out" for record in records)
        assert all(record.timeout_stage == "wall_clock" for record in records)
        assert model_call_inflight_snapshot() == (0, 0.0)


# LLM: A transport fake blocks the outbound protocol send until its socket is closed, proving the send phase owns a deadline.
# 函数用途: 防止 WebSocket 请求帧发送在连接建立后无限阻塞。
def test_websocket_request_send_has_a_deadline(monkeypatch):
    from agent_py_agent.agent.backends import responses_websocket as websocket_backend

    send_entered = threading.Event()
    socket_closed = threading.Event()
    outcome: dict[str, object] = {}

    def send(message):
        del message
        send_entered.set()
        socket_closed.wait(timeout=5.0)
        raise OSError("test socket closed during send")

    def close_socket():
        socket_closed.set()

    connection = SimpleNamespace(send=send, close_socket=close_socket, close=close_socket)
    monkeypatch.setattr(websocket_backend, "_open_connection", lambda request: connection)
    request = _fake_response_request("ws://fake.invalid", timeout=0.05)

    def call_transport():
        try:
            list(websocket_backend.iter_responses_websocket(request))
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=call_transport, daemon=True)
    started = time.monotonic()
    worker.start()
    try:
        assert send_entered.wait(timeout=0.5)
        worker.join(timeout=2.0)

        assert not worker.is_alive()
        assert time.monotonic() - started < 2.0
        error = outcome.get("error")
        assert isinstance(error, ProviderTimeoutError)
        assert error.stage == "first_event"
        assert socket_closed.is_set()
    finally:
        socket_closed.set()
        worker.join(timeout=2.0)


# LLM: Model a peer that stalls send/read and never acknowledges a graceful close, so direct abort is observable.
# 函数用途: 创建一个需要测试主动关闭的假 WebSocket 连接。
def _make_stalled_websocket_connection():
    receive_wakeup = threading.Event()
    close_release = threading.Event()
    send_entered = threading.Event()

    def send(message):
        del message
        send_entered.set()

    def recv(timeout):
        if receive_wakeup.wait(timeout):
            return "{}"
        raise TimeoutError

    def close():
        close_release.wait(timeout=5.0)

    def close_socket():
        receive_wakeup.set()
        close_release.set()

    connection = SimpleNamespace(send=send, recv=recv, close=close, close_socket=close_socket)
    return connection, send_entered


# LLM: Model a peer that blocks the outbound protocol send and never acknowledges a graceful close; only a direct
#   socket abort can wake the blocked sender, so a graceful-close cancel would stay parked past the drain window.
# 函数用途: 创建发送阶段阻塞、且不回应关闭握手的假 WebSocket 连接，分别记录直接关 socket 与关闭握手两条路径。
def _make_send_stalled_websocket_connection():
    send_entered = threading.Event()
    send_release = threading.Event()
    socket_closed_directly = threading.Event()
    close_handshake_called = threading.Event()
    close_release = threading.Event()

    def send(message):
        del message
        send_entered.set()
        send_release.wait(timeout=5.0)
        raise OSError("test socket closed during send")

    def close():
        close_handshake_called.set()
        close_release.wait(timeout=5.0)

    def close_socket():
        socket_closed_directly.set()
        send_release.set()
        close_release.set()

    connection = SimpleNamespace(send=send, close=close, close_socket=close_socket)
    return connection, send_entered, socket_closed_directly, close_handshake_called


# LLM: Consume one synthetic Responses stream in its own worker and preserve its thrown error for assertions.
# 函数用途: 收集假 WebSocket 的中断结果。
def _consume_websocket_transport(outcome, websocket_backend, request):
    try:
        list(websocket_backend.iter_responses_websocket(request))
    except BaseException as exc:
        outcome["error"] = exc


# LLM: Simulate a peer that never acknowledges a graceful close; cancellation must use the immediate transport abort path.
# 函数用途: 验证用户停止不会被 WebSocket 关闭握手拖住。
def test_websocket_interrupt_aborts_without_waiting_for_close_handshake(monkeypatch):
    from agent_py_agent.agent.backends import responses_websocket as websocket_backend
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt

    connection, send_entered = _make_stalled_websocket_connection()
    outcome: dict[str, object] = {}
    monkeypatch.setattr(websocket_backend, "_open_connection", lambda request: connection)
    request = _fake_response_request("ws://fake.invalid", timeout=5.0)
    worker = threading.Thread(
        target=_consume_websocket_transport,
        args=(outcome, websocket_backend, request),
        daemon=True,
    )
    worker.start()
    try:
        assert send_entered.wait(timeout=0.5)
        started = time.monotonic()
        set_interrupt(True, worker.ident)
        worker.join(timeout=1.5)
        elapsed = time.monotonic() - started
        assert not worker.is_alive()
        assert elapsed < 1.5
        assert isinstance(outcome.get("error"), InterruptedError)
    finally:
        connection.close_socket()
        worker.join(timeout=2.0)


# LLM: Start one model call whose WebSocket transport is the stalled-send fake, so a test can cancel a blocked sender.
# 函数用途: 在本地传输后端上启动一次卡在发送阶段的模型调用，返回线程与账本观察入口。
def _start_blocked_websocket_send_call(monkeypatch, connection):
    from agent_py_agent.agent.backends import responses_websocket as websocket_backend

    monkeypatch.setattr(websocket_backend, "_open_connection", lambda request: connection)
    backend = _LocalResponseTransportBackend(
        lambda: _fake_response_request("ws://fake.invalid", timeout=5.0),
        owns_stream_timeout=True,
    )
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10.0),
        _current_subagent_run_id="",
    )
    outcome: dict[str, object] = {}
    caller = threading.Thread(
        target=_capture_model_call,
        args=(outcome, agent, "blocked websocket send fixture"),
        daemon=True,
    )
    caller.start()
    return agent, backend, outcome, caller


# LLM: A cancel that lands while the outbound send is blocked must abort the socket directly; a graceful close
#   would wait for a peer handshake that never comes and leave the worker parked in sendall past the drain window.
# 函数用途: 验证发送阶段收到取消时直接关 socket（不走关闭握手），worker 在排空窗口内退出且调用账本结清。
def test_websocket_cancel_during_blocked_send_aborts_socket_and_closes_ledger(monkeypatch):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.agent_core.tool_model_generation import _MODEL_INTERRUPT_DRAIN_SECONDS
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt
    from agent_py_agent.agent.contracts.model_call_ledger import model_call_inflight_snapshot

    connection, send_entered, socket_closed_directly, close_handshake_called = (
        _make_send_stalled_websocket_connection()
    )
    agent, backend, outcome, caller = _start_blocked_websocket_send_call(monkeypatch, connection)
    try:
        assert send_entered.wait(timeout=0.5)
        started = time.monotonic()
        set_interrupt(True, caller.ident)
        caller.join(timeout=2.0)
        elapsed = time.monotonic() - started

        assert not caller.is_alive()
        error = outcome.get("error")
        # 取消先关 socket：阻塞中的 sendall 以 socket 错误结束；若主循环先看到中断旗则得到 InterruptedError。
        assert isinstance(error, OSError), f"unexpected error={error!r}"
        assert elapsed < _MODEL_INTERRUPT_DRAIN_SECONDS + 0.5
        assert socket_closed_directly.is_set()
        assert not close_handshake_called.is_set()
        assert len(backend.worker_threads) == 1
        backend.worker_threads[0].join(timeout=1.0)
        assert not backend.worker_threads[0].is_alive()
        records = model_call_ledger(agent).records()
        assert len(records) == 1
        assert records[0].status == "failed"
        assert records[0].error_type == type(error).__name__
        assert model_call_inflight_snapshot() == (0, 0.0)
    finally:
        connection.close_socket()
        set_interrupt(False, caller.ident)
        caller.join(timeout=2.0)


def test_stream_transport_idle_timeout_is_not_reapplied_as_total_wall_timeout():
    backend = _IdleTimeoutOwnedBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=0.01),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="hello",
            tool_rounds=0,
        )
    )

    assert response.text == "ok"


def test_model_generate_relays_named_task_interrupt_to_timeout_guard_thread():
    from agent_py_agent.agent.concurrency.interrupt import (
        interrupt_by_name,
        register_interruptible,
    )

    backend = _InterruptibleBlockingBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=600),
        _current_subagent_run_id="",
    )
    outcome: dict[str, object] = {}

    def run_model_turn() -> None:
        try:
            with register_interruptible("real-model-turn-stop"):
                generate_model_response(
                    ModelGenerateParams(
                        agent=agent,
                        params=_tool_loop_params(),
                        prompt="hello",
                        tool_rounds=0,
                    )
                )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run_model_turn)
    thread.start()
    assert backend.entered.wait(timeout=2)

    started = time.monotonic()
    assert interrupt_by_name("real-model-turn-stop") is True
    thread.join(timeout=2)

    assert backend.closed.is_set()
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), InterruptedError)
    assert time.monotonic() - started < 2


def test_model_generate_aborts_streaming_write_file_content_over_inline_limit(monkeypatch):
    monkeypatch.setattr(generation_module, "MAX_INLINE_WRITE_CONTENT_CHARS", 200)
    backend = _StreamingLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="build a shopping site",
            tool_rounds=0,
        )
    )

    assert 1 < backend.chunks_emitted < 5
    assert response.backend == backend.name
    assert response.text == ""
    violation = response.tool_protocol_violations[0]
    assert violation["code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert "inline content streaming exceeded" in violation["detail"]
    assert "site/index.html" not in str(response.tool_protocol_violations)


def test_model_generate_allows_unclosed_write_below_stream_limit_constant(monkeypatch):
    monkeypatch.setattr(generation_module, "MAX_INLINE_WRITE_CONTENT_CHARS", 50_000)
    backend = _StreamingRecoveryLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )
    params = _tool_loop_params()

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="continue writing report",
            tool_rounds=3,
        )
    )

    assert backend.chunks_emitted == 3
    assert response.tool_protocol_violations == []
    assert "reports/final.md" in response.text
    assert len(response.text) > RECOVERY_WRITE_CHUNK_CHARS


def test_model_generate_uses_stream_limit_constant_for_unclosed_write(monkeypatch):
    monkeypatch.setattr(generation_module, "MAX_INLINE_WRITE_CONTENT_CHARS", 4_000)
    backend = _StreamingRecoveryLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )
    params = _tool_loop_params()

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="continue writing report",
            tool_rounds=3,
        )
    )

    violation = response.tool_protocol_violations[0]
    evidence = json.loads(violation["evidence_preview"])
    assert 1 < backend.chunks_emitted < 4
    assert violation["code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert evidence["streaming_content_limit"] == 4_000
    assert "reports/final.md" not in str(response.tool_protocol_violations)


def test_model_generate_does_not_compact_from_plain_exception_text():
    backend = _PlainRuntimeContextTextBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )

    with pytest.raises(RuntimeError, match="context length"):
        generate_model_response(
            ModelGenerateParams(
                agent=agent,
                params=_tool_loop_params(),
                prompt="hello",
                tool_rounds=0,
            )
        )


def test_model_generate_compacts_from_typed_provider_context_window_error():
    backend = _ProviderContextWindowBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )

    params = _tool_loop_params()
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="hello",
            tool_rounds=0,
        )
    )

    assert response.runtime_status == "context_overflow"
    assert response.runtime_source == "provider_error"
    assert "_provider_context_observation" not in params.live_archive_state


def test_successful_model_generate_records_provider_context_observation() -> None:
    backend = _ProviderUsageBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )
    params = _tool_loop_params()

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="hello",
            tool_rounds=0,
        )
    )

    observation = params.live_archive_state["_provider_context_observation"]
    assert response.text == "ok"
    assert observation["schema"] == "provider_context_observation.v3"
    assert observation["raw_estimated_tokens"] > 0
    assert observation["provider_input_tokens"] == 40_000
    assert len(observation["context_surface_fingerprint"]) == 64


def test_model_generate_rejects_unclosed_long_write_after_full_response(monkeypatch):
    monkeypatch.setattr(generation_module, "MAX_INLINE_WRITE_CONTENT_CHARS", 120)
    backend = _NonStreamingUnclosedLongWriteBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=0),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="write long report",
            tool_rounds=1,
        )
    )

    assert response.text == ""
    violation = response.tool_protocol_violations[0]
    evidence = json.loads(violation["evidence_preview"])
    assert violation["code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert evidence["source_tool"] == "write_file"
    assert evidence["previous_write_committed"] is False
    assert "outputs/report.md" not in str(response.tool_protocol_violations)


def test_model_generate_keeps_all_complete_streaming_tool_blocks():
    backend = _StreamingRepeatedToolBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="read final output",
            tool_rounds=3,
        )
    )

    assert backend.chunks_emitted == 3
    assert response.text == (
        '[TOOL_CALL]\n{"tool":"read_file","path":"final.html"}\n[/TOOL_CALL]\n'
        '[TOOL_CALL]\n{"tool":"write_file","action":"append","session_id":"same","chunk_index":3,"content":"duplicate"}\n[/TOOL_CALL]'
    )


def test_model_generate_does_not_strip_prose_to_promote_text_tool_block():
    backend = _StreamingToolThenProseBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="read source",
            tool_rounds=1,
        )
    )

    assert backend.chunks_emitted == 2
    assert response.text.endswith("\nlate prose")


def test_model_generate_records_model_call_ledger_for_streaming_response():
    backend = _StreamingTokenBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(
            request_timeout=10,
            dynamic_timeout_min=1,
            dynamic_timeout_max=20,
        ),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="hello",
            tool_rounds=2,
        )
    )

    records = agent._model_call_ledger.records()
    assert response.text == "hello world"
    assert len(records) == 1
    assert records[0].status == "finished"
    assert records[0].events == ("started", "first_token", "finished")
    assert records[0].backend == backend.name
    assert records[0].model == "test-model"
    assert records[0].metadata["tool_rounds"] == 2
    assert "first_token_timeout_estimate" in records[0].metadata


def test_model_generate_keeps_literal_protocol_markers_inside_write_content():
    backend = _StreamingLiteralProtocolMarkerContentBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=10),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="write report",
            tool_rounds=1,
        )
    )

    assert backend.chunks_emitted >= 2
    assert response.tool_protocol_violations == []
    assert "`[TOOL_CALL]`" in response.text
    assert "`[/TOOL_CALL]`" in response.text


def test_effective_model_timeout_uses_dynamic_config_only_when_present():
    legacy_agent = SimpleNamespace(
        config=SimpleNamespace(request_timeout=1), backend=SimpleNamespace()
    )
    dynamic_agent = SimpleNamespace(
        config=SimpleNamespace(
            request_timeout=1,
            dynamic_timeout_min=1,
            dynamic_timeout_max=60,
        ),
        backend=SimpleNamespace(),
    )

    assert _effective_model_request_timeout_seconds(legacy_agent, 30) == 1
    assert _effective_model_request_timeout_seconds(dynamic_agent, 30) == 30


def test_effective_model_timeout_includes_output_generation_budget():
    dynamic_agent = SimpleNamespace(
        config=SimpleNamespace(
            request_timeout=1,
            dynamic_timeout_min=1,
            dynamic_timeout_max=120,
        ),
        backend=SimpleNamespace(max_tokens=1200),
    )

    assert _effective_model_request_timeout_seconds(dynamic_agent, 30) == 90


def test_model_generate_applies_dynamic_timeout_to_backend_request():
    backend = _TimeoutAwareBackend()
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(
            request_timeout=1,
            dynamic_timeout_min=1,
            dynamic_timeout_max=120,
        ),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="hello",
            tool_rounds=0,
        )
    )

    assert response.text == "ok"
    assert backend.seen_timeout > 40
    assert backend.request_timeout == 1


def test_stream_model_uses_request_local_first_event_budget_without_backend_mutation():
    backend = _KwargRecordingBackend()
    backend.stream_enabled = True
    backend.stream_timeout_is_idle = True
    backend.supports_provider_request_options = True
    backend.request_timeout = 7
    backend.max_tokens = 1200
    agent = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(
            request_timeout=7,
            dynamic_timeout_min=1,
            dynamic_timeout_max=10800,
            estimated_output_tokens_per_second=20,
        ),
        _current_subagent_run_id="",
    )

    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=_tool_loop_params(),
            prompt="hello" * 20_000,
            tool_rounds=0,
        )
    )

    assert response.text == "ok"
    options = backend.seen[0]["request_options"]
    assert options.first_event_timeout_seconds > backend.request_timeout
    assert backend.request_timeout == 7


class _KwargRecordingBackend:
    name = "kwarg-recording-backend"

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.seen.append({"prompt": prompt, **kwargs})
        return ModelResponse(text="ok", backend=self.name)


def _do_generate_with_tool_choice(backend, tool_choice):
    from agent_py_agent.agent.agent_core.tool_model_generation import _do_backend_generate

    state = SimpleNamespace(
        tools=[{"name": "remember", "description": "remember a fact"}],
        tool_choice=tool_choice,
        messages=[{"role": "user", "content": "hi"}],
        on_chunk=None,
    )
    return _do_backend_generate(backend, "prompt", state)


def test_forced_tool_choice_turn_disables_thinking_for_provider_compat():
    """LLM: 强制 tool_choice(specific/required/none)必须同时关思考——部分兼容端点(如
    工具运行时 zen)在思考模式下拒绝强制工具选择,回哑 400;与 generate_structured 同形态。"""
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    for choice in (
        ToolChoice.specific("remember", "open_required_action_unique_tool"),
        ToolChoice.required("open_required_action"),
        ToolChoice.none("open_required_action_has_no_provider_tool"),
    ):
        backend = _KwargRecordingBackend()
        backend.supports_provider_request_options = True
        _do_generate_with_tool_choice(backend, choice)
        assert backend.seen[-1]["request_options"].thinking_disabled is True
        assert backend.seen[-1]["tool_choice"] is choice


def test_auto_tool_choice_turn_keeps_original_request_shape():
    """旧 fake 的 auto 轮不接收 provider options，保持原请求形态。"""
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    backend = _KwargRecordingBackend()
    _do_generate_with_tool_choice(backend, ToolChoice.auto("ordinary_tool_turn"))
    assert "request_options" not in backend.seen[-1]


def test_isolated_presentation_turn_does_not_stream_thinking_delta():
    from agent_py_agent.agent.agent_core.tool_model_generation import _do_backend_generate
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    class Sink:
        def write_thinking_delta(self, _text: str) -> None:
            raise AssertionError("presentation reasoning must stay private")

    backend = _KwargRecordingBackend()
    state = SimpleNamespace(
        tools=[],
        tool_choice=ToolChoice.auto("presentation"),
        messages=None,
        on_chunk=None,
        params=SimpleNamespace(
            context_scope="isolated",
            effective_on_chunk=Sink(),
        ),
    )

    _do_backend_generate(backend, "prompt", state)

    assert "on_thinking_delta" not in backend.seen[-1]


def test_native_tool_turn_passes_tool_input_progress_only_to_capable_backend():
    from agent_py_agent.agent.agent_core.tool_model_generation import _do_backend_generate
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    class Sink:
        def write_tool_input_progress(self, _value: object) -> None:
            return None

    class CapableBackend(_KwargRecordingBackend):
        supports_tool_input_progress = True

    state = SimpleNamespace(
        tools=[{"name": "write_file", "description": "write"}],
        tool_choice=ToolChoice.auto("native"),
        messages=[],
        on_chunk=None,
        params=SimpleNamespace(
            context_scope="shared",
            effective_on_chunk=Sink(),
        ),
    )
    capable = CapableBackend()
    _do_backend_generate(capable, "prompt", state)
    assert callable(capable.seen[-1]["on_tool_input_progress"])

    ordinary = _KwargRecordingBackend()
    _do_backend_generate(ordinary, "prompt", state)
    assert "on_tool_input_progress" not in ordinary.seen[-1]


def test_model_turn_passes_host_guidance_only_to_system_capable_backend():
    from agent_py_agent.agent.agent_core.tool_model_generation import _do_backend_generate
    from agent_py_agent.agent.model_guidance import VERIFICATION_EVIDENCE_BOUNDARY

    class SystemCapableBackend(_KwargRecordingBackend):
        supports_system_instructions = True
        supports_provider_request_options = True

    state = SimpleNamespace(
        tools=[],
        tool_choice=None,
        messages=[],
        on_chunk=None,
        params=SimpleNamespace(context_scope="shared", effective_on_chunk=None),
        system_instruction=VERIFICATION_EVIDENCE_BOUNDARY,
    )

    capable = SystemCapableBackend()
    _do_backend_generate(capable, "user prompt", state)
    assert (
        capable.seen[-1]["request_options"].system_instruction
        == VERIFICATION_EVIDENCE_BOUNDARY
    )

    legacy = _KwargRecordingBackend()
    _do_backend_generate(legacy, "user prompt", state)
    assert "request_options" not in legacy.seen[-1]
