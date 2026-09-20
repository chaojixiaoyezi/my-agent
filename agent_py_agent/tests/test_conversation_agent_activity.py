from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _run(run_id: str, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": run_id,
        "root_id": "task-live",
        "parent_id": "task-live",
        "depth": 1,
        "agent_name": run_id,
        "role": "worker",
        "status": "RUNNING",
        "current_step": "",
        "current_tool": "",
        "last_progress_summary": "",
        "latest_summary": "",
        "description": "",
        "attributes": {},
        "runner_attempts": 1,
        "runner_active_attempt_id": "",
        "turn_end_reason": "",
        "agent_thread_id": f"thread-{run_id}",
        "agent_run_workspace_dir": "",
        "created_at": 10.0,
        "updated_at": 20.0,
        "heartbeat_at": 20.0,
        "ended_at": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_conversation_agent_activity_projects_only_active_roots_direct_children() -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    active_link = SimpleNamespace(task_id="task-live", status="active")
    interrupted_link = SimpleNamespace(task_id="task-old", status="interrupted")
    direct_running = _run(
        "child-running",
        agent_name="game-engine",
        goal="不得进入展示投影",
        attributes={"covers": ["2", "3", "2"]},
        current_step="正在生成 game.js",
        current_tool="write_file",
        runner_attempts=2,
    )
    direct_done = _run(
        "child-done",
        agent_name="level-design",
        status="DONE",
        current_step="模型已生成回复",
        last_progress_summary="最近成功调用工具: run_command",
        created_at=11.0,
        updated_at=19.0,
        heartbeat_at=19.0,
        ended_at=19.0,
    )
    grandchild = _run(
        "grandchild",
        parent_id="child-running",
        depth=2,
        created_at=12.0,
    )
    malformed_direct = _run(
        "malformed-direct",
        parent_id="different-root",
        created_at=13.0,
    )
    old_child = _run(
        "old-child",
        root_id="task-old",
        parent_id="task-old",
        created_at=1.0,
    )

    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [active_link, interrupted_link],
            [],
        )), threads=SimpleNamespace(load_report=lambda _thread_id: (
            SimpleNamespace(compact_generation=3),
            None,
        )))
    manager = SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(
            runs=[
                grandchild,
                malformed_direct,
                direct_done,
                old_child,
                direct_running,
            ],
            load_errors=[],
        )
    )

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=manager),
        store,
        "thread-1",
    )
    payload = activity.to_dict()

    assert activity.active_task_count == 1
    assert [row["run_id"] for row in activity.subagents] == [
        "child-running",
        "child-done",
    ]
    assert activity.subagents[0]["description"] == "不得进入展示投影"
    assert activity.subagents[0]["attempts"] == 2
    assert activity.subagents[0]["progress_item_ids"] == ["2", "3"]
    assert activity.subagents[1]["description"] == "worker"
    assert "activity" not in activity.subagents[0]
    assert "current_tool" not in activity.subagents[0]
    assert "goal" not in activity.subagents[0]
    assert payload["schema_version"] == "conversation_agent_activity.v6"
    assert payload["compact_count"] == 3
    assert payload["active_task_projection_ok"] is True
    assert payload["subagent_projection_ok"] is True


def test_conversation_agent_activity_uses_index_then_exact_canonical_reads() -> None:
    """生产管理器有查询索引时，活动快照不得退回全部历史 run 扫描。"""
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    direct = _run("child-indexed", description="实现核心逻辑")
    records = [
        SimpleNamespace(
            run_id=direct.id,
            root_task_id="task-live",
            parent_run_id="task-live",
            depth=1,
        ),
        SimpleNamespace(
            run_id="grandchild-indexed",
            root_task_id="task-live",
            parent_run_id=direct.id,
            depth=2,
        ),
    ]
    selected_ids: list[str] = []

    class _Manager:
        local_store = SimpleNamespace(
            list_agent_tree=lambda _root_id: SimpleNamespace(runs=records)
        )

        def list_runs_by_ids_report(self, run_ids):
            selected_ids.extend(run_ids)
            return SimpleNamespace(runs=[direct], load_errors=[])

        def list_runs_report(self):
            raise AssertionError("indexed activity lookup must not scan all runs")

    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )))

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=_Manager()),
        store,
        "thread-indexed",
    )

    assert selected_ids == [direct.id]
    assert [row["run_id"] for row in activity.subagents] == [direct.id]


def test_conversation_agent_activity_reports_projection_failure_without_claiming_rows() -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )))

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=None),
        store,
        "thread-1",
    )

    assert activity.active_task_count == 1
    assert activity.subagents == ()
    assert activity.active_task_projection_ok is True
    assert activity.subagent_projection_ok is False
    assert activity.warnings == ("subagent_manager_unavailable",)


def test_conversation_agent_activity_marks_active_roots_unknown_on_link_failure() -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    def _unavailable(_thread_id: str) -> object:
        raise OSError("temporary read failure")

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=SimpleNamespace(list_runs=lambda: [])),
        SimpleNamespace(tasks=SimpleNamespace(active_report=_unavailable)),
        "thread-1",
    )

    assert activity.active_task_count == 0
    assert activity.active_task_projection_ok is False
    assert activity.subagent_projection_ok is True
    assert activity.warnings == ("conversation_task_links_unavailable",)


def test_conversation_agent_activity_projects_goal_without_active_task() -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )
    from agent_py_agent.agent.conversation.models import ThreadGoal

    active_goal = ThreadGoal(
        goal_id="goal-one",
        thread_id="thread-goal",
        objective="持续验证 Goal 与多子代理交互",
        task_id="goal-task-one",
        name="底座验证",
        status="active",
        token_budget=80_000,
        duration_seconds=86_400,
        tokens_used=12_300,
        time_used_seconds=500,
        created_at=10.0,
        updated_at=20.0,
    )
    completed_goal = ThreadGoal(
        goal_id="goal-done",
        thread_id="thread-goal",
        objective="已经完成",
        task_id="goal-task-done",
        status="complete",
    )
    store = SimpleNamespace(goal_clock=SimpleNamespace(current_time_seconds=lambda _goal: 502), goals=SimpleNamespace(list_report=lambda _thread_id: ([active_goal, completed_goal], None)), tasks=SimpleNamespace(active_report=lambda _thread_id: ([], [])), threads=SimpleNamespace(load_report=lambda _thread_id: (
            SimpleNamespace(compact_generation=2, workspace_task_id=""),
            None,
        )))

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=None),
        store,
        "thread-goal",
    )

    assert activity.active_task_count == 0
    assert activity.goal_projection_ok is True
    assert activity.goals == (
        {
            "goal_id": "goal-one",
            "revision": 1,
            "name": "底座验证",
            "objective": "持续验证 Goal 与多子代理交互",
            "status": "active",
            "tokens_used": 12_300,
            "time_used_seconds": 502,
            "created_at": 10.0,
            "updated_at": 20.0,
            "token_budget": 80_000,
            "duration_seconds": 86_400,
        },
    )
    assert activity.to_dict()["goals"] == [dict(activity.goals[0])]


def test_conversation_agent_activity_goal_read_failure_is_explicit() -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    store = SimpleNamespace(goals=SimpleNamespace(
            list_report=lambda _thread_id: ([], {"error_code": "GOAL_STORE_CORRUPT"})
        ), tasks=SimpleNamespace(active_report=lambda _thread_id: ([], [])))

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=None),
        store,
        "thread-goal",
    )

    assert activity.goals == ()
    assert activity.goal_projection_ok is False
    assert activity.warnings == ("conversation_goal_projection_load_error",)


def test_conversation_agent_activity_retains_completed_child_roster() -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    link = SimpleNamespace(
        task_id="task-live",
        thread_id="thread-1",
        status="completed",
        task_path="",
        created_at=10.0,
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: ([], []), load_report=lambda _task_id: (link, None)), threads=SimpleNamespace(load_report=lambda _thread_id: (
            SimpleNamespace(compact_generation=2, workspace_task_id="task-live"),
            None,
        )))
    manager = SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(
            runs=[_run("child-done", status="DONE", ended_at=20.0)],
            load_errors=[],
        )
    )

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=manager),
        store,
        "thread-1",
    )

    assert activity.active_task_count == 0
    assert [row["run_id"] for row in activity.subagents] == ["child-done"]
    assert activity.main_activity == {}
    assert activity.compact_count == 2


def test_active_task_link_without_executor_or_open_children_is_display_idle(
    tmp_path: Path,
) -> None:
    """可续接的 active 链接不是 会话运行时 式 active turn，终态名册保留但 Working 收起。"""
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-idle",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-live",
            "goal": "已完成当前执行，但保留目录供后续继续",
            "now": 11.0,
        }
    )
    store.tasks.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "task-live"}
    )
    manager = SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(
            runs=[_run("child-done", status="DONE", ended_at=20.0)],
            load_errors=[],
        )
    )

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=manager),
        store,
        thread.thread_id,
    )

    assert store.tasks.load("task-live").status == "active"
    assert activity.active_task_count == 0
    assert [row["run_id"] for row in activity.subagents] == ["child-done"]
    assert activity.main_activity["phase"] == "waiting"


def test_running_child_waits_for_exact_active_attempt_first_event(tmp_path) -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )
    from agent_py_agent.agent.conversation.agent_transcript import (
        append_agent_transcript_event,
        begin_agent_transcript_turn,
    )

    task = _run(
        "child-starting",
        runner_active_attempt_id="attempt-current",
    )
    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )))
    manager = SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(runs=[task], load_errors=[])
    )
    agent = SimpleNamespace(subagents=manager, conversation_store=store)

    waiting = conversation_agent_activity(agent, store, "thread-1")
    assert waiting.subagents[0]["lifecycle_phase"] == "waiting_first_event"

    old_request = begin_agent_transcript_turn(
        agent,
        run_id=task.id,
        attempt_id="attempt-old",
    )
    assert append_agent_transcript_event(
        agent,
        thread_id="thread-child-starting",
        task_id=task.id,
        request_id=old_request,
        kind="thinking_started",
        phase="started",
        block_id=f"{old_request}:thinking",
        payload={},
    ) == 1
    still_waiting = conversation_agent_activity(agent, store, "thread-1")
    assert still_waiting.subagents[0]["lifecycle_phase"] == "waiting_first_event"

    current_request = begin_agent_transcript_turn(
        agent,
        run_id=task.id,
        attempt_id="attempt-current",
    )
    assert append_agent_transcript_event(
        agent,
        thread_id="thread-child-starting",
        task_id=task.id,
        request_id=current_request,
        kind="thinking_started",
        phase="started",
        block_id=f"{current_request}:thinking",
        payload={},
    ) == 2
    running = conversation_agent_activity(agent, store, "thread-1")
    assert running.subagents[0]["lifecycle_phase"] == "running"


def test_pending_coordinator_with_active_descendants_is_not_shown_as_starting(
    tmp_path,
) -> None:
    """已派出孙代理的 coordinator 在安全等待，不应继续冒充尚未启动。"""
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    coordinator = _run(
        "child-coordinator",
        status="PENDING",
        turn_end_reason="interrupted",
        attributes={
            "direct_child_wait": {
                "schema_version": "direct-child-wait.v1",
                "state": "waiting",
                "run_ids": ["grandchild-a", "grandchild-b"],
            }
        },
    )
    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )))
    manager = SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(runs=[coordinator], load_errors=[])
    )

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=manager, conversation_store=store),
        store,
        "thread-1",
    )

    assert activity.subagents[0]["lifecycle_phase"] == "waiting_descendants"


def test_pending_coordinator_does_not_infer_descendant_wait_from_prose_or_reason(
    tmp_path,
) -> None:
    """没有 canonical 等待标记时，旧错误文字和非协议 reason 都不能取得展示权威。"""
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    coordinator = _run(
        "child-coordinator",
        status="PENDING",
        turn_end_reason="SUBAGENTS_ACTIVE",
        runner_last_error="runner 本轮结束: interrupted (SUBAGENTS_ACTIVE)",
        attributes={
            "direct_child_wait": {
                "schema_version": "malformed",
                "state": "waiting",
                "run_ids": ["grandchild-a"],
            }
        },
    )
    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )))
    manager = SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(runs=[coordinator], load_errors=[])
    )

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=manager, conversation_store=store),
        store,
        "thread-1",
    )

    assert activity.subagents[0]["lifecycle_phase"] == "starting"


def test_completed_task_retains_child_roster_but_clears_stale_todo(
    tmp_path: Path,
) -> None:
    import hashlib

    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
        task_progress_projection_for_task,
    )
    from agent_py_agent.agent.task_progress import (
        with_task_progress_display_plan,
        write_task_progress,
    )

    owner_root = tmp_path / "owner"
    task_path = str(tmp_path / "project" / "finished-task")
    ledger_id = f"task-path:{hashlib.sha256(task_path.encode('utf-8')).hexdigest()[:16]}"
    write_task_progress(
        owner_root,
        ledger_id,
        with_task_progress_display_plan(
            {
                "items": [
                    {"id": "research", "title": "调研项目", "status": "in_progress"},
                    {"id": "report", "title": "整合报告", "status": "pending"},
                ]
            },
            generation_id="turn-finished",
            item_ids=["research", "report"],
        ),
    )
    link = SimpleNamespace(
        task_id="task-finished",
        thread_id="thread-finished",
        task_path=task_path,
        status="completed",
        created_at=20.0,
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: ([], []), load_report=lambda _task_id: (link, None), load=lambda _task_id: link), threads=SimpleNamespace(load_report=lambda _thread_id: (
            SimpleNamespace(compact_generation=1, workspace_task_id="task-finished"),
            None,
        )))
    child = _run(
        "child-done",
        root_id="task-finished",
        parent_id="task-finished",
        status="DONE",
        ended_at=30.0,
    )
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_root),
        subagents=SimpleNamespace(
            list_runs_report=lambda: SimpleNamespace(runs=[child], load_errors=[])
        ),
    )

    activity = conversation_agent_activity(agent, store, "thread-finished")

    assert activity.active_task_count == 0
    assert [row["run_id"] for row in activity.subagents] == ["child-done"]
    assert activity.task_progress_items == ()
    assert activity.task_progress_generation_id == "turn-finished"
    assert activity.task_progress_plan_revision == 1
    assert task_progress_projection_for_task(agent, store, "task-finished") == (
        (),
        "turn-finished",
        1,
    )


def test_conversation_agent_view_reads_exact_child_state_and_final_reply(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.conversation.agent_activity import conversation_agent_view

    delegated_goal = "完整派工要求：" + "逐项核对实现、测试与交付。" * 320
    usage = {
        "schema": "model_visible_context_usage.v1",
        "estimated": False,
        "context_window_tokens": 128_000,
        "compact_trigger_tokens": 115_200,
        "current_tokens": 12_300,
        "prompt_tokens": 4_000,
        "messages_tokens": 3_000,
        "runtime_guidance_tokens": 300,
        "tool_schema_tokens": 5_000,
        "protocol": "native",
    }
    child = _run(
        "child-done",
        status="DONE",
        description="设计三个关卡",
        goal=delegated_goal,
        current_tool="web_fetch",
        current_step="仍在抓取网页",
        attributes={"model_visible_context_usage": usage},
        ended_at=22.0,
    )
    grandchild = _run(
        "grandchild",
        root_id="task-live",
        parent_id="child-done",
        depth=2,
    )
    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), threads=SimpleNamespace(load_report=lambda _thread_id: (
            SimpleNamespace(compact_generation=1, model_context_usage={**usage, "compact_generation": 1}),
            None,
        )), messages=SimpleNamespace(recent_report=lambda _thread_id, *, limit: (
            [
                SimpleNamespace(
                    role="assistant",
                    content="已提交关卡设计。",
                    message_id="msg-child-final",
                    metadata={"conversation_request_id": "attempt-child-done"},
                )
            ],
            [],
        )))
    manager = SimpleNamespace(
        load=lambda run_id: child if run_id == child.id else grandchild,
        list_runs_report=lambda: SimpleNamespace(
            runs=[child, grandchild],
            load_errors=[],
        ),
    )
    agent = SimpleNamespace(
        subagents=manager,
        conversation_store=store,
        root=tmp_path,
    )

    view = conversation_agent_view(agent, store, child.id)

    assert view["terminal"] is True
    assert view["agent"]["goal"] == delegated_goal
    assert view["agent"]["context_usage"]["current_tokens"] == 12_300
    assert view["agent"]["compact_count"] == 1
    assert view["agent"]["activity"] == "已完成"
    assert [row["run_id"] for row in view["children"]] == ["grandchild"]
    assert view["final_response"] == "已提交关卡设计。"
    assert view["final_response_message_id"] == "msg-child-final"
    assert view["thread_id"] == child.agent_thread_id
    assert (
        view["final_response_request_id"]
        == "bg-agent:child-done:attempt-child-done"
    )

    child.status = "RUNNING"
    running_view = conversation_agent_view(agent, store, child.id)
    assert running_view["terminal"] is False
    assert running_view["final_response"] == ""
    assert running_view["final_response_message_id"] == ""
    assert running_view["final_response_request_id"] == ""


def test_conversation_agent_activity_reads_child_thread_generation_only(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )

    workspace = tmp_path / "child"
    compact = workspace / "memory_archive" / "compact_applies" / "ledger.jsonl"
    compact.parent.mkdir(parents=True)
    compact.write_text('{"generation":1}\n{"generation":2}\n', encoding="utf-8")
    task = _run(
        "child-usage",
        agent_run_workspace_dir=str(workspace),
        attributes={
            "model_visible_context_usage": {
                "schema": "model_visible_context_usage.v1",
                "current_tokens": 12_345,
            },
            "model_visible_context_compaction": {
                "schema": "model_visible_context_compaction.v1",
                "count": 1,
            },
        },
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )), threads=SimpleNamespace(load_report=lambda thread_id: (
            SimpleNamespace(
                compact_generation=(2 if thread_id == task.agent_thread_id else 0),
                model_context_usage=(
                    {"schema": "model_visible_context_usage.v1", "current_tokens": 12_345, "compact_generation": 2}
                    if thread_id == task.agent_thread_id else {}
                ),
                compact_checkpoint_id=(
                    "compact-child-2" if thread_id == task.agent_thread_id else ""
                ),
            ),
            None,
        )))
    manager = SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(runs=[task], load_errors=[])
    )

    activity = conversation_agent_activity(
        SimpleNamespace(subagents=manager),
        store,
        "thread-usage",
    )

    assert activity.subagents[0]["context_tokens"] == 12_345
    assert activity.subagents[0]["compact_count"] == 2


def test_background_main_activity_sink_projects_real_stage_for_active_task() -> None:
    from agent_py_agent.agent.conversation.agent_activity import (
        BackgroundMainActivitySink,
        conversation_agent_activity,
    )

    agent = SimpleNamespace(
        subagents=SimpleNamespace(
            list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])
        )
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )))
    sink = BackgroundMainActivitySink(
        agent,
        thread_id="thread-main",
        task_id="task-live",
    )
    sink.write_thinking("先核对子代理结果，再整合最终页面。")
    assert sink.write_context_usage(
        {
            "schema": "model_visible_context_usage.v1",
            "estimated": True,
            "context_window_tokens": 128_000,
            "compact_trigger_tokens": 115_200,
            "current_tokens": 42_100,
            "prompt_tokens": 8_700,
            "messages_tokens": 20_000,
            "runtime_guidance_tokens": 400,
            "tool_schema_tokens": 13_000,
            "protocol": "native",
            "prompt": "must not cross the projection",
        }
    ) is True

    activity = conversation_agent_activity(agent, store, "thread-main")

    assert activity.main_activity["phase"] == "thinking"
    assert activity.main_activity["activity"] == "先核对子代理结果，再整合最终页面。"
    assert activity.main_activity["context_usage"]["current_tokens"] == 42_100
    assert "prompt" not in activity.main_activity["context_usage"]
    assert sink.write_tool_input_progress(
        {
            "schema": "provider_tool_input_progress.v1",
            "phase": "started",
            "stream_index": 0,
            "tool": "write_file",
            "received_chars": 0,
            "partial_json": "must not cross",
        }
    )
    activity = conversation_agent_activity(agent, store, "thread-main")
    assert activity.main_activity["activity"] == "正在准备 write_file 参数"
    sink.write_tool_input_progress(
        {
            "schema": "provider_tool_input_progress.v1",
            "phase": "ready",
            "stream_index": 0,
            "tool": "write_file",
            "received_chars": 12_000,
        }
    )
    sink.write_progress({"tool": "run_command", "phase": "started"})
    activity = conversation_agent_activity(agent, store, "thread-main")
    assert activity.main_activity["activity"] == "正在使用 run_command"
    sink.finish()
    activity = conversation_agent_activity(agent, store, "thread-main")
    assert activity.main_activity["phase"] == "waiting"
    assert activity.main_activity["activity"] == "等待后续事件"
    assert activity.main_activity["context_usage"]["current_tokens"] == 42_100


def test_main_activity_clock_uses_current_workspace_root_not_panel_or_child() -> None:
    """同一长会话追加任务后，main 计时必须从当前主任务重新开始。"""
    from agent_py_agent.agent.conversation.agent_activity import (
        BackgroundMainActivitySink,
        conversation_agent_activity,
    )

    old_root = SimpleNamespace(
        task_id="task-old",
        status="active",
        created_at=10.0,
    )
    current_root = SimpleNamespace(
        task_id="task-current",
        status="active",
        created_at=300.0,
    )
    newer_child = SimpleNamespace(
        task_id="child-current",
        status="active",
        created_at=320.0,
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: (
            [old_root, current_root, newer_child],
            [],
        )), threads=SimpleNamespace(load_report=lambda _thread_id: (
            SimpleNamespace(
                workspace_task_id="task-current",
                compact_generation=0,
            ),
            None,
        )))
    agent = SimpleNamespace(
        subagents=SimpleNamespace(
            list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])
        )
    )
    BackgroundMainActivitySink(
        agent,
        thread_id="thread-long",
        task_id="task-old",
    ).write_thinking("旧回合仍在思考")

    activity = conversation_agent_activity(agent, store, "thread-long")

    assert activity.main_activity == {
        "task_id": "task-current",
        "phase": "waiting",
        "activity": "等待后续事件",
        "started_at": 300.0,
        "updated_at": 300.0,
    }

    current_sink = BackgroundMainActivitySink(
        agent,
        thread_id="thread-long",
        task_id="task-current",
    )
    current_sink.write_thinking("核对当前修复子代理")
    activity = conversation_agent_activity(agent, store, "thread-long")

    assert activity.main_activity["task_id"] == "task-current"
    assert activity.main_activity["activity"] == "核对当前修复子代理"
    assert activity.main_activity["started_at"] == 300.0


def test_conversation_agent_activity_projects_canonical_task_progress(tmp_path: Path) -> None:
    import hashlib

    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
        task_progress_items_for_task,
    )
    from agent_py_agent.agent.task_progress import write_task_progress

    owner_root = tmp_path / "owner"
    task_path = str(tmp_path / "project" / "bbb")
    ledger_id = f"task-path:{hashlib.sha256(task_path.encode('utf-8')).hexdigest()[:16]}"
    write_task_progress(
        owner_root,
        ledger_id,
        {
            "items": [
                {"id": "core", "title": "游戏核心", "status": "done"},
                {"id": "qa", "title": "整合测试", "status": "in_progress"},
            ]
        },
    )
    link = SimpleNamespace(
        task_id="task-live",
        task_path=task_path,
        status="active",
        created_at=20.0,
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: ([link], []), load=lambda _task_id: link))
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_root),
        subagents=SimpleNamespace(
            list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])
        ),
    )

    activity = conversation_agent_activity(agent, store, "thread-progress")

    assert activity.task_progress_projection_ok is True
    assert list(activity.task_progress_items) == [
        {"id": "core", "title": "游戏核心", "status": "done"},
        {"id": "qa", "title": "整合测试", "status": "in_progress"},
    ]
    assert task_progress_items_for_task(agent, store, "task-live") == (
        {"id": "core", "title": "游戏核心", "status": "done"},
        {"id": "qa", "title": "整合测试", "status": "in_progress"},
    )


def test_conversation_activity_projects_only_current_display_generation(
    tmp_path: Path,
) -> None:
    import hashlib

    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
        task_progress_projection_for_task,
    )
    from agent_py_agent.agent.task_progress import (
        with_task_progress_display_plan,
        write_task_progress,
    )

    owner_root = tmp_path / "owner"
    task_path = str(tmp_path / "project" / "long-task")
    ledger_id = f"task-path:{hashlib.sha256(task_path.encode('utf-8')).hexdigest()[:16]}"
    write_task_progress(
        owner_root,
        ledger_id,
        with_task_progress_display_plan(
            {
                "items": [
                    {"id": "old", "title": "旧阶段", "status": "done"}
                ]
            },
            generation_id="turn-old",
            item_ids=["old"],
        ),
    )
    write_task_progress(
        owner_root,
        ledger_id,
        with_task_progress_display_plan(
            {
                "items": [
                    {"id": "new", "title": "当前阶段", "status": "in_progress"}
                ]
            },
            generation_id="turn-new",
            item_ids=["new"],
        ),
    )
    link = SimpleNamespace(
        task_id="task-live",
        task_path=task_path,
        status="active",
        created_at=20.0,
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: ([link], []), load=lambda _task_id: link))
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_root),
        subagents=SimpleNamespace(
            list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])
        ),
    )

    activity = conversation_agent_activity(agent, store, "thread-progress")

    assert list(activity.task_progress_items) == [
        {"id": "new", "title": "当前阶段", "status": "in_progress"}
    ]
    assert activity.task_progress_generation_id == "turn-new"
    assert activity.task_progress_plan_revision == 2
    assert task_progress_projection_for_task(agent, store, "task-live") == (
        ({"id": "new", "title": "当前阶段", "status": "in_progress"},),
        "turn-new",
        2,
    )


def test_task_progress_projection_keeps_workspace_root_when_children_are_newer(
    tmp_path: Path,
) -> None:
    """同一 thread 的新 child link 不能把主任务 Todo 投影成空列表。"""
    import hashlib

    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )
    from agent_py_agent.agent.task_progress import write_task_progress

    owner_root = tmp_path / "owner"
    root_task_path = str(tmp_path / "project" / "root-task")
    root_ledger_id = (
        "task-path:"
        f"{hashlib.sha256(root_task_path.encode('utf-8')).hexdigest()[:16]}"
    )
    write_task_progress(
        owner_root,
        root_ledger_id,
        {
            "items": [
                {"id": "A", "title": "项目骨架", "status": "pending"},
                {"id": "B", "title": "算法实现", "status": "in_progress"},
            ]
        },
    )
    root_link = SimpleNamespace(
        task_id="task-root",
        task_path=root_task_path,
        status="active",
        created_at=10.0,
    )
    child_link = SimpleNamespace(
        task_id="child-newer",
        task_path=str(tmp_path / "project" / "root-task" / "work" / "child"),
        status="active",
        created_at=20.0,
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: ([root_link, child_link], [])), threads=SimpleNamespace(load_report=lambda _thread_id: (
            SimpleNamespace(workspace_task_id="task-root", compact_generation=0),
            None,
        )))
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_root),
        subagents=SimpleNamespace(
            list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])
        ),
    )

    activity = conversation_agent_activity(agent, store, "thread-progress")

    assert list(activity.task_progress_items) == [
        {"id": "A", "title": "项目骨架", "status": "pending"},
        {"id": "B", "title": "算法实现", "status": "in_progress"},
    ]


def test_task_progress_projection_hides_exact_direct_child_seed_after_finish(
    tmp_path: Path,
) -> None:
    """active panel 与最终 notice 都按 exact run id 隐藏自动派工 Todo，账本不改。"""
    import hashlib

    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
        task_progress_items_for_task,
    )
    from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

    owner_root = tmp_path / "owner"
    task_path = str(tmp_path / "project" / "bbb")
    ledger_id = f"task-path:{hashlib.sha256(task_path.encode('utf-8')).hexdigest()[:16]}"
    child_id = "subagent-1787400000-deadbeef"
    write_task_progress(
        owner_root,
        ledger_id,
        {
            "items": [
                {
                    "id": child_id,
                    "title": "子代理负责开发核心引擎并写入很长的绝对路径",
                    "status": "done",
                },
                {"id": "qa", "title": "整合测试", "status": "done"},
            ]
        },
    )
    link = SimpleNamespace(
        task_id="task-live",
        task_path=task_path,
        status="active",
        created_at=20.0,
    )
    store = SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: ([link], []), load=lambda _task_id: link))
    child = _run(
        child_id,
        status="DONE",
        description="开发游戏核心引擎",
        ended_at=19.0,
    )
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_root),
        subagents=SimpleNamespace(
            list_runs_report=lambda: SimpleNamespace(runs=[child], load_errors=[])
        ),
    )

    activity = conversation_agent_activity(agent, store, "thread-progress")

    assert list(activity.task_progress_items) == [
        {"id": "qa", "title": "整合测试", "status": "done"}
    ]
    assert task_progress_items_for_task(agent, store, "task-live") == (
        {"id": "qa", "title": "整合测试", "status": "done"},
    )
    assert [item["id"] for item in read_task_progress(owner_root, ledger_id)["items"]] == [
        child_id,
        "qa",
    ]


def test_task_progress_projection_applies_limit_after_hiding_child_seeds(
    tmp_path: Path,
) -> None:
    """大量 child seed 被隐藏后，后面的普通 Todo 仍能填满展示上限。"""
    import hashlib

    from agent_py_agent.agent.conversation.agent_activity import (
        conversation_agent_activity,
    )
    from agent_py_agent.agent.task_progress import write_task_progress

    owner_root = tmp_path / "owner"
    task_path = str(tmp_path / "project" / "large")
    ledger_id = f"task-path:{hashlib.sha256(task_path.encode('utf-8')).hexdigest()[:16]}"
    child_ids = [f"child-{index}" for index in range(8)]
    ordinary_items = [
        {"id": f"todo-{index}", "title": f"普通事项 {index}", "status": "pending"}
        for index in range(128)
    ]
    write_task_progress(
        owner_root,
        ledger_id,
        {
            "items": [
                *(
                    {"id": child_id, "title": "自动派工项", "status": "done"}
                    for child_id in child_ids
                ),
                *ordinary_items,
            ]
        },
    )
    link = SimpleNamespace(
        task_id="task-live",
        task_path=task_path,
        status="active",
        created_at=20.0,
    )
    children = [
        _run(child_id, status="DONE", ended_at=19.0) for child_id in child_ids
    ]
    activity = conversation_agent_activity(
        SimpleNamespace(
            home_paths=SimpleNamespace(owner_home_dir=owner_root),
            subagents=SimpleNamespace(
                list_runs_report=lambda: SimpleNamespace(
                    runs=children,
                    load_errors=[],
                )
            ),
        ),
        SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: ([link], []))),
        "thread-progress",
    )

    assert len(activity.task_progress_items) == 128
    assert activity.task_progress_items[0]["id"] == "todo-0"
    assert activity.task_progress_items[-1]["id"] == "todo-127"
