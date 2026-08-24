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

    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: (
            [active_link, interrupted_link],
            [],
        ),
        load_thread_report=lambda _thread_id: (
            SimpleNamespace(compact_generation=3),
            None,
        ),
    )
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
    assert payload["schema_version"] == "conversation_agent_activity.v5"
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

    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )
    )

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

    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )
    )

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
        SimpleNamespace(active_task_links_report=_unavailable),
        "thread-1",
    )

    assert activity.active_task_count == 0
    assert activity.active_task_projection_ok is False
    assert activity.subagent_projection_ok is True
    assert activity.warnings == ("conversation_task_links_unavailable",)


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
    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: ([], []),
        load_thread_report=lambda _thread_id: (
            SimpleNamespace(compact_generation=2, workspace_task_id="task-live"),
            None,
        ),
        load_task_link_report=lambda _task_id: (link, None),
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
        "thread-1",
    )

    assert activity.active_task_count == 0
    assert [row["run_id"] for row in activity.subagents] == ["child-done"]
    assert activity.main_activity == {}
    assert activity.compact_count == 2


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
        attributes={"model_visible_context_usage": usage},
        ended_at=22.0,
    )
    grandchild = _run(
        "grandchild",
        root_id="task-live",
        parent_id="child-done",
        depth=2,
    )
    store = SimpleNamespace(
        root=tmp_path,
        load_thread_report=lambda _thread_id: (
            SimpleNamespace(compact_generation=1),
            None,
        ),
        recent_messages_report=lambda _thread_id, *, limit: (
            [SimpleNamespace(role="assistant", content="已提交关卡设计。")],
            [],
        ),
    )
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
    assert [row["run_id"] for row in view["children"]] == ["grandchild"]
    assert view["final_response"] == "已提交关卡设计。"

    child.status = "RUNNING"
    running_view = conversation_agent_view(agent, store, child.id)
    assert running_view["terminal"] is False
    assert running_view["final_response"] == ""


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
    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        ),
        load_thread_report=lambda thread_id: (
            SimpleNamespace(
                compact_generation=(2 if thread_id == task.agent_thread_id else 0),
                compact_checkpoint_id=(
                    "compact-child-2" if thread_id == task.agent_thread_id else ""
                ),
            ),
            None,
        ),
    )
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
    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )
    )
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
    sink.write_progress({"tool": "run_command", "phase": "started"})
    activity = conversation_agent_activity(agent, store, "thread-main")
    assert activity.main_activity["activity"] == "正在使用 run_command"
    sink.finish()
    activity = conversation_agent_activity(agent, store, "thread-main")
    assert activity.main_activity["phase"] == "waiting"
    assert activity.main_activity["activity"] == "等待后续事件"
    assert activity.main_activity["context_usage"]["current_tokens"] == 42_100


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
    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: ([link], []),
        load_task_link=lambda _task_id: link,
    )
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
    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: ([root_link, child_link], []),
        load_thread_report=lambda _thread_id: (
            SimpleNamespace(workspace_task_id="task-root", compact_generation=0),
            None,
        ),
    )
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
    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: ([link], []),
        load_task_link=lambda _task_id: link,
    )
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
        SimpleNamespace(active_task_links_report=lambda _thread_id: ([link], [])),
        "thread-progress",
    )

    assert len(activity.task_progress_items) == 128
    assert activity.task_progress_items[0]["id"] == "todo-0"
    assert activity.task_progress_items[-1]["id"] == "todo-127"
