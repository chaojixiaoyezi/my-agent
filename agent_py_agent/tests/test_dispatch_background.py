from __future__ import annotations

import os
from types import SimpleNamespace

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.process_control import BackgroundStartUpdate
from agent_py_agent.agent.subagents.runner_start import (
    reserve_runner_start,
)
from agent_py_agent.cli.dispatch_background import (
    BackgroundLaunchUpdate,
    mark_background_launch,
)
from agent_py_agent.cli.models import SubagentsDispatchOptions


def _dispatch_options(run_id, attempt_id) -> SubagentsDispatchOptions:
    return SubagentsDispatchOptions(
        mutate_state=True,
        start_runners=True,
        planner=False,
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
        run_ids=[run_id],
        expected_attempt_ids={run_id: attempt_id},
        background_launch_id="launch-1",
    )


def _state(tmp_path):
    manager = SubAgentManager(tmp_path / "runs", owner_home_dir=str(tmp_path / "owner"))
    task = manager.create_run(goal="CLI 标记")
    attempt_id = reserve_runner_start(manager, task.id)
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate(
        "launch-1", "launching", replace_launch=True, attempt_id=attempt_id,
    ))
    return manager, task, _dispatch_options(task.id, attempt_id)


def test_background_launch_marker_updates_task_tree(tmp_path):
    manager, task, options = _state(tmp_path)
    report = mark_background_launch(SimpleNamespace(subagents=manager), options, BackgroundLaunchUpdate("running"))
    assert report.ok
    record = manager.load(task.id).attributes["background_start"]
    assert record["launch_id"] == "launch-1"
    assert record["attempt_id"] == options.expected_attempt_ids[task.id]
    assert record["status"] == "running"
    assert record["pid"] == os.getpid()


def test_stale_background_launch_cannot_overwrite_replacement(tmp_path):
    manager, task, options = _state(tmp_path)
    manager.mutate(task.id, lambda current: current.attributes["background_start"].update(launch_id="launch-2", pid=222))
    before = manager.load(task.id)
    report = mark_background_launch(SimpleNamespace(subagents=manager), options, BackgroundLaunchUpdate("finished"))
    assert not report.ok
    assert manager.load(task.id) == before


def test_background_launch_marker_reports_task_load_failures(tmp_path, monkeypatch):
    manager, task, options = _state(tmp_path)
    monkeypatch.setattr(manager, "mutate", lambda *_: (_ for _ in ()).throw(ValueError("bad task json")))
    report = mark_background_launch(SimpleNamespace(subagents=manager), options, BackgroundLaunchUpdate("running"))
    assert not report.ok
    assert report.save_errors[0]["run_id"] == task.id
    assert report.save_errors[0]["context"] == "background_launch.task.update"
    assert report.save_errors[0]["category"] == "data_parse"
