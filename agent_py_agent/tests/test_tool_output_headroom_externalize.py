"""余量不足时立刻外置工具输出并登记溢出：同一轮多条大结果不再把上下文冲过窗口（用户第 5 项"单回合超窗"）。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_call_archive_record as archive_module
from agent_py_agent.agent.agent_core.model.context_pressure import (
    preflight_context_pressure_response,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

BIG_OUTPUT = "这是一段很长的报告正文，用来把上下文余量吃光。" * 600


def _agent(*, window: int, **overrides) -> SimpleNamespace:
    config = AgentConfig(
        auto_save_memory=True, enable_tools=True, tool_protocol="native", model_name="native-test-model",
        memory_compact_auto_trigger_percent=90, model_context_window_tokens=window,
        tool_output_externalize_min_chars=200_000, tool_output_preview_chars=200, **overrides,
    )
    return SimpleNamespace(config=config, backend=SimpleNamespace(context_window_tokens=window, name="anthropic_compatible"))


def _params() -> SimpleNamespace:
    return SimpleNamespace(
        context_scope="conversation", tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        live_archive_state={}, tool_context=[], tool_ir_history=[], request_id="req-1", task_id="task-1",
        tool_runtime_snapshot=None,
    )


def _call(call_id: str = "call-1") -> ToolCall:
    return ToolCall(
        call_id=call_id, tool_name="read_file", arguments={"path": "report.txt", "offset": 0}, source_protocol="native",
        schema_hash="sha256:1", run_id="run-1", attempt_id="attempt-1", turn_id="turn-1",
    )


def _page(output: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(tool="read_file", ok=True, output=output)


@pytest.fixture
def archive_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(archive_module, "_tool_output_archive_root", lambda *_args: tmp_path)
    monkeypatch.setattr("agent_py_agent.agent.agent_core.model.context_pressure.resolve_native_tools", lambda _a, _p: [])
    return tmp_path


def test_output_beyond_headroom_is_externalized_and_forces_compaction_at_next_preflight(archive_root):
    agent, params = _agent(window=1_000), _params()
    projection = archive_module.archive_tool_output_projection(agent, params, _call(), _page(BIG_OUTPUT))
    record = projection.metadata["archive_output_record"]
    assert record["output_externalized"] is True, "余量不足：read_file 分页也落 artifact，模型只看预览"
    assert record["output_externalized_reason"] == "tool_result_headroom"
    body = next(block.text for block in projection.content_blocks if block.type == "text")
    assert len(body) < len(BIG_OUTPUT) // 10
    overflow = params.live_archive_state["tool_context_window_overflow"]
    assert overflow["reason"] == "tool_result_headroom" and overflow["omitted_count"] == 1
    assert overflow["original_chars"] == len(BIG_OUTPUT) and overflow["estimated_tokens"] >= overflow["remaining_to_compact_tokens"]

    response = preflight_context_pressure_response(SimpleNamespace(agent=agent, params=params, prompt="继续", tool_rounds=1))
    assert response is not None and response.runtime_status == "context_overflow"
    assert "tool_context_window_overflow=true" in response.text and "omitted_count=1" in response.text
    assert "tool_context_window_overflow" not in params.live_archive_state, "预检单次消费溢出登记"


def test_second_oversized_result_in_the_same_round_accumulates(archive_root):
    agent, params = _agent(window=1_000), _params()
    for index in range(2):
        archive_module.archive_tool_output_projection(agent, params, _call(f"call-{index}"), _page(BIG_OUTPUT))
    overflow = params.live_archive_state["tool_context_window_overflow"]
    assert overflow["omitted_count"] == 2 and overflow["original_chars"] == 2 * len(BIG_OUTPUT)


def test_page_that_fits_the_headroom_stays_inline_without_overflow_mark(archive_root):
    agent, params = _agent(window=400_000), _params()
    projection = archive_module.archive_tool_output_projection(agent, params, _call(), _page(BIG_OUTPUT))
    record = projection.metadata["archive_output_record"]
    assert record["output_externalized"] is False and "output_externalized_reason" not in record
    assert "tool_context_window_overflow" not in params.live_archive_state
    small_params = _params()
    small = archive_module.archive_tool_output_projection(_agent(window=1_000), small_params, _call(), _page("ok"))
    assert small.metadata["archive_output_record"]["output_externalized"] is False, "不长于预览的输出不算余量"
    assert "tool_context_window_overflow" not in small_params.live_archive_state


def test_switch_off_keeps_the_configured_threshold_only(archive_root):
    agent, params = _agent(window=1_000, tool_output_externalize_on_low_headroom=False), _params()
    projection = archive_module.archive_tool_output_projection(agent, params, _call(), _page(BIG_OUTPUT))
    assert projection.metadata["archive_output_record"]["output_externalized"] is False
    assert "tool_context_window_overflow" not in params.live_archive_state


def test_unknown_budget_fails_open_and_full_externalize_config_skips_the_check():
    bare_agent = SimpleNamespace(config=SimpleNamespace())
    assert archive_module._headroom_forces_externalize(bare_agent, SimpleNamespace(), BIG_OUTPUT) is False
    assert archive_module._headroom_forces_externalize(bare_agent, SimpleNamespace(), "") is False
    zero = SimpleNamespace(config=SimpleNamespace(tool_output_externalize_min_chars=0))
    assert archive_module._headroom_forces_externalize(zero, SimpleNamespace(), BIG_OUTPUT) is False, "全外置配置不需要余量判断"
