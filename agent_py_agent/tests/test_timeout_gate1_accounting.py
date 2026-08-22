from __future__ import annotations

"""第1项 B 门槛1: 记账口径对齐出站可见量。

steward seq 1500 审计细化要求:
  - start_model_call_record 的 input_tokens 必须与 model_visible_context_tokens
    是同一 canonical 口径(与最终序列化的 native 出站上下文一致)
  - 覆盖 text/native、工具多轮、compact/resume, 防止 IR 重复计数或漏计
  - gate1 先固定, 后续预算(首 token 预算)使用修正后的 effective 值

修复前事实(A 锁定测试2 实证): native 大 IR 下 unified >= 5×ledger
(记账仅 estimate_tokens(prompt), IR messages 不计入 -> prefill 时间低估
-> 多轮工具后 600s ProviderTimeout 根因之一)。本文件验收修复后语义:
记账 == 统一口径(恒等式), 不是「更接近」。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import _record_tool_call
from agent_py_agent.agent.agent_core.model.call_runtime import start_model_call_record
from agent_py_agent.agent.agent_core.model.context_pressure import (
    model_visible_context_tokens,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
)

_PROD_TIMEOUT = dict(
    request_timeout=240,
    dynamic_timeout_min=30.0,
    dynamic_timeout_max=600.0,
    dynamic_timeout_safety_margin=2.0,
    max_tokens=8192,
)


def _agent(*, protocol: str = "native") -> SimpleNamespace:
    return SimpleNamespace(
        backend=SimpleNamespace(name="anthropic_compatible", max_tokens=8192),
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


def _params(*, protocol: str, prompt: str, tool_context=None) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt=prompt,
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=tool_context or [],
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
            {"path": f"gate1-{rnd}.md"},
            call_id=f"g1-{rnd}",
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


def _recorded_input_tokens(agent, params, prompt: str) -> int:
    """start_model_call_record 实际落账的 input_tokens。

    用本次调用返回的 call_id 精确匹配记录——ledger 挂在 agent 上跨调用复用，
    records()[0] 会取到上一笔（同 agent 多笔时错账）。
    """
    request = SimpleNamespace(prompt=prompt, agent=agent, params=params)
    ledger, call_id, _ = start_model_call_record(request)
    return next(
        record.input_tokens
        for record in ledger.records()
        if record.call_id == call_id
    )


PROMPT = "请继续处理项目并产出最终报告。" * 30


# ---------------------------------------------------------------- 口径恒等/对齐

def test_start_record_text_identity_with_ir_rounds() -> None:
    """text 协议: 多轮 IR 后记账口径仍 == estimate_tokens(prompt) 恒等。"""
    agent = _agent(protocol="text")
    params = _params(protocol="text", prompt=PROMPT)
    _record_ir_rounds(agent, params, rounds=8, body_chars=400)
    assert _recorded_input_tokens(agent, params, PROMPT) == estimate_tokens(PROMPT)


def test_start_record_native_accounting_matches_unified() -> None:
    """native 协议: 记账口径 == 统一可见口径(同输入同输出, 修复核心)。"""
    agent = _agent(protocol="native")
    params = _params(protocol="native", prompt=PROMPT)
    _record_ir_rounds(agent, params, rounds=20, body_chars=2000)
    recorded = _recorded_input_tokens(agent, params, PROMPT)
    unified = model_visible_context_tokens(agent, params, PROMPT)
    assert recorded == unified


def test_start_record_persists_live_context_total_for_subagent(tmp_path: Path) -> None:
    """The same preflight total used by the provider is projected to the exact child run."""
    from dataclasses import replace

    from agent_py_agent.agent.subagents.manager import SubAgentManager

    agent = _agent(protocol="native")
    manager = SubAgentManager(tmp_path / "subagents", workspace_root=tmp_path)
    task = manager.create_run(goal="实现核心玩法", thought="", plan=["执行"])
    task.status = "RUNNING"
    task.runner_active_attempt_id = "attempt-1"
    manager.save(task)
    agent.subagents = manager
    agent._current_subagent_run_id = task.id
    params = replace(_params(protocol="native", prompt=PROMPT), run_id=task.id)

    recorded = _recorded_input_tokens(agent, params, PROMPT)
    usage = manager.load(task.id).attributes["model_visible_context_usage"]

    assert usage["schema"] == "model_visible_context_usage.v1"
    assert usage["current_tokens"] == recorded
    assert usage["updated_at"] > 0


def test_start_record_native_accounting_scales_with_ir() -> None:
    """native 协议: IR 轮次累积计入记账(rounds 0 vs 20 显著差异)。"""
    agent = _agent(protocol="native")
    empty_params = _params(protocol="native", prompt=PROMPT)
    base = _recorded_input_tokens(agent, empty_params, PROMPT)

    agent2 = _agent(protocol="native")
    params = _params(protocol="native", prompt=PROMPT)
    _record_ir_rounds(agent2, params, rounds=20, body_chars=2000)
    grown = _recorded_input_tokens(agent2, params, PROMPT)
    assert grown > base * 1.5  # IR messages 计入记账


def test_start_record_compact_resume_forwarded_guidance_no_double_count() -> None:
    """compact/resume: 已转发 guidance 不重复计入(seen 跨轮持有), 且两种
    状态下记账都 == 统一口径(无 IR 重复计数/漏计)。"""
    from dataclasses import replace

    guidance = ["护栏：进度已保存", "提示：继续处理剩余文件"]
    agent = _agent(protocol="native")
    params = _params(protocol="native", prompt=PROMPT, tool_context=guidance)
    _record_ir_rounds(agent, params, rounds=8, body_chars=400)

    # resume 前: guidance 未转发 -> 计入
    before = replace(params, live_archive_state={})
    unforwarded = _recorded_input_tokens(agent, before, PROMPT)
    assert unforwarded == model_visible_context_tokens(agent, before, PROMPT)

    # resume 后: guidance 已转发(seen 含两条) -> 不再重复计入
    after = replace(params, live_archive_state={"_forwarded_runtime_guidance": set(guidance)})
    forwarded = _recorded_input_tokens(agent, after, PROMPT)
    assert forwarded == model_visible_context_tokens(agent, after, PROMPT)
    assert forwarded < unforwarded  # 已转发条目不重复堆叠


def test_start_record_first_token_budget_uses_corrected_input() -> None:
    """gate1 修正后, 首 token 预算按出站可见量估计(大 IR 不再被 min clamp 掩盖)。"""
    agent = _agent(protocol="native")
    params = _params(protocol="native", prompt=PROMPT)
    _record_ir_rounds(agent, params, rounds=20, body_chars=2000)

    request = SimpleNamespace(prompt=PROMPT, agent=agent, params=params)
    _, _, estimate = start_model_call_record(request)
    assert estimate.timeout_seconds > 30.0  # 超过 min clamp: 大 IR 预算被正确抬高

    small_agent = _agent(protocol="native")
    small_params = _params(protocol="native", prompt=PROMPT)
    _, _, small_estimate = start_model_call_record(
        SimpleNamespace(prompt=PROMPT, agent=small_agent, params=small_params)
    )
    assert small_estimate.timeout_seconds == pytest.approx(30.0)  # 小输入仍 min clamp


def test_native_accounting_close_to_outbound_serialization() -> None:
    """native 记账/统一口径与最终出站序列化近似(无 IR 重复计数, <30 token 差)。"""
    from agent_py_agent.agent.agent_core.tool_model_generation import (
        _native_provider_messages,
    )

    agent = _agent(protocol="native")
    params = _params(protocol="native", prompt=PROMPT)
    _record_ir_rounds(agent, params, rounds=20, body_chars=2000)
    recorded = _recorded_input_tokens(agent, params, PROMPT)
    messages = _native_provider_messages(agent, params) or []
    outbound = estimate_tokens({"initial_user_prompt": PROMPT, "messages": messages})
    assert abs(recorded - outbound) < 30  # 与出站序列化同口径(空 guidance/tools 差)
