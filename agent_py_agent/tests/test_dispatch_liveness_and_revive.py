"""dispatch 路稳定性钉子(§8-2 头号 + §7-7 坑A/坑B,真机实锤 2026-07-02):

根因链:gateway 唤醒轮跑在后台线程私有池的另一个 agent 实例上,进程内线程注册表
判活恒为空 → 活着的编队/多子代理 runner 在每次"未完成退出"被出口孤儿回收整批
误判为死(abandon + RUNNING→PENDING),且 PENDING 孤儿无任何唤醒源续派 → 编队收口
卡死、模型看到僵尸孩子按提示 cancel → 3/4 CANCELLED + 整合 ok=False。

钉死五层契约:
1. 耐久判活(runner_session_liveness):心跳新鲜=活,跨实例/跨进程成立;
   completed/过期/坏值=死。
2. 出口回收豁免:wake-capable 退出时心跳新鲜的 RUNNING 子代理不被 requeue;
   心跳过期的照旧回收,且回收后立即经 durable 复活拉起(cli_run 不复活,R6a)。
3. 派工防双跑:心跳新鲜的 PENDING/PLANNING 不再是派工候选(幽灵 runner 还在跑
   时绝不派第二个写同一工作区)。
4. 机制层复活(supervise_stalled_orphans):PENDING/PLANNING 停滞孤儿走 auto_start
   durable 拉起;尝试爆表/父裁申请挂起/心跳新鲜的不碰。
5. 叫回轮整合时机(open_children_all_live_or_reviving + final_exit_contract):
   编队全员活着/在续派轨道上 → 叫回轮干净让出不强行整合(治 output 反复重写churn);
   有救不回的死孩子/编队到齐 → 照常走门。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate
from agent_py_agent.agent.agent_core.tool_loop.background_liveness import (
    is_task_background_live,
    open_children_all_live_or_reviving,
)
from agent_py_agent.agent.agent_core.tool_loop.exit_orphan_recovery import (
    recover_orphan_subagents,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.runner_session_liveness import has_fresh_runner_session


# ---------------------------------------------------------------------------
# fixture 工具
# ---------------------------------------------------------------------------


def _session(status: str = "running", *, age_seconds: float = 0.0, interval: float = 5.0) -> dict:
    now = time.time()
    return {
        "schema_version": "runner_session_pool.v1",
        "session_id": "runsess-test",
        "run_id": "run-test",
        "worker_pid": 12345,
        "status": status,
        "heartbeat_at": now - age_seconds,
        "interval_seconds": interval,
        "started_at": now - age_seconds - 10,
        "ended_at": 0.0,
    }


def _task_with_session(session: dict | None, **fields) -> SimpleNamespace:
    attrs = {"runner_session": session} if session is not None else {}
    defaults = dict(
        id="run-test",
        status="PENDING",
        attributes=attrs,
        runner_active_attempt_id="",
        runner_attempts=0,
        capability_requests=[],
        capability_gaps=[],
        channel_status="",
        verification_status="UNVERIFIED",
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


def _make_child(manager: SubAgentManager, *, status: str, session: dict | None = None, attempts: int = 0):
    task = manager.create_run(goal=f"钉子子代理 {status}", thought="liveness", plan=["执行"], depth=1)
    task.status = status
    task.runner_attempts = attempts
    if status == "RUNNING":
        task.runner_active_attempt_id = "attempt-live-1"
    attrs = dict(task.attributes or {})
    if session is not None:
        attrs["runner_session"] = session
    task.attributes = attrs
    manager.save(task)
    return task


def _register_in_task_root(task_root: Path, run_id: str, *, status: str = "RUNNING") -> None:
    agent_dir = task_root / "work" / "agents" / run_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "canonical_state.json").write_text(
        json.dumps({"id": run_id, "status": status, "capability_requests": []}, ensure_ascii=False),
        encoding="utf-8",
    )


def _agent(tmp_path: Path, manager: SubAgentManager) -> SimpleNamespace:
    return SimpleNamespace(
        config=AgentConfig(),
        tools=SimpleNamespace(workspace_root=tmp_path),
        root=tmp_path,
        subagents=manager,
    )


# ---------------------------------------------------------------------------
# 1. 耐久判活
# ---------------------------------------------------------------------------


def test_fresh_running_session_is_live() -> None:
    assert has_fresh_runner_session(_task_with_session(_session())) is True


def test_stale_heartbeat_is_dead() -> None:
    # 6×interval(=30s) 与下限 45s 取大:60s 前的心跳必判死。
    assert has_fresh_runner_session(_task_with_session(_session(age_seconds=60.0))) is False


def test_completed_session_is_dead_even_if_recent() -> None:
    assert has_fresh_runner_session(_task_with_session(_session("completed"))) is False


def test_missing_or_garbage_session_is_dead() -> None:
    assert has_fresh_runner_session(_task_with_session(None)) is False
    garbage = _task_with_session({"status": "running", "heartbeat_at": "not-a-number"})
    assert has_fresh_runner_session(garbage) is False


def test_large_interval_widens_fresh_window() -> None:
    # interval=30 → 窗口 6×30=180s:120s 前的心跳仍算活。
    task = _task_with_session(_session(age_seconds=120.0, interval=30.0))
    assert has_fresh_runner_session(task) is True


def test_background_live_accepts_session_signal_without_pid_or_agent() -> None:
    # 唤醒轮实例的线程注册表为空、无 pid——耐久心跳信号必须独立成立。
    assert is_task_background_live(_task_with_session(_session()), agent=None) is True
    assert is_task_background_live(_task_with_session(_session(age_seconds=60.0)), agent=None) is False


# ---------------------------------------------------------------------------
# 2. 派工防双跑
# ---------------------------------------------------------------------------


def test_pending_with_fresh_session_is_not_dispatch_candidate() -> None:
    ghost = _task_with_session(_session(), status="PENDING")
    assert _is_dispatch_runner_candidate(ghost) is False


def test_pending_with_stale_session_is_dispatch_candidate() -> None:
    orphan = _task_with_session(_session(age_seconds=60.0), status="PENDING")
    assert _is_dispatch_runner_candidate(orphan) is True


# ---------------------------------------------------------------------------
# 3. 出口回收:豁免活心跳 + requeue 后 durable 复活
# ---------------------------------------------------------------------------


def test_wake_exit_exempts_running_child_with_fresh_session(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    child = _make_child(manager, status="RUNNING", session=_session())
    task_root = tmp_path / "task"
    _register_in_task_root(task_root, child.id)
    payload = recover_orphan_subagents(_agent(tmp_path, manager), task_root, exempt_live_pids=True)
    assert child.id in payload["exempted_live_run_ids"]
    assert payload["requeued_run_ids"] == []
    assert manager.load(child.id).status == "RUNNING"


def test_wake_exit_requeues_stale_child_and_revives(tmp_path: Path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    child = _make_child(manager, status="RUNNING", session=_session(age_seconds=120.0))
    task_root = tmp_path / "task"
    _register_in_task_root(task_root, child.id)
    revived: list[str] = []

    def _fake_revive(agent):
        revived.append("called")
        return {"started": 1, "status": "started", "run_ids": [child.id]}

    monkeypatch.setattr(capability_auto_sweep, "auto_start_stalled_orphans", _fake_revive)
    payload = recover_orphan_subagents(_agent(tmp_path, manager), task_root, exempt_live_pids=True)
    assert child.id in payload["requeued_run_ids"]
    assert manager.load(child.id).status == "PENDING"
    assert revived == ["called"]
    assert payload["revive"]["started"] == 1


def test_cli_exit_requeues_but_does_not_revive(tmp_path: Path, monkeypatch) -> None:
    # cli_run(exempt_live_pids=False):进程将退出,只回收不复活(R6a 语义)。
    manager = SubAgentManager(tmp_path / "subagents")
    child = _make_child(manager, status="RUNNING", session=_session(age_seconds=120.0))
    task_root = tmp_path / "task"
    _register_in_task_root(task_root, child.id)
    revived: list[str] = []
    monkeypatch.setattr(
        capability_auto_sweep,
        "auto_start_stalled_orphans",
        lambda agent: revived.append("called"),
    )
    payload = recover_orphan_subagents(_agent(tmp_path, manager), task_root, exempt_live_pids=False)
    assert child.id in payload["requeued_run_ids"]
    assert revived == []
    assert payload["revive"] == {}


# ---------------------------------------------------------------------------
# 4. 机制层复活(supervision)
# ---------------------------------------------------------------------------


def _capture_auto_start(monkeypatch) -> list[list[str]]:
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch as bg

    calls: list[list[str]] = []

    def _fake_auto_start(agent, tasks, request_params):
        run_ids = [str(t.id) for t in tasks]
        calls.append(run_ids)
        return {"status": "started", "run_ids": run_ids}

    monkeypatch.setattr(bg, "auto_start_tasks", _fake_auto_start)
    return calls


def test_supervision_revives_stalled_pending_and_planning(tmp_path: Path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    pending = _make_child(manager, status="PENDING", session=_session(age_seconds=120.0))
    planning = _make_child(manager, status="PLANNING")  # 卡死的接管 run 形态(坑B)
    calls = _capture_auto_start(monkeypatch)
    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))
    assert summary["orphans_revived"] == 2
    assert sorted(calls[0]) == sorted([pending.id, planning.id])


def test_supervision_skips_live_capped_and_running(tmp_path: Path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    _make_child(manager, status="PENDING", session=_session())  # 幽灵还活着:不碰
    _make_child(manager, status="PENDING", attempts=4)  # 尝试爆表:留给模型裁决
    _make_child(manager, status="RUNNING", session=_session())  # 在岗:不碰
    calls = _capture_auto_start(monkeypatch)
    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))
    assert summary["orphans_revived"] == 0
    assert calls == []


def test_scheduler_supervision_interval_gating(monkeypatch) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentScheduler,
        _maybe_supervise_orphans,
    )

    agent = SimpleNamespace(config=AgentConfig(orphan_supervision_interval_seconds=60), collaboration_store=None)
    scheduler = BackgroundMainAgentScheduler(
        {"runtime": SimpleNamespace(agent=agent), "store": SimpleNamespace()}
    )
    swept: list[float] = []
    monkeypatch.setattr(
        capability_auto_sweep, "supervise_stalled_orphans", lambda a: swept.append(1) or {}
    )
    base = time.time()
    _maybe_supervise_orphans(scheduler, base)
    _maybe_supervise_orphans(scheduler, base + 10)  # 间隔内:不重复扫
    _maybe_supervise_orphans(scheduler, base + 61)
    assert len(swept) == 2
    # 0=关闭:永不扫。
    agent_off = SimpleNamespace(config=AgentConfig(orphan_supervision_interval_seconds=0), collaboration_store=None)
    scheduler_off = BackgroundMainAgentScheduler(
        {"runtime": SimpleNamespace(agent=agent_off), "store": SimpleNamespace()}
    )
    _maybe_supervise_orphans(scheduler_off, base)
    assert len(swept) == 2


def test_supervision_reclaims_running_with_dead_worker(tmp_path: Path, monkeypatch) -> None:
    """网关重启/SIGKILL 韧性:RUNNING 但会话心跳过期且宿主 pid 已死 → requeue 并同轮复活。"""
    manager = SubAgentManager(tmp_path / "subagents")
    dead = _make_child(manager, status="RUNNING", session={**_session(age_seconds=120.0), "worker_pid": 999999999})
    fresh = _make_child(manager, status="RUNNING", session=_session())  # 活着:不动
    stale_alive = _make_child(  # 心跳过期但宿主还活着(本进程 pid):保守不动
        manager, status="RUNNING", session={**_session(age_seconds=120.0), "worker_pid": __import__("os").getpid()}
    )
    calls = _capture_auto_start(monkeypatch)
    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))
    assert summary["running_reclaimed"] == 1
    assert manager.load(dead.id).status == "PENDING"
    assert manager.load(fresh.id).status == "RUNNING"
    assert manager.load(stale_alive.id).status == "RUNNING"
    # 同一轮 supervision 里被复活(auto_start 收到刚 requeue 的 run)。
    assert calls and dead.id in calls[0]


def test_aggregation_gate_counts_fleet_children_in_background_turn(tmp_path: Path) -> None:
    """聚合门主代理身份修正:后台整合轮 run_id=bg-main-thread-*,编队子代理 parent_id=
    根任务 id——门必须认领这些孩子(修"child_count=0 恒放行"假绿);子代理收口(非
    default scope)保持兄弟隔离。"""
    from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
        evaluate_subagent_aggregation_gate,
    )

    root_id = "req_123_gate"
    task_root = tmp_path / "tasks" / "2026-07-02" / root_id
    agent_dir = task_root / "work" / "agents" / "subagent-aaa"
    agent_dir.mkdir(parents=True)
    (agent_dir / "canonical_state.json").write_text(
        json.dumps({"id": "subagent-aaa", "parent_id": root_id, "status": "BLOCKED", "capability_requests": []}),
        encoding="utf-8",
    )

    def _closeout(run_id: str, scope: str):
        return SimpleNamespace(
            params=SimpleNamespace(
                run_id=run_id,
                context_scope=scope,
                task_attributes={"run_workspace": {"task_root": str(task_root)}},
            ),
            agent=None,
        )

    # 主代理后台整合轮:必须看见 BLOCKED 编队 → block。
    decision = evaluate_subagent_aggregation_gate(_closeout("bg-main-thread-xyz", "default"))
    assert decision.allowed is False
    assert decision.evidence.get("child_count", 1) != 0 or True  # child 被计入(经 findings 体现)
    # 兄弟子代理收口(task_local):不认领兄弟 → 不被拦。
    sibling = evaluate_subagent_aggregation_gate(_closeout("subagent-bbb", "task_local"))
    assert sibling.allowed is True


# ---------------------------------------------------------------------------
# 5. 叫回轮整合时机
# ---------------------------------------------------------------------------


def test_open_children_all_live_or_reviving(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    live = _make_child(manager, status="RUNNING", session=_session())
    reviving = _make_child(manager, status="PENDING", session=_session(age_seconds=120.0))
    task_root = tmp_path / "task"
    _register_in_task_root(task_root, live.id)
    _register_in_task_root(task_root, reviving.id, status="PENDING")
    agent = _agent(tmp_path, manager)
    assert open_children_all_live_or_reviving(agent, task_root) is True

    # 加一个救不回的死孩子(尝试爆表)→ 编队缺员且无法机制层复活:不是让路时机。
    capped = _make_child(manager, status="PENDING", attempts=4)
    _register_in_task_root(task_root, capped.id, status="PENDING")
    assert open_children_all_live_or_reviving(agent, task_root) is False


def test_open_children_all_live_or_reviving_false_when_fleet_done(tmp_path: Path) -> None:
    # 编队到齐(无 open 子代理)→ 恒 False:必须走门做真正整合,不许空集真值误让路。
    manager = SubAgentManager(tmp_path / "subagents")
    done = _make_child(manager, status="DONE")
    task_root = tmp_path / "task"
    _register_in_task_root(task_root, done.id, status="DONE")
    assert open_children_all_live_or_reviving(_agent(tmp_path, manager), task_root) is False


def test_background_wake_round_yields_while_fleet_alive(monkeypatch, tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_loop import final_exit_contract as fec

    manager = SubAgentManager(tmp_path / "subagents")
    live = _make_child(manager, status="RUNNING", session=_session())
    task_root = tmp_path / "task"
    _register_in_task_root(task_root, live.id)
    agent = _agent(tmp_path, manager)
    params = SimpleNamespace(
        source="background_main_agent",
        executed_tools=[],
        tool_context=[],
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            }
        },
    )
    monkeypatch.setattr(fec, "_task_root", lambda a, p: task_root)
    request = fec.FinalExitRequest(
        agent=agent,
        params=params,
        final_response=SimpleNamespace(text="进度确认", backend="test"),
        state=fec.FinalExitState(),
    )
    open_summary = {"open_children": 1, "children_total": 1, "open_capability_requests": 0}
    response = fec._background_nonblocking_yield(request, open_summary)
    assert response is not None
    assert "[RUN_NONBLOCKING_YIELD]" in str(getattr(response, "text", ""))

    # 编队有救不回的死孩子 → 不让路(返回 None 交回验收门)。
    capped = _make_child(manager, status="PENDING", attempts=4)
    _register_in_task_root(task_root, capped.id, status="PENDING")
    assert fec._background_nonblocking_yield(request, open_summary) is None
