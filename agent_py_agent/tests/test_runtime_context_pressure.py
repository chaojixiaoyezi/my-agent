from __future__ import annotations

"""LLM: regression tests for runtime context pressure triggers.

给人看的解释：
这些测试确认长任务里的工具上下文裁剪不会变成第二套隐形压缩；
保存型运行一旦裁剪旧工具记录，就会回到统一 compact/resume 链路。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_pressure import (
    model_visible_context_budget,
    model_visible_context_snapshot,
    model_visible_context_tokens,
    preflight_context_pressure_response,
    safe_inline_tool_result_tokens,
)
from agent_py_agent.agent.agent_core.tool_context.window import window_tool_context_params
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


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


def test_authoritative_conversation_windows_without_requesting_second_compact() -> None:
    params = SimpleNamespace(
        context_scope="conversation",
        task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        tool_context=[f"entry-{idx}-" + ("x" * 100_000) for idx in range(6)],
        archive_tool_calls=[],
        live_archive_state={},
        save=True,
    )

    window_tool_context_params(_AgentStub(), params)

    assert params.tool_context[0].startswith("[tool-context-window]")
    assert "tool_context_window_overflow" not in params.live_archive_state


def test_preflight_context_pressure_uses_tool_context_window_signal() -> None:
    params = SimpleNamespace(
        context_scope="default",
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
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
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
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
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
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


def test_live_context_snapshot_reuses_exact_total_and_exposes_no_content(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            enable_tools=True,
            tool_protocol="native",
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=128_000,
        ),
        backend=SimpleNamespace(context_window_tokens=128_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        save=True,
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        live_archive_state={},
        tool_context=["private-guidance"],
        tool_ir_history=[],
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.resolve_native_tools",
        lambda _agent, _params: [
            {
                "name": "secret_tool_name",
                "description": "secret schema content",
                "input_schema": {"type": "object"},
            }
        ],
    )

    snapshot = model_visible_context_snapshot(agent, params, "private prompt")
    public = snapshot.to_public_dict()

    assert snapshot.current_tokens == model_visible_context_tokens(
        agent,
        params,
        "private prompt",
    )
    assert snapshot.context_window_tokens == 128_000
    assert snapshot.compact_trigger_tokens == 115_200
    assert sum(
        (
            snapshot.prompt_tokens,
            snapshot.messages_tokens,
            snapshot.runtime_guidance_tokens,
            snapshot.tool_schema_tokens,
        )
    ) == snapshot.current_tokens
    assert set(public) == {
        "schema",
        "estimated",
        "context_window_tokens",
        "compact_trigger_tokens",
        "current_tokens",
        "prompt_tokens",
        "messages_tokens",
        "runtime_guidance_tokens",
        "tool_schema_tokens",
        "protocol",
    }
    assert "private" not in repr(public)
    assert "secret" not in repr(public)


def test_unsaved_model_call_keeps_configured_trigger_without_enabling_compact(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=128_000,
        ),
        backend=SimpleNamespace(context_window_tokens=128_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        save=False,
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={},
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: 120_000,
    )

    snapshot = model_visible_context_snapshot(agent, params, "presentation prompt")
    response = preflight_context_pressure_response(
        SimpleNamespace(agent=agent, params=params, prompt="presentation prompt")
    )

    assert snapshot.context_window_tokens == 128_000
    assert snapshot.compact_trigger_tokens == 115_200
    assert snapshot.current_tokens == 120_000
    assert response is None


def test_inline_tool_result_budget_reuses_current_compact_headroom(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=100_000,
        ),
        backend=SimpleNamespace(context_window_tokens=100_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="default",
        tool_context=[],
        tool_ir_history=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
    )
    agent._current_run_params = params
    agent._current_user_prompt = "当前会话"

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: 60_000,
    )
    budget = model_visible_context_budget(agent)

    assert budget.context_window_tokens == 100_000
    assert budget.compact_trigger_tokens == 90_000
    assert budget.current_tokens == 60_000
    assert budget.remaining_to_compact_tokens == 30_000
    assert safe_inline_tool_result_tokens(agent) == 30_000

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: 89_000,
    )
    assert safe_inline_tool_result_tokens(agent) == 1_000
