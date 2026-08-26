from __future__ import annotations

"""EXEC-16 回归: native 协议下 compact 触发检查必须包含 IR 工具结果。

真机背景: ma 双线 native 重跑 36 轮仍 archive_events=0——should_compact_before_more_tool_output
曾用 estimate_tokens(prompt 文本)（native 下工具结果在 IR messages 不在 prompt 文本, 恒 ~9K
达不到阈值）且 _has_previous_tool_context 文本前缀判定对 native 恒 False。两个条件叠加 =
compact 永不触发。修复后: native 下以 IR 历史 ToolResult 为准、token 口径换 model_visible_context_tokens。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_pressure import (
    model_visible_context_tokens,
    should_compact_before_more_tool_output,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolContentBlock,
    ToolResult,
)
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


def _agent(protocol: str = "native") -> SimpleNamespace:
    return SimpleNamespace(
        backend=SimpleNamespace(name="anthropic_compatible"),
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=True,
            auto_save_memory=True,
            model_context_window_tokens=4_000,  # 小窗口让测试不需要巨型内容
            memory_compact_auto_trigger_percent=70,
        ),
        root=None,
        tools=SimpleNamespace(),
    )


def _params(protocol: str = "native", *, save: bool = True, history=None):
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
        user_prompt="x",
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
        save=save,
        delivery_contract={},
        tool_ir_history=[] if history is None else history,
    )


def _big_result(call_id: str, words: int) -> ToolResult:
    body = "word " * words
    return ToolResult(
        call_id=call_id,
        tool_name="read_file",
        status="succeeded",
        content_blocks=(ToolContentBlock(type="text", text=body),),
    )


def test_native_ir_history_with_results_triggers_compact() -> None:
    """native + save=True + IR 已有 ToolResult 且总量超过阈值 → 触发 compact。"""
    agent = _agent("native")
    params = _params(
        "native",
        save=True,
        history=[
            _big_result("c1", 900),
            _big_result("c2", 900),
            _big_result("c3", 900),
        ],
    )
    assert should_compact_before_more_tool_output(agent, params, "静态前缀 prompt")


def test_native_empty_ir_history_does_not_trigger() -> None:
    """native + 无工具结果 → 不触发（空工具轮无意义触发保护仍生效）。"""
    agent = _agent("native")
    params = _params("native", save=True, history=[])
    assert not should_compact_before_more_tool_output(agent, params, "静态前缀 prompt")


def test_native_save_false_keeps_compact_disabled() -> None:
    """save=False 时 compact 不执行（保留 save 语义, 与 YAML 注释一致）。"""
    agent = _agent("native")
    params = _params(
        "native",
        save=False,
        history=[_big_result("c1", 900)],
    )
    assert not should_compact_before_more_tool_output(agent, params, "静态前缀 prompt")


def test_text_protocol_still_uses_prompt_text_threshold() -> None:
    """text 协议旧行为不变: prompt 文本估算达到阈值才触发。"""
    agent = _agent("text")
    params = _params("text", save=True, history=[])
    params.tool_context.append("[tool-record r1] old tool result body")
    long_prompt = "word " * 4000
    assert should_compact_before_more_tool_output(agent, params, long_prompt)
    short_prompt = "hello"
    assert not should_compact_before_more_tool_output(agent, params, short_prompt)


def test_context_estimate_counts_only_a_real_provider_system_channel() -> None:
    """宿主规则只在后端声明真实 system 通道时计入 provider 可见 token。"""
    legacy = _agent("text")
    capable = _agent("text")
    capable.backend.supports_system_instructions = True
    capable.backend.supports_provider_request_options = True
    params = _params("text", save=True, history=[])

    legacy_tokens = model_visible_context_tokens(legacy, params, "hello")
    capable_tokens = model_visible_context_tokens(capable, params, "hello")

    assert capable_tokens > legacy_tokens
