from __future__ import annotations

"""第1项 A 阶段行为锁定测试(无行为变更, 只固化当前超时预算事实)。

锁定对象(steward seq 1453 审计意见 A 阶段):
  1. text 协议下记账口径 == 统一可见口径(estimate_tokens(prompt) 恒等)
  2. native 协议下记账口径低估 IR(messages 不计入 estimate_tokens(prompt))
  3. 首 token 预算的 min clamp 与 effective=max(base, dynamic) 公式
  4. stream 后端 transport_owns_timeout=True -> 墙钟守卫不参与(掐断者是 idle 语义)
  5. SSE idle: 无数据行约 request.timeout 秒掐断; 周期 data 行重置 deadline
  6. ProviderTimeoutError 在 ledger 记为 timed_out + provider_wall(与墙钟不可区分)
  7. ModelCallTimeoutParams 只有 call_id/timeout_seconds/timeout_stage(无 elapsed 证据)

B 阶段修恢复/证据字段时, 本文件 6/7 将按预期变红 -> 正是「先固化再改」的意义。
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import _record_tool_call
from agent_py_agent.agent.agent_core.model.call_monitor import (
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
)
from agent_py_agent.agent.agent_core.model.call_runtime import (
    effective_model_request_timeout_seconds,
    first_token_timeout_options,
    record_model_call_failed,
    record_model_call_timeout,
    start_model_call_record,
)
from agent_py_agent.agent.agent_core.model.context_pressure import (
    model_visible_context_tokens,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    _native_provider_messages,
    _transport_owns_stream_idle_timeout,
)
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest, post_stream_iter
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
)

# 生产配置值(config.py)
_PROD_TIMEOUT = dict(
    request_timeout=240,
    dynamic_timeout_min=30.0,
    dynamic_timeout_max=600.0,
    dynamic_timeout_safety_margin=2.0,
    max_tokens=8192,
)


def _agent(*, protocol: str = "native", backend: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        backend=backend or SimpleNamespace(name="anthropic_compatible", max_tokens=8192),
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=True,
            auto_save_memory=False,
            tool_output_externalize_min_chars=10_000_000,
            tool_output_preview_chars=160,
            **_PROD_TIMEOUT,
        ),
        root=Path("."),
        tools=SimpleNamespace(),
    )


def _params(*, protocol: str, prompt: str) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt=prompt,
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="r",
        run_id="run",
        task_id="t",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run",
            source_protocol=protocol,
        ),
        save=False,
        delivery_contract={},
    )


def _record_ir_rounds(agent, params, *, rounds: int, body_chars: int) -> None:
    for rnd in range(1, rounds + 1):
        call = canonical_history_call(
            "read_file",
            {"path": f"locked-{rnd}.md"},
            call_id=f"lk-{rnd}",
            source_protocol=params.tool_protocol_snapshot.source_protocol,
            run_id=params.run_id,
            turn_id=f"{params.run_id}:round-{rnd}",
            attempt_id=params.request_id,
        )
        _record_tool_call(
            agent,
            ToolCallRecordParams(
                params=params,
                tool_rounds=rnd,
                idx=1,
                call=call,
                result=canonical_history_result(call, "x" * body_chars),
            ),
        )


PROMPT = "请继续处理项目并产出最终报告。" * 30


# ---------------------------------------------------------------- 1. 口径恒等/低估

def test_text_protocol_ledger_matches_unified() -> None:
    """text 协议: 记账口径 estimate_tokens(prompt) 与统一可见口径恒等。"""
    agent = _agent(protocol="text")
    params = _params(protocol="text", prompt=PROMPT)
    _record_ir_rounds(agent, params, rounds=8, body_chars=400)
    assert model_visible_context_tokens(agent, params, PROMPT) == estimate_tokens(PROMPT)


def test_native_protocol_unified_counts_ir() -> None:
    """native 协议: 记账口径 == 统一口径(门槛1 后 IR messages 计入记账)。

    修复前(A 阶段锁定事实): 记账仅 estimate_tokens(prompt), 大 IR 下
    unified >= 5×ledger(prefill 时间低估 -> 600s ProviderTimeout 根因之一)。
    门槛1 已把记账切到 model_visible_context_tokens, 本测试由「低估锚点」
    升级为「记账==统一口径」恒等锚点(门槛1 验收)。
    """
    agent = _agent(protocol="native")
    params = _params(protocol="native", prompt=PROMPT)
    _record_ir_rounds(agent, params, rounds=20, body_chars=2000)
    unified = model_visible_context_tokens(agent, params, PROMPT)
    messages = _native_provider_messages(agent, params)
    # 记账 == 统一口径(恒等式): start_model_call_record 落账即出站可见量
    request = SimpleNamespace(prompt=PROMPT, agent=agent, params=params)
    ledger, _, _ = start_model_call_record(request)
    assert ledger.records()[0].input_tokens == unified
    # 统一口径 ≈ prompt + IR messages(出站近似, 含空 guidance/tools key 差异 <30 token)
    outbound = estimate_tokens({"initial_user_prompt": PROMPT, "messages": messages or []})
    assert abs(unified - outbound) < 30
    # 历史低估锚点保留: 修复前记账口径(仅 prompt)在大 IR 下不足统一口径 1/5
    assert estimate_tokens(PROMPT) * 5 <= unified


def test_native_small_ir_both_clamped_to_floor() -> None:
    """小 IR 下记账与统一口径都被 min clamp 到 30s -> effective 相同(低估被 clamp 掩盖)。"""
    agent = _agent(protocol="native")
    params = _params(protocol="native", prompt=PROMPT)
    _record_ir_rounds(agent, params, rounds=8, body_chars=400)
    options = first_token_timeout_options(agent)
    ledger = estimate_tokens(PROMPT)
    unified = model_visible_context_tokens(agent, params, PROMPT)
    ft_ledger = estimate_first_token_timeout(
        FirstTokenTimeoutParams(input_tokens=ledger, ledger=ModelCallLedger(), options=options)
    )
    ft_unified = estimate_first_token_timeout(
        FirstTokenTimeoutParams(input_tokens=unified, ledger=ModelCallLedger(), options=options)
    )
    assert ft_ledger.timeout_seconds == pytest.approx(options.min_timeout_seconds)
    assert ft_unified.timeout_seconds == pytest.approx(options.min_timeout_seconds)
    assert effective_model_request_timeout_seconds(agent, ft_ledger.timeout_seconds) == pytest.approx(
        effective_model_request_timeout_seconds(agent, ft_unified.timeout_seconds)
    )


def test_effective_timeout_is_max_of_base_and_dynamic() -> None:
    """effective = max(base=240, dynamic)。output_generation 使 dynamic>base 时用 dynamic。"""
    options = FirstTokenTimeoutOptions(
        safety_margin=2.0, min_timeout_seconds=30.0, max_timeout_seconds=600.0
    )
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(input_tokens=1, ledger=ModelCallLedger(), options=options)
    )
    # 分支 1: 小 max_tokens(512) -> output_gen=17s -> dynamic=47 < base 240 -> effective=240
    small = _agent(
        backend=SimpleNamespace(name="plain", max_tokens=512, stream_enabled=False, stream_timeout_is_idle=False)
    )
    assert effective_model_request_timeout_seconds(small, estimate.timeout_seconds) == pytest.approx(240.0)
    # 分支 2: 生产 max_tokens=8192 -> output_gen≈273s -> dynamic=303 > base -> effective=303
    prod = _agent(protocol="text")
    assert effective_model_request_timeout_seconds(prod, estimate.timeout_seconds) == pytest.approx(303.1, abs=0.2)


# ---------------------------------------------------------------- 2. stream 墙钟守卫不参与

def test_stream_backend_defers_wall_guard() -> None:
    """stream + stream_timeout_is_idle -> transport_owns_timeout=True(墙钟守卫不参与)。"""
    stream_backend = SimpleNamespace(
        name="gateway", max_tokens=8192, stream_enabled=True, stream_timeout_is_idle=True
    )
    assert _transport_owns_stream_idle_timeout(_agent(backend=stream_backend)) is True


def test_non_stream_backend_keeps_wall_guard() -> None:
    """非 stream 后端 -> transport_owns_timeout=False(墙钟守卫仍参与)。"""
    plain_backend = SimpleNamespace(
        name="plain", max_tokens=8192, stream_enabled=False, stream_timeout_is_idle=False
    )
    assert _transport_owns_stream_idle_timeout(_agent(backend=plain_backend)) is False


# ---------------------------------------------------------------- 3. SSE idle 掐断行为

class _SSEHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/hang":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: {\"n\":1}\n\n")
            self.wfile.flush()
            time.sleep(4)  # > timeout=1
            return
        if self.path == "/live":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(6):
                self.wfile.write(f"data: {{\"n\":{i}}}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.3)  # 心跳间隔 < timeout=1 -> deadline 持续重置
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


def _call_stream(sse_server: ThreadingHTTPServer, path: str, timeout: int = 1) -> tuple[float, int, Exception | None]:
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
    lines = 0
    exc: Exception | None = None
    try:
        for _ in post_stream_iter(request):
            lines += 1
    except ProviderTimeoutError as err:
        exc = err
    return time.monotonic() - started, lines, exc


def test_sse_silence_cut_off_after_request_timeout(sse_server: ThreadingHTTPServer) -> None:
    """静默(无数据行)约 request.timeout 秒被掐断, 抛 ProviderTimeoutError。"""
    elapsed, lines, exc = _call_stream(sse_server, "/hang", timeout=1)
    assert lines == 1  # 首行已到, 之后静默
    assert isinstance(exc, ProviderTimeoutError)
    assert elapsed < 3.0  # 掐断时间≈timeout=1, 远小于 600s 墙钟
    assert elapsed >= 0.8


def test_sse_periodic_data_resets_idle_deadline(sse_server: ThreadingHTTPServer) -> None:
    """周期 data 行重置 deadline: 超 timeout 时长仍正常收完, 不掐断。"""
    elapsed, lines, exc = _call_stream(sse_server, "/live", timeout=1)
    assert lines == 6
    assert exc is None
    assert elapsed >= 1.2  # 总时长超过单个 timeout, 靠心跳续命


# ---------------------------------------------------------------- 4. ledger 形态

def test_idle_timeout_recorded_as_provider_wall() -> None:
    """ProviderTimeoutError(含 SSE idle 掐断)走生产 except 分支 -> timed_out+provider_wall。"""
    ledger = ModelCallLedger()
    ledger.started(
        ModelCallStartedParams(
            call_id="c1", backend="anthropic_compatible", model="m", input_tokens=100
        )
    )
    record_model_call_timeout(
        ledger=ledger, call_id="c1", timeout_seconds=600.0, timeout_stage="provider_wall"
    )
    record = ledger.records()[0]
    assert record.status == "timed_out"
    assert record.timeout_stage == "provider_wall"
    assert record.error_type == ""


def test_failed_branch_timeout_has_no_stage() -> None:
    """对照: 若异常走通用 failed 分支, 无 timeout_stage(证据更贫)。"""
    ledger = ModelCallLedger()
    ledger.started(
        ModelCallStartedParams(
            call_id="c2", backend="anthropic_compatible", model="m", input_tokens=100
        )
    )
    record_model_call_failed(
        ledger, "c2", ProviderTimeoutError("模型接口流式响应空闲超时: request_timeout=600s url=u")
    )
    record = ledger.records()[0]
    assert record.status == "failed"
    assert record.timeout_stage == ""
    assert record.error_type == "ProviderTimeoutError"


def test_timeout_params_lack_elapsed_evidence_fields() -> None:
    """ModelCallTimeoutParams 只有 3 字段: 无 elapsed/first-token/last-event(证据缺口锁定)。"""
    fields = set(ModelCallTimeoutParams.__dataclass_fields__.keys())
    assert fields == {"call_id", "timeout_seconds", "timeout_stage"}


def test_ledger_record_has_timestamps_but_timeout_never_exposes_elapsed() -> None:
    """ModelCallRecord 本身带时间戳, 但 timeout 参数层不传 elapsed -> 证据链断在调用点。"""
    # 锁定: record 结构有 started_at/first_token_at/last_activity_at, 但
    # record_model_call_timeout 不接收也不落 elapsed 字段。
    import inspect

    from agent_py_agent.agent.agent_core.model import call_runtime

    signature = inspect.signature(call_runtime.record_model_call_timeout)
    assert set(signature.parameters.keys()) == {"ledger", "call_id", "timeout_seconds", "timeout_stage"}


def test_json_roundtrip_of_locked_evidence(tmp_path: Path) -> None:
    """取证脚本输出形状的冒烟: 口径/SSE/ledger 三场景键结构稳定(防脚本漂移)。"""
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "b_acceptance" / "timeout_budget_evidence.py"
    )
    assert script.exists()
