#!/usr/bin/env python3
"""第1项 A 阶段(600s 动态超时)无行为变更取证: 口径对比 + SSE idle 掐断复现 + ledger 证据现状。

角色: 只取证、不改任何生产行为。三个场景:
  1. input_tokens 口径: 同一真实 ToolLoopExecuteParams 在 text/native 两协议下, 对比
     记账口径 estimate_tokens(prompt)(call_runtime.start_model_call_record 同源) 与
     统一可见口径 model_visible_context_tokens(含 IR messages + tools schema),
     并推导 estimate_first_token_timeout / effective_model_request_timeout_seconds 的
     低估幅度。IR 历史用与 tests 相同的 canonical_history_call/_record_tool_call 构造。
  2. SSE idle 掐断复现: 本地 ThreadingHTTPServer 假端点, 真实 gateway_helpers
     post_stream_iter 客户端, 验证「stream 下实际掐断者是 idle watchdog 而非墙钟守卫」:
     hang 场景(发首行后静默) 应约 timeout 秒抛 ProviderTimeoutError「空闲超时」,
     live 场景(周期性 data 行) 应重置 deadline 正常收完。
  3. ledger 证据现状: 同一 ProviderTimeoutError 由 idle 掐断产生时, 生产路径
     (except ProviderTimeoutError -> _record_provider_timeout) 记为
     timed_out+timeout_stage="provider_wall"; 与墙钟 timed_out 不可区分;
     ModelCallTimeoutParams 无 elapsed/first-token/last-event 字段。

约束:
  - 只操作本地临时目录与 127.0.0.1 临时端口, 绝不触碰生产 home/gateway/8420
  - 假端点纯本地回环, 不访问外网
  - 输出脱敏 JSON 到 --output
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

PKG_ROOT = Path(__file__).resolve().parents[3]  # 仓库根(含 agent_py_agent/ 包)
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams  # noqa: E402
from agent_py_agent.agent.agent_core._tool_loop_service import _record_tool_call  # noqa: E402
from agent_py_agent.agent.agent_core.model.call_monitor import (  # noqa: E402
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
)
from agent_py_agent.agent.agent_core.model.call_runtime import (  # noqa: E402
    effective_model_request_timeout_seconds,
    record_model_call_failed,
    record_model_call_timeout,
)
from agent_py_agent.agent.agent_core.model.context_pressure import (  # noqa: E402
    model_visible_context_tokens,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolCallRecordParams,  # noqa: E402
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    _native_provider_messages,  # noqa: E402
)
from agent_py_agent.agent.backends.errors import ProviderTimeoutError  # noqa: E402
from agent_py_agent.agent.backends.gateway_helpers import (  # noqa: E402
    GatewayRequest,
    post_stream_iter,
)
from agent_py_agent.agent.contracts.model_call_ledger import (  # noqa: E402
    ModelCallLedger,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from agent_py_agent.agent.memory_archive import estimate_tokens  # noqa: E402
from agent_py_agent.tests._tool_runtime_harness import (  # noqa: E402
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
)

# 生产配置值(config.py): request_timeout=240, dynamic_timeout_min=30,
# dynamic_timeout_max=600, dynamic_timeout_safety_margin=2.0, max_output_tokens=8192
_PROD_TIMEOUT_CONFIG = dict(
    request_timeout=240,
    dynamic_timeout_min=30.0,
    dynamic_timeout_max=600.0,
    dynamic_timeout_safety_margin=2.0,
    max_tokens=8192,
)


def _native_agent(root: Path, *, protocol: str) -> SimpleNamespace:
    return SimpleNamespace(
        backend=SimpleNamespace(name="anthropic_compatible", max_tokens=8192),
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=True,
            auto_save_memory=False,
            tool_output_externalize_min_chars=10_000_000,
            tool_output_preview_chars=160,
            **_PROD_TIMEOUT_CONFIG,
        ),
        root=root,
        tools=SimpleNamespace(),
    )


def _params(*, protocol: str, user_prompt: str) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt=user_prompt,
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


def _record_rounds(agent, params, *, rounds: int, body_chars: int) -> None:
    """按 tests 同款 canonical_history_call 构造 N 轮真实 IR 往返。"""
    for rnd in range(1, rounds + 1):
        call = canonical_history_call(
            "read_file",
            {"path": f"evidence-{rnd}.md"},
            call_id=f"ev-{rnd}",
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


def _timeout_estimates(agent, params, prompt: str) -> dict[str, object]:
    ledger_estimate = estimate_tokens(prompt)  # 记账口径: call_runtime.py:32 同源
    unified = model_visible_context_tokens(agent, params, prompt)  # 统一可见口径
    messages = _native_provider_messages(agent, params)
    outbound = ledger_estimate + estimate_tokens(messages or [])  # 出站近似: prompt + IR messages

    options = FirstTokenTimeoutOptions(
        safety_margin=_PROD_TIMEOUT_CONFIG["dynamic_timeout_safety_margin"],
        min_timeout_seconds=_PROD_TIMEOUT_CONFIG["dynamic_timeout_min"],
        max_timeout_seconds=_PROD_TIMEOUT_CONFIG["dynamic_timeout_max"],
    )
    ft_ledger = estimate_first_token_timeout(
        FirstTokenTimeoutParams(input_tokens=ledger_estimate, ledger=ModelCallLedger(), options=options)
    )
    ft_unified = estimate_first_token_timeout(
        FirstTokenTimeoutParams(input_tokens=unified, ledger=ModelCallLedger(), options=options)
    )
    return {
        "ledger_prompt_tokens": ledger_estimate,
        "unified_visible_tokens": unified,
        "outbound_approx_tokens": outbound,
        "unified_minus_ledger_tokens": unified - ledger_estimate,
        "first_token_timeout_ledger_s": round(ft_ledger.timeout_seconds, 1),
        "first_token_timeout_unified_s": round(ft_unified.timeout_seconds, 1),
        "effective_request_timeout_ledger_s": round(
            effective_model_request_timeout_seconds(agent, ft_ledger.timeout_seconds), 1
        ),
        "effective_request_timeout_unified_s": round(
            effective_model_request_timeout_seconds(agent, ft_unified.timeout_seconds), 1
        ),
    }


def _measure_protocol(*, protocol: str, rounds: int, body_chars: int, prompt: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="timeout-evidence-") as tmp:
        agent = _native_agent(Path(tmp), protocol=protocol)
        params = _params(protocol=protocol, user_prompt=prompt)
        _record_rounds(agent, params, rounds=rounds, body_chars=body_chars)
        return {"protocol": protocol, "rounds": rounds, **(_timeout_estimates(agent, params, prompt))}


# ---------------------------------------------------------------- 场景 2: SSE idle

class _SSEHandler(BaseHTTPRequestHandler):
    """本地假端点: /stream-hang 发首行后静默; /stream-live 周期发 data 行。"""

    def do_POST(self) -> None:  # noqa: N802 - http.server 命名
        if self.path == "/stream-hang":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: {\"n\":1}\n\n")
            self.wfile.flush()
            time.sleep(6)  # 远大于客户端 timeout=3, 连接保持静默
            return
        if self.path == "/stream-live":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(6):
                self.wfile.write(f"data: {{\"n\":{i}}}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.5)  # 周期心跳, 重置 idle deadline
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:  # 静默日志
        del args


def _call_stream(path: str, timeout: int) -> dict[str, object]:
    port = int(_SSE_SERVER.server_address[1])
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
    exception: dict[str, str] | None = None
    try:
        for _ in post_stream_iter(request):
            lines += 1
    except ProviderTimeoutError as exc:
        exception = {"type": "ProviderTimeoutError", "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 - 取证要如实记录异常形态
        exception = {"type": type(exc).__name__, "detail": str(exc)[:200]}
    return {
        "path": path,
        "request_timeout_s": timeout,
        "elapsed_s": round(time.monotonic() - started, 2),
        "lines_received": lines,
        "exception": exception,
    }


_SSE_SERVER: ThreadingHTTPServer | None = None


def _scenario_stream_idle() -> dict[str, object]:
    global _SSE_SERVER
    _SSE_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=_SSE_SERVER.serve_forever, daemon=True)
    thread.start()
    try:
        return {
            "hang": _call_stream("/stream-hang", timeout=3),
            "live": _call_stream("/stream-live", timeout=3),
        }
    finally:
        _SSE_SERVER.shutdown()
        _SSE_SERVER.server_close()


# ---------------------------------------------------------------- 场景 3: ledger 形态

def _scenario_ledger_evidence() -> dict[str, object]:
    # 生产路径: idle 掐断抛 ProviderTimeoutError -> except ProviderTimeoutError
    # (tool_model_generation.py:139) -> _record_provider_timeout -> record_model_call_timeout
    # timeout_stage="provider_wall" (tool_model_generation.py:368)
    ledger_idle = ModelCallLedger()
    ledger_idle.started(
        ModelCallStartedParams(
            call_id="idle-cut", backend="anthropic_compatible", model="evidence",
            input_tokens=1000,
        )
    )
    record_model_call_timeout(
        ledger=ledger_idle, call_id="idle-cut",
        timeout_seconds=600.0, timeout_stage="provider_wall",
    )
    idle_record = ledger_idle.records()[0].to_dict()

    # 对照: 若走通用 except Exception 分支(record_model_call_failed)的形态
    ledger_failed = ModelCallLedger()
    ledger_failed.started(
        ModelCallStartedParams(
            call_id="idle-failed", backend="anthropic_compatible", model="evidence",
            input_tokens=1000,
        )
    )
    record_model_call_failed(
        ledger_failed, "idle-failed",
        ProviderTimeoutError("模型接口流式响应空闲超时: request_timeout=600s url=http://..."),
    )
    failed_record = ledger_failed.records()[0].to_dict()

    return {
        "idle_cut_ledger_status": idle_record["status"],
        "idle_cut_timeout_stage": idle_record["timeout_stage"],
        "idle_cut_timeout_seconds": idle_record["timeout_seconds"],
        "idle_cut_error_type": idle_record["error_type"],
        "failed_branch_status": failed_record["status"],
        "failed_branch_error_type": failed_record["error_type"],
        "failed_branch_timeout_stage": failed_record["timeout_stage"],
        "timeout_params_fields": list(ModelCallTimeoutParams.__dataclass_fields__.keys()),
        "has_elapsed_or_first_token_field": False,
        "timeout_stage_production_values": ["provider_wall"],  # 全库唯一生产取值
        "is_probe_production_sites": ["llm_activation_readiness.py:178"],  # 探针路径唯一埋点
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="timeout_budget_evidence",
        description="第1项 A 阶段超时预算取证(不裁决、不改行为, 只收集结构化证据)。",
    )
    parser.add_argument("--output", required=True, help="证据 JSON 输出路径")
    args = parser.parse_args(argv)

    prompt = "请继续处理项目, 检查剩余用例后产出最终报告。" * 30
    payload = {
        "schema": "timeout-budget-evidence.v1",
        "note": "无行为变更取证; 假端点仅本地回环; 不触碰生产",
        "scenarios": {
            "measurement": {
                "text_small": _measure_protocol(
                    protocol="text", rounds=8, body_chars=400, prompt=prompt
                ),
                "native_small": _measure_protocol(
                    protocol="native", rounds=8, body_chars=400, prompt=prompt
                ),
                "native_large": _measure_protocol(
                    protocol="native", rounds=20, body_chars=2000, prompt=prompt
                ),
            },
            "stream_idle": _scenario_stream_idle(),
            "ledger_evidence": _scenario_ledger_evidence(),
        },
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"timeout_budget_evidence: 证据已写入 {output}")
    for name, scenario in payload["scenarios"].items():
        print(f"  [{name}] {json.dumps(scenario, ensure_ascii=False, sort_keys=True)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
