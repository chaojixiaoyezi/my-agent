"""准确启动身份的确定性回归；使用临时数据库和替身，不冒充真实 TUI 验收。"""
from __future__ import annotations

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.process_control import BackgroundStartUpdate
from agent_py_agent.agent.subagents.runner_start import (
    reserve_runner_start,
    update_background_start,
)


@pytest.fixture
def state(tmp_path):
    manager = SubAgentManager(tmp_path / "runs", owner_home_dir=str(tmp_path / "owner"))
    task = manager.create_run(goal="准确执行轮")
    repo = manager.runtime_db
    run = repo.agent_run_for_run_id(task.id)
    return manager, task, repo, str(run["agent_run_id"])


def test_exact_pending_activation_never_follows_replacement(state):
    manager, task, repo, run_id = state
    original = reserve_runner_start(manager, task.id)
    repo.settle_agent_run(agent_run_id=run_id, attempt_id=original, status="cancelled")
    replacement = repo.queue_pending_attempt(run_id, source="explicit_user_resume")["attempt_id"]
    before = manager.load(task.id)
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=original)
    assert manager.load(task.id) == before
    assert repo.current_attempt(run_id)["attempt_id"] == replacement
    assert repo.current_attempt(run_id)["status"] == "pending"
    assert len(repo.attempts_for_run(run_id)) == 2
    assert not repo.has_active_exec_lock(run_id)


@pytest.mark.parametrize("status", ["cancelled", "done", "failed", "unknown"])
def test_exact_activation_does_not_reopen_closed_or_unknown_attempt(state, status):
    manager, task, repo, run_id = state
    expected = reserve_runner_start(manager, task.id)
    with repo._runtime_connection() as conn:
        conn.execute("UPDATE agent_attempts SET status = ?, ended_at = 1 WHERE attempt_id = ?", (status, expected))
        conn.commit()
    before = [dict(row) for row in repo.attempts_for_run(run_id)]
    with pytest.raises(RuntimeConflictError):
        repo.create_attempt(run_id, reuse_pending=True, expected_pending_attempt_id=expected)
    assert [dict(row) for row in repo.attempts_for_run(run_id)] == before
    assert not repo.has_active_exec_lock(run_id)


def test_two_exact_activations_have_one_winner(state):
    manager, task, repo, run_id = state
    expected = reserve_runner_start(manager, task.id)

    def activate():
        try:
            return repo.create_attempt(run_id, reuse_pending=True, expected_pending_attempt_id=expected)["attempt_id"]
        except RuntimeConflictError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: activate(), range(2)))
    assert sorted(results) == sorted([expected, "rejected"])
    assert len(repo.attempts_for_run(run_id)) == 1


def test_late_launch_and_failure_cannot_overwrite_new_attempt(state):
    manager, task, repo, run_id = state
    old = reserve_runner_start(manager, task.id)
    update_background_start(manager, task.id, BackgroundStartUpdate("old", "launching", replace_launch=True, attempt_id=old))
    update_background_start(manager, task.id, BackgroundStartUpdate("old", "failed", attempt_id=old))
    repo.settle_agent_run(agent_run_id=run_id, attempt_id=old, status="cancelled")
    new = reserve_runner_start(manager, task.id)
    update_background_start(manager, task.id, BackgroundStartUpdate("new", "launching", replace_launch=True, attempt_id=new))
    update_background_start(manager, task.id, BackgroundStartUpdate("new", "running", pid=222, attempt_id=new))
    before = manager.load(task.id)
    for status in ("launching", "running", "finished", "failed"):
        with pytest.raises(RuntimeConflictError):
            update_background_start(manager, task.id, BackgroundStartUpdate(
                "old", status, pid=111, attempt_id=old, replace_launch=status == "launching",
            ), channel_failure=status == "failed")
        assert manager.load(task.id) == before


def test_reclaimed_launch_cannot_be_marked_running(state):
    manager, task, _, _ = state
    expected = reserve_runner_start(manager, task.id)
    update_background_start(manager, task.id, BackgroundStartUpdate("launch", "launching", replace_launch=True, attempt_id=expected))
    manager.mutate(task.id, lambda current: current.attributes["background_start"].update(status="reclaimed"))
    with pytest.raises(RuntimeConflictError):
        update_background_start(manager, task.id, BackgroundStartUpdate("launch", "running", attempt_id=expected))
    assert manager.load(task.id).attributes["background_start"]["status"] == "reclaimed"


def test_delayed_marker_waits_for_creation_guard_and_rechecks_original_identity(state):
    manager, task, repo, run_id = state
    old = reserve_runner_start(manager, task.id)
    update_background_start(manager, task.id, BackgroundStartUpdate("old", "launching", replace_launch=True, attempt_id=old))
    started = threading.Event()

    def delayed():
        started.set()
        update_background_start(manager, task.id, BackgroundStartUpdate("old", "running", attempt_id=old))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with manager.creation_guard():
            future = executor.submit(delayed)
            assert started.wait(2)
            assert not future.done()
            update_background_start(manager, task.id, BackgroundStartUpdate("old", "failed", attempt_id=old))
            repo.settle_agent_run(agent_run_id=run_id, attempt_id=old, status="cancelled")
            new = reserve_runner_start(manager, task.id)
            update_background_start(manager, task.id, BackgroundStartUpdate("new", "launching", replace_launch=True, attempt_id=new))
            before = manager.load(task.id)
        with pytest.raises(RuntimeConflictError):
            future.result(timeout=3)
    assert manager.load(task.id) == before


def test_old_session_cannot_publish_or_heartbeat_over_new_attempt(state):
    from agent_py_agent.agent.agent_core.runner.session_pool import (
        RunnerSessionPoolLease,
        runner_session_lease,
    )

    manager, task, repo, run_id = state
    old = reserve_runner_start(manager, task.id)
    manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=old)
    repo.settle_agent_attempt(agent_run_id=run_id, attempt_id=old)
    manager.lifecycle.prepare_runner_attempt(task.id)
    before = manager.load(task.id)
    with pytest.raises(RuntimeConflictError):
        with runner_session_lease(RunnerSessionPoolLease(manager, task.id, attempt_id=old)):
            pytest.fail("旧轮不得进入模型执行")
    assert manager.load(task.id) == before


def test_original_user_stop_eligibility_does_not_authorize_old_start(state, monkeypatch):
    from agent_py_agent.agent.subagents.recovery_eligibility import user_stopped_run_is_resumable
    from agent_py_agent.agent.subagents.services import lifecycle_runner_attempts as lifecycle

    manager, task, repo, run_id = state
    expected = reserve_runner_start(manager, task.id)
    repo.settle_agent_run(agent_run_id=run_id, attempt_id=expected, status="cancelled")

    def stop(current):
        current.status = "CANCELLED"
        current.failure_type = "cancelled"
        current.attributes["cancel_subagents"] = {"reason": "conversation_user_stop", "previous_status": "PENDING"}

    manager.mutate(task.id, stop)
    assert user_stopped_run_is_resumable(manager.load(task.id))
    monkeypatch.setattr(lifecycle, "_reactivate_user_stopped_conversation_link", lambda *_: pytest.fail("旧身份不能恢复会话"))
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected)
    assert manager.load(task.id).status == "CANCELLED"
    assert len(repo.attempts_for_run(run_id)) == 1


def test_rejected_worker_does_not_publish_session_or_result(state, monkeypatch, tmp_path):
    from agent_py_agent.agent.agent_core.runner import worker as module
    from agent_py_agent.agent.settings import AgentConfig

    manager, task, repo, run_id = state
    expected = reserve_runner_start(manager, task.id)
    repo.settle_agent_run(agent_run_id=run_id, attempt_id=expected, status="cancelled")
    monkeypatch.setattr(module, "_build_worker_agent", lambda *_: SimpleNamespace(subagents=manager))
    monkeypatch.setattr(module, "runner_session_lease", lambda *_: pytest.fail("拒绝后不能发布租约"))
    result = module._run_subagent_worker(module.RunSubagentWorkerParams(
        AgentConfig(), tmp_path, task.id, "", False, 0, False, "", expected_attempt_id=expected,
    ))
    assert not result.ok
    assert repo.current_attempt(run_id)["status"] == "cancelled"
    assert "runner_session" not in manager.load(task.id).attributes


def test_initial_session_write_failure_closes_owned_attempt(state, monkeypatch, tmp_path):
    from agent_py_agent.agent.agent_core.runner import worker as module
    from agent_py_agent.agent.settings import AgentConfig

    manager, task, repo, run_id = state
    expected = reserve_runner_start(manager, task.id)
    worker = SimpleNamespace(subagents=manager, config=AgentConfig(), run_subagent=lambda **_: pytest.fail("未发布租约不能执行模型"))
    monkeypatch.setattr(module, "_build_worker_agent", lambda *_: worker)
    monkeypatch.setattr(manager, "save_runner_session", lambda *_a, **_k: (_ for _ in ()).throw(OSError("storage unavailable")))
    result = module._run_subagent_worker(module.RunSubagentWorkerParams(
        worker.config, tmp_path, task.id, "", False, 0, False, "", expected_attempt_id=expected,
    ))
    assert not result.ok
    assert repo.current_attempt(run_id)["status"] == "failed"
    assert not repo.has_active_exec_lock(run_id)


def _parser():
    from agent_py_agent.cli.subagents import add_subagents_subcommands

    parser = argparse.ArgumentParser()
    add_subagents_subcommands(parser.add_subparsers())
    return parser


def test_cli_transports_exact_pair_into_dispatch_params():
    from agent_py_agent.cli._dispatch import _dispatch_params, _subagents_dispatch_options

    args = _parser().parse_args([
        "subagents-dispatch", "--apply", "--start-runners", "--run-id", "run-a",
        "--background-launch-id", "launch-a", "--expected-attempt", "run-a", "attempt-a",
    ])
    params = _dispatch_params(_subagents_dispatch_options(args))
    assert params.expected_attempt_ids == {"run-a": "attempt-a"}
    assert params.background_launch_id == "launch-a"
    assert not params.runner_instruction


@pytest.mark.parametrize("extra", [
    [], ["--expected-attempt", "run-other", "attempt-a"],
    ["--expected-attempt", "run-a", "attempt-a", "--expected-attempt", "run-a", "attempt-a"],
    ["--expected-attempt", "run-a", "attempt-a", "--watch"],
])
def test_invalid_internal_cli_launch_has_no_runtime_side_effects(extra, monkeypatch):
    from agent_py_agent.cli import _dispatch

    args = _parser().parse_args([
        "subagents-dispatch", "--apply", "--start-runners", "--run-id", "run-a",
        "--background-launch-id", "launch-a", *extra,
    ])
    monkeypatch.setattr(_dispatch, "make_agent", lambda *_: pytest.fail("参数拒绝必须先于构造宿主"))
    assert _dispatch.cmd_subagents_dispatch(args) == 2


def test_empty_attempt_is_only_valid_in_explicit_unmanaged_mode(state):
    manager, task, _, _ = state
    with pytest.raises(RuntimeConflictError):
        reserve_runner_start(manager, task.id, expected_attempt_id="")


@pytest.mark.parametrize("attempt", ["attempt-a", ""])
def test_actual_host_command_round_trip_preserves_binding(tmp_path, attempt):
    from agent_py_agent.agent.agent_core.orchestration.background.dispatch import (
        _background_dispatch_command,
        _BackgroundDispatchRequest,
    )
    from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
    from agent_py_agent.cli._dispatch import _dispatch_params, _subagents_dispatch_options

    agent = SimpleNamespace(root=tmp_path, config=SimpleNamespace(config_path=str(tmp_path / "config.yaml")))
    request = _BackgroundDispatchRequest(agent, ["run-a"], "launch-a", None, None,
                                         DispatchParams(expected_attempt_ids={"run-a": attempt}))
    command = _background_dispatch_command(agent, request)
    args = _parser().parse_args(command[command.index("subagents-dispatch"):])
    params = _dispatch_params(_subagents_dispatch_options(args))
    assert params.expected_attempt_ids == {"run-a": attempt}
    assert params.include_run_ids == ["run-a"]
    assert params.background_launch_id == "launch-a"


def test_normal_watch_has_no_frozen_launch_identity():
    from agent_py_agent.cli._dispatch import _subagents_dispatch_options, _watch_params

    args = _parser().parse_args(["subagents-dispatch", "--watch", "--run-id", "run-a"])
    params = _watch_params(_subagents_dispatch_options(args))
    assert params.expected_attempt_ids is None
    assert params.include_run_ids == ["run-a"]
    assert not params.background_launch_id
    from agent_py_agent.agent.agent_core.orchestration.dispatch.params import WatchParams

    with pytest.raises(ValueError):
        WatchParams(expected_attempt_ids={"run-a": "attempt-a"})


def test_cli_partial_marker_failure_closes_only_its_accepted_records(state, monkeypatch):
    from agent_py_agent.cli import _dispatch

    manager, first, repo, run_id = state
    second = manager.create_run(goal="另一轮已替换")
    ids = {task.id: reserve_runner_start(manager, task.id) for task in (first, second)}
    for task in (first, second):
        update_background_start(manager, task.id, BackgroundStartUpdate("launch", "launching", replace_launch=True, attempt_id=ids[task.id]))
    manager.mutate(second.id, lambda current: current.attributes["background_start"].update(launch_id="replacement"))
    before = manager.load(second.id)
    argv = ["subagents-dispatch", "--apply", "--start-runners", "--background-launch-id", "launch"]
    for key, value in ids.items():
        argv += ["--run-id", key, "--expected-attempt", key, value]
    agent = SimpleNamespace(subagents=manager, config=None, dispatch_subagents=lambda *_a, **_k: pytest.fail("不得部分派工"))
    monkeypatch.setattr(_dispatch, "make_agent", lambda *_: agent)
    monkeypatch.setattr(_dispatch, "load_capability_config", lambda *_: None)
    monkeypatch.setattr(_dispatch, "make_capability_router", lambda *_: None)
    assert _dispatch.cmd_subagents_dispatch(_parser().parse_args(argv)) == 2
    assert manager.load(first.id).attributes["background_start"]["status"] == "failed"
    assert manager.load(second.id) == before
    assert repo.current_attempt(run_id)["status"] == "pending"


def test_repeated_start_reuses_exact_admission_without_new_worker(state, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch
    from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams

    manager, task, repo, run_id = state
    expected = reserve_runner_start(manager, task.id)
    update_background_start(manager, task.id, BackgroundStartUpdate("original", "launching", replace_launch=True, attempt_id=expected))
    agent = SimpleNamespace(subagents=manager)
    monkeypatch.setattr(dispatch, "_auto_start_dispatch_args", lambda *_a, **_k: (None, None, DispatchParams()))
    monkeypatch.setattr(dispatch, "_start_inprocess_dispatch", lambda *_: pytest.fail("不得重复启动"))
    monkeypatch.setattr(dispatch, "_spawn_background_dispatch_process", lambda *_: pytest.fail("不得重复启动"))
    result = dispatch._start_background_dispatch(agent, [task.id])
    assert result["status"] == "started"
    assert result["reused_run_ids"] == [task.id]
    assert manager.load(task.id).attributes["background_start"]["launch_id"] == "original"
    assert len(repo.attempts_for_run(run_id)) == 1


@pytest.mark.parametrize("fail_new_marker", [False, True])
def test_mixed_batch_keeps_existing_launch_and_only_admits_new_child(state, monkeypatch, fail_new_marker):
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch
    from agent_py_agent.agent.agent_core.orchestration.create_payload import _pending_start_tasks
    from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams

    manager, old, _, _ = state
    old_attempt = reserve_runner_start(manager, old.id)
    update_background_start(manager, old.id, BackgroundStartUpdate("old", "launching", replace_launch=True, attempt_id=old_attempt))
    new = manager.create_run(goal="本批新孩子")
    before = manager.load(old.id)
    agent = SimpleNamespace(subagents=manager)
    monkeypatch.setattr(dispatch, "_auto_start_dispatch_args", lambda *_a, **_k: (None, None, DispatchParams()))
    monkeypatch.setattr(dispatch, "_use_inprocess_autostart", lambda *_: True)
    started = []

    def start(_agent, request, _errors):
        assert request.run_ids == request.params.include_run_ids == [new.id]
        assert set(request.params.expected_attempt_ids) == {new.id}
        started.extend(request.run_ids)
        return {"status": "started", "run_ids": request.run_ids}

    monkeypatch.setattr(dispatch, "_start_inprocess_dispatch", start)
    if fail_new_marker:
        original = manager.mutate
        def mutate(run_id, reducer):
            if run_id == new.id:
                raise OSError("marker unavailable")
            return original(run_id, reducer)
        monkeypatch.setattr(manager, "mutate", mutate)
    result = dispatch._start_background_dispatch(agent, [old.id, new.id])
    assert manager.load(old.id) == before
    assert result["reused_run_ids"] == [old.id]
    assert started == ([] if fail_new_marker else [new.id])
    assert _pending_start_tasks([old, new], {}, result) == ([new] if fail_new_marker else [])
