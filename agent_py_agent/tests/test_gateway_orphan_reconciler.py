from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.background import dispatch as background_dispatch
from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
    _reconcile_conversation_parent_lifecycle,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.conversation_lifecycle_gate import (
    conversation_lifecycle_decisions,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
from agent_py_agent.agent.ingestion.source_worker import settle_audit_source_worker
from agent_py_agent.agent.ingestion.watch_state import (
    load_state,
    new_state,
    persist_state,
    state_dir,
)
from agent_py_agent.agent.owner_scoped_pool import shared_active_owner_registry
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_loops


# LLM: 巡检冷却从实际完成计算，慢调用和异常均不能缩短下一轮等待。
# 函数用途: 验证基础及owner巡检在慢速结束后仍保留完整冷却期。
@pytest.mark.parametrize("lane", ["base", "owner"])
@pytest.mark.parametrize("fails", [False, True])
def test_slice2_slow_sweep_has_post_completion_cooldown(tmp_path, monkeypatch, lane, fails):
    from concurrent.futures import Future

    base, owner, _scoped = _scoped_restart_fixture(tmp_path)
    shared_active_owner_registry(base).record(owner)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _: base)
    reconciler = gateway_loops._GatewayOrphanReconciler(_context(base, tmp_path))
    reconciler._next_discovery_at = float("inf")
    monkeypatch.setattr(reconciler, "_ensure_owner_pool", lambda: _PassThroughOwnerPool())
    clock = [100.0]
    monkeypatch.setattr(gateway_loops.time, "monotonic", lambda: clock[0])
    calls = []

    # LLM: 只推进受控单调时钟，按参数决定成功或失败，不实际等待两分钟。
    # 函数用途: 模拟一轮慢巡检并记录标签。
    def slow(_agent, label):
        calls.append(label)
        clock[0] += 120.0
        if fails:
            raise RuntimeError("sweep failed")
        return {"owner": label}

    # LLM: 同步future替身保留结果/异常接口，不启动真实线程池。
    # 类用途: 让慢巡检测试确定性执行提交动作。
    class Executor:
        # LLM: 异常保存在future中，由原收集入口读取，不伪造成功。
        # 函数用途: 同步执行一次提交并返回已结future。
        def submit(self, fn, *args):
            future = Future()
            try:
                future.set_result(fn(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future

    monkeypatch.setattr(reconciler, "_sweep", slow)
    reconciler._base_executor = reconciler._owner_executor = Executor()
    submit = reconciler._submit_base_sweep if lane == "base" else reconciler._submit_owner_sweeps
    submit(clock[0])
    reconciler._collect_finished_sweeps()
    due = reconciler._base_next_at if lane == "base" else reconciler._owner_next_at[reconciler._owner_key(owner)]
    assert due == 280.0
    clock[0] = 279.0
    submit(clock[0])
    assert len(calls) == 1
    clock[0] = 280.0
    submit(clock[0])
    assert len(calls) == 2


# LLM: owner池初始化失败或不可用不得把已有更晚的冷却时间拉早。
# 函数用途: 验证owner准备异常仍保留原先排定的截止时间。
@pytest.mark.parametrize("fails", [False, True])
def test_slice2_owner_setup_cooldown_preserves_later_deadline(tmp_path, monkeypatch, fails):
    base, owner, _scoped = _scoped_restart_fixture(tmp_path)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _: base)
    reconciler = gateway_loops._GatewayOrphanReconciler(_context(base, tmp_path))
    key = reconciler._owner_key(owner)
    reconciler._owner_next_at[key] = 400.0
    monkeypatch.setattr(gateway_loops.time, "monotonic", lambda: 220.0)

    # LLM: 仅替代owner池准备结果，不改变reconciler的冷却判断。
    # 函数用途: 模拟owner池不可用或初始化抛错。
    def unavailable():
        if fails:
            raise RuntimeError("setup failed")
        return None

    monkeypatch.setattr(reconciler, "_ensure_owner_pool", unavailable)
    if fails:
        with pytest.raises(RuntimeError, match="setup failed"):
            reconciler._sweep_owner(owner, "owner")
    else:
        assert reconciler._sweep_owner(owner, "owner")["state"] == "owner_pool_unavailable"
    assert reconciler._owner_next_at[key] == 400.0


# LLM: 返回结构化旧代runner事实，不据普通文字赋予存活或执行资格。
# 函数用途: 构造具有指定心跳时间的重启会话夹具。
def _runner_session(*, heartbeat_at: float) -> dict[str, object]:
    return {
        "schema_version": "runner_session_pool.v1",
        "session_id": "runsess-before-restart",
        "run_id": "run-before-restart",
        "worker_pid": os.getpid(),
        "worker_id": "subagent-worker:run-before-restart",
        "status": "running",
        "heartbeat_at": heartbeat_at,
        "interval_seconds": 5.0,
        "started_at": heartbeat_at - 10.0,
        "ended_at": 0.0,
        "process_epoch": "old-gateway-generation",
        "in_process": True,
    }


# LLM: 只包装本地agent和测试配置位置，不创建新owner或协调权限。
# 函数用途: 提供reconciler所需的最小Gateway上下文。
def _context(base_agent: SimpleAgent, tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(agent=base_agent, config_path=tmp_path / "agent_config.yaml")


# LLM: 只在临时home经真实owner池构造作用域实例，不操作生产Gateway。
# 函数用途: 准备基础agent、owner身份与对应作用域agent。
def _scoped_restart_fixture(tmp_path: Path):
    config = AgentConfig(
        enable_tools=False,
        memory_path="memory.jsonl",
        gateway_per_user_owner_scoping=True,
        my_agent_home=str(tmp_path / "home"),
        orphan_supervision_interval_seconds=60,
    )
    base_agent = SimpleAgent(config, tmp_path)
    owner = OwnerIdentity.provider_user("feishu", "u-restart")
    scoped = _owner_pool(base_agent).get(owner)
    return base_agent, owner, scoped


# LLM: 通过原manager持久化旧代RUNNING记录，心跳由调用方控制。
# 函数用途: 创建可用于重启回收测试的运行中子任务。
def _running_restart_task(scoped, *, heartbeat_at: float):
    task = scoped.subagents.create_run(
        goal="continue after gateway restart",
        thought="restart recovery",
        plan=["continue"],
        depth=1,
    )
    task.status = "RUNNING"
    task.attributes = {
        **dict(task.attributes or {}),
        "runner_session": _runner_session(heartbeat_at=heartbeat_at),
        "background_start": {
            "launch_id": "launch-before-restart",
            "status": "running",
            "updated_at": time.time(),
        },
    }
    scoped.subagents.save(task)
    return task


# LLM: 原Store绑定父子TaskLink并保存准确会话引用，不从goal推断血缘。
# 函数用途: 为重启任务准备指定父子生命周期状态。
def _bind_conversation_task(
    scoped,
    task,
    *,
    parent_status: str = "active",
    run_status: str = "active",
):
    store = scoped.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "u-restart",
            "channel": "feishu",
            "channel_conversation_id": f"chat-{task.id}",
            "channel_user_id": "u-restart",
        }
    )
    parent_task_id = f"parent-{task.id}"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": parent_task_id,
            "goal": "parent task",
            "status": parent_status,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": task.id,
            "goal": task.goal,
            "status": run_status,
        }
    )
    task = scoped.subagents.load(task.id)
    task.attributes = {
        **dict(task.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(task)
    return thread, parent_task_id


# LLM: 捕获原派发意图而不启动真实后台模型，不将替身记录当作实际执行。
# 函数用途: 收集重启对账请求启动的子任务编号。
def _capture_background_starts(monkeypatch) -> list[list[str]]:
    starts: list[list[str]] = []

    # LLM: 仅返回原启动接口形状，不产生进程或新授权。
    # 函数用途: 记录一次后台启动请求的任务集合。
    def _capture_start(_agent, tasks, _params):
        starts.append([str(item.id) for item in tasks])
        return {"status": "started", "run_ids": starts[-1]}

    monkeypatch.setattr(background_dispatch, "auto_start_tasks", _capture_start)
    return starts


# LLM: 旧心跳过期应由宿主巡检回收，不依赖另一个模型回合触发。
# 函数用途: 验证无模型tick时陈旧运行仍可被重排。
def test_reconciler_reclaims_after_heartbeat_stales_without_another_model_tick(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)
    reconciler = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path))

    first = reconciler.tick()

    assert scoped.subagents.load(task.id).status == "RUNNING"
    assert not any(int(report.get("running_reclaimed") or 0) for report in first)

    stale = scoped.subagents.load(task.id)
    stale.attributes["runner_session"] = _runner_session(heartbeat_at=time.time() - 120.0)
    scoped.subagents.save(stale)

    second = reconciler.tick()

    recovered = scoped.subagents.load(task.id)
    assert recovered.status == "PENDING"
    assert recovered.attributes["background_start"]["status"] == "reclaimed"
    assert starts == [[task.id]]
    assert any(int(report.get("running_reclaimed") or 0) == 1 for report in second)


# 替身 owner 池：按 owner 身份原样返回实例；巡检以 touch=False 租用并 pin，不刷新空闲时间。
# LLM: owner池替身保留get/pin接口及touch参数，不刷新真实owner空闲时间。
# 类用途: 在巡检测试中原样借出已准备的owner对象。
class _PassThroughOwnerPool:
    # LLM: 本替身不解析身份或构造资源，只返回调用方给定对象。
    # 函数用途: 原样取得测试owner实例。
    def get(self, owner, *, touch: bool = True):
        return owner

    # LLM: 只提供借用寿命形状，不创建租约或修改空闲状态。
    # 函数用途: 在测试上下文内原样借出agent。
    @contextmanager
    def pin(self, agent, *, touch: bool = True):
        yield agent


# LLM: 单owner阻塞不能拖住基础巡检和其它owner，线程退出仍须真实等待。
# 函数用途: 验证卡住的owner不造成全局巡检阻塞。
def test_reconciler_stuck_owner_does_not_delay_base_or_other_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, _owner, _scoped = _scoped_restart_fixture(tmp_path)
    slow_owner = OwnerIdentity.provider_user("feishu", "u-slow")
    fast_owner = OwnerIdentity.provider_user("feishu", "u-fast")
    registry = shared_active_owner_registry(base_agent)
    registry.record(slow_owner)
    registry.record(fast_owner)
    monkeypatch.setattr(
        gateway_loops,
        "_gateway_agent_from_context",
        lambda _context: base_agent,
    )
    reconciler = gateway_loops._GatewayOrphanReconciler(
        _context(base_agent, tmp_path)
    )
    reconciler.interval = 0.05
    monkeypatch.setattr(reconciler, "_seed_owner_registry", lambda: None)
    monkeypatch.setattr(
        reconciler,
        "_ensure_owner_pool",
        lambda: _PassThroughOwnerPool(),
    )
    slow_entered = threading.Event()
    release_slow = threading.Event()
    labels: list[str] = []

    # LLM: 仅指定慢owner等待释放，其余调用立即返回，保留真实线程交错。
    # 函数用途: 模拟一个卡住的owner并记录各巡检标签。
    def _sweep(_agent, label: str) -> dict[str, object]:
        labels.append(label)
        if label.endswith("/u-slow"):
            slow_entered.set()
            release_slow.wait(timeout=2.0)
        return {"owner": label}

    monkeypatch.setattr(reconciler, "_sweep", _sweep)
    stop_event = threading.Event()
    thread = threading.Thread(target=reconciler.run, args=(stop_event,))
    thread.start()
    try:
        assert slow_entered.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while (
            labels.count("base") < 2
            or not any(label.endswith("/u-fast") for label in labels)
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert labels.count("base") >= 2
        assert any(label.endswith("/u-fast") for label in labels)
        assert sum(label.endswith("/u-slow") for label in labels) == 1
    finally:
        stop_event.set()
        release_slow.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


# LLM: 队列必须受worker上限约束，并按原近期owner顺序挑选。
# 函数用途: 验证巡检提交有界且优先最近活跃owner。
def test_reconciler_bounds_queue_and_prioritizes_recent_owners(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, _owner, _scoped = _scoped_restart_fixture(tmp_path)
    registry = shared_active_owner_registry(base_agent)
    for index in range(6):
        registry.record(OwnerIdentity.provider_user("feishu", f"u-{index}"))
    monkeypatch.setattr(
        gateway_loops,
        "_gateway_agent_from_context",
        lambda _context: base_agent,
    )
    reconciler = gateway_loops._GatewayOrphanReconciler(
        _context(base_agent, tmp_path)
    )
    reconciler._owner_worker_limit = 2
    reconciler._next_discovery_at = float("inf")
    submitted: list[str] = []

    # LLM: future始终未结，以检验提交端而非完成端的容量门。
    # 类用途: 模拟仍占据巡检槽位的异步工作。
    class _PendingFuture:
        # LLM: 固定未完成状态，不凭等待时长模拟成功。
        # 函数用途: 保持测试future处于进行中。
        def done(self) -> bool:
            return False

    # LLM: 只收集提交，不运行真实owner巡检。
    # 类用途: 观察有界队列的owner提交顺序。
    class _Executor:
        # LLM: 返回未结future保留占槽事实，不改变原挑选算法。
        # 函数用途: 记录被提交的巡检标签。
        def submit(self, _callable, _owner, label):
            submitted.append(label)
            return _PendingFuture()

    reconciler._owner_executor = _Executor()

    reconciler._submit_owner_sweeps(10.0)

    assert submitted == ["feishu/user/u-5", "feishu/user/u-4"]
    assert len(reconciler._owner_inflight) == 2


# LLM: 仅软记忆活动不能升级为硬执行巡检责任。
# 函数用途: 验证只做记忆整理的owner不被额外巡检。
def test_reconciler_does_not_sweep_memory_curator_only_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, _owner, _scoped = _scoped_restart_fixture(tmp_path)
    hard_owner = OwnerIdentity.provider_user("feishu", "u-running")
    soft_owner = OwnerIdentity.provider_user("feishu", "u-memory-only")
    registry = shared_active_owner_registry(base_agent)
    registry.record(hard_owner, hard=True)
    registry.record(soft_owner, hard=False)
    monkeypatch.setattr(
        gateway_loops,
        "_gateway_agent_from_context",
        lambda _context: base_agent,
    )
    reconciler = gateway_loops._GatewayOrphanReconciler(
        _context(base_agent, tmp_path)
    )
    monkeypatch.setattr(reconciler, "_seed_owner_registry", lambda: None)
    monkeypatch.setattr(
        reconciler,
        "_ensure_owner_pool",
        lambda: _PassThroughOwnerPool(),
    )
    labels: list[str] = []

    # LLM: 替身仅观察原筛选后的巡检集合，不主动增加owner。
    # 函数用途: 记录巡检标签并返回最小本地报告。
    def _sweep(_agent, label: str) -> dict[str, object]:
        labels.append(label)
        return {"owner": label}

    monkeypatch.setattr(reconciler, "_sweep", _sweep)

    reports = reconciler.tick()

    assert labels == ["base", "feishu/user/u-running"]
    assert [report["owner"] for report in reports] == labels


# LLM: runner已完成的结构化记录不能因旧心跳被重复派发。
# 函数用途: 验证巡检不重跑已完成的执行会话。
def test_reconciler_does_not_replay_completed_runner_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    completed = scoped.subagents.load(task.id)
    completed.attributes["runner_session"] = {
        **dict(completed.attributes["runner_session"]),
        "status": "completed",
        "ended_at": time.time() - 110.0,
    }
    scoped.subagents.save(completed)
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    recovered = scoped.subagents.load(task.id)
    assert recovered.status == "RUNNING"
    assert starts == []
    assert not any(int(report.get("running_reclaimed") or 0) for report in reports)
    assert not any(int(report.get("orphans_revived") or 0) for report in reports)


# LLM: 冷启动沿持久owner投影发现运行，不依赖旧进程内对象。
# 函数用途: 验证新Gateway上下文发现并回收旧owner运行。
def test_reconciler_cold_start_discovers_owner_projection_and_reclaims(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = AgentConfig(
        enable_tools=False,
        memory_path="memory.jsonl",
        gateway_per_user_owner_scoping=True,
        my_agent_home=str(tmp_path / "home"),
        orphan_supervision_interval_seconds=60,
    )
    owner = OwnerIdentity.provider_user("feishu", "u-cold-restart")
    before_restart = SimpleAgent(config, tmp_path)
    old_scoped = _owner_pool(before_restart).get(owner)
    task = _running_restart_task(old_scoped, heartbeat_at=time.time() - 120.0)
    owner_projection = old_scoped.home_paths.owner_agents_dir / task.id / "state.json"
    assert owner_projection.is_file()

    after_restart = SimpleAgent(config, tmp_path)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: after_restart)
    reconciler = gateway_loops._GatewayOrphanReconciler(_context(after_restart, tmp_path))

    reports = reconciler.tick()

    recovered_scoped = _owner_pool(after_restart).get(owner)
    recovered = recovered_scoped.subagents.load(task.id)
    assert recovered.status == "PENDING"
    assert recovered.attributes["background_start"]["status"] == "reclaimed"
    assert starts == [[task.id]]
    assert any(report.get("owner") == "feishu/user/u-cold-restart" for report in reports)
    assert any(int(report.get("running_reclaimed") or 0) == 1 for report in reports)


# LLM: 父TaskLink和本run都活动才可恢复，单边活动不够授权。
# 函数用途: 验证作用域运行恢复同时核对父子生命周期。
def test_reconciler_recovers_scoped_run_only_while_parent_and_run_are_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _bind_conversation_task(scoped, task)
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "PENDING"
    assert starts == [[task.id]]
    assert any(int(report.get("running_reclaimed") or 0) == 1 for report in reports)


# LLM: 父会话已完成时旧活动run应关闭，不因旧runner记录复活。
# 函数用途: 验证父任务完成后的子运行取消。
def test_reconciler_cancels_old_run_when_parent_conversation_completed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "CANCELLED"
    assert starts == []
    links = {link.task_id: link for link in scoped.conversation_store.tasks.list(thread.thread_id)}
    assert links[parent_task_id].status == "completed"
    assert links[task.id].status == "cancelled"
    assert any(int(report.get("parent_closed_cancelled") or 0) == 1 for report in reports)


# LLM: 父清理不得覆盖子任务已保存的终态结果。
# 函数用途: 验证收尾取消保留既有子终态。
@pytest.mark.parametrize(
    "terminal_status", ["FAILED", "BLOCKED", "TIMEOUT", "CHANNEL_ERROR", "DONE"]
)
def test_parent_cleanup_preserves_existing_child_terminal_result(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    _thread, _parent_task_id = _bind_conversation_task(scoped, task, run_status="failed")
    ended = scoped.subagents.load(task.id)
    ended.status = terminal_status
    ended.failure_type = "runner_error" if terminal_status == "FAILED" else ""
    ended.runner_last_error = "typed Compact failure" if terminal_status == "FAILED" else ""
    scoped.subagents.save(ended)

    decision = conversation_lifecycle_decisions(scoped, [ended])[task.id]
    assert decision.should_cancel
    assert decision.reason == "run_link_closed"

    summary = _reconcile_conversation_parent_lifecycle(scoped)

    preserved = scoped.subagents.load(task.id)
    assert preserved.status == terminal_status
    assert preserved.failure_type == ended.failure_type
    assert preserved.runner_last_error == ended.runner_last_error
    assert "cancel_subagents" not in preserved.attributes
    assert summary["parent_closed_cancelled"] == 0


# LLM: 父会话中断属于原停止事实，不能自动续跑旧子任务。
# 函数用途: 验证父中断后巡检关闭旧运行。
def test_reconciler_cancels_old_run_when_parent_conversation_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "interrupted", "expected_status": "active"}
    )
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "CANCELLED"
    assert starts == []
    assert any(int(report.get("parent_closed_cancelled") or 0) == 1 for report in reports)


# LLM: 父链接缺失是未知，不可按默认活动处理。
# 函数用途: 验证缺父TaskLink的作用域运行保持暂停恢复。
def test_reconciler_holds_scoped_run_when_parent_link_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    thread = scoped.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "u-restart",
            "channel": "feishu",
            "channel_conversation_id": f"chat-{task.id}",
            "channel_user_id": "u-restart",
        }
    )
    stale = scoped.subagents.load(task.id)
    stale.attributes = {
        **dict(stale.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": "missing-parent",
    }
    scoped.subagents.save(stale)
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "RUNNING"
    assert starts == []
    assert any(int(report.get("parent_recovery_held") or 0) == 1 for report in reports)


# LLM: 父链接损坏不能被缓存或缺省值洗成有效身份。
# 函数用途: 验证坏父TaskLink不会触发子运行恢复。
def test_reconciler_holds_scoped_run_when_parent_link_is_corrupt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    (scoped.conversation_store.storage.tasks_dir / f"{parent_task_id}.json").write_text(
        "{broken",
        encoding="utf-8",
    )
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "RUNNING"
    assert starts == []
    assert any(int(report.get("parent_recovery_held") or 0) == 1 for report in reports)


# LLM: 自动启动与显式派发必须共守父终态拒门。
# 函数用途: 验证两个启动出口都拒绝已完成父任务下的运行。
def test_auto_start_and_dispatch_both_reject_run_under_completed_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    pending = scoped.subagents.load(task.id)
    pending.status = "PENDING"
    pending.attributes.pop("background_start", None)
    pending.attributes.pop("runner_session", None)
    scoped.subagents.save(pending)
    monkeypatch.setattr(
        background_dispatch,
        "_start_background_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    auto_start = background_dispatch.auto_start_tasks(scoped, [pending], {})
    report = scoped.dispatch_subagents(
        CapabilityRouter(config=CapabilityConfig(), tool_specs=scoped.tools.specs()),
        CapabilityConfig(),
        params=DispatchParams(
            apply=True,
            start_runners=True,
            include_run_ids=[task.id],
        ),
    )

    assert auto_start["status"] == "blocked"
    assert auto_start["conversation_gate"][0]["reason"] == "parent_link_closed"
    assert any(record.action == "conversation_lifecycle_blocked" for record in report.records)
    assert scoped.subagents.load(task.id).status == "PENDING"


# LLM: 活动claim与父投影分责，投影完成不能抹掉原已授执行责任。
# 函数用途: 验证有效claim在父展示投影结束后仍被准确处理。
def test_live_claim_keeps_child_allowed_after_parent_projection_completed(tmp_path: Path) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    claim = scoped.conversation_store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": parent_task_id,
            "reason": "user_guidance",
            "lease_seconds": 300,
        }
    )
    assert claim is not None

    current = scoped.subagents.load(task.id)
    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert decision.allowed is True
    assert decision.reason == "parent_execution_active"
    assert decision.parent_status == "completed"
    assert decision.parent_allows_children is True


# LLM: 显式停止再恢复只可重开原准确运行，不能波及其它会话任务。
# 函数用途: 验证用户恢复沿原会话运行身份执行。
def test_explicit_user_stop_resume_reopens_only_the_same_conversation_run(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, _parent_task_id = _bind_conversation_task(scoped, task)
    stopped = scoped.subagents.load(task.id)
    stopped.status = "CANCELLED"
    stopped.failure_type = "cancelled"
    stopped.verification_status = "UNVERIFIED"
    stopped.attributes = {
        **dict(stopped.attributes or {}),
        "cancel_subagents": {
            "reason": "conversation_user_stop",
            "previous_status": "RUNNING",
            "previous_failure_type": "",
        },
    }
    scoped.subagents.save(stopped)
    cancelled_link = scoped.conversation_store.tasks.update_status(
        {
            "task_id": task.id,
            "status": "cancelled",
            "expected_status": "active",
        }
    )
    assert cancelled_link is not None

    closed = conversation_lifecycle_decisions(scoped, [stopped])[task.id]
    explicit_resume = conversation_lifecycle_decisions(
        scoped,
        [stopped],
        resume_run_ids={task.id},
    )[task.id]

    assert closed.allowed is False
    assert closed.reason == "run_link_closed"
    assert explicit_resume.allowed is True
    assert explicit_resume.reason == "user_stopped_same_run_resume"
    assert explicit_resume.thread_id == thread.thread_id

    prepared = scoped.subagents.lifecycle.prepare_runner_attempt(
        task.id,
        retry_reason="reason_code=conversation_user_stop; mode=same_run_resume",
    )
    links, errors = scoped.conversation_store.tasks.list_report(thread.thread_id)
    child_link = next(item for item in links if item.task_id == task.id)

    assert errors == []
    assert prepared.id == task.id
    assert prepared.status == "RUNNING"
    assert child_link.status == "active"
    assert [item.id for item in scoped.subagents.list_runs()].count(task.id) == 1


# LLM: 独立活动Audit来源责任不因前台run链接关闭而消失。
# 函数用途: 验证前台结束后有效来源worker继续保留。
def test_active_audit_source_worker_survives_closed_foreground_run_link(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(
        scoped,
        task,
        run_status="cancelled",
    )
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.totals["spool_candidates"] = 1
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert decision.allowed is True
    assert decision.reason == "audit_source_watch_active"
    assert decision.parent_allows_children is True


# LLM: 无积压变化的来源等待不应重复调用模型，读取以磁盘进展为准。
# 函数用途: 验证空闲来源等到真实积压推进再恢复。
def test_idle_audit_source_worker_waits_without_model_call_until_disk_backlog_advances(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(
        scoped,
        task,
        run_status="cancelled",
    )
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://idle-audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "idle-audit-source"
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    idle = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert idle.allowed is False
    assert idle.action == "hold"
    assert idle.reason == "audit_source_waiting_for_records"

    # Simulate a harvester in another process committing one complete record.
    # The already-cached lifecycle state must refresh this monotonic counter
    # from disk before deciding whether to dispatch the model worker.
    state_path = state_dir(owner_home) / f"{state.watch_id}.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["totals"]["spool_candidates"] = 1
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    ready = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert ready.allowed is True
    assert ready.reason == "audit_source_watch_active"


# LLM: watch活动不覆盖父Audit已取消事实。
# 函数用途: 验证来源watch不能复活已取消命名Audit。
def test_active_watch_cannot_revive_after_named_audit_parent_is_cancelled(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    thread, parent_task_id = _bind_conversation_task(
        scoped,
        task,
        parent_status="cancelled",
    )
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://cancelled-audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "cancelled-audit-source"
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.runner_active_attempt_id = ""
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]
    summary = _reconcile_conversation_parent_lifecycle(scoped)

    assert decision.allowed is False
    assert decision.should_cancel is True
    assert decision.reason == "parent_link_closed"
    assert summary["parent_closed_cancelled"] == 1
    assert scoped.subagents.load(task.id).status == "CANCELLED"
    closed_state = load_state(owner_home, state.watch_id)
    assert closed_state is not None
    assert closed_state.closed is True
    assert closed_state.close_reason == "audit_parent_inactive"


# LLM: 控制关闭与自然完成分开，不能把取消记成完成。
# 函数用途: 验证关闭的来源worker投影为CANCELLED。
def test_closed_audit_source_worker_is_cancelled_not_completed(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.closed = True
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert decision.allowed is False
    assert decision.should_cancel is True
    assert decision.should_complete is False
    assert decision.reason == "audit_source_watch_closed"


# LLM: 自然结算消费真实来源终态，完成与核验字段须共同投影。
# 函数用途: 验证自然结束来源投影为DONE和VERIFIED。
def test_naturally_settled_audit_source_worker_projects_done_verified(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.watch_window_seconds = 1
    state.opened_at = time.time() - 10
    state.window_finalized_at = time.time() - 1
    state.totals["spool_candidates"] = 0
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.runner_active_attempt_id = ""
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    decision = conversation_lifecycle_decisions(
        scoped,
        [scoped.subagents.load(task.id)],
    )[task.id]
    summary = _reconcile_conversation_parent_lifecycle(scoped)

    completed = scoped.subagents.load(task.id)
    links = {
        link.task_id: link
        for link in scoped.conversation_store.tasks.list(thread.thread_id)
    }
    assert decision.allowed is False
    assert decision.should_cancel is False
    assert decision.should_complete is True
    assert decision.reason == "audit_source_watch_complete"
    assert completed.status == "DONE"
    assert completed.verification_status == "VERIFIED"
    assert completed.attributes["audit_source_terminal"]["reason"] == (
        "watch_window_settled"
    )
    assert links[task.id].status == "completed"
    assert summary["source_workers_completed"] == 1
    assert summary["parent_closed_cancelled"] == 0


# LLM: 最终边界事件的原事实可驱动结算投影，不依赖正文里的状态词。
# 函数用途: 验证缺显式status时仍按最终来源边界收尾。
def test_final_boundary_event_projects_settled_source_worker_without_status(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.watch_window_seconds = 1
    state.opened_at = time.time() - 10
    state.window_finalized_at = time.time() - 1
    state.totals["spool_candidates"] = 0
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.runner_active_attempt_id = ""
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    assert settle_audit_source_worker(scoped, state) is True

    completed = scoped.subagents.load(task.id)
    assert completed.status == "DONE"
    assert completed.verification_status == "VERIFIED"
    assert completed.attributes["audit_source_terminal"]["reason"] == (
        "watch_window_settled"
    )


# LLM: 只按已结来源事实修复历史误取消，不能放开普通取消。
# 函数用途: 验证来源自然结算可纠正旧错误取消投影。
def test_settled_source_worker_repairs_legacy_false_cancellation(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.watch_window_seconds = 1
    state.opened_at = time.time() - 10
    state.window_finalized_at = time.time() - 1
    state.totals["spool_candidates"] = 0
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "CANCELLED"
    current.failure_type = "cancelled"
    current.runner_active_attempt_id = ""
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
        "cancel_subagents": {
            "reason": "conversation_lifecycle:audit_source_watch_complete",
            "previous_status": "RUNNING",
        },
    }
    scoped.subagents.save(current)
    assert scoped.conversation_store.tasks.update_status(
        {
            "task_id": task.id,
            "status": "cancelled",
            "expected_status": "active",
        }
    ) is not None

    summary = _reconcile_conversation_parent_lifecycle(scoped)

    repaired = scoped.subagents.load(task.id)
    links = {
        link.task_id: link
        for link in scoped.conversation_store.tasks.list(thread.thread_id)
    }
    assert repaired.status == "DONE"
    assert repaired.verification_status == "VERIFIED"
    assert "cancel_subagents" not in repaired.attributes
    assert repaired.attributes["audit_source_terminal"]["previous_status"] == (
        "CANCELLED"
    )
    assert links[task.id].status == "completed"
    assert summary["source_workers_completed"] == 1


# LLM: 恢复命名提示不能覆盖真实手工取消状态。
# 函数用途: 验证手工取消的运行不会被名称暗中重开。
def test_manual_cancel_stays_closed_even_when_named_as_a_resume_run(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    _thread, _parent_task_id = _bind_conversation_task(scoped, task)
    cancelled = scoped.subagents.load(task.id)
    cancelled.status = "CANCELLED"
    cancelled.failure_type = "cancelled"
    cancelled.attributes = {
        **dict(cancelled.attributes or {}),
        "cancel_subagents": {
            "reason": "administrator_cancel",
            "previous_status": "RUNNING",
        },
    }
    scoped.subagents.save(cancelled)
    assert scoped.conversation_store.tasks.update_status(
        {
            "task_id": task.id,
            "status": "cancelled",
            "expected_status": "active",
        }
    ) is not None

    decision = conversation_lifecycle_decisions(
        scoped,
        [cancelled],
        resume_run_ids={task.id},
    )[task.id]

    assert decision.allowed is False
    assert decision.reason == "run_link_closed"


# LLM: 同父子任务共用本次执行快照，不能逐子重复查询或混入不同观察点。
# 函数用途: 验证已完成父任务的子运行检查复用一次执行事实。
def test_completed_parent_children_share_one_execution_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    first = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, first)
    second = _running_restart_task(scoped, heartbeat_at=time.time())
    scoped.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": second.id,
            "goal": second.goal,
            "status": "active",
        }
    )
    second = scoped.subagents.load(second.id)
    second.attributes = {
        **dict(second.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(second)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    assert scoped.conversation_store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": parent_task_id,
            "reason": "user_guidance",
            "lease_seconds": 300,
        }
    ) is not None

    calls = {"policies": 0, "claim": 0}
    original_policies = scoped.conversation_store.progress.list_report
    original_claim = scoped.conversation_store.claims.load_report

    # LLM: 透传原策略查询，仅计数，不改变终态或恢复授权。
    # 函数用途: 观察共享快照中的策略读取次数。
    def policies(*args, **kwargs):
        calls["policies"] += 1
        return original_policies(*args, **kwargs)

    # LLM: 透传原claim查询，不伪造存活执行责任。
    # 函数用途: 观察共享快照中的claim读取次数。
    def claim(*args, **kwargs):
        calls["claim"] += 1
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(scoped.conversation_store.progress, 'list_report', policies)
    monkeypatch.setattr(scoped.conversation_store.claims, "load_report", claim)

    current = [scoped.subagents.load(first.id), scoped.subagents.load(second.id)]
    decisions = conversation_lifecycle_decisions(scoped, current)

    assert all(decisions[item.id].reason == "parent_execution_active" for item in current)
    assert calls == {"policies": 1, "claim": 1}


# LLM: 执行状态不可读保持未知，不凭父投影完成推断子运行可恢复。
# 函数用途: 验证坏执行状态会阻止子任务自动恢复。
def test_unreadable_execution_state_holds_child_under_completed_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    monkeypatch.setattr(
        scoped.conversation_store.claims,
        "load_report",
        lambda _thread_id: ({"task_id": parent_task_id, "expires_at": "invalid"}, None),
    )

    current = scoped.subagents.load(task.id)
    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert decision.allowed is False
    assert decision.should_cancel is False
    assert decision.reason == "parent_execution_state_unavailable"


# LLM: 原create_run生产真实locator/canonical后，仅移除本例临时canonical；计数不替换原读取实现。
# 函数用途: 复现历史子任务清理后的重复加载，并保留稍后恢复原文件的真实字节。
def _missing_canonical_case(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.agent_run_state import (
        canonical_state_path_from_payload,
    )

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path='memory.jsonl'), tmp_path)
    task = agent.subagents.create_run(goal='missing canonical cache', agent_name='worker')
    service = agent.subagents.persistence
    locator = service.workspace / task.id / 'task.json'
    payload = json.loads(locator.read_text(encoding='utf-8'))
    canonical = canonical_state_path_from_payload(payload)
    raw = canonical.read_bytes()
    canonical.unlink()
    stamp = locator.stat()
    os.utime(locator, ns=(stamp.st_atime_ns, time.time_ns() - 10_000_000_000))
    service._run_cache.clear()
    original_load = service.load
    original_read = Path.read_text
    calls, reads = [], []

    # LLM: Spy透传真实canonical loader；不把错误换成伪成功，也不改变磁盘读门。
    # 函数用途: 计数每轮全量扫描是否又进入同一个缺失locator的重解析。
    def load(run_id):
        calls.append(run_id)
        return original_load(run_id)

    # LLM: 只计本例准确locator的打开，其他产品文件仍由原Path读口处理。
    # 函数用途: 证明缓存命中不反复打开locator正文。
    def read(path, *args, **kwargs):
        if path == locator:
            reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(service, 'load', load)
    monkeypatch.setattr(Path, 'read_text', read)
    return SimpleNamespace(agent=agent, service=service, run_id=task.id, locator=locator,
        canonical=canonical, raw=raw, payload=payload, calls=calls, reads=reads)


# LLM: 已确认缺失的规范定位器可负缓存，不能每轮重读同一不存在文件。
#   ee2113192 起缓存层先解析 locator，规范文件缺失时直接报缺失、不再进入完整 loader，故以 locator 读取次数计重解析。
# 函数用途: 验证孤儿巡检复用规范位置缺失结果。
def test_missing_canonical_locator_is_loaded_only_once_across_orphan_scans(tmp_path, monkeypatch):
    case = _missing_canonical_case(tmp_path, monkeypatch)
    for _ in range(5):
        report = case.service.list_runs_report()
        assert report.runs == []
        assert len(report.load_errors) == 1 and report.load_errors[0]['run_id'] == case.run_id
    assert case.calls == []
    assert len(case.reads) <= 2


# LLM: 目标重新出现立即废弃缺失缓存，不能遮蔽新canonical事实。
# 函数用途: 验证规范文件重建后重新加载。
def test_missing_canonical_cache_reloads_when_target_reappears(tmp_path, monkeypatch):
    case = _missing_canonical_case(tmp_path, monkeypatch)
    assert case.service.list_runs_report().load_errors
    assert case.service.list_runs_report().load_errors
    case.canonical.write_bytes(case.raw)
    report = case.service.list_runs_report()
    assert report.load_errors == [] and [run.id for run in report.runs] == [case.run_id]
    # 缺失期由 locator 预检报错，只有目标重现后的那次才进入完整 loader。
    assert case.calls == [case.run_id]
    assert case.run_id not in getattr(case.service, '_missing_canonical_cache', {})


# LLM: 定位器替换即使保留旧mtime也须让缺失缓存失效。
# 函数用途: 验证原子替换定位器能触发重读。
def test_missing_canonical_cache_invalidates_on_locator_replace_with_old_mtime(tmp_path, monkeypatch):
    case = _missing_canonical_case(tmp_path, monkeypatch)
    assert case.service.list_runs_report().load_errors
    assert case.service.list_runs_report().load_errors
    target = case.canonical.parent.with_name('relocated') / case.canonical.name
    target.parent.mkdir(parents=True)
    canonical_payload = {**json.loads(case.raw), 'goal': 'relocated canonical', 'agent_run_workspace_dir': str(target.parent)}
    target.write_text(json.dumps(canonical_payload), encoding='utf-8')
    replacement = case.locator.with_suffix('.replacement')
    replacement.write_text(json.dumps({**case.payload, 'agent_run_workspace_dir': str(target.parent)}), encoding='utf-8')
    stamp = case.locator.stat()
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(case.locator)
    report = case.service.list_runs_report()
    assert report.load_errors == []
    assert report.runs[0].goal == 'relocated canonical'
    assert case.calls == [case.run_id]


# LLM: 新近缺失仍在保护窗口内，不能过早负缓存并掩盖发表竞态。
# 函数用途: 验证缺失缓存遵守原保护窗口。
def test_recent_missing_canonical_is_not_cached_until_guard_window_expires(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import service as persistence_module

    case = _missing_canonical_case(tmp_path, monkeypatch)
    mtime = case.locator.stat().st_mtime_ns / 1e9
    guard = case.service._RUN_CACHE_COARSE_MTIME_GUARD_SECONDS
    monkeypatch.setattr(persistence_module, '_now', lambda: mtime + guard / 2)
    assert case.service.list_runs_report().load_errors
    assert case.service.list_runs_report().load_errors
    # 缺失由 locator 预检确认（不进完整 loader）；窗口内每轮都重读 locator，过窗后只再读一次即负缓存。
    assert case.calls == [] and len(case.reads) == 2
    assert case.run_id not in getattr(case.service, '_missing_canonical_cache', {})
    monkeypatch.setattr(persistence_module, '_now', lambda: mtime + guard + 1)
    assert case.service.list_runs_report().load_errors
    assert case.service.list_runs_report().load_errors
    assert case.calls == [] and len(case.reads) == 3


# LLM: 定位器删除须精确清理对应负缓存，不依赖其它run是否仍存在。
# 函数用途: 验证删除一个定位器后其旧缓存不再保留。
def test_deleted_locator_evicts_negative_cache_with_other_runs_still_present(tmp_path, monkeypatch):
    case = _missing_canonical_case(tmp_path, monkeypatch)
    assert case.service.list_runs_report().load_errors
    cache = getattr(case.service, '_missing_canonical_cache', {})
    assert case.run_id in cache
    other = case.agent.subagents.create_run(goal='unrelated live run', agent_name='worker')
    case.locator.unlink()
    report = case.service.list_runs_report()
    assert report.load_errors == [] and [run.id for run in report.runs] == [other.id]
    assert case.run_id not in cache


# LLM: 非缺失错误不能被降格为not-found并缓存，失败事实必须保留。
# 函数用途: 验证规范读取的其它错误不进入负缓存。
@pytest.mark.parametrize('failure', ['invalid_json', 'permission'])
def test_nonmissing_canonical_errors_never_enter_negative_cache(tmp_path, monkeypatch, failure):
    case = _missing_canonical_case(tmp_path, monkeypatch)
    if failure == 'invalid_json':
        case.locator.write_text('{broken', encoding='utf-8')
    else:
        # 规范文件存在时才越过 locator 缺失预检，让权限错误真正来自完整 loader。
        case.canonical.write_bytes(case.raw)

        # LLM: 合成权限错误不代表文件不存在；不得以负缓存将它固化为缺失。
        # 函数用途: 保留每轮真实错误重试，不把权限失败静默成空任务列表。
        def denied(run_id):
            case.calls.append(run_id)
            raise PermissionError('synthetic denied')

        monkeypatch.setattr(case.service, 'load', denied)
    assert case.service.list_runs_report().load_errors
    assert case.service.list_runs_report().load_errors
    # 坏 locator 在预检解析时报错、不进 loader；两种错误都每轮重读 locator，不进入负缓存。
    assert case.calls == ([] if failure == 'invalid_json' else [case.run_id, case.run_id])
    assert len(case.reads) == 2
    assert case.run_id not in getattr(case.service, '_missing_canonical_cache', {})


# LLM: 符号链接改指向改变规范来源，旧缺失缓存不能继续沿用。
# 函数用途: 验证定位器改指向后重新核对目标。
def test_missing_canonical_cache_does_not_hide_symlink_retarget(tmp_path, monkeypatch):
    case = _missing_canonical_case(tmp_path, monkeypatch)
    link = case.canonical.parent.with_name('canonical-link')
    link.symlink_to(case.canonical.parent, target_is_directory=True)
    case.locator.write_text(json.dumps({**case.payload, 'agent_run_workspace_dir': str(link)}), encoding='utf-8')
    stamp = case.locator.stat()
    os.utime(case.locator, ns=(stamp.st_atime_ns, time.time_ns() - 10_000_000_000))
    assert case.service.list_runs_report().load_errors
    restored = case.canonical.parent.with_name('canonical-restored') / case.canonical.name
    restored.parent.mkdir(parents=True)
    restored.write_bytes(case.raw)
    link.unlink()
    link.symlink_to(restored.parent, target_is_directory=True)
    report = case.service.list_runs_report()
    assert report.load_errors == []
    assert [run.id for run in report.runs] == [case.run_id]
