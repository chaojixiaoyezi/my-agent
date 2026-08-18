from __future__ import annotations

"""第1项 B 门槛2 验收测试: 超时 stage 三值区分 + provider_wall 兼容读取 + elapsed 证据。

steward seq 1500 细化要求(门槛2): stream_idle/wall_clock/provider_declared stage
+ provider_wall 兼容读取。A 锁定测试(test_timeout_budget_locked.py)已同步更新
2 条契约: 参数层暴露 elapsed_seconds 但绝不暴露原始时间戳。

三来源行为测的构造(实证确认):
- stream_idle: SSE 保活空行(非 data 行, socket 永不超时) + data 行空闲超时
- provider_declared: SSE 首行后完全静默, socket read 超时先于 SSE deadline 触发
- wall_clock: 非 stream 后端 + generate 挂起, 墙钟守卫线程超时
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _ProviderTimeoutRecord,
    _provider_timeout_elapsed,
    _provider_timeout_idle_silence,
    _record_provider_timeout,
    generate_model_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest, post_stream_iter
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallStartedParams,
)
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


# ---------------------------------------------------------------- helpers

def _agent(*, request_timeout: float = 30.0) -> SimpleNamespace:
    """无 dynamic_timeout 字段 -> effective == base == request_timeout(测试可控)。"""
    return SimpleNamespace(
        backend=SimpleNamespace(name="test", max_tokens=512),
        config=SimpleNamespace(request_timeout=request_timeout),
    )


def _clock_ledger(
    *,
    start: float = 1000.0,
    after: float | None = 1012.5,
    call_id: str = "call-1",
) -> ModelCallLedger:
    """可控时钟 ledger: started 用 start, 之后 now() 返回 after(模拟墙钟经过)。

    after=None 时时钟静止(返回固定 start), 用于不关心 elapsed 的 stage 用例。
    注意 started/timeout 落账各自调用 now(), 迭代器须能供应多次。
    """
    state = {"calls": 0}

    def now() -> float:
        state["calls"] += 1
        if after is None:
            return start
        return start if state["calls"] == 1 else after

    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=now))
    ledger.started(
        ModelCallStartedParams(
            call_id=call_id, backend="test", model="m", input_tokens=100
        )
    )
    return ledger


def _record_timeout(
    ledger: ModelCallLedger,
    *,
    stage: str = "stream_idle",
    call_id: str = "call-1",
) -> None:
    record = _ProviderTimeoutRecord(
        request=SimpleNamespace(
            agent=_agent(), params=SimpleNamespace(), tool_rounds=0
        ),
        ledger=ledger,
        call_id=call_id,
        first_token_timeout_seconds=0.0,
        exc=ProviderTimeoutError("timeout", stage=stage),
    )
    _record_provider_timeout(record)


def _timed_out_record(ledger: ModelCallLedger, call_id: str = "call-1"):
    return next(
        record for record in ledger.records() if record.call_id == call_id
    )


# ---------------------------------------------------------------- A. stage 落账单元测

def test_stream_idle_stage_recorded() -> None:
    """SSE 数据流空闲掐断 -> 落账 stage=stream_idle。"""
    ledger = _clock_ledger(after=None)
    _record_timeout(ledger, stage="stream_idle")
    assert _timed_out_record(ledger).timeout_stage == "stream_idle"
    assert _timed_out_record(ledger).status == "timed_out"


def test_wall_clock_stage_recorded() -> None:
    """本进程墙钟守卫线程超时 -> 落账 stage=wall_clock。"""
    ledger = _clock_ledger(after=None)
    _record_timeout(ledger, stage="wall_clock")
    assert _timed_out_record(ledger).timeout_stage == "wall_clock"


def test_provider_declared_stage_recorded() -> None:
    """provider 网络层声明的 connect/read 超时 -> 落账 stage=provider_declared。"""
    ledger = _clock_ledger(after=None)
    _record_timeout(ledger, stage="provider_declared")
    assert _timed_out_record(ledger).timeout_stage == "provider_declared"


def test_legacy_exception_defaults_to_provider_wall() -> None:
    """旧调用不传 stage -> 异常默认 provider_wall, 落账兼容读取(门槛2 前语义)。"""
    ledger = _clock_ledger(after=None)
    exc = ProviderTimeoutError("legacy timeout")  # 不传 stage
    assert exc.stage == "provider_wall"
    record = _ProviderTimeoutRecord(
        request=SimpleNamespace(
            agent=_agent(), params=SimpleNamespace(), tool_rounds=0
        ),
        ledger=ledger,
        call_id="call-1",
        first_token_timeout_seconds=0.0,
        exc=exc,
    )
    _record_provider_timeout(record)
    assert _timed_out_record(ledger).timeout_stage == "provider_wall"


# ---------------------------------------------------------------- B. elapsed 证据

def test_elapsed_computed_from_started_at() -> None:
    """elapsed = now - record.started_at(掐断时刻真实墙钟经过)。"""
    ledger = _clock_ledger(start=1000.0, after=1012.5)
    _record_timeout(ledger, stage="wall_clock")
    record = _timed_out_record(ledger)
    assert record.elapsed_seconds == pytest.approx(12.5)
    # 双持一致: ledger 层 total_latency_seconds 与参数层 elapsed 同源同值
    assert record.total_latency_seconds == pytest.approx(record.elapsed_seconds)
    assert record.timeout_at - record.started_at == pytest.approx(12.5)


def test_elapsed_clamped_nonnegative_on_clock_regression() -> None:
    """时钟回拨(now < started_at) -> elapsed 钳到 0.0, 不落负数。"""
    ledger = _clock_ledger(start=1000.0, after=990.0)
    _record_timeout(ledger, stage="stream_idle")
    assert _timed_out_record(ledger).elapsed_seconds == 0.0


def test_elapsed_zero_when_record_not_in_ledger() -> None:
    """call_id 不在账(理论不可达) -> 0.0 不抛, 证据兜底不炸调用链。"""
    ledger = _clock_ledger(after=None)
    assert _provider_timeout_elapsed(ledger, "ghost-call") == 0.0


def test_elapsed_ignores_activity_when_no_first_token() -> None:
    """无首 token 时 elapsed 从 started_at 起算, 不依赖 last_activity_at。"""
    ledger = _clock_ledger(start=100.0, after=107.0)
    ledger.activity(
        ModelCallActivityParams(call_id="call-1", output_tokens_seen=1)
    )
    _record_timeout(ledger, stage="provider_declared")
    assert _timed_out_record(ledger).elapsed_seconds == pytest.approx(7.0)


def test_idle_silence_equals_elapsed_when_no_activity() -> None:
    """完全静默: 静默时长 == 总墙钟经过(last_activity_at == started_at)。"""
    ledger = _clock_ledger(start=1000.0, after=1012.5)
    _record_timeout(ledger, stage="stream_idle")
    record = _timed_out_record(ledger)
    assert record.idle_silence_seconds == pytest.approx(12.5)
    assert record.idle_silence_seconds == pytest.approx(record.elapsed_seconds)


def test_idle_silence_less_than_elapsed_after_activity() -> None:
    """有活动后超时: 静默时长 < 总经过(从 last_activity_at 起算, 不混 token 延迟)。"""
    state = {"calls": 0}
    times = [1000.0, 1005.0]  # started -> activity(中间活动)

    def now() -> float:
        value = times[state["calls"]] if state["calls"] < len(times) else 1012.5
        state["calls"] += 1
        return value

    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=now))
    ledger.started(
        ModelCallStartedParams(
            call_id="call-1", backend="test", model="m", input_tokens=100
        )
    )
    ledger.activity(ModelCallActivityParams(call_id="call-1", output_tokens_seen=1))
    _record_timeout(ledger, stage="stream_idle")
    record = _timed_out_record(ledger)
    # 活动在 1005s, 掐断在 1012.5s -> 静默 7.5s, 总经过 12.5s
    assert record.idle_silence_seconds == pytest.approx(7.5)
    assert record.idle_silence_seconds < record.elapsed_seconds
    assert record.elapsed_seconds == pytest.approx(12.5)


def test_idle_silence_none_when_record_not_in_ledger() -> None:
    """call_id 不在账 -> None 不抛(缺失=未计算, 与参数层 None 合同一致)。

    门槛2 终审边界③(seq1622-3): fallback 不再用 0.0——「缺失/回退」与
    「真实零静默」结构化可辨识; ghost-call 缺失为 None, 真实零静默为 0.0。
    """
    ledger = _clock_ledger(after=None)
    assert _provider_timeout_idle_silence(ledger, "ghost-call") is None


# ---------------------------------------------------------------- C. 生产打标点行为测

class _SSEHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        # 消费请求体: 不读则 handler 结束时连接上有未读数据,
        # close 时发 RST 而非 FIN(客户端读到 ConnectionResetError 假噪声)
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if self.path == "/noreply":
            # 连接建立但从不发 HTTP 响应头: 客户端阻塞在读头, read 超时
            # -> provider 网络层超时(provider_declared)。watchdog 尚未启动
            # (响应头未到, _stream_with_watchdog 在 guard 建立后才 start),
            # 无竞态, 确定性走 provider_declared。
            try:
                time.sleep(3)
            finally:
                try:
                    self.send_response(502)
                    self.end_headers()
                except Exception:
                    pass
            return
        if self.path == "/keepalive":
            # 保活空行持续写: socket 永不超时, data 行空闲 1s -> stream_idle
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"n":1}\n\n')
            self.wfile.flush()
            end = time.monotonic() + 3
            try:
                while time.monotonic() < end:
                    self.wfile.write(b"\n\n")
                    self.wfile.flush()
                    time.sleep(0.2)
            except BrokenPipeError:  # 客户端掐断后的正常噪音
                pass
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:  # 静默
        del args


@pytest.fixture(scope="module")
def sse_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _call_stream(
    sse_server: ThreadingHTTPServer, path: str, timeout: int = 1
) -> tuple[float, ProviderTimeoutError]:
    port = int(sse_server.server_address[1])
    request = GatewayRequest(
        api_base=f"http://127.0.0.1:{port}",
        api_key="test-key",
        path=path,
        payload={"model": "evidence"},
        headers={},
        timeout=timeout,
    )
    started = time.monotonic()
    exc: ProviderTimeoutError | None = None
    try:
        for _ in post_stream_iter(request):
            pass
    except ProviderTimeoutError as err:
        exc = err
    assert exc is not None, f"{path} 应抛 ProviderTimeoutError"
    return time.monotonic() - started, exc


def test_sse_noreply_raises_provider_declared(sse_server: ThreadingHTTPServer) -> None:
    """连接建立但响应头永不到达: 网络层 read 超时 -> stage=provider_declared。"""
    elapsed, exc = _call_stream(sse_server, "/noreply", timeout=1)
    assert exc.stage == "provider_declared"
    assert elapsed < 3.0
    assert elapsed >= 0.8


def test_sse_keepalive_without_data_raises_stream_idle(
    sse_server: ThreadingHTTPServer,
) -> None:
    """连接持续有保活数据但 data 行空闲: SSE idle 掐断 -> stage=stream_idle。"""
    elapsed, exc = _call_stream(sse_server, "/keepalive", timeout=1)
    assert exc.stage == "stream_idle"
    assert elapsed < 3.0
    assert elapsed >= 0.8


class _BlockingBackend:
    name = "blocking-test-backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        time.sleep(0.5)  # 挂起 > request_timeout -> 墙钟守卫掐断
        return ModelResponse(text="late response", backend=self.name)


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
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        save=False,
        delivery_contract={},
    )


def test_wall_clock_guard_raises_wall_clock_stage() -> None:
    """非 stream 后端 + generate 挂起: 墙钟守卫线程超时 -> stage=wall_clock。"""
    agent = SimpleNamespace(
        backend=_BlockingBackend(),
        config=SimpleNamespace(request_timeout=0.05),
        _current_subagent_run_id="",
    )
    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError) as err:
        generate_model_response(
            ModelGenerateParams(
                agent=agent,
                params=_tool_loop_params(),
                prompt="hello",
                tool_rounds=0,
            )
        )
    assert err.value.stage == "wall_clock"
    assert time.monotonic() - started < 0.5


def test_stream_backend_delegates_to_transport_not_wall_guard() -> None:
    """stream + stream_timeout_is_idle -> 墙钟守卫不参与, 挂起 generate 不抛墙钟超时。

    该场景的掐断者是 transport(stream_idle/provider_declared), 墙钟守卫不越权。
    """

    class _StreamOwnedBackend:
        name = "stream-owned-test-backend"
        stream_enabled = True
        stream_timeout_is_idle = True

        def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
            time.sleep(0.05)
            return ModelResponse(text="ok", backend=self.name)

    agent = SimpleNamespace(
        backend=_StreamOwnedBackend(),
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
    assert response.text == "ok"  # transport 拥有 idle 语义, 墙钟守卫不拦


# ---------------------------------------------------------------- 门槛2 终审补证(seq1613c): idle_silence 缺失 vs 真实零值

def test_idle_silence_none_when_not_computed() -> None:
    """调用方不传 idle_silence(旧调用/缺失) -> 落账 None(缺失可辨识)。"""
    from agent_py_agent.agent.agent_core.model.call_runtime import (
        record_model_call_timeout,
    )

    ledger = ModelCallLedger()
    ledger.started(
        ModelCallStartedParams(
            call_id="c-missing", backend="test", model="m", input_tokens=100
        )
    )
    record_model_call_timeout(
        ledger=ledger,
        call_id="c-missing",
        timeout_seconds=30.0,
        timeout_stage="stream_idle",
        elapsed_seconds=12.5,
        # 不传 idle_silence_seconds -> 缺失
    )
    record = _timed_out_record(ledger, "c-missing")
    assert record.idle_silence_seconds is None  # 缺失/回退可辨识
    assert record.elapsed_seconds == 12.5


def test_idle_silence_zero_is_real_not_missing() -> None:
    """真实零静默(活动持续到超时瞬间) -> 落账 0.0, 与缺失 None 可辨识。"""
    from agent_py_agent.agent.agent_core.model.call_runtime import (
        record_model_call_timeout,
    )

    ledger = ModelCallLedger()
    ledger.started(
        ModelCallStartedParams(
            call_id="c-zero", backend="test", model="m", input_tokens=100
        )
    )
    record_model_call_timeout(
        ledger=ledger,
        call_id="c-zero",
        timeout_seconds=30.0,
        timeout_stage="stream_idle",
        elapsed_seconds=30.0,
        idle_silence_seconds=0.0,  # 真实计算: 静默 0 秒
    )
    record = _timed_out_record(ledger, "c-zero")
    assert record.idle_silence_seconds == 0.0  # 真实零静默, 非缺失
    assert record.elapsed_seconds == 30.0


# ---------------------------------------------------------------- 门槛2 终审补证(seq1613b): stage 合同封闭校验

def test_stage_contract_closed_unknown_rejected() -> None:
    """未知 stage 构造即抛 ValueError(fail-closed, 合同封闭)。"""
    with pytest.raises(ValueError):
        ProviderTimeoutError("t", stage="unknown_stage")


def test_stage_contract_all_known_accepted() -> None:
    """四个合法 stage(三值+legacy provider_wall)全部可构造。"""
    for stage in ("stream_idle", "wall_clock", "provider_declared", "provider_wall"):
        err = ProviderTimeoutError("t", stage=stage)
        assert err.stage == stage


def test_legacy_default_stage_provider_wall() -> None:
    """旧调用不传 stage -> 默认 provider_wall(legacy 兼容读取锚点)。"""
    err = ProviderTimeoutError("legacy")
    assert err.stage == "provider_wall"
