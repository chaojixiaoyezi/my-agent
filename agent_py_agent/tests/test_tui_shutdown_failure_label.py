"""子代理被宿主停机打断时，TUI 与 IM 显示“宿主停机中断”，不再笼统显示“失败”（ae step17d 冒烟发现，2026-10-02）。

背景：子代理被宿主停机打断后结构化状态是对的（failure_type=host_shutdown_interrupted 或 model_call_admission_closed），
但 TUI 子代理状态条只按 status 映射，FAILED 一律显示“× … · 失败”；终态活动文字也只按 status（“执行失败”）；
IM 的 /status 只给“异常 N”。

锁定：
- 唯一权威是 subagents/runner_display_projection：runner_failure_label 给失败类型专属标签（没有时为空），
  runner_display_label 复用它。客户端不另写失败类型映射。
- Gateway 名册行带出标量 failure_label；runtime 与导航两道 TUI 白名单放行；名册行徽标、子代理页头部、终态活动文字都用它，
  没有专属标签时照旧（其它失败仍是“失败”）；渲染缓存键随它变化。
- IM /status 的“异常”按同一权威标签细分；没有异常时原文逐字不变；TUI 走 Gateway 时有界解析细分。
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.agent_activity import (
    _subagent_current_activity,
    _subagent_row,
)
from agent_py_agent.agent.conversation.control_commands import (
    ConversationTaskStatus,
    render_conversation_task_status,
)
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.runner_display_projection import (
    runner_display_label,
    runner_failure_label,
)
from agent_py_agent.cli.chat_parts.control_runtime import _conversation_task_status_from_body
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    render_tui_snapshot,
    tui_render_context_key,
)
from agent_py_agent.cli.chat_parts.tui_interaction import TuiInteractionState
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
from agent_py_agent.cli.chat_parts.tui_ui_setup import _make_render_context_factory
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore

SHUTDOWN_TYPES = ["host_shutdown_interrupted", "model_call_admission_closed"]


def _manager_agent(tmp_path):
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
    agent = SimpleNamespace(
        config=SimpleNamespace(enable_subagents=True, max_subagents=10, access_mode="workspace-write"),
        subagents=manager,
        tools=SimpleNamespace(specs=lambda: []),
    )
    return manager, agent


# 函数用途: 用真实子代理管理器造一个已结束的子代理，返回 Gateway 名册行与权威任务对象。
def _ended_row(tmp_path, status: str, failure_type: str):
    manager, agent = _manager_agent(tmp_path)
    task = manager.create_run(goal="整理三条要点", thought="执行", plan=["整理"], role="worker")

    def settle(current) -> None:
        current.status = status
        current.failure_type = failure_type

    manager.mutate(task.id, settle)
    loaded = manager.load(task.id)
    return _subagent_row(agent, loaded), loaded


def _panel_lines(rows: list[dict[str, object]]) -> list[str]:
    store = TuiStateStore()
    runtime = TuiRuntime("shutdown-label", store=store)
    runtime.update_background_activity(1, {"subagents": rows})
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=120))
    return [fragments_text(line) for line in frame.agent_lines]


# ---------------------------------------------------------------- 唯一权威


@pytest.mark.parametrize("failure_type", SHUTDOWN_TYPES)
def test_authority_names_both_shutdown_failures(failure_type):
    assert runner_failure_label("FAILED", failure_type) == "宿主停机中断"
    assert runner_display_label("FAILED", failure_type) == "宿主停机中断"


def test_authority_keeps_other_failures_and_running_plain():
    assert runner_failure_label("FAILED", "provider_quota_exhausted") == "额度不足"
    assert runner_failure_label("FAILED", "runner_error") == ""
    assert runner_display_label("FAILED", "runner_error") == "失败"
    assert runner_failure_label("RUNNING", "host_shutdown_interrupted") == ""


# ---------------------------------------------------------------- Gateway 名册行与 TUI


@pytest.mark.parametrize("failure_type", SHUTDOWN_TYPES)
def test_shutdown_failures_show_the_shutdown_label_in_the_roster(tmp_path, failure_type):
    row, task = _ended_row(tmp_path, "FAILED", failure_type)

    lines = _panel_lines([row])

    assert row["failure_label"] == "宿主停机中断"
    assert any("宿主停机中断" in line for line in lines)
    assert not any("· 失败" in line or " 失败" in line for line in lines)
    assert _subagent_current_activity(task) == "宿主停机中断"


@pytest.mark.parametrize(
    "case",
    [
        ("FAILED", "runner_error", "失败", "执行失败"),
        ("TIMEOUT", "", "失败", "已超时"),
        ("FAILED", "provider_quota_exhausted", "额度不足", "额度不足"),
    ],
    ids=["other-failure", "timeout", "quota"],
)
def test_other_failures_stay_as_before_and_quota_says_so(tmp_path, case):
    status, failure_type, label, activity = case
    row, task = _ended_row(tmp_path, status, failure_type)

    lines = _panel_lines([row])

    assert row["failure_label"] == ("" if label == "失败" else label)
    assert any(label in line for line in lines)
    assert _subagent_current_activity(task) == activity


def test_queued_rerun_does_not_reuse_a_stale_failure_label(tmp_path):
    row, task = _ended_row(tmp_path, "PENDING", "host_shutdown_interrupted")

    assert _subagent_current_activity(task) != "宿主停机中断"
    assert not any("宿主停机中断" in line for line in _panel_lines([row]))


# 函数用途: 进入一个被停机打断的子代理页，返回导航状态与渲染上下文工厂。
def _entered_child(tmp_path):
    row, _task = _ended_row(tmp_path, "FAILED", "host_shutdown_interrupted")
    run_id = str(row["run_id"])
    root = TuiRuntime("nav-shutdown")
    navigation = TuiAgentNavigationState(root)
    navigation.update_rows("", [row])
    navigation.move_selection(1)
    navigation.enter_selected()
    assert navigation.snapshot().active_run_id == run_id
    navigation.apply_agent_view(run_id, {
        "ok": True, "agent": row, "terminal": True, "children": [], "hidden_child_count": 0,
        "task_progress_items": [], "transcript_events": [], "event_cursor": 0, "final_response": "",
    })
    params = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(), root=""), tui_runtime=root,
                             agent_navigation=navigation)
    return navigation, _make_render_context_factory(params, TuiInteractionState(), TuiTranscriptModeState())


def test_child_page_header_shows_the_shutdown_label(tmp_path):
    navigation, factory = _entered_child(tmp_path)

    context = factory(100)
    frame = render_tui_snapshot(navigation.active_runtime().store.snapshot(), context)
    text = "\n".join(fragments_text(line) for line in (*frame.transcript_lines, *frame.overlay_lines,
                                                        *frame.input_status_lines, *frame.agent_lines, frame.footer))

    assert navigation.snapshot().active_failure_label == "宿主停机中断"
    assert context.focused_agent_failure_label == "宿主停机中断"
    assert "· 宿主停机中断" in text and "· 失败" not in text


def test_render_cache_key_changes_with_the_failure_label():
    snapshot = TuiRuntime("shutdown-key").store.snapshot()
    base = TuiRenderContext(width=80, focused_agent_run_id="child", focused_agent_status="FAILED")

    labelled = replace(base, focused_agent_failure_label="宿主停机中断")

    assert tui_render_context_key(snapshot, base) != tui_render_context_key(snapshot, labelled)


# ---------------------------------------------------------------- IM /status


def _status_agent(tasks: list[object]) -> object:
    ids = [str(task.id) for task in tasks]
    return SimpleNamespace(subagent_run_ids_for_request=lambda _request_id: ids,
                           subagents=SimpleNamespace(list_runs=lambda: tasks))


def test_im_status_breaks_down_the_abnormal_children_by_label():
    tasks = [
        SimpleNamespace(id="r1", status="FAILED", failure_type="host_shutdown_interrupted"),
        SimpleNamespace(id="r2", status="FAILED", failure_type="model_call_admission_closed"),
        SimpleNamespace(id="r3", status="FAILED", failure_type="runner_error"),
        SimpleNamespace(id="r4", status="DONE", failure_type=""),
        SimpleNamespace(id="r5", status="RUNNING", failure_type=""),
    ]

    counts = control_service._subagent_status(_status_agent(tasks), ["gwreq-1"])
    text = render_conversation_task_status(ConversationTaskStatus(
        state="running", subagent_total=counts.total, subagent_running=counts.running,
        subagent_done=counts.done, subagent_failed=counts.other, subagent_other_labels=counts.other_labels,
    ))

    assert tuple(counts[:4]) == (5, 1, 1, 3)
    assert counts.other_labels == (("宿主停机中断", 2), ("失败", 1))
    assert "子代理 5（运行 1，完成 1，异常 3：宿主停机中断 2，失败 1）" in text


def test_im_status_without_abnormal_children_is_unchanged():
    text = render_conversation_task_status(ConversationTaskStatus(
        state="running", subagent_total=2, subagent_running=1, subagent_done=1, subagent_failed=0,
    ))

    assert "子代理 2（运行 1，完成 1，异常 0）" in text


def test_tui_parses_the_gateway_breakdown_with_bounds():
    body = {"state": "running", "subagent_total": 3, "subagent_failed": 3,
            "subagent_other_labels": [["宿主停机中断", 2], ["失败", 1], ["x" * 40, 1], ["坏", "nope"], "junk"]}

    status = _conversation_task_status_from_body(body)

    assert status is not None
    assert status.subagent_other_labels == (("宿主停机中断", 2), ("失败", 1))
    assert "异常 3：宿主停机中断 2，失败 1" in render_conversation_task_status(status)
