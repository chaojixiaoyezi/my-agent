"""I5（2026-10-02）：已预留执行轮、runner 还没启动的子代理，宿主停机后不再启动，重启后照样被拉起。

背景（9b 探针，证据 ~/.my-agent/decision-evidence/i5-restart-pickup-probe-20261002/）：
- 派工线程跑完、启动记录收尾成 finished 的，重启后孤儿监督第一轮就拉起；
- 进程在派工线程收尾前没了（进程内线程没有 pid，或派工子进程 pid 已死），启动记录冻在 running：候选判定已按过期放行，
  但 existing_runner_launch 不判过期，把冻住的记录当现存启动原样复用、什么都不派，监督每轮还报 orphans_revived=1，永久卡住。

锁定：
- 过期判定只有一处权威 process_control.background_start_record_stale，派工候选与重复投递复用都调它；过期的旧启动不复用：
  受管模式沿用同一 pending attempt 换新启动记录，文件模式先撤销旧预留（旧执行轮进放弃名单）再预留。
- orphans_revived 只算真派出去的 run，复用现存启动另记 orphan_launches_reused / launch_reused。
- worker._run_subagent_worker 入口读准入关门：顺序批次与并行批次都不建 worker、不激活、不写 FAILED，预留原样保留。
- 同步派工路径（唤醒前预扫、watch、CLI 经 collect_runner_candidates）对冻住的现场沿用原执行轮。

全部用真实 SubAgentManager（受管 = 带 owner runtime.db；文件 = 显式无库），只截获最终的 dispatch_subagents，不发模型请求。
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import settle_open_model_calls_for_shutdown
from agent_py_agent.agent.agent_core.orchestration.background import dispatch as background_dispatch
from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
from agent_py_agent.agent.agent_core.runner import worker as worker_module
from agent_py_agent.agent.agent_core.runner.gate import (
    ConcurrentRunnerParams,
    SingleRunnerParams,
    run_concurrent_runners,
    run_single_runner,
)
from agent_py_agent.agent.contracts import model_call_ledger
from agent_py_agent.agent.runtime_db.execution_mode import ExecutionMode
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.runner_start import reserve_runner_start
from agent_py_agent.agent.subagents.services.lifecycle_runner_attempts import prepare_runner_attempt
from agent_py_agent.tests.test_dispatch_liveness_and_revive import _agent, _make_child

MODES = ["managed", "file"]


# 类用途: worker 一旦真的开始建 SimpleAgent 就抛它，证明检查点之后的路径被走到了。
class _WorkerBuilt(Exception):
    pass


def _manager(tmp_path, mode: str) -> SubAgentManager:
    if mode == "managed":
        return SubAgentManager(tmp_path / "runs", owner_home_dir=str(tmp_path / "owner"))
    return SubAgentManager(tmp_path / "runs", execution_mode=ExecutionMode.LOCAL_UNMANAGED.value)


def _current_attempt(manager, run_id: str) -> tuple[str, str]:
    repo = manager.runtime_db
    if repo is None:
        return "", ""
    run = repo.agent_run_for_run_id(run_id)
    attempt = repo.get_attempt(str(run["current_attempt_id"] or ""))
    return str(run["current_attempt_id"] or ""), str(attempt["status"])


def _record(manager, run_id: str) -> dict:
    return dict((manager.load(run_id).attributes or {}).get("background_start") or {})


# 函数用途: 造一个带真实管理器的派工宿主；dispatch_subagents 只登记收到的批次，可选地像 worker 一样先激活准确执行轮。
def _host(tmp_path, manager, *, activate: bool = False, inner=None):
    agent = _agent(tmp_path, manager)
    agent.capability_router = object()
    agent.local_store = None
    dispatched: list[dict] = []

    def dispatch_subagents(_router, _cfg, *, params):
        row = {"run_ids": list(params.include_run_ids or []), "attempts": dict(params.expected_attempt_ids or {})}
        if inner is not None:
            row["inner"] = inner(agent, row["attempts"])
        if activate:
            row["activated"] = {run_id: prepare_runner_attempt(manager, run_id, expected_attempt_id=attempt).status
                                for run_id, attempt in row["attempts"].items()}
        dispatched.append(row)
        return SimpleNamespace(summary="test", records=[])

    agent.dispatch_subagents = dispatch_subagents
    return agent, dispatched


def _dead_pid() -> int:
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    return proc.pid


def _wait_record_status(manager, run_id: str, status: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _record(manager, run_id).get("status") == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"启动记录没有到 {status}")


# 函数用途: 旧进程在派工线程里、关门之后才走真实 runner 批次入口：worker 拒绝启动，线程照常收尾成 finished。
def _old_process_refused_in_thread(tmp_path, manager, run_id):
    gate, refused = threading.Event(), []

    def runner_after_close(agent, attempts):
        assert gate.wait(10)
        refused.append(run_single_runner(SingleRunnerParams(
            agent=agent, run_id=run_id, task_timeout=0.0, instruction="", start_runner=True, max_cards=0,
            probe=False, retry_reason="", expected_attempt_id=attempts[run_id])))

    agent, dispatched = _host(tmp_path, manager, inner=runner_after_close)
    assert background_dispatch.auto_start_tasks(agent, [manager.load(run_id)], {})["status"] == "started"
    settle_open_model_calls_for_shutdown()
    gate.set()
    _wait_record_status(manager, run_id, "finished")
    return agent, dispatched, refused


# 函数用途: 旧进程在派工线程收尾前就没了：启动记录冻在 running（进程内线程没有 pid；派工子进程留下已死的 pid）。
def _old_process_died_in_flight(monkeypatch, agent, run_id, pid):
    def died_in_flight(_agent, request, mark_errors=None):
        assert background_dispatch.mark_background_start(request, status="running", pid=pid) == []
        return {"status": "started", "run_ids": request.run_ids, "launch_id": request.launch_id}

    original = background_dispatch._start_inprocess_dispatch
    monkeypatch.setattr(background_dispatch, "_start_inprocess_dispatch", died_in_flight)
    assert background_dispatch.auto_start_tasks(agent, [agent.subagents.load(run_id)], {})["status"] == "started"
    monkeypatch.setattr(background_dispatch, "_start_inprocess_dispatch", original)
    settle_open_model_calls_for_shutdown()


# 函数用途: 模拟重启：换一张开着的准入表，新建管理器（新 RuntimeDB 连接）和宿主。
def _restart(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(model_call_ledger, "_ADMISSION_REGISTRY", model_call_ledger._ModelCallAdmissionRegistry())
    manager = _manager(tmp_path, mode)
    agent, dispatched = _host(tmp_path, manager, activate=True)
    return manager, agent, dispatched


def _wait_dispatched(dispatched: list[dict]) -> list[dict]:
    deadline = time.monotonic() + 10
    while not dispatched and time.monotonic() < deadline:
        time.sleep(0.02)
    return dispatched


def _supervise_at(monkeypatch, agent, offset_seconds: float) -> dict:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_seconds)
    try:
        return capability_auto_sweep.supervise_stalled_orphans(agent)
    finally:
        monkeypatch.setattr(time, "time", real_time)


@pytest.fixture(autouse=True)
def _in_process_dispatch(monkeypatch):
    # Gateway 里非 local/main 的 owner 走进程内线程派工，停机打断的正是这条路。
    monkeypatch.setattr(background_dispatch, "_use_inprocess_autostart", lambda _agent: True)

    def built(*_args, **_kwargs):
        raise _WorkerBuilt()

    monkeypatch.setattr(worker_module, "_build_worker_agent", built)


# ---------------------------------------------------------------- ① 线程收尾成 finished


@pytest.mark.parametrize("mode", MODES)
def test_runner_refused_after_close_keeps_the_reservation_and_restart_picks_it_up(tmp_path, monkeypatch, mode):
    manager = _manager(tmp_path, mode)
    task = manager.create_run(goal="停机时还在排队", thought="t", plan=["p"], role="worker", depth=1)

    old_agent, old_dispatched, refused = _old_process_refused_in_thread(tmp_path, manager, task.id)
    reserved = old_dispatched[0]["attempts"][task.id]

    [result] = refused
    assert (result.ok, result.status, result.turn_end_reason) == (False, "PENDING", "model_call_admission_closed")
    loaded = manager.load(task.id)
    assert (loaded.status, loaded.runner_active_attempt_id, loaded.runner_attempts) == ("PLANNING", "", 0)
    if mode == "managed":
        assert _current_attempt(manager, task.id) == (reserved, "pending"), "预留原样保留"
    assert capability_auto_sweep.supervise_stalled_orphans(old_agent)["orphans_revived"] == 0
    assert len(old_dispatched) == 1, "关门后同进程不再派工"

    new_manager, new_agent, dispatched = _restart(tmp_path, monkeypatch, mode)
    summary = capability_auto_sweep.supervise_stalled_orphans(new_agent)

    assert (summary["orphans_revived"], summary["orphan_launches_reused"]) == (1, 0)
    assert [row["run_ids"] for row in _wait_dispatched(dispatched)] == [[task.id]]
    assert dispatched[0]["activated"] == {task.id: "RUNNING"}
    if mode == "managed":
        assert dispatched[0]["attempts"][task.id] == reserved, "沿用预留的同一执行轮"


# ---------------------------------------------------------------- ② 启动记录冻在 running


@pytest.mark.parametrize("variant", ["thread_without_pid", "dead_pid"])
@pytest.mark.parametrize("mode", MODES)
def test_frozen_launch_record_is_not_reused_after_restart(tmp_path, monkeypatch, mode, variant):
    manager = _manager(tmp_path, mode)
    task = manager.create_run(goal="派工途中进程没了", thought="t", plan=["p"], role="worker", depth=1)
    pid = _dead_pid() if variant == "dead_pid" else 0
    _old_process_died_in_flight(monkeypatch, _host(tmp_path, manager)[0], task.id, pid)
    frozen = _record(manager, task.id)
    assert frozen["status"] == "running" and frozen.get("pid", 0) == pid

    new_manager, new_agent, dispatched = _restart(tmp_path, monkeypatch, mode)
    if variant == "thread_without_pid":
        early = _supervise_at(monkeypatch, new_agent, 0.0)
        assert (early["orphans_revived"], dispatched) == (0, []), "过期窗内仍当在途启动，不抢跑"
    summary = _supervise_at(monkeypatch, new_agent, 181.0 if variant == "thread_without_pid" else 0.0)

    assert (summary["orphans_revived"], summary["orphan_launches_reused"]) == (1, 0)
    assert [row["run_ids"] for row in _wait_dispatched(dispatched)] == [[task.id]], "冻住的旧记录不能被原样复用"
    assert dispatched[0]["activated"] == {task.id: "RUNNING"}
    assert _record(new_manager, task.id)["launch_id"] != frozen["launch_id"]
    abandoned = new_manager.load(task.id).runner_abandoned_attempt_ids
    if mode == "managed":
        assert dispatched[0]["attempts"][task.id] == frozen["attempt_id"], "受管模式沿用同一 pending attempt"
        assert frozen["attempt_id"] not in abandoned, "沿用的执行轮不能进放弃名单，否则它的结果会被当成旧轮拒收"
    else:
        assert frozen["attempt_id"] in abandoned, "文件旧预留已撤销"


@pytest.mark.parametrize("mode", MODES)
def test_sync_dispatch_path_continues_a_frozen_reservation(tmp_path, monkeypatch, mode):
    """唤醒前预扫、watch、CLI 派工经 collect_runner_candidates → reserve_runner_start(launch_id="")，沿用原执行轮。"""
    manager = _manager(tmp_path, mode)
    task = manager.create_run(goal="同步派工接续", thought="t", plan=["p"], role="worker", depth=1)
    _old_process_died_in_flight(monkeypatch, _host(tmp_path, manager)[0], task.id, 0)
    frozen = _record(manager, task.id)

    new_manager = _restart(tmp_path, monkeypatch, mode)[0]
    again = reserve_runner_start(new_manager, task.id, launch_id="")

    assert again == frozen["attempt_id"]
    assert prepare_runner_attempt(new_manager, task.id, expected_attempt_id=again).status == "RUNNING"


# ---------------------------------------------------------------- 计数：复用不算复活


@pytest.mark.parametrize("case", [
    (["r"], ["r"], (0, 1)),
    (["r", "s"], ["r"], (1, 1)),
], ids=["only-reused", "mixed"])
def test_reused_launches_are_counted_apart_from_revived_runs(tmp_path, monkeypatch, case):
    run_ids, reused, counts = case
    manager = SubAgentManager(tmp_path / "subagents")
    orphans = [_make_child(manager, status="PENDING") for _ in run_ids]
    ids = {name: task.id for name, task in zip(run_ids, orphans)}
    reply = {"status": "started", "run_ids": [ids[name] for name in run_ids],
             "reused_run_ids": [ids[name] for name in reused]}
    monkeypatch.setattr(background_dispatch, "auto_start_tasks", lambda *_args, **_kwargs: dict(reply))

    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))
    targeted = capability_auto_sweep.auto_start_orphan_run(_agent(tmp_path, manager), orphans[0].id)

    assert (summary["orphans_revived"], summary["orphan_launches_reused"]) == counts
    assert (targeted["started"], targeted["launch_reused"]) == counts


# ---------------------------------------------------------------- 检查点在 worker 公共入口


def _reserved_jobs(manager, count: int):
    tasks = [manager.create_run(goal=f"排队 {index}", thought="t", plan=["p"], role="worker", depth=1)
             for index in range(count)]
    return tasks, {task.id: reserve_runner_start(manager, task.id) for task in tasks}


def test_sequential_and_concurrent_batches_both_refuse_after_close(tmp_path):
    manager = _manager(tmp_path, "managed")
    agent, _dispatched = _host(tmp_path, manager)
    tasks, attempts = _reserved_jobs(manager, 3)
    settle_open_model_calls_for_shutdown()

    single = run_single_runner(SingleRunnerParams(
        agent=agent, run_id=tasks[0].id, task_timeout=0.0, instruction="", start_runner=True, max_cards=0,
        probe=False, retry_reason="", expected_attempt_id=attempts[tasks[0].id]))
    concurrent = run_concurrent_runners(ConcurrentRunnerParams(
        agent=agent, pending_jobs=[(task.id, task, "") for task in tasks[1:]], runner_concurrency=2,
        runner_timeout_seconds=0.0, instruction="", start_runners=True, max_cards=0, probe=False,
        expected_attempt_ids={task.id: attempts[task.id] for task in tasks[1:]}))

    results = [single, *(result for result, _after in concurrent.values())]
    assert [(item.ok, item.status, item.runner_last_error) for item in results] == [
        (False, "PENDING", "MODEL_CALL_ADMISSION_CLOSED")] * 3
    for task in tasks:
        loaded = manager.load(task.id)
        assert (loaded.status, loaded.failure_type, loaded.runner_active_attempt_id) == ("PLANNING", "", "")
        assert _current_attempt(manager, task.id) == (attempts[task.id], "pending")


def test_open_admission_reaches_the_worker_build(tmp_path):
    """对照：准入开着时照常往下建 worker（这里用替身在建的那一刻抛出）。"""
    manager = _manager(tmp_path, "managed")
    agent, _dispatched = _host(tmp_path, manager)
    tasks, attempts = _reserved_jobs(manager, 1)

    with pytest.raises(_WorkerBuilt):
        run_single_runner(SingleRunnerParams(
            agent=agent, run_id=tasks[0].id, task_timeout=0.0, instruction="", start_runner=True, max_cards=0,
            probe=False, retry_reason="", expected_attempt_id=attempts[tasks[0].id]))


def test_preview_is_not_refused_after_close(tmp_path):
    """预览（dry_run）不发模型调用，关门后照样放行；真启动才拦。"""
    settle_open_model_calls_for_shutdown()
    params = worker_module.RunSubagentWorkerParams(
        config=SimpleNamespace(), root=tmp_path, run_id="r", instruction="", dry_run=True, max_cards=0, probe=False,
        retry_reason="")

    assert worker_module._admission_closed_refusal(params) is None
    assert worker_module._admission_closed_refusal(replace(params, dry_run=False)).status == "PENDING"
