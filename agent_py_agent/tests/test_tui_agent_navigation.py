from __future__ import annotations

from agent_py_agent.agent.conversation.background_transcript import (
    BACKGROUND_TRANSCRIPT_SCHEMA,
)
from agent_py_agent.cli.chat_parts.tui_agent_navigation import (
    TuiAgentNavigationState,
)
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    fragments_text,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


def _row(run_id: str, *, parent: str = "", status: str = "RUNNING") -> dict[str, object]:
    return {
        "run_id": run_id,
        "root_task_id": "task-root",
        "parent_run_id": parent,
        "depth": 2 if parent else 1,
        "name": run_id,
        "role": "worker",
        "status": status,
        "description": f"处理 {run_id}",
        "attempts": 1,
        "context_tokens": 2_000,
        "compact_count": 0,
        "created_at": 10.0,
        "updated_at": 20.0,
    }


def test_down_selects_children_enter_opens_and_back_never_stops() -> None:
    root = TuiRuntime("nav-root")
    navigation = TuiAgentNavigationState(root)
    changed_to: list[TuiRuntime] = []
    navigation.set_view_change_callback(changed_to.append)
    navigation.update_rows("", [_row("child-a"), _row("child-b")])

    assert navigation.move_selection(1) is True
    assert navigation.snapshot().selected_run_id == "child-a"
    assert navigation.move_selection(1) is True
    assert navigation.snapshot().selected_run_id == "child-b"
    assert navigation.move_selection(-1) is True
    assert navigation.snapshot().selected_run_id == "child-a"

    assert navigation.enter_selected() is True
    assert navigation.snapshot().active_run_id == "child-a"
    assert navigation.snapshot().depth == 1
    child_runtime = navigation.active_runtime()
    assert child_runtime is changed_to[-1]
    assert child_runtime is not root

    assert navigation.back() is True
    assert navigation.snapshot().active_run_id == ""
    assert navigation.active_runtime() is root
    assert changed_to[-1] is root
    assert root.store.snapshot().status.phase == "idle"


def test_child_view_applies_live_events_todo_context_children_and_final() -> None:
    root = TuiRuntime("nav-detail")
    navigation = TuiAgentNavigationState(root)
    navigation.update_rows("", [_row("child-a")])
    navigation.move_selection(1)
    navigation.enter_selected()
    delegated_goal = "实现核心玩法，并逐项验证碰撞、关卡与启动方式。"
    request_id = "bg-agent:child-a:attempt-a"
    events = [
        {
            "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
            "seq": 1,
            "task_id": "child-a",
            "request_id": request_id,
            "kind": "thinking_started",
            "phase": "started",
            "block_id": f"{request_id}:thinking:1",
            "payload": {"started_at": 10.0},
        },
        {
            "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
            "seq": 2,
            "task_id": "child-a",
            "request_id": request_id,
            "kind": "thinking_delta",
            "phase": "delta",
            "block_id": f"{request_id}:thinking:1",
            "payload": {"text": "先阅读现有代码。"},
        },
        {
            "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
            "seq": 3,
            "task_id": "child-a",
            "request_id": request_id,
            "kind": "thinking_completed",
            "phase": "completed",
            "block_id": f"{request_id}:thinking:1",
            "payload": {"text": "先阅读现有代码。", "duration_seconds": 1.2},
        },
        {
            "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
            "seq": 4,
            "task_id": "child-a",
            "request_id": request_id,
            "kind": "assistant_completed",
            "phase": "completed",
            "block_id": f"{request_id}:assistant:1",
            "payload": {"text": "我先检查入口和测试。", "process": True},
        },
        {
            "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
            "seq": 5,
            "task_id": "child-a",
            "request_id": request_id,
            "kind": "tool_started",
            "phase": "started",
            "block_id": f"{request_id}:tool:1:0",
            "payload": {
                "tool": "run_command",
                "round": 1,
                "call_index": 0,
                "phase": "started",
                "detail": "python -m pytest",
            },
        },
        {
            "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
            "seq": 6,
            "task_id": "child-a",
            "request_id": request_id,
            "kind": "tool_completed",
            "phase": "completed",
            "block_id": f"{request_id}:tool:1:0",
            "payload": {
                "tool": "run_command",
                "round": 1,
                "call_index": 0,
                "phase": "finished",
                "status": "完成",
                "output": "12 passed",
                "ok": True,
            },
        },
    ]
    payload = {
        "ok": True,
        "agent": {
            **_row("child-a"),
            "goal": delegated_goal,
            "activity": "正在运行测试",
            "context_usage": {
                "schema": "model_visible_context_usage.v1",
                "estimated": False,
                "context_window_tokens": 128_000,
                "compact_trigger_tokens": 115_200,
                "current_tokens": 8_000,
                "prompt_tokens": 4_000,
                "messages_tokens": 2_000,
                "runtime_guidance_tokens": 100,
                "tool_schema_tokens": 1_900,
                "protocol": "native",
            },
        },
        "terminal": False,
        "children": [_row("grandchild", parent="child-a")],
        "hidden_child_count": 0,
        "task_progress_items": [
            {"id": "qa", "title": "运行完整测试", "status": "in_progress"}
        ],
        "transcript_events": events,
        "event_cursor": 6,
        "final_response": "",
    }

    assert navigation.apply_agent_view("child-a", payload) is True
    snapshot = navigation.active_runtime().store.snapshot()
    assert snapshot.status.context_tokens == 8_000
    assert any(
        block.role == "user" and block.text == delegated_goal
        for block in snapshot.stable_blocks
    )
    assert any(block.role == "thinking" for block in snapshot.stable_blocks)
    assert any(
        block.role == "assistant"
        and block.metadata.get("process") is True
        and "检查入口" in block.text
        for block in snapshot.stable_blocks
    )
    assert any(
        block.role == "tool" and block.metadata.get("tool") == "run_command"
        for block in snapshot.stable_blocks
    )
    assert any(block.role == "todo" for block in snapshot.active_blocks)
    assert navigation.event_cursor("child-a") == 6

    assert navigation.move_selection(1) is True
    assert navigation.snapshot().selected_run_id == "grandchild"
    assert navigation.enter_selected() is True
    assert navigation.snapshot().active_run_id == "grandchild"
    assert navigation.back() is True

    terminal = {
        **payload,
        "agent": {**payload["agent"], "status": "DONE", "activity": "已完成"},
        "terminal": True,
        "children": [],
        "task_progress_items": [],
        "transcript_events": [],
        "final_response": "子代理已完成并提交结果。",
    }
    assert navigation.apply_agent_view("child-a", terminal) is True
    assert navigation.snapshot().terminal is True
    snapshot = navigation.active_runtime().store.snapshot()
    assert any(
        block.role == "assistant" and "提交结果" in block.text
        for block in snapshot.stable_blocks
    )
    assert navigation.active_runtime().needs_periodic_refresh() is False


def test_completed_root_roster_stays_selectable_but_does_not_animate() -> None:
    runtime = TuiRuntime("nav-completed-roster")
    done = _row("child-done", status="DONE")

    assert runtime.update_background_activity(0, subagents=[done]) is True
    assert runtime.needs_periodic_refresh() is False
    frame = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(width=100, selected_agent_run_id="child-done"),
    )
    assert frame.transcript_lines == ()
    assert any("›" in fragments_text(line) for line in frame.agent_lines)
    assert "Enter 查看" in fragments_text(frame.footer)

    assert runtime.update_background_activity(0, subagents=[]) is True
    assert not any(
        block.role == "background" for block in runtime.store.snapshot().active_blocks
    )


def test_child_footer_makes_back_and_escape_semantics_explicit() -> None:
    running = TuiRuntime("nav-footer-running")
    running.update_background_activity(
        1,
        main_activity={"task_id": "child-a", "phase": "running", "activity": "编写代码"},
    )
    running_frame = render_tui_snapshot(
        running.store.snapshot(),
        TuiRenderContext(
            width=100,
            focused_agent_run_id="child-a",
            focused_agent_name="worker-a",
            focused_agent_status="RUNNING",
        ),
    )
    assert fragments_text(running_frame.footer).strip() == (
        "Ctrl+G 返回 · Esc 停止 · PgUp/Ctrl+Home 历史 · F6 滚轮"
    )
    running_notice = render_tui_snapshot(
        running.store.snapshot(),
        TuiRenderContext(
            width=100,
            focused_agent_run_id="child-a",
            focused_agent_name="worker-a",
            focused_agent_status="RUNNING",
            notice="已发送给当前子代理：继续检查操作手感",
        ),
    )
    assert fragments_text(running_notice.footer).strip() == (
        "已发送给当前子代理：继续检查操作手感"
    )

    terminal = TuiRuntime("nav-footer-terminal")
    terminal.update_background_activity(
        0,
        main_activity={"task_id": "child-a", "phase": "completed", "activity": "已完成"},
    )
    terminal_frame = render_tui_snapshot(
        terminal.store.snapshot(),
        TuiRenderContext(
            width=100,
            focused_agent_run_id="child-a",
            focused_agent_name="worker-a",
            focused_agent_status="DONE",
        ),
    )
    assert fragments_text(terminal_frame.footer).strip() == (
        "Ctrl+G 返回 · 已结束，只读 · PgUp/Ctrl+Home 历史 · F6 滚轮"
    )


def test_selected_child_footer_overrides_root_running_hint() -> None:
    runtime = TuiRuntime("nav-selected-running")
    sequencer = TuiEventSequencer("nav-selected-running")
    runtime.store.publish(sequencer.emit("turn_started", "started", "turn"))

    frame = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(width=100, selected_agent_run_id="child-a"),
    )

    assert fragments_text(frame.footer).strip() == (
        "↑↓ 选择 · Enter 查看 · PgUp/Ctrl+Home 历史 · F6 滚轮 · Esc 停止主代理"
    )
