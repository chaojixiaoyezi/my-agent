"""Regression tests for NW3 W5: per-second clock split & workspace context deduplication."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import _render_tool_loop_prompt
from agent_py_agent.agent.agent_core.native_tool_protocol import select_tool_protocol
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.tool_ir_history import (
    RuntimeFactsTurn,
    project_native_prompt_history,
    summarized_runtime_fact_indexes,
)
from agent_py_agent.agent.common import agent_time
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuilder,
    PromptBuildRequest,
    PromptRenderInput,
    ToolSections,
    render_prepared_prompt,
)
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult


def _la_builder(tmp_path) -> PromptBuilder:
    return PromptBuilder(
        AgentConfig(prompt_files=[], timezone="America/Los_Angeles", week_start="monday"),
        tmp_path,
    )


def test_same_day_workspace_snapshot_byte_stable_while_clock_splits(tmp_path, monkeypatch) -> None:
    builder = _la_builder(tmp_path)
    t1 = datetime(2026, 11, 15, 10, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
    t2 = t1 + timedelta(seconds=5)
    current = [t1]
    monkeypatch.setattr(agent_time, "now", lambda _tz="": current[0])

    ws_t1 = builder.snapshot_workspace_context()
    clk_t1 = builder.snapshot_clock_context()
    prompt_t1 = builder.build(
        request=PromptBuildRequest(
            "第一轮问题",
            [],
            tools=ToolSections(native_tool_use=True, tool_catalog_section="# Tools\n- echo"),
        )
    )

    current[0] = t2
    ws_t2 = builder.snapshot_workspace_context()
    clk_t2 = builder.snapshot_clock_context()
    prompt_t2 = builder.build(
        request=PromptBuildRequest(
            "第二轮问题",
            [],
            tools=ToolSections(native_tool_use=True, tool_catalog_section="# Tools\n- echo"),
        )
    )

    assert ws_t1 == ws_t2
    assert "current_local_date: 2026-11-15" in ws_t1
    assert "current_local_year: 2026" in ws_t1
    assert "current_week_range:" in ws_t1
    assert "last_7_days_range:" in ws_t1
    assert "current_local_time:" not in ws_t1

    assert clk_t1 == "- current_local_time: 2026-11-15T10:00:01-08:00 (Sunday)"
    assert clk_t2 == "- current_local_time: 2026-11-15T10:00:06-08:00 (Sunday)"

    layout_1 = prompt_cache_layout(prompt_t1)
    layout_2 = prompt_cache_layout(prompt_t2)
    assert layout_1 is not None and layout_2 is not None
    assert layout_1.stable_prefix == layout_2.stable_prefix

    sources_1 = [source for source, _ in layout_1.volatile_sections]
    assert "prompt.workspace" in sources_1
    assert "prompt.clock" in sources_1
    assert sources_1.index("prompt.clock") == sources_1.index("prompt.workspace") + 1

    sections_1 = dict(layout_1.volatile_sections)
    sections_2 = dict(layout_2.volatile_sections)
    assert sections_1["prompt.workspace"] == sections_2["prompt.workspace"]
    assert sections_1["prompt.clock"] == f"# Current Time\n{clk_t1}"
    assert sections_2["prompt.clock"] == f"# Current Time\n{clk_t2}"


def test_non_native_render_places_current_time_right_after_workspace_context(tmp_path, monkeypatch) -> None:
    builder = _la_builder(tmp_path)
    t1 = datetime(2026, 11, 15, 10, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(agent_time, "now", lambda _tz="": t1)

    rendered = builder.build("查看时间", [])
    assert "# Workspace Context\n" in rendered
    assert "# Current Time\n- current_local_time: 2026-11-15T10:00:01-08:00 (Sunday)\n\n# Runtime Injection" in rendered
    assert rendered.index("# Workspace Context") < rendered.index("# Current Time") < rendered.index("# Runtime Injection")


def test_facts_only_clock_snapshot_returns_empty_and_omits_prompt_clock(tmp_path, monkeypatch) -> None:
    builder = _la_builder(tmp_path)
    t1 = datetime(2026, 11, 15, 10, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(agent_time, "now", lambda _tz="": t1)

    ws_facts = builder.snapshot_workspace_context(facts_only=True)
    clk_facts = builder.snapshot_clock_context(facts_only=True)
    assert clk_facts == ""
    assert "current_local_date: 2026-11-15" in ws_facts
    assert "current_local_time:" not in ws_facts

    rendered = builder.build(
        request=PromptBuildRequest(
            "facts check",
            [],
            workspace_context_override=ws_facts,
            clock_context_override=clk_facts,
            tools=ToolSections(native_tool_use=True, tool_catalog_section="# Tools\n- echo"),
        )
    )
    layout = prompt_cache_layout(rendered)
    assert layout is not None
    sources = [source for source, _ in layout.volatile_sections]
    assert "prompt.workspace" in sources
    assert "prompt.clock" not in sources

    non_native = render_prepared_prompt(
        PromptRenderInput(
            system_prompt="sys",
            memory_text="",
            owner_scope="scope",
            dynamic="",
            workspace_context=ws_facts,
            injection_fragments=(),
            tool_catalog="# Tools",
            recommendations="# Recommended Tools",
            execution_facts="",
            user_prompt="q",
            task_and_transcript="",
            native_tool_use=False,
            clock_context="",
        )
    )
    assert "# Current Time" not in non_native


def test_legacy_workspace_current_local_time_suppresses_prompt_clock(tmp_path) -> None:
    builder = _la_builder(tmp_path)
    legacy_ws = "- 当前工具工作目录（仅供执行定位）: /legacy\n- current_local_time: frozen"
    rendered = builder.build(
        request=PromptBuildRequest(
            "legacy compat",
            [],
            workspace_context_override=legacy_ws,
            clock_context_override="- current_local_time: 2026-11-15T10:00:01-08:00 (Sunday)",
            tools=ToolSections(native_tool_use=True, tool_catalog_section="# Tools\n- echo"),
        )
    )
    layout = prompt_cache_layout(rendered)
    assert layout is not None
    sources = [source for source, _ in layout.volatile_sections]
    assert "prompt.clock" not in sources
    assert dict(layout.volatile_sections)["prompt.workspace"].endswith("- current_local_time: frozen")

    non_native = builder.build(
        request=PromptBuildRequest(
            "legacy compat text",
            [],
            workspace_context_override=legacy_ws,
            clock_context_override="- current_local_time: 2026-11-15T10:00:01-08:00 (Sunday)",
            tools=ToolSections(native_tool_use=False),
        )
    )
    assert "# Current Time" not in non_native


def test_tool_ir_history_dedups_prompt_workspace_and_supersedes_prompt_clock(tmp_path, monkeypatch) -> None:
    builder = _la_builder(tmp_path)
    t1 = datetime(2026, 11, 15, 10, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
    t2 = t1 + timedelta(seconds=5)
    current = [t1]
    monkeypatch.setattr(agent_time, "now", lambda _tz="": current[0])

    prompt_r1 = builder.build(
        request=PromptBuildRequest(
            "turn 1",
            [],
            tools=ToolSections(native_tool_use=True, tool_catalog_section="# Tools\n- echo"),
        )
    )
    ir_params = SimpleNamespace(tool_ir_history=[], compact_degradation_stubs=None)
    _, history = project_native_prompt_history(ir_params, prompt_r1)
    ir_params.tool_ir_history = history
    # Same-turn repeat must append zero new workspace or clock turns.
    _, history = project_native_prompt_history(ir_params, prompt_r1)
    ir_params.tool_ir_history = history

    ws_turns = [item for item in history if isinstance(item, RuntimeFactsTurn) and item.source == "prompt.workspace"]
    clk_turns = [item for item in history if isinstance(item, RuntimeFactsTurn) and item.source == "prompt.clock"]
    assert len(ws_turns) == 1
    assert len(clk_turns) == 1

    # Turn 2 advances by 5 seconds on the same calendar date: only prompt.clock appends.
    current[0] = t2
    prompt_r2 = builder.build(
        request=PromptBuildRequest(
            "turn 2",
            [],
            tools=ToolSections(native_tool_use=True, tool_catalog_section="# Tools\n- echo"),
        )
    )
    _, history = project_native_prompt_history(ir_params, prompt_r2)

    ws_turns_after = [item for item in history if isinstance(item, RuntimeFactsTurn) and item.source == "prompt.workspace"]
    clk_turns_after = [item for item in history if isinstance(item, RuntimeFactsTurn) and item.source == "prompt.clock"]
    assert len(ws_turns_after) == 1
    assert len(clk_turns_after) == 2
    assert "2026-11-15T10:00:01-08:00" in clk_turns_after[0].text
    assert "2026-11-15T10:00:06-08:00" in clk_turns_after[1].text

    tool_result = ToolResult(call_id="c1", tool_name="echo", status="succeeded")
    history_for_compact = [*history[:-1], tool_result, history[-1]]
    tool_idx = history_for_compact.index(tool_result)
    first_clk_idx = next(
        idx for idx, item in enumerate(history_for_compact)
        if isinstance(item, RuntimeFactsTurn) and item.source == "prompt.clock"
    )
    last_clk_idx = max(
        idx for idx, item in enumerate(history_for_compact)
        if isinstance(item, RuntimeFactsTurn) and item.source == "prompt.clock"
    )
    ws_idx = next(
        idx for idx, item in enumerate(history_for_compact)
        if isinstance(item, RuntimeFactsTurn) and item.source == "prompt.workspace"
    )
    retired = summarized_runtime_fact_indexes(history_for_compact, {tool_idx})
    assert first_clk_idx in retired
    assert last_clk_idx not in retired
    assert ws_idx not in retired


def test_loop_support_and_tool_loop_freeze_clock_snapshot_across_rounds(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(
            prompt_files=[],
            timezone="America/Los_Angeles",
            enable_tools=False,
            memory_path="memory.jsonl",
        ),
        tmp_path,
    )
    t1 = datetime(2026, 11, 15, 10, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
    t2 = t1 + timedelta(seconds=15)
    current = [t1]
    monkeypatch.setattr(agent_time, "now", lambda _tz="": current[0])

    normal_params = SimpleNamespace(task_attributes={})
    ws_snapshot = loop_support._workspace_context_snapshot(agent, normal_params)
    clk_snapshot = loop_support._clock_context_snapshot(agent, normal_params)
    assert clk_snapshot == "- current_local_time: 2026-11-15T10:00:01-08:00 (Sunday)"

    audit_attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: "audit-1",
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: "src-1",
        AUDIT_SOURCE_WATCH_ID_ATTR: "watch-1",
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key("audit-1", "watch-1"),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(tmp_path),
    }
    assert loop_support._clock_context_snapshot(agent, SimpleNamespace(task_attributes=audit_attrs)) == ""

    execute_params = ToolLoopExecuteParams(
        user_prompt="check frozen clock",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="# Tools\n- echo",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary={"allowed_write_roots": [str(tmp_path)]},
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=select_tool_protocol(agent, run_id="run-1"),
        workspace_context_snapshot=ws_snapshot,
        clock_context_snapshot=clk_snapshot,
    )
    rendered_round_1 = _render_tool_loop_prompt(agent, execute_params)
    current[0] = t2
    rendered_round_2 = _render_tool_loop_prompt(agent, execute_params)

    assert rendered_round_1 == rendered_round_2
    layout = prompt_cache_layout(rendered_round_1)
    assert layout is not None
    assert dict(layout.volatile_sections)["prompt.clock"] == f"# Current Time\n{clk_snapshot}"

    # Fake prompt holder lacking snapshot_clock_context degrades cleanly to None.
    fake_prompts_no_clock = SimpleNamespace(snapshot_workspace_context=MagicMock(return_value=ws_snapshot))
    assert loop_support._clock_context_snapshot(SimpleNamespace(prompts=fake_prompts_no_clock), normal_params) is None


def test_workspace_context_drops_sixth_task_progress_bullet(tmp_path) -> None:
    for enabled in (True, False):
        builder = PromptBuilder(
            AgentConfig(prompt_files=[], task_progress_closeout_guidance_enabled=enabled),
            tmp_path,
        )
        ws = builder.snapshot_workspace_context()
        assert "task_progress 是软清单" not in ws
        assert "task_progress 是模型可选的当前运行清单" not in ws
        assert "不能把写报告文件列入 task_progress" in ws
        assert replace(PromptBuildRequest("u", [], tools=ToolSections())).user_prompt == "u"
