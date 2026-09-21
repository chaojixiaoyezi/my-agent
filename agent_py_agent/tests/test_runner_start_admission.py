"""准确启动身份的确定性回归；使用临时文件、数据库和受控进程，不冒充真实 TUI 验收。"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.process_control import BackgroundStartUpdate
from agent_py_agent.agent.subagents.runner_start import (
    reserve_runner_start,
)


@pytest.fixture
def state(tmp_path):
    manager = SubAgentManager(tmp_path / "runs", owner_home_dir=str(tmp_path / "owner"))
    task = manager.create_run(goal="准确执行轮")
    repo = manager.runtime_db
    run = repo.agent_run_for_run_id(task.id)
    return manager, task, repo, str(run["agent_run_id"])


# LLM: 显式无数据库模式仍使用真实 canonical 文件与 creation guard；不能以 mock 空库代替正式模式。
# 函数用途: 给文件启动接纳回归提供隔离的任务管理器。
@pytest.fixture
def file_state(tmp_path):
    from agent_py_agent.agent.runtime_db.execution_mode import ExecutionMode

    manager = SubAgentManager(tmp_path / "file-runs", execution_mode=ExecutionMode.LOCAL_UNMANAGED.value)
    assert manager.runtime_db is None
    return manager, manager.create_run(goal="文件模式准确启动")


# LLM: 测试通过正式取消入口写原 reason/终态，且不请求系统进程清理；不要直接伪造可恢复状态。
# 函数用途: 停止一个临时文件模式任务，供迟到启动与显式恢复用例共用。
def _stop_file_run(manager, run_id, *, preserved=False):
    from agent_py_agent.agent.subagents.cancellation import (
        CancelSubagentTaskRequest,
        cancel_subagent_task,
    )

    result = cancel_subagent_task(SimpleNamespace(subagents=manager, conversation_store=None),
        CancelSubagentTaskRequest(manager.load(run_id), "conversation_user_stop", kill_process=False))
    assert result["ok"] and result["cancel_status"] == ("preserved" if preserved else "CANCELLED")


def test_file_queued_start_cannot_revive_stopped_task(file_state):
    manager, task = file_state
    expected = reserve_runner_start(manager, task.id)
    _stop_file_run(manager, task.id)
    stopped = manager.load(task.id)
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected)
    assert manager.load(task.id) == stopped


def test_file_exact_activation_has_only_one_winner(file_state):
    manager, task = file_state
    expected = reserve_runner_start(manager, task.id)

    def activate():
        try:
            return manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected).runner_active_attempt_id
        except RuntimeConflictError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: activate(), range(2)))
    assert results.count("rejected") == 1
    assert expected and expected in results


def test_file_explicit_resume_does_not_authorize_old_pending(file_state):
    manager, task = file_state
    old = reserve_runner_start(manager, task.id)
    _stop_file_run(manager, task.id)
    new = reserve_runner_start(manager, task.id, resume_user_stop=True)
    assert new and new != old
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=old)
    active = manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=new)
    assert active.status == "RUNNING" and active.runner_active_attempt_id == new
    assert old in active.runner_abandoned_attempt_ids


def test_file_second_stop_revokes_pending_while_business_status_is_still_cancelled(file_state):
    manager, task = file_state
    reserve_runner_start(manager, task.id)
    _stop_file_run(manager, task.id)
    new = reserve_runner_start(manager, task.id, resume_user_stop=True)
    _stop_file_run(manager, task.id, preserved=True)
    stopped = manager.load(task.id)
    assert new in stopped.runner_abandoned_attempt_ids
    assert stopped.attributes["background_start"]["status"] == "reclaimed"
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=new)
    assert manager.load(task.id) == stopped


@pytest.mark.parametrize("marker", ["running", "finished", "failed", "reclaimed"])
def test_file_marker_cannot_erase_consumption_after_active_pointer_is_cleared(file_state, marker):
    from agent_py_agent.agent.subagents.process_control import reclaim_background_start

    manager, task = file_state
    expected = reserve_runner_start(manager, task.id, launch_id="original")
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate(
        "original", "launching", replace_launch=True, attempt_id=expected))
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("original", "running", attempt_id=expected))
    active = manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected)
    activated_at = active.attributes["background_start"]["activated_at"]
    # 使用正式的结果字段收口，消费标记须独立于已清空的活动指针。
    from agent_py_agent.agent.subagents.runner_result_state import (
        RunnerAttemptParams,
        _apply_runner_attempt_fields,
    )

    active.status = "PENDING"
    _apply_runner_attempt_fields(RunnerAttemptParams(active, False, False, "已让出", activated_at + 1, "interrupted"))
    manager.save(active)
    if marker == "reclaimed":
        manager.mutate(task.id, reclaim_background_start)
    else:
        manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("original", marker, attempt_id=expected))
    before = manager.load(task.id)
    assert not before.runner_active_attempt_id
    assert before.attributes["background_start"]["activated_at"] == activated_at
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected)
    assert manager.load(task.id) == before


@pytest.mark.parametrize("snapshot_phase", ["before_reserve", "before_activate", "old_terminal"])
def test_file_old_full_save_cannot_undo_reservation_or_activation(file_state, snapshot_phase):
    manager, task = file_state
    old = manager.load(task.id)
    expected = reserve_runner_start(manager, task.id)
    if snapshot_phase == "old_terminal":
        _stop_file_run(manager, task.id)
        old = manager.load(task.id)
        expected = reserve_runner_start(manager, task.id, resume_user_stop=True)
    elif snapshot_phase == "before_activate":
        old = manager.load(task.id)
    active = manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected)
    manager.save(old)
    current = manager.load(task.id)
    assert current.status == "RUNNING" and current.runner_active_attempt_id == expected
    assert current.attributes["background_start"] == active.attributes["background_start"]
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected)


def test_file_old_reclaim_cannot_close_new_unactivated_reservation(file_state):
    from agent_py_agent.agent.subagents.process_control import reclaim_background_start

    manager, task = file_state
    reserve_runner_start(manager, task.id, launch_id="old")
    old = manager.load(task.id)
    _stop_file_run(manager, task.id)
    new = reserve_runner_start(manager, task.id, resume_user_stop=True, launch_id="new")
    reclaim_background_start(old)
    old.status = "CANCELLED"
    manager.save(old)
    current = manager.load(task.id)
    assert current.attributes["background_start"]["attempt_id"] == new
    assert current.attributes["background_start"]["status"] == "reserved"
    assert manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=new).runner_active_attempt_id == new


def test_file_full_save_can_only_reclaim_same_identity(file_state):
    from agent_py_agent.agent.subagents.process_control import reclaim_background_start

    manager, task = file_state
    expected = reserve_runner_start(manager, task.id)
    current = manager.load(task.id)
    current.attributes["background_start"]["attempt_id"] = "forged"
    manager.save(current)
    assert manager.load(task.id).attributes["background_start"]["attempt_id"] == expected
    current = manager.load(task.id)
    reclaim_background_start(current)
    manager.save(current)
    assert manager.load(task.id).attributes["background_start"]["status"] == "reclaimed"
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected)


def test_file_direct_start_invalidates_original_queued_identity(file_state):
    manager, task = file_state
    old = reserve_runner_start(manager, task.id)
    direct = manager.lifecycle.prepare_runner_attempt(task.id)
    assert direct.runner_active_attempt_id and direct.runner_active_attempt_id != old
    assert old in direct.runner_abandoned_attempt_ids
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=old)
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id)


def test_file_pending_cannot_be_rebound_to_different_launch(file_state):
    manager, task = file_state
    expected = reserve_runner_start(manager, task.id, launch_id="original")
    before = manager.load(task.id)
    with pytest.raises(RuntimeConflictError):
        reserve_runner_start(manager, task.id, launch_id="replacement")
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate(
            "replacement", "launching", replace_launch=True, attempt_id=expected))
    assert manager.load(task.id) == before


@pytest.mark.parametrize("status,reason", [("PENDING", "interrupted"), ("FAILED", "model_error")])
def test_file_real_result_path_releases_consumed_slot_for_new_attempt(file_state, status, reason):
    from agent_py_agent.agent.subagents.manager_runner_result_payload import (
        RecordRunnerResultParams,
    )

    manager, task = file_state
    old = reserve_runner_start(manager, task.id)
    manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=old)
    result = manager.runner_result.record_runner_result(RecordRunnerResultParams(
        task.id, False, False, "本轮已结束", attempt_id=old, status=status, turn_end_reason=reason,
    ))
    assert not result.ok
    closed = manager.load(task.id)
    assert not closed.runner_active_attempt_id
    assert closed.attributes["background_start"]["status"] == "reclaimed"
    new = reserve_runner_start(manager, task.id)
    assert new != old
    assert manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=new).runner_active_attempt_id == new


def test_file_independent_processes_share_one_consumption(file_state):
    manager, task = file_state
    expected = reserve_runner_start(manager, task.id)
    code = """
import sys
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.subagents.manager import SubAgentManager
manager = SubAgentManager(sys.argv[1], execution_mode='local_unmanaged')
try:
    task = manager.lifecycle.prepare_runner_attempt(sys.argv[2], expected_attempt_id=sys.argv[3])
    print(task.runner_active_attempt_id)
except RuntimeConflictError:
    print('rejected')
"""
    children = [subprocess.Popen([sys.executable, "-c", code, str(manager.workspace), task.id, expected],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        results = [child.communicate(timeout=20) for child in children]
        assert all(child.returncode == 0 for child in children), results
        assert sorted(output.strip() for output, _ in results) == sorted([expected, "rejected"])
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


@pytest.mark.parametrize("apply,start", [(False, False), (False, True), (True, False)])
def test_file_preview_or_plan_without_start_does_not_reserve(file_state, monkeypatch, apply, start):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import runner_batches as module
    from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams

    manager, task = file_state
    ctx = DispatchParams(apply=apply, start_runners=start)
    monkeypatch.setattr(module, "conversation_lifecycle_decisions", lambda *_a, **_k: {task.id: SimpleNamespace(allowed=True)})
    monkeypatch.setattr(module, "dry_runner_record", lambda *_a: {"preview": True})
    module.collect_runner_candidates(SimpleNamespace(subagents=manager), ctx, 2, [task])
    assert manager.load(task.id) == task
    assert ctx.expected_attempt_ids == {}


def test_file_candidate_collection_retains_original_supplied_identity(file_state, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import runner_batches as module
    from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams

    manager, task = file_state
    expected = reserve_runner_start(manager, task.id, launch_id="original")
    ctx = DispatchParams(apply=True, start_runners=True, background_launch_id="original",
                         expected_attempt_ids={task.id: expected})
    monkeypatch.setattr(module, "conversation_lifecycle_decisions", lambda *_a, **_k: {task.id: SimpleNamespace(allowed=True)})
    _, jobs = module.collect_runner_candidates(SimpleNamespace(subagents=manager), ctx, 2, [task])
    assert len(jobs) == 1 and ctx.expected_attempt_ids == {task.id: expected}
    _stop_file_run(manager, task.id)
    before = manager.load(task.id)
    with pytest.raises(RuntimeConflictError):
        module.collect_runner_candidates(SimpleNamespace(subagents=manager), ctx, 2, [task])
    assert manager.load(task.id) == before


def test_file_failed_start_can_record_channel_failure_without_reopening(file_state):
    manager, task = file_state
    expected = reserve_runner_start(manager, task.id, launch_id="original")
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate(
        "original", "launching", replace_launch=True, attempt_id=expected))
    failure = BackgroundStartUpdate("original", "failed", error="宿主启动失败", attempt_id=expected)
    manager.lifecycle.update_background_start(task.id, failure)
    manager.lifecycle.update_background_start(task.id, failure, channel_failure=True)
    closed = manager.load(task.id)
    assert closed.status == "CHANNEL_ERROR"
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(task.id, expected_attempt_id=expected)
    new = reserve_runner_start(manager, task.id, launch_id="next")
    assert new != expected


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
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("old", "launching", replace_launch=True, attempt_id=old))
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("old", "failed", attempt_id=old))
    repo.settle_agent_run(agent_run_id=run_id, attempt_id=old, status="cancelled")
    new = reserve_runner_start(manager, task.id)
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("new", "launching", replace_launch=True, attempt_id=new))
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("new", "running", pid=222, attempt_id=new))
    before = manager.load(task.id)
    for status in ("launching", "running", "finished", "failed"):
        with pytest.raises(RuntimeConflictError):
            manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate(
                "old", status, pid=111, attempt_id=old, replace_launch=status == "launching",
            ), channel_failure=status == "failed")
        assert manager.load(task.id) == before


def test_reclaimed_launch_cannot_be_marked_running(state):
    manager, task, _, _ = state
    expected = reserve_runner_start(manager, task.id)
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("launch", "launching", replace_launch=True, attempt_id=expected))
    manager.mutate(task.id, lambda current: current.attributes["background_start"].update(status="reclaimed"))
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("launch", "running", attempt_id=expected))
    assert manager.load(task.id).attributes["background_start"]["status"] == "reclaimed"


def test_delayed_marker_waits_for_creation_guard_and_rechecks_original_identity(state):
    manager, task, repo, run_id = state
    old = reserve_runner_start(manager, task.id)
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("old", "launching", replace_launch=True, attempt_id=old))
    started = threading.Event()

    def delayed():
        started.set()
        manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("old", "running", attempt_id=old))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with manager.creation_guard():
            future = executor.submit(delayed)
            assert started.wait(2)
            assert not future.done()
            manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("old", "failed", attempt_id=old))
            repo.settle_agent_run(agent_run_id=run_id, attempt_id=old, status="cancelled")
            new = reserve_runner_start(manager, task.id)
            manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("new", "launching", replace_launch=True, attempt_id=new))
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
    ["--expected-attempt", "run-a", ""],
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


def test_empty_attempt_is_rejected_in_both_execution_modes(state, file_state):
    manager, task, _, _ = state
    with pytest.raises(RuntimeConflictError):
        reserve_runner_start(manager, task.id, expected_attempt_id="")
    manager, task = file_state
    with pytest.raises(RuntimeConflictError):
        reserve_runner_start(manager, task.id, expected_attempt_id="")


@pytest.mark.parametrize("attempt", ["attempt-a", "attempt-file-a"])
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
        manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("launch", "launching", replace_launch=True, attempt_id=ids[task.id]))
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
    manager.lifecycle.update_background_start(task.id, BackgroundStartUpdate("original", "launching", replace_launch=True, attempt_id=expected))
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
    manager.lifecycle.update_background_start(old.id, BackgroundStartUpdate("old", "launching", replace_launch=True, attempt_id=old_attempt))
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
