from __future__ import annotations

"""LLM: regression tests for runtime context pressure triggers.

给人看的解释：
这些测试确认长任务里的工具上下文裁剪不会变成第二套隐形压缩；
保存型运行一旦裁剪旧工具记录，就会回到统一 compact/resume 链路。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_pressure import (
    preflight_context_pressure_response,
)
from agent_py_agent.agent.agent_core.tool_context.window import window_tool_context_params
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
