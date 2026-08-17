from __future__ import annotations

"""防回归：native 下「夹在工具往返中间」的系统指引也要到达模型（不仅尾部）。

根因（native 回归二阶）：第一版 native 指引转发（``tool_ir_guidance``）只接 ``tool_context``
**尾部连续**的非工具指引——即「最后一条工具记录之后」那段。问题是大量系统指引在被注入
后，模型下一轮又调了工具，新的 ``[tool-record]`` 追加到这条指引**之后**，把它从尾部挤进
**中段**：尾部口径再也取不到它，native 模型彻底看不到。运行时护栏或进度指引都可能
出现这种位置，因此必须按结构记录转发，而不是依赖它恰好位于尾部。

修复：``unforwarded_runtime_guidance(tool_context, seen)`` 改成扫全表，凡是非 IR 承载、
且没转发过（不在跨轮 ``seen`` 里）的指引都接回；``seen`` 挂在 ``live_archive_state`` 上跨轮
存活，做精确文本去重，保证每条唯一指引整个会话只转发一次、绝不逐轮堆叠。本测试锁死：
中段指引被转发、且重复调用不二次转发。text 协议一字不动（``_native_provider_messages``
对 text 返回 None）。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_ir_guidance import (
    append_runtime_guidance_user_message,
    trailing_runtime_guidance,
    unforwarded_runtime_guidance,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    _forwarded_guidance_seen,
    _native_provider_messages,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
)


def _native_agent():
    return SimpleNamespace(
        backend=SimpleNamespace(name="anthropic_compatible"),
        config=SimpleNamespace(tool_protocol="native", enable_tools=True),
        tools=SimpleNamespace(),
    )


def _one_call_ir():
    call = canonical_history_call(
        "write_file",
        {"path": "a.py"},
        call_id="t1",
    )
    turn = AssistantTurn(text="", tool_calls=[call])
    result = canonical_history_result(call, "[tool=write_file; status=ok]")
    return [turn, result]


# --- pure slicing: unforwarded_runtime_guidance picks up MIDDLE entries -------


def test_unforwarded_collects_middle_guidance_that_trailing_drops():
    # soft-hint is BEFORE a later tool-record → not in the trailing run → trailing口径丢失。
    tool_context = [
        "[tool-loop-guardrail-hint]\n请根据结构化失败事实调整调用参数",
        "[tool-record round=2 index=1]\n{...}\n[tool-output-record round=2 index=1]\nok",
    ]
    # trailing-only loses it (documented historical behavior)
    assert trailing_runtime_guidance(tool_context) == []
    # the broader口径 rescues it
    seen: set[str] = set()
    assert unforwarded_runtime_guidance(tool_context, seen) == [
        "[tool-loop-guardrail-hint]\n请根据结构化失败事实调整调用参数"
    ]


def test_unforwarded_skips_ir_backed_and_dedupes_via_seen():
    tool_context = [
        "[tool-record round=1 index=1]\n{...}\n[tool-output-record round=1 index=1]\nok",
        "[assistant-tool-round-1]\n模型本轮文本",
        "[SUBAGENT_RESULT]\n{...}\n[/SUBAGENT_RESULT]",
        "[tool-loop-guardrail-hint]\n护栏提示",
        "[task-local-progress]\n进度提示",
    ]
    seen: set[str] = set()
    first = unforwarded_runtime_guidance(tool_context, seen)
    assert first == ["[tool-loop-guardrail-hint]\n护栏提示", "[task-local-progress]\n进度提示"]
    # 第二次扫同一张表：都已在 seen 里 → 不再转发，绝不重复
    assert unforwarded_runtime_guidance(tool_context, seen) == []


def test_unforwarded_handles_non_list_and_blank():
    assert unforwarded_runtime_guidance(None, set()) == []
    assert unforwarded_runtime_guidance([], set()) == []
    assert unforwarded_runtime_guidance(["  ", ""], set()) == []


def test_append_with_seen_forwards_middle_guidance_once():
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "write_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    tool_context = [
        "[task-local-progress]\n还有结构化进度项需要继续处理",
        "[tool-record round=3 index=1]\n{...}\n[tool-output-record round=3 index=1]\nok",
    ]
    seen: set[str] = set()
    out = append_runtime_guidance_user_message(messages, tool_context, seen=seen)
    assert out[-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "[task-local-progress]\n还有结构化进度项需要继续处理"}],
    }
    # second pass with the same seen → nothing appended (no duplication)
    assert append_runtime_guidance_user_message(out, tool_context, seen=seen) == out


# --- integration: _native_provider_messages surfaces middle guidance + dedup ---


def test_native_messages_forward_middle_guidance_then_dedupe_across_calls():
    agent = _native_agent()
    params = SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol="native"
        ),
        tool_ir_history=_one_call_ir(),
        live_archive_state={},  # persists the seen set across calls, like the real params
        tool_context=[
            # buried by the tool-record below → trailing-only would lose it
            "[tool-loop-guardrail-hint]\n请根据结构化失败事实调整调用参数",
            "[tool-record round=1 index=1]\n{...}\n[tool-output-record round=1 index=1]\nok",
        ],
    )
    first = _native_provider_messages(agent, params)
    assert [m["role"] for m in first] == ["assistant", "user", "user"]
    assert "结构化失败事实" in first[-1]["content"][0]["text"]
    # 工具往返文本不折回（在 IR 里）
    assert not any("tool-record" in str(m) for m in first)
    # seen 已登记这条指引
    assert len(_forwarded_guidance_seen(params)) == 1

    # 第二次（同一 params，live_archive_state 跨轮存活）→ 不再二次转发同一指引
    second = _native_provider_messages(agent, params)
    assert [m["role"] for m in second] == ["assistant", "user"]


def test_native_messages_forward_new_guidance_but_not_old():
    agent = _native_agent()
    state: dict = {}
    params = SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol="native"
        ),
        tool_ir_history=_one_call_ir(),
        live_archive_state=state,
        tool_context=[
            "[tool-loop-guardrail-hint]\n旧指引",
            "[tool-record round=1 index=1]\n{...}\n[tool-output-record round=1 index=1]\nok",
        ],
    )
    _native_provider_messages(agent, params)
    # 下一轮：又注入了一条新指引（中段），旧的仍在表里
    params.tool_context.append("[task-local-progress]\n请继续处理最新结构化进度")
    params.tool_context.append("[tool-record round=2 index=1]\n{...}\n[tool-output-record round=2 index=1]\nok")
    out = _native_provider_messages(agent, params)
    tail_text = out[-1]["content"][0]["text"]
    assert "最新结构化进度" in tail_text  # 新指引转发
    assert "旧指引" not in tail_text  # 旧指引不重复


def test_text_protocol_unaffected():
    agent = SimpleNamespace(
        backend=SimpleNamespace(name="anthropic_compatible"),
        config=SimpleNamespace(enable_tools=True),
        tools=SimpleNamespace(),
    )
    params = SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol="text"
        ),
        tool_ir_history=_one_call_ir(),
        live_archive_state={},
        tool_context=["[tool-loop-guardrail-hint]\nx", "[tool-record round=1 index=1]\nok"],
    )
    assert _native_provider_messages(agent, params) is None


def test_forwarded_guidance_seen_falls_back_without_state():
    # 拿不到 live_archive_state dict 时返回一次性空集合（不崩、不跨轮记忆）
    params = SimpleNamespace()
    seen = _forwarded_guidance_seen(params)
    assert seen == set()
    # 同一 params 没有 dict 容器 → 每次返回的是新集合，但仍是 set
    assert isinstance(_forwarded_guidance_seen(params), set)
