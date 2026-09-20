from __future__ import annotations

"""Step 2 集成测试：native 工具历史 → IR → 出站 Anthropic messages。

覆盖治本红线区的端到端数据流（不依赖真实模型/网络）：
1. 真实 ``_record_tool_call`` 路径在 native 下并行产 IR（与 tool_context 文本共存）；
2. 真实 provider tool_use id 透传：archive 用入站 id 覆盖合成 id，IR 用同一真实 id；
3. ``_native_provider_messages`` 把多轮 IR 翻成正确的 assistant(tool_use)/
   user(tool_result) 序列（真实 id 配对、连续结果合并、跨轮不串合）；
4. backend.generate(messages=...) 走结构化对话并保留原始 user prompt；text 协议零改动；
5. builder native 旁路：prompt 不再折入 [tool-record] 文本；
6. 子代理收口结果在 native 下落进 IR 历史。
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import (
    _record_tool_call,
    _runtime_injections_with_delivery_contract,
)
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _native_initial_tool_ir_history,
    _native_provider_history_messages,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _materialize_native_prompt_facts,
    _native_provider_messages,
)
from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, ToolResult, UserTurn
from agent_py_agent.agent.conversation.models import ConversationHistorySeed, MessageLogEntry
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
    provider_history_messages_from_rows,
)
from agent_py_agent.agent.prompting_parts.builder import PromptBuilder, ToolSections
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)

# --- minimal native agent + params (no archive/network side effects) ---------


def _native_agent(root: Path, *, protocol: str = "native", backend: str = "anthropic_compatible"):
    """A stub agent whose config/backend make native_tool_use_active(...) == True."""
    return SimpleNamespace(
        backend=SimpleNamespace(name=backend),
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=True,
            auto_save_memory=False,  # disable live archive writes for a clean unit
            tool_output_externalize_min_chars=10_000_000,  # keep results inline
            tool_output_preview_chars=160,
        ),
        root=root,
        tools=SimpleNamespace(),
    )


def _params(*, protocol: str = "native") -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="分析两个文件并汇总",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="# Tools\n- read_file",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        save=False,
        delivery_contract={},
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol=protocol
        ),
    )


def test_reasoning_only_continue_preserves_each_response_before_next_tool_round(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.loop_support import _completed_turn_native_messages
    from agent_py_agent.agent.agent_core.tool_ir_history import open_assistant_turn_ir
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend

    agent, params = _native_agent(tmp_path), _params()
    params = replace(params, tool_runtime_snapshot=runtime_snapshot_for_model_specs((), run_id=params.run_id))
    counters = ToolLoopRepairCounters()
    thoughts = ["先前完整分析一。" * 4000, "继续先前分析二。" * 1000]
    for index, thought in enumerate(thoughts):
        decision = tool_loop_response_decision(ToolLoopResponseDecisionRequest(
            agent, params, ModelResponse(text="", backend="openai_compatible",
                assistant_content_blocks=[{"type": "thinking", "thinking": thought}]),
            counters, turn_id=f"model-{index}",
        ))
        assert decision.action == "continue"
        counters = decision.counters
    assert counters.empty_text_repairs == 2
    messages = _native_provider_messages(agent, params)
    assert [m["content"][0]["thinking"] for m in messages if m["role"] == "assistant"] == thoughts

    captured = {}
    backend = OpenAICompatibleBackend(BackendOptions(api_base="https://example.test/v1",
        api_key="test", model_name="test", stream_enabled=False))

    def reply(path, payload, headers):
        captured.update(payload)
        return {"choices": [{"message": {"content": "已完成"}, "finish_reason": "stop"}]}

    backend.request_json = reply
    backend.generate("继续", messages=messages)
    assert [m["reasoning_content"] for m in captured["messages"] if m["role"] == "assistant"] == thoughts
    open_assistant_turn_ir(params, tool_rounds=1, response_text="执行验证")
    _record(agent, params, tool_rounds=1, idx=1, tool_name="read_file", call_id="c1",
            arguments={"path": "a"}, output="真实结果")
    final = ModelResponse(text="已经完成", backend="openai_compatible")
    decision = tool_loop_response_decision(ToolLoopResponseDecisionRequest(agent, params, final, counters))
    assert decision.action == "break"
    saved = _completed_turn_native_messages(params, final)
    assert [b["thinking"] for m in saved for b in m["content"] if b["type"] == "thinking"] == thoughts
    assert sum(b.get("text") == "已经完成" for m in saved for b in m["content"]) == 1


@pytest.mark.parametrize("block", [
    {"type": "thinking", "thinking": "已生成的思考", "signature": "original"},
    {"type": "redacted_thinking", "data": "opaque"},
    {"type": "responses_reasoning", "model": "test",
     "item": {"type": "reasoning", "encrypted_content": "opaque", "summary": []}},
])
def test_reasoning_only_retry_is_bounded_without_public_answer(tmp_path, block):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    agent, params = _native_agent(tmp_path), _params()
    params = replace(params, tool_runtime_snapshot=runtime_snapshot_for_model_specs((), run_id=params.run_id))
    response = ModelResponse(text="", backend="anthropic_compatible", assistant_content_blocks=[block])
    counters = ToolLoopRepairCounters()
    actions = []
    for _ in range(3):
        decision = tool_loop_response_decision(ToolLoopResponseDecisionRequest(agent, params, response, counters))
        counters = decision.counters
        actions.append(decision.action)
    assert actions == ["continue", "continue", "break"]
    assert len(params.tool_ir_history) == 2
    assert decision.response.text == ""


def _record(
    agent,
    params,
    *,
    tool_rounds: int,
    idx: int,
    tool_name: str,
    call_id: str,
    arguments: dict[str, object],
    output: str,
    error_code: str = "",
):
    call = canonical_history_call(
        tool_name,
        arguments,
        call_id=call_id,
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-{tool_rounds}",
        attempt_id=params.request_id,
    )
    result = canonical_history_result(
        call,
        output,
        ok=not error_code,
        error_code=error_code or "TOOL_EXECUTION_FAILED",
    )
    _record_tool_call(
        agent,
        ToolCallRecordParams(
            params=params,
            tool_rounds=tool_rounds,
            idx=idx,
            call=call,
            result=result,
        ),
    )
    return call, result


# --- 1+2+3: multi-round IR through the real _record_tool_call path ------------


def test_multi_round_native_history_produces_paired_merged_messages(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()

    # round 1: two read_file calls with real provider tool_use ids (toolu_*)
    _record(
        agent, params, tool_rounds=1, idx=1,
        tool_name="read_file", call_id="toolu_aaa", arguments={"path": "a.md"},
        output="BODY-A",
    )
    _record(
        agent, params, tool_rounds=1, idx=2,
        tool_name="read_file", call_id="toolu_bbb", arguments={"path": "b.md"},
        output="BODY-B",
    )
    # round 2: one write_file call
    _record(
        agent, params, tool_rounds=2, idx=1,
        tool_name="write_file", call_id="toolu_ccc", arguments={"path": "out.md"},
        output="WROTE",
    )

    messages = _native_provider_messages(agent, params)

    # assistant(2 tool_use) / user(2 tool_result merged) / assistant(1 tool_use) / user(1 tool_result)
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant", "user"]
    # round 1 assistant carries both tool_use blocks with the REAL provider ids
    assert [b["type"] for b in messages[0]["content"]] == ["tool_use", "tool_use"]
    assert [b["id"] for b in messages[0]["content"]] == ["toolu_aaa", "toolu_bbb"]
    assert [b["name"] for b in messages[0]["content"]] == ["read_file", "read_file"]
    # input is structured (no json string), control keys stripped
    assert messages[0]["content"][0]["input"] == {"path": "a.md"}
    # round 1 results merged into ONE user message, tool_use_id pairs the call id
    assert [b["tool_use_id"] for b in messages[1]["content"]] == ["toolu_aaa", "toolu_bbb"]
    assert messages[1]["content"][0]["content"].startswith(
        "[tool-result; tool=read_file; status=succeeded;"
    )
    assert messages[1]["content"][0]["is_error"] is False
    # round 2 is a separate assistant/user pair — does NOT cross-merge with round 1
    assert messages[2]["content"][0]["id"] == "toolu_ccc"
    assert messages[3]["content"][0]["tool_use_id"] == "toolu_ccc"


def test_error_result_marks_is_error_true_in_messages(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _record(
        agent, params, tool_rounds=1, idx=1,
        tool_name="web_fetch", call_id="toolu_e", arguments={"url": "http://x"},
        output='{"error_code":"WEB_FETCH_FAILED"}', error_code="WEB_FETCH_FAILED",
    )

    messages = _native_provider_messages(agent, params)

    result_block = messages[1]["content"][0]
    assert result_block["tool_use_id"] == "toolu_e"
    assert result_block["is_error"] is True


def test_native_history_replays_provider_thinking_blocks_on_next_tool_round(tmp_path):
    from agent_py_agent.agent.agent_core.tool_ir_history import open_assistant_turn_ir

    agent = _native_agent(tmp_path)
    params = _params()
    open_assistant_turn_ir(
        params,
        tool_rounds=1,
        response_text="我先读取。",
        response_content_blocks=[
            {"type": "thinking", "thinking": "先检查", "signature": "sig-1"},
            {"type": "text", "text": "我先读取。"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {"path": "README.md"},
            },
        ],
    )
    _record(
        agent,
        params,
        tool_rounds=1,
        idx=1,
        tool_name="read_file",
        call_id="toolu_1",
        arguments={"path": "README.md"},
        output="BODY",
    )

    messages = _native_provider_messages(agent, params)

    assert messages[0]["content"][:2] == [
        {"type": "thinking", "thinking": "先检查", "signature": "sig-1"},
        {"type": "text", "text": "我先读取。"},
    ]
    assert messages[0]["content"][2]["id"] == "toolu_1"
    assert messages[1]["content"][0]["tool_use_id"] == "toolu_1"


def test_native_record_coexists_with_text_tool_context(tmp_path):
    # Gray-dual-track: IR history AND the legacy text tool_context both get populated.
    agent = _native_agent(tmp_path)
    params = _params()
    _record(
        agent, params, tool_rounds=1, idx=1,
        tool_name="read_file", call_id="toolu_z", arguments={"path": "z.md"},
        output="ZBODY",
    )

    assert params.tool_ir_history, "native must populate IR history"
    assert any("[tool-record" in str(item) for item in params.tool_context), "text track must still be written"


def test_native_history_replays_only_provider_authored_tool_arguments(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    public_arguments = {
        "artifact_ref": "run-web:fetch-1",
        "mode": "tail",
        "max_chars": 800,
    }
    execution_arguments = {
        **public_arguments,
        "run_id": "run-web",
        "task_id": "task-web",
        "request_id": "request-web",
    }
    model_call = canonical_history_call(
        "read_artifact",
        public_arguments,
        call_id="toolu_safe",
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-1",
        attempt_id=params.request_id,
    )
    execution_call = canonical_history_call(
        "read_artifact",
        execution_arguments,
        call_id="toolu_safe",
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-1",
        attempt_id=params.request_id,
    )
    result = canonical_history_result(
        execution_call,
        '{"ok": true, "content": "Copyright ©2001-2026"}',
    )

    _record_tool_call(
        agent,
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            call=execution_call,
            result=result,
            model_call=model_call,
        ),
    )

    messages = _native_provider_messages(agent, params)
    assert messages[0]["content"][0]["input"] == public_arguments
    assert "run_id" not in params.tool_context[-1]
    assert params.archive_tool_calls[-1]["parameters"] == execution_arguments
    assert params.archive_tool_calls[-1]["model_parameters"] == public_arguments


# --- real id stamping back onto the result ------------------------------------


def test_archive_stamps_real_provider_id_onto_result_call_id(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    call, result = _record(
        agent, params, tool_rounds=3, idx=2,
        tool_name="read_file", call_id="toolu_real", arguments={"path": "p"},
        output="B",
    )
    # Provider identity is already canonical before recording and remains unchanged.
    assert call.call_id == "toolu_real"
    assert result.call_id == "toolu_real"
    # and the archive record carries the same real id.
    assert params.archive_tool_calls[-1]["call_id"] == "toolu_real"


# --- text protocol stays synthetic (gray-line: text path untouched) -----------


def test_text_protocol_keeps_canonical_call_id_and_no_native_ir(tmp_path):
    agent = _native_agent(tmp_path, protocol="text")
    params = _params(protocol="text")
    _record(
        agent, params, tool_rounds=3, idx=2,
        tool_name="read_file", call_id="text-call-1", arguments={"path": "p"},
        output="B",
    )
    assert params.archive_tool_calls[-1]["call_id"] == "text-call-1"
    assert params.tool_ir_history == []
    assert _native_provider_messages(agent, params) is None


def test_openai_backend_uses_the_same_native_ir_history(tmp_path):
    agent = _native_agent(tmp_path, backend="openai_compatible")
    params = _params()
    _record(
        agent, params, tool_rounds=1, idx=1,
        tool_name="read_file", call_id="toolu_x", arguments={"path": "p"},
        output="B",
    )
    assert len(params.tool_ir_history) == 2
    assert params.tool_ir_history[0].tool_calls[0].tool_name == "read_file"
    assert params.tool_ir_history[1].call_id == "toolu_x"
    assert params.archive_tool_calls[-1]["call_id"] == "toolu_x"


# --- 4: backend.generate(messages=...) wiring ---------------------------------


def test_anthropic_backend_keeps_initial_user_prompt_before_native_history():
    backend = AnthropicCompatibleBackend(
        BackendOptions(
            api_base="https://api.example.com",
            api_key="k",
            model_name="claude-x",
            request_timeout=60,
            max_tokens=512,
            temperature=0.2,
            stream_enabled=False,
        )
    )
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "done"}]}

    backend.request_json = fake_request_json
    msgs = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "A", "is_error": False}]},
    ]

    resp = backend.generate("SYSTEM+TASK PROMPT", messages=msgs)

    assert resp.text == "done"
    # The first request sent prompt as a user turn; every continuation preserves that identity.
    assert captured["payload"]["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "SYSTEM+TASK PROMPT",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        msgs[0],
        {
            **msgs[1],
            "content": [
                {
                    **msgs[1]["content"][0],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
    ]
    assert "cache_control" not in msgs[-1]["content"][-1]
    assert "system" not in captured["payload"]


def test_anthropic_backend_without_messages_keeps_single_user_prompt():
    backend = AnthropicCompatibleBackend(
        BackendOptions(
            api_base="https://api.example.com",
            api_key="k",
            model_name="claude-x",
            request_timeout=60,
            max_tokens=512,
            temperature=0.2,
            stream_enabled=False,
        )
    )
    captured: dict[str, object] = {}
    backend.request_json = lambda path, payload, headers: (captured.__setitem__("payload", payload) or {"content": [{"type": "text", "text": "ok"}]})

    backend.generate("PLAIN PROMPT")

    # text protocol path unchanged: one user message, no system field.
    assert captured["payload"]["messages"] == [{"role": "user", "content": "PLAIN PROMPT"}]
    assert "system" not in captured["payload"]


# --- 5: builder native bypass -------------------------------------------------


def _builder(tmp_path) -> PromptBuilder:
    config = AgentConfig()
    config.system_prompt = "SYS"
    return PromptBuilder(config, Path(tmp_path))


def test_builder_native_drops_tool_record_text(tmp_path):
    builder = _builder(tmp_path)
    tool_context = ['[tool-record round=1 index=1]\n{"tool":"read_file"}\n[tool-output-record round=1 index=1]\nSECRET-BODY']

    native_prompt = builder.build(
        "TASK",
        [],
        tools=ToolSections(tool_context=tool_context, native_tool_use=True),
    )
    text_prompt = builder.build(
        "TASK",
        [],
        tools=ToolSections(tool_context=tool_context, native_tool_use=False),
    )

    # native: the [tool-record] text body must NOT be folded into the prompt.
    assert "SECRET-BODY" not in native_prompt
    assert "tool-record" not in native_prompt
    # but the task is still present.
    assert "TASK" in native_prompt
    # text protocol: the transcript IS folded (unchanged behavior).
    assert "SECRET-BODY" in text_prompt


def test_native_completed_conversation_precedes_current_user_without_rewriting() -> None:
    seed = ConversationHistorySeed(
        compact_summary="更早轮次摘要",
        compact_generation=2,
        messages=(
            ("user", "第一轮问题"),
            ("assistant", "第一轮答复"),
        ),
    )
    params = SimpleNamespace(
        conversation_history_seed=seed,
        user_prompt="第二轮问题",
    )

    history = _native_initial_tool_ir_history(
        params,
        carried_handoff="",
        carried_user_inputs=[],
    )
    messages = [
        *_native_provider_history_messages(params),
        *AnthropicMessageAdapter().to_provider_messages(history),
    ]

    assert [message["role"] for message in messages] == [
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert messages[1]["content"] == [
        {"type": "text", "text": "# User Task\n第一轮问题"}
    ]
    assert messages[-1]["content"] == [
        {"type": "text", "text": "# User Task\n第二轮问题"}
    ]


def test_native_dynamic_facts_make_later_tool_request_append_only(tmp_path) -> None:
    agent = _native_agent(tmp_path)
    params = _params()
    params.tool_ir_history.append(UserTurn("# User Task\n检查仓库"))

    first_request = _materialize_native_prompt_facts(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=CacheStructuredPrompt("stable", "runtime-facts-1"),
            tool_rounds=0,
        )
    )
    assert first_request.prompt.cache_layout.volatile_suffix == ""
    first_messages = _native_provider_messages(agent, params)
    assert first_messages is not None

    _record(
        agent,
        params,
        tool_rounds=1,
        idx=1,
        tool_name="read_file",
        call_id="call-prefix-1",
        arguments={"path": "README.md"},
        output="ok",
    )
    second_request = _materialize_native_prompt_facts(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=CacheStructuredPrompt("stable", "runtime-facts-2"),
            tool_rounds=1,
        )
    )
    assert second_request.prompt.cache_layout.volatile_suffix == ""
    second_messages = _native_provider_messages(agent, params)
    assert second_messages is not None

    assert second_messages[: len(first_messages)] == first_messages
    assert "runtime-facts-2" in str(second_messages[-1])
    assert sum("runtime-facts-1" in str(message) for message in second_messages) == 1


def test_native_current_compact_generation_is_a_latest_typed_runtime_fact(tmp_path) -> None:
    agent = _native_agent(tmp_path)
    params = replace(
        _params(),
        conversation_history_seed=ConversationHistorySeed(compact_generation=3),
    )
    params.tool_ir_history.extend(
        [
            UserTurn("# User Task\n告诉我 Compact 次数"),
            RuntimeFactsTurn(
                '# Conversation Runtime State\n'
                '{"schema":"conversation_runtime_state.v1","compact_generation":2}'
            ),
        ]
    )

    _materialize_native_prompt_facts(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=CacheStructuredPrompt("stable", "other-current-facts"),
            tool_rounds=0,
        )
    )
    messages = _native_provider_messages(agent, params)

    assert messages is not None
    latest = messages[-1]["content"][0]["text"]
    assert '"schema":"conversation_runtime_state.v1"' in latest
    assert '"compact_generation":3' in latest
    assert '"authority":"conversation_history_seed.compact_generation"' in latest


def test_native_builder_materializes_only_changed_fact_sources(tmp_path) -> None:
    agent = _native_agent(tmp_path)
    params = _params()
    params.tool_ir_history.append(UserTurn("继续整理自己的项目"))
    builder = PromptBuilder(AgentConfig(system_prompt="stable system", prompt_files=[]), tmp_path)
    previous_messages = []
    for index, injection in enumerate(("约定甲", "约定甲", "约定乙", "约定甲", "")):
        prompt = builder.build(
            "继续整理自己的项目", [],
            inject=[injection] if injection else [],
            workspace_context_override="unchanged workspace " * 300,
            tools=ToolSections(
                native_tool_use=True,
                execution_facts_section=f"# Current Turn Execution Facts\nstep={index}",
            ),
        )
        _materialize_native_prompt_facts(
            ModelGenerateParams(agent=agent, params=params, prompt=prompt, tool_rounds=index)
        )
        messages = _native_provider_messages(agent, params)
        assert messages[:len(previous_messages)] == previous_messages
        previous_messages = messages
        if index < 4:
            _record(agent, params, tool_rounds=index + 1, idx=1, tool_name="read_file",
                    call_id=f"source-delta-{index}", arguments={"path": "README.md"}, output="ok")

    facts = [item for item in params.tool_ir_history if isinstance(item, RuntimeFactsTurn)]
    assert sum("unchanged workspace" in item.text for item in facts) == 1
    assert sum("# Related Memory" in item.text for item in facts) == 1
    assert [item.text for item in facts if item.source == "prompt.runtime_injection"] == [
        "# Runtime Injection\n约定甲", "# Runtime Injection\n约定乙",
        "# Runtime Injection\n约定甲", "# Runtime Injection\n（无）",
    ]
    assert len([item for item in facts if item.source == "prompt.execution"]) == 5
    assert params.tool_ir_history[0] == UserTurn("继续整理自己的项目")


def test_native_batch_facts_keep_old_request_prefix_and_all_tool_pairs(tmp_path):
    from agent_py_agent.agent.tooling.operation_verification import (
        render_current_turn_execution_facts,
    )

    agent = _native_agent(tmp_path)
    params = _params()
    params.tool_ir_history.append(UserTurn("继续处理自己的文件"))
    before = []
    for index in range(1, 21):
        _record(agent, params, tool_rounds=index, idx=1, tool_name="read_file",
                call_id=f"batch-{index}", arguments={"path": "README.md"}, output=f"result-{index}")
        facts = render_current_turn_execution_facts(agent, params.archive_tool_calls)
        prompt = CacheStructuredPrompt("stable", volatile_sections=(("prompt.execution", facts),))
        _materialize_native_prompt_facts(ModelGenerateParams(agent=agent, params=params, prompt=prompt, tool_rounds=index))
        messages = _native_provider_messages(agent, params)
        assert messages[:len(before)] == before
        assert f'"call_id":"batch-{index}"' in facts
        if index > 1:
            assert f'"call_id":"batch-{index - 1}"' not in facts
        before = messages
    execution = [item for item in params.tool_ir_history if isinstance(item, RuntimeFactsTurn) and item.source == "prompt.execution"]
    assert len(execution) == 20
    assert sum(isinstance(item, ToolResult) for item in params.tool_ir_history) == 20
    assert sum(item.text.count('"call_id":') for item in execution) == 20


def test_native_pressure_projection_matches_materialized_request_without_mutation(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        _model_visible_context_components,
    )

    agent = _native_agent(tmp_path)
    params = _params()
    params.tool_ir_history.append(UserTurn("真实用户输入"))
    for fact in ("旧状态", "新状态", "新状态"):
        prompt = CacheStructuredPrompt("stable", volatile_sections=(
            ("workspace", "不变的工作区" * 500), ("execution", fact),
        ))
        original = list(params.tool_ir_history)
        before = _model_visible_context_components(agent, params, prompt)
        assert params.tool_ir_history == original
        materialized = _materialize_native_prompt_facts(ModelGenerateParams(
            agent=agent, params=params, prompt=prompt, tool_rounds=0,
        ))
        after = _model_visible_context_components(agent, params, materialized.prompt)
        assert before == after


def test_runtime_fact_delta_uses_latest_surviving_source_not_ever_seen() -> None:
    from copy import deepcopy

    from agent_py_agent.agent.agent_core.tool_ir_history import record_runtime_facts_turn_ir

    params = _params()
    assert record_runtime_facts_turn_ir(params, "A", source="clock")
    assert record_runtime_facts_turn_ir(params, "A", source="memory")
    assert not record_runtime_facts_turn_ir(params, "A", source="clock")
    checkpoint = deepcopy(params.tool_ir_history)
    assert record_runtime_facts_turn_ir(params, "B", source="clock")
    assert record_runtime_facts_turn_ir(params, "A", source="clock")
    assert [item.text for item in params.tool_ir_history if item.source == "clock"] == ["A", "B", "A"]
    params.tool_ir_history[:] = checkpoint
    assert record_runtime_facts_turn_ir(params, "B", source="clock")
    assert "_native_runtime_facts_seen" not in params.live_archive_state


def test_summary_retains_latest_fact_for_each_source() -> None:
    from agent_py_agent.agent.agent_core.tool_ir_history import drop_tool_call_pairs
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn

    params = _params()
    user = UserTurn("接着做原任务")
    unchanged = RuntimeFactsTurn("当前工作区", source="workspace")
    old = RuntimeFactsTurn("old", source="execution")
    latest = RuntimeFactsTurn("new", source="execution")
    call = canonical_history_call("read_file", {"path": "README.md"}, call_id="old-pair")
    result = canonical_history_result(call, "ok")
    params.tool_ir_history.extend([
        user, unchanged, old, AssistantTurn(tool_calls=[call]), result, latest,
    ])
    assert drop_tool_call_pairs(params, {"old-pair"}, drop_completed_tool_turns=True) == 1
    assert params.tool_ir_history == [user, unchanged, latest]


def test_text_protocol_receives_the_same_exact_compact_generation() -> None:
    params = replace(
        _params(protocol="text"),
        conversation_history_seed=ConversationHistorySeed(compact_generation=4),
    )

    injections = _runtime_injections_with_delivery_contract(params)

    assert any('"schema":"conversation_runtime_state.v1"' in row for row in injections)
    assert any('"compact_generation":4' in row for row in injections)


def test_completed_native_tool_turn_round_trips_through_conversation_metadata(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        _completed_turn_native_messages,
    )

    agent = _native_agent(tmp_path)
    params = _params()
    params.tool_ir_history.append(UserTurn("# User Task\n第一轮"))
    _record(
        agent,
        params,
        tool_rounds=1,
        idx=1,
        tool_name="read_file",
        call_id="call-persist-1",
        arguments={"path": "README.md"},
        output="文件内容",
    )
    completed = _completed_turn_native_messages(
        params,
        SimpleNamespace(text="第一轮完成", assistant_content_blocks=[]),
    )
    envelope = canonical_native_messages_envelope(completed)
    rows = [
        MessageLogEntry(
            message_id="m-user",
            thread_id="thread-1",
            role="user",
            content="第一轮",
            metadata={"conversation_request_id": "req-1"},
        ),
        MessageLogEntry(
            message_id="m-assistant",
            thread_id="thread-1",
            role="assistant",
            content="第一轮完成",
            metadata={
                "conversation_request_id": "req-1",
                CANONICAL_NATIVE_MESSAGES_METADATA_KEY: envelope,
            },
        ),
    ]

    replayed = provider_history_messages_from_rows(rows)
    assert list(replayed) == completed
    assert any("tool_use" in str(message) for message in replayed)
    assert any("tool_result" in str(message) for message in replayed)

    next_params = _params()
    next_params.provider_history_messages.extend(replayed)
    next_params.tool_ir_history.append(UserTurn("# User Task\n第二轮"))
    next_messages = _native_provider_messages(agent, next_params)
    assert next_messages is not None
    assert next_messages[: len(replayed)] == list(replayed)
    assert "第二轮" in str(next_messages[-1])


@pytest.mark.parametrize("error,end", [(InterruptedError("stop"), "aborted"), (ValueError("provider failure"), "error")])
def test_partial_native_turn_preserves_pairs_and_unknown_effects(tmp_path, error, end):
    from agent_py_agent.agent.agent_core.runtime.loop_support import _persist_partial_native_turn
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn

    agent, params = _native_agent(tmp_path), _params()
    params.tool_ir_history.append(UserTurn("# User Task\n原任务"))
    _record(agent, params, tool_rounds=1, idx=1, tool_name="read_file", call_id="read-done",
            arguments={"path": "README.md"}, output="不可丢的已读结果")
    pending = canonical_history_call("write_file", {"path": "unknown.txt"}, call_id="pending-write")
    params.tool_ir_history.append(AssistantTurn(tool_calls=[pending]))
    before = list(params.tool_ir_history)
    saved = []
    _persist_partial_native_turn(saved.append, params, error)
    assert len(saved) == 1 and saved[0].response == ""
    assert saved[0].turn_end_reason == end
    native = saved[0].canonical_native_messages
    results = {b["tool_use_id"]: b for m in native for b in m["content"] if b.get("type") == "tool_result"}
    assert "不可丢的已读结果" in results["read-done"]["content"]
    assert results["pending-write"]["is_error"] is True
    assert '"effect_outcome":"unknown"' in results["pending-write"]["content"]
    assert "压缩中回收" not in results["pending-write"]["content"]
    assert params.tool_ir_history == before


# --- compact prep: integer-pair drop interface (Step 3/4 contract) ------------


def test_drop_tool_call_pairs_removes_both_sides_no_orphans(tmp_path):
    from agent_py_agent.agent.agent_core.tool_ir_history import drop_tool_call_pairs

    agent = _native_agent(tmp_path)
    params = _params()
    _record(
        agent, params, tool_rounds=1, idx=1,
        tool_name="read_file", call_id="toolu_old", arguments={"path": "old"},
        output="OLD",
    )
    _record(
        agent, params, tool_rounds=2, idx=1,
        tool_name="read_file", call_id="toolu_new", arguments={"path": "new"},
        output="NEW",
    )

    removed = drop_tool_call_pairs(params, {"toolu_old"})

    assert removed == 1
    messages = _native_provider_messages(agent, params)
    ids = [
        b.get("id") or b.get("tool_use_id")
        for m in messages
        for b in m["content"]
        if b["type"] in ("tool_use", "tool_result")
    ]
    # no dangling reference to the dropped pair; the newer pair survives intact.
    assert "toolu_old" not in ids
    assert ids.count("toolu_new") == 2  # tool_use + tool_result both present


def test_drop_tool_call_pair_discards_invalidated_thinking_signature(tmp_path):
    from agent_py_agent.agent.agent_core.tool_ir_history import (
        drop_tool_call_pairs,
        open_assistant_turn_ir,
    )

    agent = _native_agent(tmp_path)
    params = _params()
    open_assistant_turn_ir(
        params,
        tool_rounds=1,
        response_text="读取旧文件",
        response_content_blocks=[
            {"type": "thinking", "thinking": "旧推理", "signature": "signed-old"},
            {"type": "text", "text": "读取旧文件"},
            {
                "type": "tool_use",
                "id": "toolu_old",
                "name": "read_file",
                "input": {"path": "old"},
            },
        ],
    )
    _record(
        agent,
        params,
        tool_rounds=1,
        idx=1,
        tool_name="read_file", call_id="toolu_old", arguments={"path": "old"},
        output="OLD",
    )

    drop_tool_call_pairs(params, {"toolu_old"})
    messages = _native_provider_messages(agent, params)

    assert messages == [
        {"role": "assistant", "content": [{"type": "text", "text": "读取旧文件"}]}
    ]
