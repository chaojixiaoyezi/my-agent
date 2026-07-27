from __future__ import annotations

"""LLM: regression tests for runtime context pressure triggers.

给人看的解释：
这些测试确认长任务里的工具上下文裁剪不会变成第二套隐形压缩；
保存型运行一旦裁剪旧工具记录，就会回到统一 compact/resume 链路。
"""

from copy import deepcopy
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._tool_loop_service import (
    _compact_live_conversation_tool_context,
)
from agent_py_agent.agent.agent_core.model.context_pressure import (
    model_visible_context_tokens,
    preflight_context_pressure_response,
)
from agent_py_agent.agent.agent_core.tool_context.window import window_tool_context_params
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolCall, ToolResult
from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.agent.settings import AgentConfig


class _AgentStub:
    config = AgentConfig(auto_save_memory=True)
    backend = SimpleNamespace(context_window_tokens=128_000, name="fake")


def test_tool_context_window_requests_compact_for_saved_runs() -> None:
    params = SimpleNamespace(
        tool_context=[f"entry-{idx}-" + ("x" * 100_000) for idx in range(6)],
        archive_tool_calls=[],
        live_archive_state={},
        save=True,
    )

    window_tool_context_params(_AgentStub(), params)

    overflow = params.live_archive_state["tool_context_window_overflow"]
    assert overflow["omitted_count"] > 0
    assert overflow["original_chars"] > 512_000
    assert params.tool_context[0].startswith("[tool-context-window]")


def test_tool_context_window_scales_with_model_compact_threshold() -> None:
    params = SimpleNamespace(
        tool_context=[f"entry-{idx}-" + ("x" * 35_000) for idx in range(5)],
        archive_tool_calls=[],
        live_archive_state={},
        save=True,
    )

    window_tool_context_params(_AgentStub(), params)

    assert "tool_context_window_overflow" not in params.live_archive_state
    assert not params.tool_context[0].startswith("[tool-context-window]")


def test_tool_context_window_does_not_request_compact_for_unsaved_runs() -> None:
    params = SimpleNamespace(
        tool_context=[f"entry-{idx}-" + ("x" * 100_000) for idx in range(6)],
        archive_tool_calls=[],
        live_archive_state={},
        save=False,
    )

    window_tool_context_params(_AgentStub(), params)

    assert "tool_context_window_overflow" not in params.live_archive_state
    assert params.tool_context[0].startswith("[tool-context-window]")


def test_preflight_context_pressure_uses_tool_context_window_signal() -> None:
    params = SimpleNamespace(
        context_scope="default",
        live_archive_state={
            "tool_context_window_overflow": {
                "omitted_count": 12,
                "original_chars": 80_000,
                "preserved_count": 5,
            }
        },
    )
    request = SimpleNamespace(agent=_AgentStub(), params=params, prompt="短 prompt", tool_rounds=9)

    response = preflight_context_pressure_response(request)

    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert response.runtime_source == "preflight"
    assert response.text.startswith("[RUN_CONTEXT_PRESSURE]")
    assert project_user_reply(response.text).internal_signal is True
    assert project_user_reply(response.text).content == ""
    assert "tool_context_window_overflow=true" in response.text
    assert "tool_context_window_overflow" not in params.live_archive_state


def test_preflight_uses_configured_threshold_without_a_second_ceiling(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(auto_save_memory=True, memory_compact_auto_trigger_percent=70),
        backend=SimpleNamespace(context_window_tokens=1000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="default",
        live_archive_state={},
    )
    request = SimpleNamespace(agent=agent, params=params, prompt="系统上下文", tool_rounds=3)

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _prompt: 699,
    )
    assert preflight_context_pressure_response(request) is None

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _prompt: 700,
    )
    response = preflight_context_pressure_response(request)

    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert "compact_threshold=700" in response.text


def test_preflight_native_counts_tool_schemas_before_first_tool_call(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            enable_tools=True,
            tool_protocol="native",
            model_name="native-test-model",
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=1_000,
        ),
        backend=SimpleNamespace(context_window_tokens=1_000, name="anthropic_compatible"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        live_archive_state={},
        tool_context=[],
        tool_ir_history=[],
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.resolve_native_tools",
        lambda _agent, _params: [
            {
                "name": "large_native_tool",
                "description": "schema-" + ("x" * 8_000),
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            }
        ],
    )
    request = SimpleNamespace(
        agent=agent,
        params=params,
        prompt="短 prompt",
        tool_rounds=0,
    )

    visible = model_visible_context_tokens(agent, params, request.prompt)
    response = preflight_context_pressure_response(request)

    assert visible >= 900
    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert response.runtime_source == "preflight"
    assert f"model_visible_tokens={visible}" in response.text


def test_live_conversation_tool_context_compacts_once_below_exact_threshold(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=2_000,
            memory_compact_semantic_summary_enabled=False,
        ),
        backend=SimpleNamespace(context_window_tokens=2_000, name="fake"),
    )
    records = [
        {
            "run_id": "run-1",
            "scoped_call_id": f"run-1:{index}",
            "tool": "read_file",
            "parameters": {"path": f"source-{index}.txt"},
            "output_preview": f"fact-{index}-" + ("y" * 100),
            "ok": True,
        }
        for index in range(14)
    ]
    original_records = deepcopy(records)
    params = SimpleNamespace(
        context_scope="conversation",
        tool_context=[f"[tool-record {index}]\n" + ("x" * 900) for index in range(14)],
        archive_tool_calls=records,
        active_turn_user_inputs=[
            {
                "schema_version": "active-turn-user-input.v1",
                "input_ids": ["btw-1"],
                "text": "补充：保留这个当前请求。",
            }
        ],
        live_archive_state={},
    )

    def render(_agent, current):
        return ("base\n" + ("b" * 300) + "\n") + "\n".join(current.tool_context)

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core._tool_loop_service._render_tool_loop_prompt",
        render,
    )
    prompt = render(agent, params)
    assert estimate_tokens(prompt) >= 1_800

    compacted = _compact_live_conversation_tool_context(agent, params, prompt)

    stats = params.live_archive_state["conversation_tool_context_compaction"]
    assert estimate_tokens(compacted) < 1_800
    assert stats["threshold_tokens"] == 1_800
    assert stats["event_count"] == 1
    assert stats["below_threshold"] is True
    assert stats["material_reduction"] is True
    assert any("补充：保留这个当前请求。" in item for item in params.tool_context)
    assert records == original_records
    first_context = list(params.tool_context)
    first_stats = dict(stats)

    second = _compact_live_conversation_tool_context(agent, params, compacted)

    assert second == compacted
    assert params.tool_context == first_context
    assert params.live_archive_state["conversation_tool_context_compaction"] == first_stats

    params.tool_context.extend(
        f"[new-tool-record {index}]\n" + ("z" * 900) for index in range(14)
    )
    third_prompt = render(agent, params)
    assert estimate_tokens(third_prompt) >= 1_800
    _compact_live_conversation_tool_context(agent, params, third_prompt)

    repeated_stats = params.live_archive_state["conversation_tool_context_compaction"]
    assert repeated_stats["event_count"] == 2
    assert repeated_stats["peak_before_tokens"] >= repeated_stats["before_tokens"]
    assert repeated_stats["total_reclaimed_tokens"] > first_stats["total_reclaimed_tokens"]
    assert repeated_stats["all_below_threshold"] is True


def test_live_conversation_compact_bounds_no_progress_summary(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=1_200,
        ),
        backend=SimpleNamespace(context_window_tokens=1_200, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        tool_context=["old-" + ("x" * 1_000) for _index in range(8)],
        archive_tool_calls=[{"tool": "read_file", "scoped_call_id": "call-1"}],
        active_turn_user_inputs=[],
        live_archive_state={},
    )

    def render(_agent, current):
        return ("base\n" + ("b" * 300) + "\n") + "\n".join(current.tool_context)

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core._tool_loop_service._render_tool_loop_prompt",
        render,
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core._tool_loop_service._semantic_tool_context_replacement",
        lambda _agent, _params, _original: [
            "[compact-semantic-summary]\n" + ("s" * 5_000),
            *["mechanical-" + ("m" * 800) for _index in range(8)],
        ],
    )
    prompt = render(agent, params)

    compacted = _compact_live_conversation_tool_context(agent, params, prompt)

    assert estimate_tokens(compacted) < 1_080
    assert params.live_archive_state["conversation_tool_context_compaction"]["below_threshold"] is True
    assert params.tool_context[0].startswith("[tool-context-window]")


def test_live_conversation_compacts_native_tool_pairs_against_whole_request(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            enable_tools=True,
            tool_protocol="native",
            model_name="native-test-model",
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=2_000,
            memory_compact_semantic_summary_enabled=False,
        ),
        backend=SimpleNamespace(context_window_tokens=2_000, name="anthropic_compatible"),
    )
    records = [
        {
            "run_id": "run-native",
            "scoped_call_id": f"native-{index}",
            "tool": "read_file",
            "parameters": {"path": f"source-{index}.txt"},
            "output_preview": f"native-fact-{index}",
            "ok": True,
        }
        for index in range(10)
    ]
    original_records = deepcopy(records)
    history = []
    for index in range(10):
        call_id = f"call-{index}"
        history.extend(
            [
                AssistantTurn(
                    text=f"round-{index}",
                    tool_calls=[ToolCall(call_id, "read_file", {"path": f"source-{index}.txt"})],
                ),
                ToolResult(call_id, "z" * 900),
            ]
        )
    original_history_len = len(history)
    params = SimpleNamespace(
        context_scope="conversation",
        tool_context=[f"[tool-record {index}]\n" + ("x" * 300) for index in range(10)],
        archive_tool_calls=records,
        active_turn_user_inputs=[],
        live_archive_state={},
        tool_ir_history=history,
    )

    def render(_agent, _current):
        return "native-base\n" + ("b" * 300)

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core._tool_loop_service._render_tool_loop_prompt",
        render,
    )
    prompt = render(agent, params)

    compacted = _compact_live_conversation_tool_context(agent, params, prompt)

    stats = params.live_archive_state["conversation_tool_context_compaction"]
    assert stats["before_tokens"] >= 1_800
    assert stats["after_tokens"] < 1_800
    assert stats["below_threshold"] is True
    assert len(params.tool_ir_history) < original_history_len
    assert params.tool_context[0].startswith("[tool-context-window]")
    assert any("native-9" in item for item in params.tool_context)
    assert records == original_records
    messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
    tool_use_ids = {
        block["id"]
        for message in messages
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    tool_result_ids = {
        block["tool_use_id"]
        for message in messages
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    assert tool_use_ids == tool_result_ids
    assert compacted == prompt
