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


# LLM: 外部连接边界用确定性替身，注册表/锁/权限视图和关闭路径仍运行正式代码；不启动真实服务。
# 函数用途: 构造可计数的MCP发现边界和真实工具注册表。
def _perf_a_registry(tmp_path, monkeypatch, *, deferred=True, running=True):
    from agent_py_agent.agent.tooling import registry as module
    from agent_py_agent.agent.tooling.mcp_registration import MCPProxyTool
    from agent_py_agent.agent.tooling.models import (
        EffectResolverPolicy,
        ToolModelSpec,
        ToolRuntimePolicy,
    )

    assert 'defer_mcp_connect' in module.ToolRegistryParams.__dataclass_fields__, 'DEFER_MCP_CONNECT_NOT_IMPLEMENTED'

    client = SimpleNamespace(tools_changed=False)
    stopped, connected = [], []
    transport = object()
    client.is_running = lambda: running and not stopped
    client.is_closed = lambda: bool(stopped)
    client.stop = lambda: stopped.append(True)
    client.connection = lambda: transport

    def connect(registry, servers):
        connected.append(servers)
        if running:
            registry.register(MCPProxyTool(client, 'lookup',
                ToolModelSpec(name='mcp__unit__lookup', description='unit lookup',
                              input_schema={'type': 'object', 'properties': {}}),
                ToolRuntimePolicy(effect_resolver=EffectResolverPolicy('read_only')), transport=transport))
        return [client]

    monkeypatch.setattr(module, '_connect_mcp_servers', connect)
    options = {} if deferred is None else {'defer_mcp_connect': deferred}
    params = module.ToolRegistryParams(workspace_root=tmp_path, max_chars=1000, max_entries=10,
        max_matches=10, web_max_chars=1000, http_timeout=10, catalog_limit=10, retrieval_limit=3,
        vector_search_enabled=False, mcp_servers={'unit': {'command': 'not-executed'}}, **options)
    return module.ToolRegistry(params), connected, stopped


@pytest.mark.parametrize('entry', ['ensure', 'prepare'])
def test_perf_a_registry_defers_initial_connection_and_never_revives(tmp_path, monkeypatch, entry):
    registry, connected, stopped = _perf_a_registry(tmp_path, monkeypatch)
    assert connected == [] and registry._mcp_clients == []
    assert 'mcp__unit__lookup' not in registry.tools
    if entry == 'ensure':
        assert registry.ensure_mcp_connected() is True
    else:
        registry.prepare_for_run()
    assert len(connected) == 1 and len(registry._mcp_clients) == 1
    assert 'mcp__unit__lookup' in registry.tools
    assert registry.ensure_mcp_connected() is False
    registry.close_mcp_clients()
    assert stopped and not registry._mcp_clients
    assert registry.ensure_mcp_connected() is False
    registry.prepare_for_run()
    assert len(connected) == 1


def test_perf_a_default_registry_still_connects_eagerly(tmp_path, monkeypatch):
    registry, connected, _ = _perf_a_registry(tmp_path, monkeypatch, deferred=None, running=False)
    try:
        assert len(connected) == 1 and registry._mcp_connected_state == [True]
        assert next(iter(registry._mcp_retry_state.values()))[0] == 1
        assert registry.ensure_mcp_connected() is False
    finally:
        registry.close_mcp_clients()


def test_perf_a_permission_views_share_one_initialization_and_close(tmp_path, monkeypatch):
    registry, connected, _ = _perf_a_registry(tmp_path, monkeypatch)
    view = registry.with_access_policy(access_mode='full-access', path_access_mode='full', owner_scope_root='')
    assert view is not registry
    for attribute in ('_mcp_clients', '_mcp_prepare_lock', '_mcp_closed', '_mcp_connected_state'):
        assert getattr(view, attribute) is getattr(registry, attribute)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda item: item.ensure_mcp_connected(), [registry, view, view, registry]))
        assert results.count(True) == 1 and len(connected) == 1
    finally:
        view.close_mcp_clients()
    assert registry.ensure_mcp_connected() is False


def test_perf_a_agent_catalog_syncs_even_if_another_caller_already_connected(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import CapabilityRouter
    from agent_py_agent.agent.core import SimpleAgent

    method = getattr(SimpleAgent, 'ensure_mcp_catalog_ready', None)
    assert callable(method), 'MCP_CATALOG_PREPARER_NOT_IMPLEMENTED'
    registry, connected, _ = _perf_a_registry(tmp_path, monkeypatch)
    own, other = CapabilityRouter(), CapabilityRouter()
    agent = SimpleNamespace(tools=registry, capability_router=own)
    try:
        assert registry.ensure_mcp_connected() is True
        method(agent, other)
        for router in (own, other):
            assert any(card.name == 'mcp__unit__lookup' for card in router.cards())
        method(agent, other)
        assert len(connected) == 1
    finally:
        registry.close_mcp_clients()


@pytest.mark.parametrize('statuses', [[], ['CLOSED'], ['OPEN', 'OPEN']])
def test_perf_a_capability_catalog_prepares_once_only_for_real_open_requests(monkeypatch, statuses):
    from agent_py_agent.agent.subagents.services import capability_service as module

    events = []
    tasks = [SimpleNamespace(capability_requests=[SimpleNamespace(status=status) for status in statuses])]
    service = module.SubAgentCapabilityService(SimpleNamespace(indexing=SimpleNamespace(select_runs=lambda _: tasks)))
    router = SimpleNamespace(_ensure_mcp_catalog=lambda: events.append('prepare'),
        search=lambda *_a, **_k: events.append('search') or [])
    monkeypatch.setattr(module, '_capability_request_query', lambda *_: 'unit lookup')
    monkeypatch.setattr(module, 'build_capability_route_report', lambda records, **_: records)
    monkeypatch.setattr(service, '_route_capability_request', lambda *_a, **_k: object())
    report = service.route_capability_requests(router)
    expected = ['prepare', 'search', 'search'] if 'OPEN' in statuses else []
    assert events == expected and len(report) == statuses.count('OPEN')


@pytest.mark.parametrize('flag', [None, False, True, 'yes'])
def test_perf_a_make_agent_keeps_single_argument_and_strict_boolean_opt_in(tmp_path, monkeypatch, flag):
    from agent_py_agent.cli import common

    calls, config = [], object()
    args = SimpleNamespace(_defer_mcp_connect=flag)
    monkeypatch.setattr(common, 'agent_configuration', lambda received: (config, [tmp_path]))
    monkeypatch.setattr(common, 'SimpleAgent', lambda *a, **kw: calls.append((a, kw)) or object())
    common.make_agent(args)
    assert calls[0][0] == (config, tmp_path)
    expected = {'workspace_roots': [tmp_path]}
    if flag is True:
        expected['defer_mcp_connect'] = True
    assert calls[0][1] == expected


# LLM: 单位置make_agent替身只模拟外部构造开销；CLI正式校验、派工及finally仍执行，原args不可被污染。
# 函数用途: 提供可观察构造和关闭次数的CLI控制面。
def _perf_a_cli_agent(tmp_path, monkeypatch):
    from agent_py_agent.cli import _dispatch

    received, connected, closed = [], [], []
    report = SimpleNamespace(summary={'total': 0}, records=[])
    agent = SimpleNamespace(subagents=SimpleNamespace(workspace=tmp_path),
        tools=SimpleNamespace(close_mcp_clients=lambda: closed.append(True)),
        dispatch_subagents=lambda *_a, **_k: report)

    def make(args):
        received.append(args)
        if getattr(args, '_defer_mcp_connect', None) is not True:
            connected.append(True)
        return agent

    monkeypatch.setattr(_dispatch, 'make_agent', make)
    monkeypatch.setattr(_dispatch, 'load_capability_config', lambda *_: None)
    monkeypatch.setattr(_dispatch, 'make_capability_router', lambda *_: None)
    return _dispatch, agent, received, connected, closed


@pytest.mark.parametrize('planner', [False, True])
def test_perf_a_dispatch_uses_deferred_copy_only_outside_planner_and_closes(tmp_path, monkeypatch, planner):
    module, _, received, connected, closed = _perf_a_cli_agent(tmp_path, monkeypatch)
    args = _parser().parse_args(['subagents-dispatch', *(['--planner'] if planner else [])])
    assert module.cmd_subagents_dispatch(args) == 0
    assert len(received) == 1
    assert getattr(received[0], '_defer_mcp_connect', None) is (None if planner else True)
    assert connected == ([True] if planner else [])
    assert closed == [True] and not hasattr(args, '_defer_mcp_connect')


@pytest.mark.parametrize('fault', ['config', 'dispatch', 'watch', 'close'])
def test_perf_a_cli_dispatch_closes_on_failure_without_masking_business(tmp_path, monkeypatch, fault):
    module, agent, _, _, closed = _perf_a_cli_agent(tmp_path, monkeypatch)
    args = _parser().parse_args(['subagents-dispatch', *(['--watch'] if fault == 'watch' else [])])

    def fail(*_a, **_kw):
        raise RuntimeError('unit failure')

    if fault == 'close':
        agent.tools.close_mcp_clients = fail
        assert module.cmd_subagents_dispatch(args) == 0
        return
    if fault == 'config':
        monkeypatch.setattr(module, 'load_capability_config', fail)
    else:
        setattr(agent, 'watch_subagents' if fault == 'watch' else 'dispatch_subagents', fail)
    if fault == 'watch':
        assert module.cmd_subagents_dispatch(args) == 2
    else:
        with pytest.raises(RuntimeError, match='unit failure'):
            module.cmd_subagents_dispatch(args)
    assert closed == [True]


@pytest.mark.parametrize('execute', [False, True])
def test_perf_a_cli_run_defers_only_execute_outer_agent_and_always_closes(tmp_path, monkeypatch, execute):
    module, _, received, connected, closed = _perf_a_cli_agent(tmp_path, monkeypatch)
    result = SimpleNamespace(ok=True, run_id='unit', status='DONE', verification_status='passed', message='',
        execution_context_json='', result_json='', result_file='', prompt_file='', response_file='')
    monkeypatch.setattr(module, '_execute_subagent_run_cli', lambda *_: result)
    args = SimpleNamespace(execute=execute)
    assert module.cmd_subagent_run(args) == 0
    assert getattr(received[0], '_defer_mcp_connect', None) is (True if execute else None)
    assert connected == ([] if execute else [True])
    assert closed == [True] and not hasattr(args, '_defer_mcp_connect')


@pytest.mark.parametrize('view_first', [False, True])
def test_perf_a_deferred_initial_catalog_is_available_in_every_permission_view(tmp_path, monkeypatch, view_first):
    registry, connected, _ = _perf_a_registry(tmp_path, monkeypatch)
    view = registry.with_access_policy(access_mode='full-access', path_access_mode='full', owner_scope_root='')
    first, second = (view, registry) if view_first else (registry, view)
    try:
        assert first.ensure_mcp_connected() is True
        assert second.ensure_mcp_connected() is False
        assert 'mcp__unit__lookup' in {spec.name for spec in first.specs()}
        assert 'mcp__unit__lookup' in {spec.name for spec in second.specs()}
        assert len(connected) == 1
    finally:
        registry.close_mcp_clients()


def test_perf_a_concurrent_initialization_waits_for_complete_catalog(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import registry as module

    registry, connected, _ = _perf_a_registry(tmp_path, monkeypatch)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    connect = module._connect_mcp_servers

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return connect(*args)

    def ensure_again():
        second_started.set()
        return registry.ensure_mcp_connected()

    monkeypatch.setattr(module, '_connect_mcp_servers', delayed)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(registry.ensure_mcp_connected)
            try:
                assert entered.wait(3)
                second = executor.submit(ensure_again)
                assert second_started.wait(3)
                with pytest.raises(TimeoutError):
                    second.result(timeout=0.1)
            finally:
                release.set()
            assert first.result(timeout=3) is True
            assert second.result(timeout=3) is False
        assert 'mcp__unit__lookup' in registry.tools and len(connected) == 1
    finally:
        registry.close_mcp_clients()


@pytest.mark.parametrize('deferred', [False, True])
def test_perf_a_real_agent_forwards_instance_policy_without_changing_server_config(tmp_path, monkeypatch, deferred):
    from agent_py_agent.agent import core
    from agent_py_agent.agent.settings import AgentConfig

    prototype, connected, _ = _perf_a_registry(tmp_path, monkeypatch)
    config = AgentConfig(my_agent_home=str(tmp_path / 'home'), enable_plugins=False,
                         mcp_servers={'unit': {'command': 'not-executed'}})
    monkeypatch.setattr(core, 'get_backend', lambda *_: SimpleNamespace())
    agent = None
    try:
        agent = core.SimpleAgent(config, tmp_path / 'project', defer_mcp_connect=deferred)
        assert agent.tools._construction_params.defer_mcp_connect is deferred
        assert len(connected) == (0 if deferred else 1)
        assert config.mcp_servers == {'unit': {'command': 'not-executed'}}
        agent.ensure_mcp_catalog_ready()
        assert len(connected) == 1
        assert any(card.name == 'mcp__unit__lookup' for card in agent.capability_router.cards())
        assert callable(agent.capability_router._ensure_mcp_catalog)
    finally:
        if agent is not None:
            agent.tools.close_mcp_clients()
        prototype.close_mcp_clients()


def test_perf_a_make_agent_does_not_treat_mock_attribute_as_true(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from agent_py_agent.cli import common

    calls = []
    monkeypatch.setattr(common, 'agent_configuration', lambda _: (object(), [tmp_path]))
    monkeypatch.setattr(common, 'SimpleAgent', lambda *a, **kw: calls.append(kw))
    common.make_agent(MagicMock())
    assert calls == [{'workspace_roots': [tmp_path]}]


def test_perf_a_cli_router_callback_is_lazy_and_targets_the_given_router():
    from agent_py_agent.agent.capability import CapabilityRouter
    from agent_py_agent.cli.common import make_capability_router

    prepared, roots = [], []
    router = CapabilityRouter()
    agent = SimpleNamespace(capability_router=router, skills_service=SimpleNamespace(set_extra_roots=roots.append),
                            ensure_mcp_catalog_ready=prepared.append)
    assert make_capability_router(agent, object(), ['unit-skills']) is router
    assert roots == [['unit-skills']] and prepared == []
    router._ensure_mcp_catalog()
    assert prepared == [router]


def test_perf_a_close_before_initialization_never_connects(tmp_path, monkeypatch):
    registry, connected, _ = _perf_a_registry(tmp_path, monkeypatch)
    registry.close_mcp_clients()
    assert registry.ensure_mcp_connected() is False
    registry.prepare_for_run()
    assert not connected


def test_perf_a_copy_failure_keeps_original_args_and_still_closes(tmp_path, monkeypatch):
    module, _, received, connected, closed = _perf_a_cli_agent(tmp_path, monkeypatch)

    class Uncopyable(argparse.Namespace):
        def __copy__(self):
            raise TypeError('unit copy refusal')

    args = Uncopyable(**vars(_parser().parse_args(['subagents-dispatch'])))
    assert module.cmd_subagents_dispatch(args) == 0
    assert received == [args] and connected == [True] and closed == [True]
    assert not hasattr(args, '_defer_mcp_connect')


def test_perf_a_run_worker_stays_separate_and_outer_closes_after_worker(tmp_path, monkeypatch):
    module, agent, _, connected, closed = _perf_a_cli_agent(tmp_path, monkeypatch)
    agent.config, agent.root = object(), tmp_path
    args = SimpleNamespace(execute=True, run_id='unit', instruction='', max_cards=0, no_probe=False)
    result = SimpleNamespace(ok=True, run_id='unit', status='DONE', verification_status='passed', message='',
        execution_context_json='', result_json='', result_file='', prompt_file='', response_file='')
    workers = []

    def work(params):
        assert closed == []
        workers.append(params)
        return result

    monkeypatch.setattr(module, '_cli_subagent_run_timeout', lambda *_: 1.25)
    monkeypatch.setattr(module, '_run_subagent_worker', work)
    assert module.cmd_subagent_run(args) == 0
    assert len(workers) == 1 and workers[0].config is agent.config
    assert workers[0].timeout_seconds == 1.25
    assert connected == [] and closed == [True]
