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
        )
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
    assert payload["schema_version"] == "conversation_agent_activity.v4"
    assert payload["active_task_projection_ok"] is True
    assert payload["subagent_projection_ok"] is True


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


def test_conversation_agent_activity_reads_live_child_context_and_compact_ledgers(
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
            }
        },
    )
    store = SimpleNamespace(
        active_task_links_report=lambda _thread_id: (
            [SimpleNamespace(task_id="task-live", status="active")],
            [],
        )
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
