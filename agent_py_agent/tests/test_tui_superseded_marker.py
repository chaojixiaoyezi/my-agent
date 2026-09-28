"""TUI 子代理名册标出“已被 X 接替”（2026-09-28）。

背景：已结束子代理被显式接替后，权威状态保持 DONE、只记 superseded_by；父级模型视图已带 replaced_by，
但 TUI 名册仍只显示“已完成”，用户看不出旧结果已被取代。

锁定：
- Gateway 名册行只摊平 kernel 的唯一投影 replaced_by_view（replaced_by_run_id / replaced_by_disposition），
  没被接替的行不带这两个键，不从状态或正文推断。
- runtime 与视图模型两道 TUI 白名单都放行这两个标量。
- 渲染器在状态标签后标出“已被 <接替者 run_id 末段> 接替”，接替者本身不标；窄屏时标注随状态保留。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.conversation.agent_activity import _subagent_row
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, render_tui_snapshot
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


def _replaced_rows(tmp_path, status: str) -> tuple[dict[str, object], dict[str, object]]:
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
    source = manager.create_run(goal="旧子代理整理三条要点", thought="执行", plan=["整理"], role="worker")
    manager.lifecycle.set_status(source.id, status)
    agent = SimpleNamespace(
        config=SimpleNamespace(enable_subagents=True, max_subagents=10, access_mode="workspace-write"),
        subagents=manager,
        tools=SimpleNamespace(specs=lambda: []),
    )
    result = CreateSubagentsTool(agent).execute(
        {"goal": "接替旧子代理整理三条要点", "role": "worker", "replacement_for_run_ids": [source.id],
         "defer_start": True}
    )
    replacement_id = json.loads(result.output)["created_run_ids"][0]
    return _subagent_row(agent, manager.load(source.id)), _subagent_row(agent, manager.load(replacement_id))


def _agent_lines(rows: list[dict[str, object]], width: int = 120) -> list[str]:
    store = TuiStateStore()
    runtime = TuiRuntime("superseded-marker", store=store)
    runtime.update_background_activity(1, {"subagents": rows})
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=width))
    return [fragments_text(line) for line in frame.agent_lines]


@pytest.mark.parametrize("status,disposition", [("DONE", "superseded"), ("BLOCKED", "taken_over")])
def test_gateway_row_flattens_the_kernel_replaced_by_view(tmp_path, status, disposition):
    source_row, replacement_row = _replaced_rows(tmp_path, status)

    assert source_row["replaced_by_run_id"] == replacement_row["run_id"]
    assert source_row["replaced_by_disposition"] == disposition
    assert "replaced_by_run_id" not in replacement_row and "replaced_by_disposition" not in replacement_row


def test_tui_panel_marks_the_superseded_child_and_not_its_successor(tmp_path):
    source_row, replacement_row = _replaced_rows(tmp_path, "DONE")
    short_id = str(replacement_row["run_id"]).rsplit("-", 1)[-1]

    lines = _agent_lines([source_row, replacement_row])

    marked = [line for line in lines if f"已被 {short_id} 接替" in line]
    assert len(marked) == 1 and "已完成" in marked[0]
    # 接替者自己的职责描述里也有“接替”二字，这里只数“已被 … 接替”这一标注。
    assert sum("已被 " in line for line in lines) == 1


def test_marker_survives_a_narrow_panel_before_the_description(tmp_path):
    source_row, _replacement_row = _replaced_rows(tmp_path, "DONE")
    short_id = str(source_row["replaced_by_run_id"]).rsplit("-", 1)[-1]

    lines = _agent_lines([source_row], width=72)

    assert any(f"已被 {short_id} 接替" in line for line in lines)
