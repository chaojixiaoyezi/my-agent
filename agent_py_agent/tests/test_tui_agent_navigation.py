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


def _row(
    run_id: str,
    *,
    parent: str = "",
    status: str = "RUNNING",
    lifecycle_phase: str = "running",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "root_task_id": "task-root",
        "parent_run_id": parent,
        "depth": 2 if parent else 1,
        "name": run_id,
        "role": "worker",
        "status": status,
        "lifecycle_phase": lifecycle_phase,
        "description": f"处理 {run_id}",
        "attempts": 1,
        "context_tokens": 2_000,
        "compact_count": 0,
        "created_at": 10.0,
        "updated_at": 20.0,
        "ended_at": 0.0,
    }


def _goal(
    goal_id: str = "goal-one",
    *,
    status: str = "active",
) -> dict[str, object]:
    return {
        "goal_id": goal_id,
        "name": "底座持续验证",
        "objective": "逐项验证 Goal、思考折叠和多子代理交互，发现问题后修复。",
        "status": status,
        "tokens_used": 12_300,
        "token_budget": 80_000,
        "time_used_seconds": 502,
        "duration_seconds": 86_400,
        "created_at": 10.0,
        "updated_at": 20.0,
    }


def test_goal_precedes_children_and_enter_expands_without_opening_agent() -> None:
    root = TuiRuntime("nav-goal")
    navigation = TuiAgentNavigationState(root)
    changed_to: list[TuiRuntime] = []
    navigation.set_view_change_callback(changed_to.append)
    goal = _goal()
    child = _row("child-a")
    navigation.update_goal_rows([goal])
    navigation.update_rows("", [child])
    root.update_background_activity(
        1,
        {"goals": [goal], "subagents": [child]},
    )

    assert navigation.move_selection(1) is True
    assert navigation.snapshot().selected_run_id == "goal:goal-one"
    assert navigation.enter_selected() is True
    snapshot = navigation.snapshot()
    assert snapshot.active_run_id == ""
    assert snapshot.expanded_goal_id == "goal:goal-one"
    assert changed_to == []

    frame = render_tui_snapshot(
        root.store.snapshot(),
        TuiRenderContext(
            width=120,
            selected_agent_run_id=snapshot.selected_run_id,
            expanded_goal_id=snapshot.expanded_goal_id,
        ),
    )
    rendered = "\n".join(fragments_text(line) for line in frame.agent_lines)
    assert "Goal 底座持续验证 · 进行中" in rendered
    assert "逐项验证 Goal、思考折叠和多子代理交互" in rendered
    assert "12.3k/80.0k tokens" in rendered
    assert "Ctrl+G 收起 Goal" in fragments_text(frame.footer)

    assert navigation.move_selection(1) is True
    assert navigation.snapshot().selected_run_id == "child-a"
    assert navigation.snapshot().expanded_goal_id == ""
    assert navigation.enter_selected() is True
    assert navigation.snapshot().active_run_id == "child-a"


def test_goal_navigation_drops_malformed_rows_and_back_collapses_detail() -> None:
    root = TuiRuntime("nav-goal-malformed")
    navigation = TuiAgentNavigationState(root)
    assert navigation.update_goal_rows([{}, _goal(status="paused")]) is True
    assert navigation.move_selection(1) is True
    assert navigation.enter_selected() is True
    assert navigation.snapshot().expanded_goal_id == "goal:goal-one"
    assert navigation.back() is True
    assert navigation.snapshot().expanded_goal_id == ""
    assert navigation.snapshot().active_run_id == ""


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


def test_back_reconciles_descendant_terminal_status_before_parent_poll() -> None:
    root = TuiRuntime("nav-parent-roster-reconcile")
    navigation = TuiAgentNavigationState(root)
    coordinator = _row("coordinator")
    grandchild_running = _row(
        "grandchild",
        parent="coordinator",
        status="RUNNING",
    )
    navigation.update_rows("", [coordinator])
    navigation.move_selection(1)
    navigation.enter_selected()
    navigation.apply_agent_view(
        "coordinator",
        {
            "ok": True,
            "agent": {**coordinator, "goal": "协调审计"},
            "terminal": False,
            "children": [grandchild_running],
            "hidden_child_count": 0,
            "task_progress_items": [],
            "transcript_events": [],
            "event_cursor": 0,
            "final_response": "",
        },
    )
    navigation.move_selection(1)
    navigation.enter_selected()
    navigation.apply_agent_view(
        "grandchild",
        {
            "ok": True,
            "agent": {
                **grandchild_running,
                "status": "CANCELLED",
                "lifecycle_phase": "terminal",
                "activity": "已停止",
                "updated_at": 30.0,
                "ended_at": 30.0,
                "goal": "执行代码审计",
            },
            "terminal": True,
            "children": [],
            "hidden_child_count": 0,
            "task_progress_items": [],
            "transcript_events": [],
            "event_cursor": 0,
            "final_response": "已停止。",
            "thread_id": "thread-grandchild",
            "final_response_message_id": "msg-grandchild-final",
        },
    )

    assert navigation.back() is True
    snapshot = navigation.snapshot()
    assert snapshot.active_run_id == "coordinator"
    assert snapshot.selected_run_id == "grandchild"
    frame = render_tui_snapshot(
        navigation.active_runtime().store.snapshot(),
        TuiRenderContext(
            width=100,
            selected_agent_run_id=snapshot.selected_run_id,
            focused_agent_run_id="coordinator",
            focused_agent_name="coordinator",
            focused_agent_status="RUNNING",
        ),
    )
    rendered = "\n".join(fragments_text(line) for line in frame.agent_lines)
    assert "grandchild" in rendered
    assert "已停止" in rendered
    assert "grandchild · 运行中" not in rendered


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
        "agent": {
            **payload["agent"],
            "status": "DONE",
            "activity": "已完成",
            "ended_at": 22.0,
        },
        "terminal": True,
        "children": [],
        "task_progress_items": [],
        "transcript_events": [
            {
                "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
                "seq": 7,
                "task_id": "child-a",
                "request_id": request_id,
                "kind": "tool_started",
                "phase": "started",
                "block_id": f"{request_id}:tool:2:0",
                "payload": {
                    "tool": "web_fetch",
                    "round": 2,
                    "call_index": 0,
                    "phase": "started",
                    "detail": "https://example.test",
                },
            }
        ],
        "event_cursor": 7,
        "final_response": "子代理已完成并提交结果。",
        "thread_id": "thread-child-a",
        "final_response_message_id": "msg-child-a-final",
    }
    navigation.active_runtime().set_notice(
        "这个子代理已经结束，当前页面只读；Ctrl+G 返回父代理",
        duration_seconds=60.0,
    )
    assert navigation.apply_agent_view("child-a", terminal) is True
    assert navigation.snapshot().terminal is True
    assert (
        navigation.active_runtime().notice()
        == "这个子代理已经结束，当前页面只读；Ctrl+G 返回父代理"
    )
    snapshot = navigation.active_runtime().store.snapshot()
    assert any(
        block.role == "assistant" and "提交结果" in block.text
        for block in snapshot.stable_blocks
    )
    assert not any(block.role == "tool" for block in snapshot.active_blocks)
    assert navigation.active_runtime().clear_notice() is True
    assert navigation.active_runtime().needs_periodic_refresh() is True
    assert navigation.active_runtime().needs_periodic_refresh() is False
    frame = render_tui_snapshot(
        snapshot,
        TuiRenderContext(
            width=100,
            now=1_000.0,
            focused_agent_run_id="child-a",
            focused_agent_name="child-a",
            focused_agent_status="DONE",
        ),
    )
    assert any("0:12" in fragments_text(line) for line in frame.transcript_lines)


def test_completed_root_roster_stays_selectable_but_does_not_animate() -> None:
    runtime = TuiRuntime("nav-completed-roster")
    done = _row("child-done", status="DONE", lifecycle_phase="terminal")

    assert runtime.update_background_activity(0, {"subagents": [done]}) is True
    assert runtime.needs_periodic_refresh() is False
    frame = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(width=100, selected_agent_run_id="child-done"),
    )
    assert frame.transcript_lines == ()
    assert any("›" in fragments_text(line) for line in frame.agent_lines)
    assert "Enter 查看" in fragments_text(frame.footer)

    assert runtime.update_background_activity(0, {"subagents": []}) is True
    assert not any(
        block.role == "background" for block in runtime.store.snapshot().active_blocks
    )


def test_child_final_response_fallback_does_not_duplicate_typed_final_event() -> None:
    root = TuiRuntime("nav-final-dedupe")
    navigation = TuiAgentNavigationState(root)
    row = _row("child-final", status="DONE", lifecycle_phase="terminal")
    navigation.update_rows("", [row])
    navigation.move_selection(1)
    navigation.enter_selected()
    final_text = "子代理已完成并提交唯一最终回复。"
    request_id = "bg-agent:child-final:attempt-child-final"
    payload = {
        "ok": True,
        "agent": {**row, "goal": "完成唯一回复测试", "ended_at": 22.0},
        "terminal": True,
        "children": [],
        "task_progress_items": [],
        "transcript_events": [
            {
                "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
                "seq": 1,
                "task_id": "child-final",
                "request_id": request_id,
                "kind": "assistant_completed",
                "phase": "completed",
                "block_id": f"{request_id}:assistant:1",
                "payload": {"text": final_text, "process": False},
            }
        ],
        "event_cursor": 1,
        "final_response": "",
    }

    assert navigation.apply_agent_view("child-final", payload) is True
    followup = {
        **payload,
        "transcript_events": [],
        "final_response": final_text,
        "final_response_request_id": request_id,
        "thread_id": "thread-child-final",
        "final_response_message_id": "msg-child-final",
    }
    assert navigation.apply_agent_view("child-final", followup) is True
    matches = [
        block
        for block in navigation.active_runtime().store.snapshot().stable_blocks
        if block.role == "assistant" and block.text == final_text
    ]
    assert len(matches) == 1


def test_hidden_ninth_child_scrolls_into_panel_and_enter_opens_same_run() -> None:
    root = TuiRuntime("nav-roster-window")
    navigation = TuiAgentNavigationState(root)
    rows = [_row(f"child-{index}") for index in range(1, 10)]
    navigation.update_rows("", rows)
    root.update_background_activity(9, {"subagents": rows})

    for _ in rows:
        assert navigation.move_selection(1) is True
    selected = navigation.snapshot().selected_run_id
    assert selected == "child-9"

    frame = render_tui_snapshot(
        root.store.snapshot(),
        TuiRenderContext(width=100, selected_agent_run_id=selected),
    )
    rendered_rows = [fragments_text(line) for line in frame.agent_lines]
    assert not any("child-1 " in line for line in rendered_rows)
    assert any("› ✻ child-9 " in line for line in rendered_rows)
    assert any("还有 1 个子代理未展开" in line for line in rendered_rows)
    assert navigation.enter_selected() is True
    assert navigation.snapshot().active_run_id == "child-9"


def test_enter_starting_child_shows_notice_until_goal_or_first_event() -> None:
    root = TuiRuntime("nav-starting")
    navigation = TuiAgentNavigationState(root)
    row = _row(
        "child-starting",
        lifecycle_phase="waiting_first_event",
    )
    navigation.update_rows("", [row])
    navigation.move_selection(1)

    assert navigation.enter_selected() is True
    runtime = navigation.active_runtime()
    assert "等待模型首个响应" in runtime.notice()

    payload = {
        "ok": True,
        "agent": {**row, "goal": "", "activity": "等待模型首个响应"},
        "terminal": False,
        "children": [],
        "task_progress_items": [],
        "transcript_events": [],
        "event_cursor": 0,
        "final_response": "",
    }
    assert navigation.apply_agent_view("child-starting", payload) is True
    assert "等待模型首个响应" in runtime.notice()

    payload["agent"] = {**payload["agent"], "goal": "实现地图加载器"}
    assert navigation.apply_agent_view("child-starting", payload) is True
    assert runtime.notice() == ""


def test_subagent_startup_phase_changes_label_and_spinner_frame() -> None:
    runtime = TuiRuntime("nav-startup-render")
    row = _row(
        "child-starting",
        lifecycle_phase="waiting_first_event",
    )
    assert runtime.update_background_activity(1, {"subagents": [row]}) is True

    first = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(width=100, spinner_index=0),
    )
    second = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(width=100, spinner_index=1),
    )
    first_text = "\n".join(fragments_text(line) for line in first.agent_lines)
    second_text = "\n".join(fragments_text(line) for line in second.agent_lines)
    assert "等待模型" in first_text
    assert "✻" in first_text
    assert "✢" in second_text


def test_subagent_waiting_descendants_has_distinct_live_label() -> None:
    runtime = TuiRuntime("nav-waiting-descendants")
    row = _row(
        "child-coordinator",
        status="PENDING",
        lifecycle_phase="waiting_descendants",
    )
    assert runtime.update_background_activity(1, {"subagents": [row]}) is True

    frame = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(width=100, spinner_index=0),
    )
    rendered = "\n".join(fragments_text(line) for line in frame.agent_lines)
    assert "等待下级" in rendered
    assert "✻" in rendered
    assert "启动中" not in rendered


def test_child_footer_makes_back_and_escape_semantics_explicit() -> None:
    running = TuiRuntime("nav-footer-running")
    running.update_background_activity(
        1,
        {
            "main_activity": {
                "task_id": "child-a",
                "phase": "running",
                "activity": "编写代码",
            }
        },
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
        "Ctrl+G 返回 · Esc 停止 · 滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式"
    )
    running_notice = render_tui_snapshot(
        running.store.snapshot(),
        TuiRenderContext(
            width=100,
            focused_agent_run_id="child-a",
            focused_agent_name="worker-a",
            focused_agent_status="RUNNING",
            notice="已排队给当前子代理：继续检查操作手感",
        ),
    )
    assert fragments_text(running_notice.footer).strip() == (
        "已排队给当前子代理：继续检查操作手感"
    )

    terminal = TuiRuntime("nav-footer-terminal")
    terminal.update_background_activity(
        0,
        {
            "main_activity": {
                "task_id": "child-a",
                "phase": "completed",
                "activity": "已完成",
            }
        },
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
        "Ctrl+G 返回 · 已结束，只读 · 滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式"
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
        "↑↓ 选择 · Enter 查看 · 滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式 · Esc 停止主代理"
    )


def test_child_execution_generations_do_not_claim_failure_retries() -> None:
    for status, phase, expected in (
        ("RUNNING", "waiting_descendants", "等待下级"),
        ("RUNNING", "running", "运行中"),
        ("DONE", "completed", "已完成"),
        ("FAILED", "failed", "失败"),
    ):
        runtime = TuiRuntime("execution-generations")
        child = _row("coordinator", status=status, lifecycle_phase=phase)
        child.update(attempts=8, context_tokens=60_000, compact_count=3)
        runtime.update_background_activity(1, {"subagents": [child]})
        frame = render_tui_snapshot(
            runtime.store.snapshot(), TuiRenderContext(width=120)
        )
        rendered = "\n".join(fragments_text(line) for line in frame.agent_lines)
        assert "coordinator" in rendered
        assert expected in rendered
        assert "ctx 60.0k" in rendered
        assert "compact 3" in rendered
        assert "重试" not in rendered
