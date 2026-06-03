from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.cli.dispatch_background import (
    BackgroundLaunchUpdate,
    mark_background_launch,
)
from agent_py_agent.cli.models import SubagentsDispatchOptions


def _dispatch_options() -> SubagentsDispatchOptions:
    return SubagentsDispatchOptions(
        mutate_state=True,
        start_runners=True,
        planner=False,
        workflow_mode="off",
        max_runners=1,
        limit=20,
        reviewer="test",
        note="",
        instruction="",
        max_cards=0,
        probe=True,
        take_over_by="",
        locked_files=[],
        interval=0.0,
        max_cycles=0,
        advance=False,
        force_lock=False,
        watch=False,
        run_ids=["run_a"],
        background_launch_id="launch-1",
    )


def test_background_launch_marker_updates_task_tree() -> None:
    """后台 dispatch 进程要把生命周期写回任务树，父代理才能查到启动状态。"""
    task = SimpleNamespace(id="run_a", attributes={})
    manager = MagicMock()
    manager.load.return_value = task
    agent = SimpleNamespace(subagents=manager)

    report = mark_background_launch(agent, _dispatch_options(), BackgroundLaunchUpdate("running"))

    assert report.ok is True
    assert task.attributes["background_start"]["launch_id"] == "launch-1"
    assert task.attributes["background_start"]["status"] == "running"
    manager.save.assert_called_once_with(task)


def test_background_launch_marker_reports_task_load_failures() -> None:
    """后台启动标记不能把账本读取失败静默解释成没有任务。"""
    manager = MagicMock()
    manager.load.side_effect = ValueError("bad task json")
    agent = SimpleNamespace(subagents=manager)

    report = mark_background_launch(agent, _dispatch_options(), BackgroundLaunchUpdate("running"))

    assert report.ok is False
    assert report.load_errors[0]["run_id"] == "run_a"
    assert report.load_errors[0]["context"] == "background_launch.task.load"
    assert report.load_errors[0]["category"] == "data_parse"
    manager.save.assert_not_called()
