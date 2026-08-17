from __future__ import annotations

"""防回归：native 下「系统注入的运行时指引」必须接回发往 provider 的 messages。

根因（native 回归实锤）：builder 的 native 旁路把整段文本 ``tool_context`` 从 prompt 里
丢掉（工具往返改由 IR messages 承载），但 ``tool_context`` 里还累积着一大类**不是工具
调用**的系统指引——运行时策略、任务状态更新、工具护栏和问句逃逸守卫等。这些不进 IR、
又被旁路掉，于是 native 模型完全收不到「当前结构化状态 / 下一步该做什么」。本测试锁死
修复：``_native_provider_messages`` 把这类尾部指引接成一条收尾 user
文本消息，且只接「最后一条工具记录之后」的指引，绝不重复折回已在 IR 里的工具往返。
"""

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_ir_guidance import (
    append_runtime_guidance_user_message,
    trailing_runtime_guidance,
)
from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
)


def _native_agent(root: Path, *, protocol: str = "native", backend: str = "anthropic_compatible"):
    return SimpleNamespace(
        backend=SimpleNamespace(name=backend),
        config=SimpleNamespace(tool_protocol=protocol, enable_tools=True),
        root=root,
        tools=SimpleNamespace(),
    )


# --- pure slicing: trailing_runtime_guidance ---------------------------------


def test_trailing_guidance_takes_only_entries_after_last_tool_record():
    tool_context = [
        "[tool-record round=1 index=1]\n{...}\n[tool-output-record round=1 index=1]\nA",
        "[tool-record round=2 index=1]\n{...}\n[tool-output-record round=2 index=1]\nB",
        "[runtime-policy-guidance]\n{...}\n请按结构化运行状态继续处理。",
        "[task-state-guidance]\n{...}\n请先处理仍开放的步骤。",
    ]
    tail = trailing_runtime_guidance(tool_context)
    assert tail == [
        "[runtime-policy-guidance]\n{...}\n请按结构化运行状态继续处理。",
        "[task-state-guidance]\n{...}\n请先处理仍开放的步骤。",
    ]


def test_trailing_guidance_empty_when_tail_is_a_tool_record():
    tool_context = [
        "[stale-runtime-guidance]\n旧指引（已被后续工具动作越过）",
        "[tool-record round=3 index=1]\n{...}\n[tool-output-record round=3 index=1]\nC",
    ]
    assert trailing_runtime_guidance(tool_context) == []


def test_trailing_guidance_skips_subagent_result_and_assistant_round_markers():
    tool_context = [
        "[assistant-tool-round-1]\n模型本轮文本+调用",
        "[SUBAGENT_RESULT]\n{...}\n[/SUBAGENT_RESULT]",
        "[tool-loop-guardrail-hint]\n护栏提示应当转发",
    ]
    assert trailing_runtime_guidance(tool_context) == [
        "[tool-loop-guardrail-hint]\n护栏提示应当转发"
    ]


def test_trailing_guidance_handles_non_list_and_empty():
    assert trailing_runtime_guidance(None) == []
    assert trailing_runtime_guidance([]) == []
    assert trailing_runtime_guidance(["   ", ""]) == []


# --- append_runtime_guidance_user_message ------------------------------------


def test_append_adds_single_trailing_user_text_message():
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "write_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    ]
    tool_context = [
        "[tool-record round=1 index=1]\n{...}\n[tool-output-record round=1 index=1]\nok",
        "[verification-guidance]\n请补跑测试后再继续。",
    ]
    out = append_runtime_guidance_user_message(messages, tool_context)
    assert len(out) == 3
    assert out[-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "[verification-guidance]\n请补跑测试后再继续。"}],
    }
    # input list not mutated
    assert len(messages) == 2


def test_append_noop_without_guidance_and_forwards_guidance_without_ir_messages():
    messages = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
    ]
    only_tool_records = [
        "[tool-record round=1 index=1]\n{...}\n[tool-output-record round=1 index=1]\nok"
    ]
    assert append_runtime_guidance_user_message(messages, only_tool_records) == messages
    assert append_runtime_guidance_user_message([], ["[runtime-policy-guidance]\nx"]) == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "[runtime-policy-guidance]\nx"}],
        }
    ]


# --- integration: _native_provider_messages forwards guidance ----------------


def _ir_history_with_one_call():
    call = canonical_history_call(
        "write_file",
        {"path": "lru.py"},
        call_id="toolu_w",
    )
    turn = AssistantTurn(text="", tool_calls=[call])
    result = canonical_history_result(call, "[tool=write_file; status=ok]")
    return [turn, result]


def test_native_provider_messages_appends_runtime_guidance(tmp_path):
    agent = _native_agent(tmp_path)
    params = SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol="native"
        ),
        tool_ir_history=_ir_history_with_one_call(),
        tool_context=[
            "[tool-record round=1 index=1]\n{...}\n[tool-output-record round=1 index=1]\nok",
            "[verification-evidence-rework]\n{...}\n请先真实运行测试，确认全部通过后再提交。",
        ],
    )
    messages = _native_provider_messages(agent, params)
    # tool_use / tool_result pair preserved, guidance appended as a trailing user text message
    assert [m["role"] for m in messages] == ["assistant", "user", "user"]
    assert messages[-1]["content"][0]["type"] == "text"
    assert "请先真实运行测试" in messages[-1]["content"][0]["text"]
    # the [tool-record] text body is NOT folded back (it lives in IR)
    assert not any("tool-record" in str(m) for m in messages)


def test_native_provider_messages_no_guidance_keeps_pairs_only(tmp_path):
    agent = _native_agent(tmp_path)
    params = SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol="native"
        ),
        tool_ir_history=_ir_history_with_one_call(),
        # only a tool-record at the tail → nothing to forward
        tool_context=[
            "[tool-record round=1 index=1]\n{...}\n[tool-output-record round=1 index=1]\nok"
        ],
    )
    messages = _native_provider_messages(agent, params)
    assert [m["role"] for m in messages] == ["assistant", "user"]


def test_native_provider_messages_forwards_first_turn_guidance_without_ir(tmp_path):
    agent = _native_agent(tmp_path)
    params = SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-first", source_protocol="native"
        ),
        tool_ir_history=[],
        live_archive_state={},
        tool_context=["[tool-protocol-violation]\n上一轮正文不是可执行调用，请按结构化协议重试。"],
    )

    first = _native_provider_messages(agent, params)
    assert first == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "[tool-protocol-violation]\n上一轮正文不是可执行调用，请按结构化协议重试。",
                }
            ],
        }
    ]
    # The exact guidance is sent only once across model retries.
    assert _native_provider_messages(agent, params) is None


def test_text_protocol_unaffected_by_guidance_forwarding(tmp_path):
    # text protocol → _native_provider_messages returns None, no structured messages at all.
    agent = _native_agent(tmp_path, protocol="text")
    params = SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-1", source_protocol="native"),
        tool_ir_history=_ir_history_with_one_call(),
        tool_context=["[runtime-policy-guidance]\nx"],
    )
    assert _native_provider_messages(agent, params) is None
