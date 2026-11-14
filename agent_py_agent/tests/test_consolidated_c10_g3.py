"""Wave 3 consolidated tests (test_consolidated_c10_g3.py).

Merged sources:
- test_rework_r9_capacity_wait_admission.py: R9-17：子代理容量已满是预期等待，不计执行失败；容量释放后由同一已受理任务继续。
- test_rework_r9_cli_task_control_races.py: R9-12 调用方与竞态：PAUSED 与已落盘未投影的控制在启动预留、最终激活、协作选择、迟到启动回执、runner 心跳、
- test_rework_r9_detached_credential_scope.py: R9-22：detached 任务的审批、凭据与自学习授权读持久 TaskLink 声明的原 detached claim 车道；旧代、已取消与外来车道一律拒绝。
- test_rework_r9_extraction_quota_lock_order.py: R9-R1 P1-R9-10：提炼提交与资产投递在同一 owner 上按 quota→资产锁同序，1 GB 配额下并发不成环。
- test_rework_r9_gateway_admission_cleanup.py: R9-R1 P1-R9-21：Count0 准入记账后，移交原 executor 前的每个出口都恰好释放一次。
- test_rework_r9_gateway_drain_auxiliary.py: R9-R1 P1-R9-16：原审批桥正常消费后遗留的空锁设施不再阻断停止库存与排空快照；未结/活跃/危险布局仍阻断。
- test_rework_r9_harvester_request_cleanup.py
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
    auto_start_orphan_run,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.collaboration_candidates import (
    _can_run_for_collaboration_request,
)
from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
    ResolveCapabilityRequestsTool,
)
from agent_py_agent.agent.agent_core.runner.session_pool import (
    RunnerSessionPoolLease,
    _relay_runner_stop,
    runner_session_lease,
)
from agent_py_agent.agent.assets import turn_extractor as extract
from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.common.json_io import locked_json_path, read_json_object_report
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.concurrency.interrupt import is_interrupted, register_interruptible
from agent_py_agent.agent.contracts.state_machine import (
    RunStateFacts,
    can_dispatch,
    can_repair,
    lifecycle_phase,
    normalize_status,
    recovery_decision,
    run_state_snapshot_from_task,
    terminal_outcome,
    waiting_reason,
)
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from agent_py_agent.agent.conversation.agent_tool_approval import _agent_approval_session_scope
from agent_py_agent.agent.conversation.execution_threads import IM_TASK_EXECUTION_KEY
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.tool_approval_scope import (
    tool_approval_scope,
    tool_approval_scope_is_current,
)
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_POISON_SAME_CAUSE_LIMIT_COUNT,
    WAKE_UNCOUNTED_STALL_SECONDS,
    WAKE_VERDICT_FAILURE,
    WAKE_VERDICT_NEUTRAL,
    WakePoisonState,
    next_poison_state,
    quarantine_decision,
    verdict_for_admission,
)
from agent_py_agent.agent.core import SimpleAgent, configure_canonical_background_contracts
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts import (
    multiprocess_background_execution as bridge,
)
from agent_py_agent.agent.gateway_parts import (
    multiprocess_mode as mode,
)
from agent_py_agent.agent.gateway_parts import (
    request_worker as rw,
)
from agent_py_agent.agent.gateway_parts import (
    standalone_inventory as inventory,
)
from agent_py_agent.agent.gateway_parts.command_stream import serve_command_stream
from agent_py_agent.agent.gateway_parts.command_stream_protocol import command_approval_path
from agent_py_agent.agent.gateway_parts.io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    gateway_request_fingerprint,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    freeze_source,
    read_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import bind_claimed_request
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_claim import (
    ClaimCandidate,
    freeze_claim_snapshot,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_sources import (
    durable_drain_snapshot,
    durable_items,
    same_durable_snapshot,
)
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.multiprocess_route import (
    RouteStamp,
    WorkerPartition,
    freeze_request_route,
)
from agent_py_agent.agent.gateway_parts.multiprocess_stop_scope import CoordinatorStopScope
from agent_py_agent.agent.gateway_parts.paths import (
    GatewayPaths,
    claimed_request_chunk_path,
    gateway_chunk_path,
    gateway_paths_from_root,
)
from agent_py_agent.agent.gateway_parts.permission_bridge import (
    gateway_permission_decision_path,
    write_gateway_permission_decision,
)
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_migration_errors import (
    StandaloneMigrationRejected,
)
from agent_py_agent.agent.gateway_parts.standalone_runtime_inventory import responsibility_digest
from agent_py_agent.agent.gateway_parts.standalone_stop import record_standalone_stop
from agent_py_agent.agent.gateway_parts.stream_approval import (
    StreamApproval,
    StreamApprovalRequestOptions,
)
from agent_py_agent.agent.ingestion import (
    harvest_execution as execution,
)
from agent_py_agent.agent.ingestion import (
    harvest_parent,
    source_worker,
)
from agent_py_agent.agent.ingestion import (
    harvester as hv,
)
from agent_py_agent.agent.ingestion import (
    watch_state as ws,
)
from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.deployment_policy import update_deployment_policy
from agent_py_agent.agent.subagents import cancellation
from agent_py_agent.agent.subagents.process_control import BackgroundStartUpdate
from agent_py_agent.agent.subagents.recovery_eligibility import user_stopped_run_is_resumable
from agent_py_agent.agent.subagents.runner_control import runner_attempt_cancelled
from agent_py_agent.agent.subagents.runner_start import reserve_runner_start
from agent_py_agent.agent.subagents.services import lifecycle_runner_attempts
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
from agent_py_agent.agent.subagents.task_control import (
    TaskControlError,
    confirm_task_control_cleanup,
    control_task,
    task_control_record,
)
from agent_py_agent.agent.tooling.process_registry import capture_process_birth_token
from agent_py_agent.agent.tooling.skill_summarize_tool import _authorized_learning_scope
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.local_user_store import LocalUser, LocalUserStore
from agent_py_agent.agent.user_space.owner_quota import (
    OwnerQuotaLockContention,
    owner_quota_enforcer_from_policy,
)
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.agent.user_space.task_credentials import (
    credential_headers,
    credential_path,
    request_credential_card,
)
from agent_py_agent.cli import common, gateway_process
from agent_py_agent.cli.gateway_coordinated_run import _cold_gateway_paths
from agent_py_agent.cli.gateway_loops import _build_background_scheduler
from agent_py_agent.tests.support.multiprocess_dispatch_cases import reservation_for
from agent_py_agent.tests.test_im_delegation_single_speaker import _set_route, ask, ordinary_chain

# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r9_capacity_wait_admission.py
# R9-17：子代理容量已满是预期等待，不计执行失败；容量释放后由同一已受理任务继续。
#
# 真实 Gateway 请求、SessionTask、唤醒尝试账、后台调度与 create_subagents 容量规则；只有供应商与 IM 运输是记录型替身。
# ==========================================================================

# 相邻两拍间隔超过不计数退避上限（300 秒），每一拍这条唤醒都已到期。
_TICK_GAP_SECONDS = 400.0


# LLM: 原执行路径里容量等待在模型回合内抛出，默认部署重试阶梯会按文本把它当供应商过载原地退避约六分钟
#   （R3 范围外的原生阻塞，见交付记录）。这里经真实部署策略写口显式关闭该阶梯，执行路径本身不替换。
# 函数用途: 在 Agent 构造前为临时宿主写入"关闭供应商短链重试"的部署策略。
def _disable_provider_retry_ladder(tmp_path):
    host = replace(home_paths(tmp_path / "home"), owner_provider="local", owner_kind="main", owner_id="main")
    update_deployment_policy(trusted_host_paths=host, expected_revision=0,
                             changes={"runtime_guards": {"provider_transient_auto_resume_delays_seconds": []}})


def _thread_wakes(store, thread_id):
    return [signal for signal in store.wakes.pending(limit=0) if signal.thread_id == thread_id]


def _children_for(agent, task_id):
    return [run for run in agent.subagents.list_runs()
            if run.attributes.get("conversation_request_id") == task_id]


# LLM: 当前IM任务分派须先经可信route开关，委派偏好不能隐式开启路由；原五拍与同任务续跑断言不变。
# LLM: 测试屏障只延后真实孩子进入core，不替换容量裁决或执行结果；五拍后解除，必须由原runner完成。
# 函数用途: 在真正启用任务路由的普通链上核验容量等待与释放后的继续执行。
def test_five_capacity_waits_stay_uncounted_and_same_task_continues(tmp_path, monkeypatch, request):
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.runtime_context import current_subagent_run_id
    from agent_py_agent.agent.subagents.manager import creation_capacity_counts

    _disable_provider_retry_ladder(tmp_path)
    agent, paths, _provider, calls, _ = ordinary_chain(tmp_path, monkeypatch)
    child_entered = Event()
    release_child = Event()
    dispatch_finished = Event()
    request.addfinalizer(release_child.set)
    original_run = SimpleAgent.run
    original_dispatch = dispatch._background_dispatch_worker
    original_start = dispatch._start_inprocess_dispatch
    dispatch_returns = {}

    # 后继孩子极快完成时，它的启动/收尾写入会与同tick第一任务的完成唤醒交叠。
    # 容量资格用例只等自己新派工的真实返回，不改变默认tick范围或吞掉历史读取错误。
    def start(current, dispatch_request, mark_errors=None):
        finished = None
        if current is agent:
            finished = Event()
            dispatch_returns[dispatch_request.launch_id] = finished
        result = original_start(current, dispatch_request, mark_errors)
        if finished is not None and len(dispatch_returns) > 1:
            assert finished.wait(timeout=30), 'capacity test successor dispatch did not finish'
        return result

    # LLM: 保留真实run输入与返回，只让占位孩子等待显式测试事件；有界超时失败且异常退出亦释放。
    # 函数用途: 排除离线孩子在第五拍前已跑完的时序竞态。
    def run(current, prompt, *, params):
        if current_subagent_run_id(current):
            child_entered.set()
            if not release_child.wait(timeout=120):
                raise TimeoutError('capacity test child release was not signalled')
        return original_run(current, prompt, params=params)

    # DONE 后仍有 runner-session/background_start 提交；观察真实派工返回才是尾账完成边界。
    def observe_dispatch(dispatch_request):
        try:
            return original_dispatch(dispatch_request)
        finally:
            if dispatch_request.agent is agent:
                dispatch_finished.set()
                dispatch_returns[dispatch_request.launch_id].set()

    monkeypatch.setattr(SimpleAgent, 'run', run)
    monkeypatch.setattr(dispatch, '_background_dispatch_worker', observe_dispatch)
    monkeypatch.setattr(dispatch, '_start_inprocess_dispatch', start)
    assert agent.runtime_guard_policy.provider_transient_auto_resume_delays_seconds == ()
    store = agent.conversation_store
    _set_route(agent, True)
    ask(agent, paths, "first", "整理工作区里的资料")
    scheduler = _build_background_scheduler(agent, agent.delivery_service)
    base = time.time()
    scheduler.tick(now=base)
    (first,) = store.session_tasks.list_report(limit=0)[0]
    (occupant,) = _children_for(agent, first.task_id)
    # tick 返回不代表异步 runner 已完成启动记账；先固定阻塞位置，再等权威容量收敛。
    # 不重试五拍中的失败，也不把 pending 索引当作正常容量等待。
    scheduler.prepare_tick(now=base + _TICK_GAP_SECONDS)
    assert child_entered.wait(timeout=30), 'capacity test child did not enter the run barrier'
    deadline = time.monotonic() + 30
    while True:
        try:
            settled = creation_capacity_counts(agent, '')[0] == 1
        except RuntimeError:
            settled = False
        if settled:
            break
        assert time.monotonic() < deadline, agent.subagents.load(occupant.id).status
        time.sleep(0.01)
    # 原 owner 策略只留一个子代理名额，已有孩子占满；第二个任务沿原受理路径进入后台。
    agent.owner_policy = replace(agent.owner_policy, max_subagents=1)
    ask(agent, paths, "second", "整理另一组资料")
    tasks, errors = store.session_tasks.list_report(limit=0)
    assert not errors
    (second,) = [task for task in tasks if task.task_id != first.task_id]
    (wake,) = _thread_wakes(store, second.target_thread_id)
    held, base = None, base + 2 * _TICK_GAP_SECONDS
    for index in range(5):
        before = len(calls)
        scheduler.tick(now=base + index * _TICK_GAP_SECONDS)
        state, error = store.wakes.attempts.state_report(wake.wake_signal_id)
        assert error is None
        # 每一拍都真的领到并走到容量准入，按预期等待记账，不是没轮到。
        assert [call for call in calls[before:] if call["thread"] == second.target_thread_id and not call["child"]]
        assert state.uncounted_count == index + 1, state
        assert state.uncounted_reason_code == "admission:im_subagent_capacity"
        assert (state.total_count, state.same_cause_count, state.reason_code) == (0, 0, "")
        assert quarantine_decision(state) is None
        current = store.session_tasks.load(second.task_id)
        assert current.status not in {"done", "failed", "cancelled"}
        assert current.task_run_id == second.task_run_id
        held = held or current.status
        assert current.status == held
        assert [signal.wake_signal_id for signal in _thread_wakes(store, second.target_thread_id)] == [wake.wake_signal_id]
        assert not _children_for(agent, second.task_id)
    assert store.wakes.attempts.quarantined_count() == 0

    # 已有孩子由原调度监督与 runner 正常跑完，名额自然释放；之后由同一任务、同一唤醒继续委派。
    release_child.set()
    assert dispatch_finished.wait(timeout=120), 'capacity test child dispatch did not finish'
    assert creation_capacity_counts(agent, '')[0] == 0
    assert agent.subagents.load(occupant.id).status == 'DONE'

    deadline, index = time.monotonic() + 120, 5
    while not _children_for(agent, second.task_id):
        assert time.monotonic() < deadline, agent.subagents.load(occupant.id).status
        scheduler.tick(now=base + index * _TICK_GAP_SECONDS)
        index += 1
        time.sleep(0.05)
    assert agent.subagents.load(occupant.id).status == "DONE"
    (child,) = _children_for(agent, second.task_id)
    thread = store.threads.require(second.target_thread_id)
    assert thread.metadata[IM_TASK_EXECUTION_KEY]["child_run_ids"] == [child.id]
    resumed = store.session_tasks.load(second.task_id)
    assert (resumed.task_id, resumed.task_run_id, resumed.status) == (second.task_id, second.task_run_id, held)
    assert wake.wake_signal_id not in {signal.wake_signal_id for signal in store.wakes.pending(limit=0)}
    state, error = store.wakes.attempts.state_report(wake.wake_signal_id)
    assert error is None and state == WakePoisonState()
    assert store.wakes.attempts.quarantined_count() == 0


def test_capacity_wait_is_neutral_while_other_admissions_still_count():
    assert verdict_for_admission("im_subagent_capacity").kind == WAKE_VERDICT_NEUTRAL
    for code in ("host_delivery_consumed", "wake_source_unreadable", "im_subagent_capacity_v2"):
        assert verdict_for_admission(code).kind == WAKE_VERDICT_FAILURE


def test_capacity_wait_keeps_configured_neutral_stall_closeout():
    verdict = verdict_for_admission("im_subagent_capacity")
    state, now = WakePoisonState(), 1_000.0
    for _ in range(WAKE_POISON_SAME_CAUSE_LIMIT_COUNT):
        state = next_poison_state(state, verdict, now=now)
        now = state.next_attempt_at
    # 短时间连续等待只是退避，不结案、不计失败。
    assert (state.total_count, state.same_cause_count) == (0, 0)
    assert quarantine_decision(state) is None
    # 原 24 小时不计数窗口仍按原规则结案，原因保留为这次等待。
    state = next_poison_state(state, verdict, now=state.neutral_since + WAKE_UNCOUNTED_STALL_SECONDS)
    decision = quarantine_decision(state)
    assert decision is not None and decision.reason_code == "admission:im_subagent_capacity"
    assert (decision.total_count, decision.same_cause_count) == (0, 0)



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r9_cli_task_control_races.py
# R9-12 调用方与竞态：PAUSED 与已落盘未投影的控制在启动预留、最终激活、协作选择、迟到启动回执、runner 心跳、
# 整份旧快照保存与窄心跳里都保持住；授权裁决不复活暂停的孩子；共享宿主上的兄弟不受影响；状态机把 PAUSED 登记为
# 非终态、不可派工、不可修复的等待。
#
# 真实 SimpleAgent、SubAgentManager、RuntimeDB、ConversationStore、原租约与原控制服务；控制中途失败用一次模拟 I/O 错误代替。
# ==========================================================================

def _agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path / "workspace")


# LLM: 孩子沿原 create_run 建立并按原写法关联到父会话；需要运行时由原 lifecycle 准备 exact attempt。
# 函数用途: 建一个直属根会话任务的孩子，返回 (agent, child, attempt)。
def _child(tmp_path, *, agent=None, running=True, goal="整理资料", **create_kwargs):
    agent = agent or _agent(tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": agent.home_paths.owner_id,
        "channel_conversation_id": "session-main", "channel_user_id": "local-agent",
    })
    if store.tasks.load_report("task-root")[0] is None:
        store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-root", "goal": goal, "status": "active"})
    child = agent.subagents.create_run(goal=goal, thought="", plan=["执行"], parent_id="task-root", root_id="task-root",
                                       **create_kwargs)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": child.id, "goal": child.goal,
                      "task_path": str(child.task_dir or ""), "status": "active"})
    attempt = agent.subagents.lifecycle.prepare_runner_attempt(child.id).runner_active_attempt_id if running else ""
    return agent, child, attempt


def _attempt_ids(agent, run_id):
    repo = agent.subagents.runtime_db
    return [row["attempt_id"] for row in repo.attempts_for_run(repo.agent_run_for_run_id(run_id)["agent_run_id"])]


def _cli_races_lease(agent, run_id, attempt_id):
    lease = runner_session_lease(RunnerSessionPoolLease(
        manager=agent.subagents, run_id=run_id, attempt_id=attempt_id, interval_seconds=30.0,
    ))
    return lease, lease.__enter__()


def test_paused_run_is_blocked_at_reservation_activation_collaboration_and_late_receipts(tmp_path):
    agent, child, _ = _child(tmp_path, running=False)
    manager = agent.subagents
    pending = reserve_runner_start(manager, child.id)
    manager.lifecycle.update_background_start(child.id, BackgroundStartUpdate(
        launch_id="launch-1", status="launching", replace_launch=True, attempt_id=pending))
    attempt = manager.lifecycle.prepare_runner_attempt(child.id, expected_attempt_id=pending).runner_active_attempt_id
    assert attempt == pending
    assert control_task(agent, child.id, action="pause")["outcome"] == "applied"
    before = manager.load(child.id)
    attempts = _attempt_ids(agent, child.id)

    with pytest.raises(RuntimeConflictError):
        reserve_runner_start(manager, child.id)
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(child.id)
    for status in ("running", "finished"):
        with pytest.raises(RuntimeConflictError):
            manager.lifecycle.update_background_start(child.id, BackgroundStartUpdate(
                launch_id="launch-1", status=status, attempt_id=attempt))
    assert _can_run_for_collaboration_request(before) is False
    assert runner_attempt_cancelled(manager, child.id, attempt) is True
    assert auto_start_orphan_run(agent, child.id)["status"] == "not_needed"

    after = manager.load(child.id)
    assert after.status == "PAUSED" and after.runner_active_attempt_id == ""
    assert after.attributes["background_start"] == before.attributes["background_start"]
    assert task_control_record(after) == task_control_record(before)
    assert _attempt_ids(agent, child.id) == attempts


def test_pending_control_receipt_blocks_execution_until_original_control_reapplies(tmp_path, monkeypatch):
    agent, child, attempt = _child(tmp_path)
    manager = agent.subagents
    lease, _session = _cli_races_lease(agent, child.id, attempt)
    try:
        def interrupted(*_args, **_kwargs):
            raise OSError("simulated I/O error")

        # 回执已落盘、原撤权尚未执行时控制中断：回执保持 requested，并挡住全部执行入口。
        monkeypatch.setattr(cancellation, "prepare_subagent_stops", interrupted)
        with pytest.raises(OSError, match="simulated I/O error"):
            control_task(agent, child.id, action="pause")
        monkeypatch.undo()
        task = manager.load(child.id)
        control = task_control_record(task)
        assert (control["generation"], control["state"], control["cleanup"]) == (
            1, "requested", {"state": "pending", "attempt_id": attempt})
        assert task.status == "RUNNING" and task.runner_active_attempt_id == attempt
        assert manager.runtime_db.get_attempt(attempt)["status"] == "running"
        with pytest.raises(RuntimeConflictError):
            reserve_runner_start(manager, child.id)
        with pytest.raises(RuntimeConflictError):
            manager.lifecycle.prepare_runner_attempt(child.id)
        assert _can_run_for_collaboration_request(task) is False
        assert runner_attempt_cancelled(manager, child.id, attempt) is True

        # 重跑原控制：换代覆盖未生效回执，沿用同一执行轮的清理义务，再走原撤权与投影。
        report = control_task(agent, child.id, action="pause")
        assert (report["outcome"], report["control_generation"], report["cleanup_state"]) == ("applied", 2, "pending")
        task = manager.load(child.id)
        assert task.status == "PAUSED" and manager.runtime_db.get_attempt(attempt)["status"] == "cancelled"
    finally:
        lease.__exit__(None, None, None)
    assert task_control_record(manager.load(child.id))["cleanup"]["state"] == "confirmed"


def test_activation_reducer_rechecks_control_that_lands_after_admission(tmp_path, monkeypatch):
    agent, child, _ = _child(tmp_path, running=False)
    manager = agent.subagents
    original = lifecycle_runner_attempts._recover_unsubmitted_agent_guidance

    # 准入复核之后、最终激活 reducer 之前落盘一份 requested 控制回执。
    def guidance_then_control(manager_arg, task, attempt_id):
        original(manager_arg, task, attempt_id)

        def request(current):
            current.attributes = {**dict(current.attributes or {}), "task_control": {
                "schema_version": "subagent_task_control.v1", "generation": 1, "action": "pause",
                "target_status": "PAUSED", "state": "requested", "run_id": current.id,
            }}

        manager_arg.mutate(task.id, request)

    monkeypatch.setattr(lifecycle_runner_attempts, "_recover_unsubmitted_agent_guidance", guidance_then_control)
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(child.id)
    task = manager.load(child.id)
    assert task.status != "RUNNING" and task.runner_active_attempt_id == ""


def test_heartbeat_consumes_exact_control_and_only_owning_attempt_confirms_cleanup(tmp_path):
    agent, child, attempt = _child(tmp_path)
    manager = agent.subagents
    lease, session = _cli_races_lease(agent, child.id, attempt)
    try:
        assert control_task(agent, child.id, action="pause")["cleanup_state"] == "pending"
        # 控制所在进程没有原执行令牌；原执行轮所在进程的心跳读到精确回执后才中转中断。
        ready, release, seen = threading.Event(), threading.Event(), {}

        def execution():
            with register_interruptible(f"subagent-runner-attempt:{child.id}:{attempt}"):
                ready.set()
                release.wait(10)
                seen["interrupted"] = is_interrupted()

        worker = threading.Thread(target=execution, daemon=True)
        worker.start()
        assert ready.wait(10)
        assert _relay_runner_stop(RunnerSessionPoolLease(manager=manager, run_id=child.id, attempt_id=attempt)) is True
        release.set()
        worker.join(10)
        assert seen == {"interrupted": True}
        # 别的执行轮或旧会话不能代为确认。
        other = {**session, "session_id": "runsess-other", "attempt_id": "attempt-other"}
        assert confirm_task_control_cleanup(manager, child.id, attempt_id="attempt-other", session=other,
                                            runner_status="completed") is False
        assert task_control_record(manager.load(child.id))["cleanup"]["state"] == "pending"
        with pytest.raises(TaskControlError) as refused:
            control_task(agent, child.id, action="resume")
        assert refused.value.code == "TASK_CLEANUP_UNCONFIRMED"
    finally:
        lease.__exit__(None, None, None)
    cleanup = task_control_record(manager.load(child.id))["cleanup"]
    assert (cleanup["state"], cleanup["session_id"], cleanup["runner_exit"]) == ("confirmed", session["session_id"], "completed")
    assert confirm_task_control_cleanup(manager, child.id, attempt_id=attempt, session=session,
                                        runner_status="completed") is False


def test_stale_full_saves_preserve_control_generation_and_positive_cleanup(tmp_path):
    agent, child, attempt = _child(tmp_path)
    manager = agent.subagents
    before_control = manager.load(child.id)
    lease, _session = _cli_races_lease(agent, child.id, attempt)
    control_task(agent, child.id, action="pause")
    during_pause = manager.load(child.id)
    paused_control = task_control_record(during_pause)

    # 控制前读取的整份快照：普通字段照常合并，控制投影与回执保持 canonical。
    before_control.latest_summary = "旧进度"
    manager.save(before_control)
    task = manager.load(child.id)
    assert task.latest_summary == "旧进度"
    assert task.status == "PAUSED" and task.runner_active_attempt_id == "" and attempt in task.runner_abandoned_attempt_ids
    assert task_control_record(task) == paused_control

    lease.__exit__(None, None, None)
    assert task_control_record(manager.load(child.id))["cleanup"]["state"] == "confirmed"
    # 同代旧快照（清理确认前读取）不能把正向清理洗回 pending。
    manager.save(during_pause)
    assert task_control_record(manager.load(child.id))["cleanup"]["state"] == "confirmed"

    assert control_task(agent, child.id, action="resume")["status"] == "PENDING"
    manager.save(during_pause)
    task = manager.load(child.id)
    assert task.status == "PENDING" and task_control_record(task)["generation"] == 2


def test_narrow_runner_session_updates_keep_control_and_fence_old_attempt(tmp_path):
    agent, child, attempt = _child(tmp_path)
    manager = agent.subagents
    lease, session = _cli_races_lease(agent, child.id, attempt)
    try:
        control_task(agent, child.id, action="pause")
        paused_control = task_control_record(manager.load(child.id))
        now = time.time()
        assert manager.save_runner_session(child.id, {**session, "status": "running"}, now=now) is False
        assert task_control_record(manager.load(child.id)) == paused_control
        # 窄心跳本身不是清理证明；原会话最终回执照常落盘，回执不变。
        assert manager.save_runner_session(child.id, {**session, "status": "failed", "ended_at": now}, now=now) is True
        task = manager.load(child.id)
        assert task_control_record(task) == paused_control and task.status == "PAUSED"
    finally:
        lease.__exit__(None, None, None)


@pytest.mark.parametrize("decision", ["grant", "deny"])
def test_capability_resolution_keeps_paused_child_paused_without_reopening_link(tmp_path, decision):
    # 创建该孩子的父回合快照含 read_file（原 direct_parent_tool_authority 权威来源）。
    agent, child, _ = _child(tmp_path, running=False, attributes={"direct_parent_tool_authority": {
        "schema_version": "direct_parent_tool_authority.v1", "parent_run_id": "task-root",
        "available_tool_names": ["read_file"], "snapshot_hash": "sha256:exact", "owner_type": "main_agent"}})
    manager = agent.subagents
    request = manager.lifecycle.record_capability_request(child.id, RecordCapabilityRequestParams(
        problem="当前快照没有 read_file", needed_capability="read_file", capability_type="tool",
        requested_tools=["read_file"]))
    assert control_task(agent, child.id, action="pause")["outcome"] == "applied"
    result = ResolveCapabilityRequestsTool(agent).execute({
        "run_id": child.id, "request_id": request.id, "decision": decision, "reason": "父级裁决"})
    assert "run_paused_by_control" in result.output, result.output
    task = manager.load(child.id)
    # 裁决审计保留（授予落原 grant 账，拒绝落协议终态 CLOSED 与原因）；暂停不被改成 PENDING，父会话链接仍是 interrupted。
    if decision == "grant":
        assert task.capability_requests[0].status == "GRANTED"
        assert [grant.tools for grant in task.capability_grants] == [["read_file"]]
    else:
        assert task.capability_requests[0].status == "CLOSED"
        assert task.capability_requests[0].constraints["denial_reason"] == "父级裁决"
    assert task.status == "PAUSED" and "capability_resolution_continuation" not in task.attributes
    assert agent.conversation_store.tasks.load_report(child.id)[0].status == "interrupted"
    assert task_control_record(task)["state"] == "applied"


def test_pause_spares_sibling_sharing_the_runner_host(tmp_path):
    agent, first, first_attempt = _child(tmp_path)
    _, sibling, sibling_attempt = _child(tmp_path, agent=agent)
    manager = agent.subagents
    now, pid = time.time(), os.getpid()

    # 两个孩子由同一个派工子进程宿主承载（同 launch、同 pid）。
    def shared_host(attempt_id):
        def record(current):
            current.attributes = {
                **dict(current.attributes or {}),
                "background_start": {"launch_id": "launch-shared", "attempt_id": attempt_id, "pid": pid,
                                     "status": "running", "updated_at": now},
                "runner_session": {"session_id": f"runsess-{current.id}", "run_id": current.id, "attempt_id": attempt_id,
                                   "launch_id": "launch-shared", "worker_pid": pid,
                                   "worker_birth_token": capture_process_birth_token(pid), "in_process": False,
                                   "status": "running", "heartbeat_at": now, "interval_seconds": 30.0},
            }
        return record

    manager.mutate(first.id, shared_host(first_attempt))
    manager.mutate(sibling.id, shared_host(sibling_attempt))
    report = control_task(agent, first.id, action="pause")
    assert report["outcome"] == "applied"
    pid_report = report["local_cleanup"]["pid_report"]
    assert pid_report["status"] == "shared_host_cooperative" and pid_report["escalated"] is False
    assert pid_report["shared_run_ids"] == [sibling.id]
    untouched = manager.load(sibling.id)
    assert untouched.status == "RUNNING" and untouched.runner_active_attempt_id == sibling_attempt
    assert manager.runtime_db.get_attempt(sibling_attempt)["status"] == "running"
    assert task_control_record(untouched) == {}


def test_paused_is_not_a_user_stop_and_user_stop_is_not_resumed_by_task_control(tmp_path):
    agent, child, attempt = _child(tmp_path)
    lease, _ = _cli_races_lease(agent, child.id, attempt)
    control_task(agent, child.id, action="pause")
    lease.__exit__(None, None, None)
    assert user_stopped_run_is_resumable(agent.subagents.load(child.id)) is False
    _, stopped, _ = _child(tmp_path, agent=agent, goal="另一项")
    cancellation.cancel_subagent_tree(agent, cancellation.CancelSubagentTaskRequest(
        agent.subagents.load(stopped.id), "conversation_user_stop", kill_process=False))
    with pytest.raises(TaskControlError) as refused:
        control_task(agent, stopped.id, action="resume")
    assert refused.value.code == "TASK_NOT_PAUSED"
    assert agent.subagents.load(stopped.id).status == "CANCELLED"


def test_state_machine_registers_paused_as_nonterminal_non_repairable_wait():
    facts = RunStateFacts(status="PAUSED", channel_status="BROKEN", attempts=0, max_attempts=3)
    assert normalize_status("PAUSED") == "PAUSED"
    assert can_dispatch(facts) is False and can_dispatch(facts, force=True) is False
    assert can_repair(facts) is False
    assert waiting_reason(facts) == "paused" and lifecycle_phase(facts) == "PAUSED"
    assert terminal_outcome(facts) == "active"
    decision = recovery_decision(facts)
    assert (decision.action, decision.allow_new_run, decision.reason) == ("wait", False, "paused")
    snapshot = run_state_snapshot_from_task(type("Task", (), {"id": "run-1", "status": "PAUSED"})())
    assert snapshot["status"] == "PAUSED" and snapshot["status_protocol_error"] == ""



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r9_detached_credential_scope.py
# R9-22：detached 任务的审批、凭据与自学习授权读持久 TaskLink 声明的原 detached claim 车道；旧代、已取消与外来车道一律拒绝。
#
# 真实 SimpleAgent、ConversationStore、TaskLink、claim、RuntimeDB、后台 control 与 core 身份发布；只替换 IM 运输。
# ==========================================================================

_URL = "https://example.com/api"


# LLM: 同一线程上另有前台车道 claim（用户继续聊天），detached 任务自己的 claim 在原 detached 车道；二者互不授权。
# 函数用途: 建立一个已领取 detached 车道的后台任务和同线程前台任务。
@pytest.fixture
def detached(tmp_path):
    from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    execute_approval_mode_operation(agent.home_paths, "set", "ask")
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": agent.home_paths.owner_id,
        "channel_conversation_id": "session-main", "channel_user_id": "local-agent",
    })
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "chat-work", "goal": "继续前台对话", "status": "active"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "detached-work", "goal": "执行后台独立工作",
                      "status": "active", "cancellation_scope": "detached"})
    lane = detached_task_claim_scope_id(thread.thread_id, "detached-work")
    foreground = store.claims.acquire({"thread_id": thread.thread_id, "task_id": "chat-work", "reason": "thread_goal_continue"})
    claim = store.claims.acquire({"thread_id": thread.thread_id, "claim_scope_id": lane, "task_id": "detached-work",
                                  "reason": "thread_goal_continue"})
    assert claim and foreground and claim["claim_id"] != foreground["claim_id"]
    return SimpleNamespace(agent=agent, store=store, thread=thread, task_id="detached-work", lane=lane,
                           claim=claim, foreground=foreground)


# LLM: 沿生产后台 control、core binder 与原 claim publisher 在 detached 车道发布身份；RuntimeDB 决定代次。
# 函数用途: 让当前测试线程成为这条 detached 任务的真实热执行片。
def _bind_detached(ctx, *, lane=None, claim_id=None):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
    from agent_py_agent.agent.conversation.background_execution import _BackgroundRunControl
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    control = _BackgroundRunControl("detached-source", None)
    binding = ClaimExecutionBinding(ctx.store.claims, ctx.thread.thread_id, claim_id or ctx.claim["claim_id"],
                                    ctx.lane if lane is None else lane)
    with bind_claim_execution(binding):
        params = _bind_main_agent_authority(ctx.agent, RunParams(
            request_id="detached-source", run_id="detached-core", task_id=ctx.task_id,
            task_attributes={"conversation_thread_id": ctx.thread.thread_id, "conversation_task_id": ctx.task_id},
            conversation_task_binding_callback=control))
    ctx.agent._current_run_params = params
    return params


def _learning_service(agent):
    home = agent.home_paths
    return SimpleNamespace(store=SimpleNamespace(directory=Path(home.owner_skill_learning_dir),
                                                 skills_root=Path(home.owner_home_dir) / "skills"))


# LLM: 合成凭据只经原私人卡片发布与提交入口落盘；运输只记录卡片，不访问网络。
# 函数用途: 在当前 detached 执行里取得一张可用的 basic 凭据引用。
def _ready_credential(ctx):
    ctx.agent.config.feishu_session_lock_enabled = False
    bind_admin_channel_identity(ctx.agent.home_paths.root, "feishu", "ou_synthetic")

    def send(_target, _card, **_kwargs):
        return {"message_id": "om_" + __import__("uuid").uuid4().hex, "chat_id": "oc_private"}

    ctx.agent.channel_registry.register_adapter("feishu", SimpleNamespace(send_control_card=send),
                                                capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    reference, _ = request_credential_card(ctx.agent, url=_URL, authentication="basic")
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, reference), context="test").payload
    body = {"reference": row["reference"], "decision": "submit", "message_id": row["message_id"],
            "chat_id": row["chat_id"], "fields": {"username": "synthetic-user", "secret": "Synthetic-API-Secret-123"}}
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_synthetic")["ok"]
    return reference


def _consumers_allowed(ctx, scope, reference):
    attrs = ctx.agent._current_run_params.task_attributes
    allowed = {
        "scope": scope.active and tool_approval_scope_is_current(ctx.agent, scope),
        "approval_session": bool(_agent_approval_session_scope(ctx.agent, scope)),
        "learning": _authorized_learning_scope(ctx.agent, _learning_service(ctx.agent), attrs),
    }
    try:
        allowed["credential"] = bool(credential_headers(ctx.agent, reference, _URL)[0])
    except ValueError:
        allowed["credential"] = False
    return allowed


def test_current_detached_execution_reads_its_own_claim_lane(detached):
    params = _bind_detached(detached)
    scope = tool_approval_scope(detached.agent, params.run_id)
    assert scope.active
    assert (scope.root_task_id, scope.thread_id) == (detached.task_id, detached.thread.thread_id)
    assert scope.claim_id == detached.claim["claim_id"] != detached.foreground["claim_id"]
    assert scope.execution_attempt_id == params.attempt_id
    reference = _ready_credential(detached)
    assert _consumers_allowed(detached, scope, reference) == {
        "scope": True, "approval_session": True, "learning": True, "credential": True}
    # cold 读者只按同一 detached 定位复读原发布身份，不从当前参数补猜。
    hot = detached.agent._current_run_params
    del detached.agent._current_run_params
    cold = tool_approval_scope(detached.agent, detached.task_id)
    assert cold.active and cold == scope
    detached.agent._current_run_params = hot
    # 前台车道仍只属于前台任务，不被 detached 身份借用。
    assert not tool_approval_scope(detached.agent, "chat-work").active


@pytest.mark.parametrize("case", ["stale_attempt", "expired_claim", "cancelled_task", "finished_claim",
                                  "foreign_lane", "foreground_link", "foreign_thread"])
def test_stale_cancelled_or_foreign_detached_execution_is_denied(detached, case):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    params = _bind_detached(detached)
    reference = _ready_credential(detached)
    path = detached.store.claims.storage.background_claim_path(detached.lane)
    if case == "stale_attempt":
        repo = detached.agent.subagents.runtime_db
        assert repo.create_attempt(repo.agent_run_for_run_id(params.run_id)["agent_run_id"])["attempt_id"] != params.attempt_id
    elif case == "expired_claim":
        update_json_file_atomic(path, lambda value: {**value, "expires_at": 1.0})
    elif case == "cancelled_task":
        detached.store.tasks.update_status({"task_id": detached.task_id, "status": "cancelled"})
    elif case == "finished_claim":
        assert detached.store.claims.finish({"thread_id": detached.thread.thread_id, "claim_scope_id": detached.lane,
                                             "claim_id": detached.claim["claim_id"], "status": "finished"})
    elif case == "foreign_lane":
        # 同一 detached 任务的身份若落在线程前台车道，不能被扫描借用：定位只认持久链接推出的 detached 车道。
        thread_id = detached.thread.thread_id
        for lane, claim in ((detached.lane, detached.claim), ("", detached.foreground)):
            assert detached.store.claims.finish({"thread_id": thread_id, "claim_scope_id": lane,
                                                 "claim_id": claim["claim_id"], "status": "finished"})
        borrowed = detached.store.claims.acquire({"thread_id": thread_id, "task_id": detached.task_id,
                                                  "reason": "thread_goal_continue"})
        params = _bind_detached(detached, lane="", claim_id=borrowed["claim_id"])
    elif case == "foreground_link":
        # 持久链接不是 detached 时不推出 detached 车道，原线程车道属于前台任务。
        update_json_file_atomic(detached.store.storage.task_path(detached.task_id),
                                lambda value: {**value, "cancellation_scope": "foreground"})
    elif case == "foreign_thread":
        detached.agent._current_run_params = replace(params, task_attributes={
            **params.task_attributes, "conversation_thread_id": "thread-foreign"})
    scope = tool_approval_scope(detached.agent, detached.task_id)
    allowed = _consumers_allowed(detached, scope, reference)
    assert allowed == {"scope": False, "approval_session": False, "learning": False, "credential": False}, allowed



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r9_extraction_quota_lock_order.py
# R9-R1 P1-R9-10：提炼提交与资产投递在同一 owner 上按 quota→资产锁同序，1 GB 配额下并发不成环。
#
# 真实临时 Owner、LocalUserStore、ConversationStore、提炼提案、ScriptStore 与 AssetDeliveryService；只在原
# 锁内调用点放置观察钩子编排交错，不替换配额或锁实现，不访问网络。
# ==========================================================================

QUOTA_MB = 1024
TOGGLE_SCRIPT = "r9toggle"


def _local_owner(root, uid):
    home = root / "owners/local" / uid
    home.mkdir(parents=True)
    users = LocalUserStore(root)
    user = LocalUser(uid, "enabled", 1, uid, "2026-10-25", "2026-10-25", 1)
    users.save(uid, {"schema": "local-user.v1", "user": user.to_dict(),
                     "provision": {"phase": "committed", "operation_id": "fixture",
                                   "home_identity": users.directory_identity(home.relative_to(root).parts)},
                     "operations": {"fixture": user.to_dict()}})
    (home / "quota.json").write_text(json.dumps({"max_disk_mb": QUOTA_MB}), encoding="utf-8")
    return home


@pytest.fixture
def kit(tmp_path):
    root = tmp_path / "host"
    (root / "owners/local/main").mkdir(parents=True)
    home = _local_owner(root, "alice")
    other = _local_owner(root, "bob")
    ScriptStore(home).save_script(AssetDescriptor(TOGGLE_SCRIPT, "script", "投递开关"), 'print("{}")\n')
    conversations = ConversationStore(home / "conversations")
    thread = conversations.threads.get_or_create({"channel": "tui", "channel_conversation_id": "r9", "title": "日报"})
    first = conversations.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "整理中文订单",
                                           "now": 1792886400})
    native = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call1", "name": "run_command", "input": {"command": "printf hello"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "hello"}]},
    ]
    conversations.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "订单整理完毕",
                                   "metadata": {"canonical_native_messages": canonical_native_messages_envelope(native)},
                                   "now": 1792886401})
    draft = extract.propose_assets_from_turns(conversations, owner_home=home,
                                              turn_refs=[{"thread_id": thread.thread_id, "message_id": first.message_id}])
    assert draft["status"] == "DRAFT" and draft["revision"] == 1
    return SimpleNamespace(root=root, home=home, other=other, draft=draft, service=AssetDeliveryService(root))


def _quota_held(home) -> bool:
    # 从另一线程非阻塞试同一 owner 原配额锁；争用即说明当前事务仍持有该准入。
    seen = []

    def probe():
        try:
            with owner_quota_enforcer_from_policy(home).admission(blocking=False):
                seen.append(False)
        except OwnerQuotaLockContention:
            seen.append(True)

    worker = threading.Thread(target=probe, name="r9-quota-probe")
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and len(seen) == 1
    return seen[0]


def _run_into(results, name, call):
    try:
        results[name] = call()
    except BaseException as exc:  # noqa: BLE001 - 交给测试线程断言
        results[name] = exc


def _commit(kit):
    return extract.commit_extracted_assets(kit.home, proposal_id=kit.draft["proposal_id"], expected_revision=1)


def _assert_published(kit, receipt):
    assert receipt["ok"] is True and receipt["status"] == "COMMITTED" and len(receipt["committed_assets"]) == 3
    proposal = extract.get_extraction_proposal(kit.home, kit.draft["proposal_id"])
    assert proposal["status"] == "COMMITTED" and proposal["revision"] == 2
    registry = SkillLearningStore(kit.home / "data" / "skill_learning", kit.home / "skills").load_registry()
    assert len(registry.assets) == 3
    assert len(ScriptStore(kit.home).list_scripts()) == 2


def test_concurrent_extraction_and_delivery_complete_at_1gb_quota(kit, monkeypatch):
    in_assets, delivery_quota = threading.Event(), threading.Event()
    order, results = [], {}
    real_check, real_scope = extract._check_dependencies, AssetDeliveryService._write_scope

    def check_dependencies(root, chosen, scripts):
        # 提炼此刻已持资产根与集合锁；旧顺序下投递能先拿到配额再等资产锁，提炼随后等配额即成环。
        order.append(("extraction-assets", _quota_held(kit.home)))
        in_assets.set()
        delivery_quota.wait(1.0)
        return real_check(root, chosen, scripts)

    @contextmanager
    def write_scope(self, homes):
        with real_scope(self, homes) as admissions:
            order.append(("delivery-quota", extract.get_extraction_proposal(kit.home, kit.draft["proposal_id"])["status"]))
            delivery_quota.set()
            yield admissions

    def deliver():
        assert in_assets.wait(15), "extraction did not reach its asset-locked section"
        return kit.service.toggle_asset("local/alice", "script", TOGGLE_SCRIPT, True)

    monkeypatch.setattr(extract, "_check_dependencies", check_dependencies)
    monkeypatch.setattr(AssetDeliveryService, "_write_scope", write_scope)
    threads = [threading.Thread(target=_run_into, args=(results, "extraction", lambda: _commit(kit)), daemon=True),
               threading.Thread(target=_run_into, args=(results, "delivery", deliver), daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads), f"extraction/delivery lock cycle: {order}"
    # 提炼在资产锁内仍持有本 owner 配额准入；投递只能在提炼整体结束后取得配额。
    assert order == [("extraction-assets", True), ("delivery-quota", "COMMITTED")]
    _assert_published(kit, results["extraction"])
    assert results["delivery"]["ok"] is True and results["delivery"]["enabled"] is True
    assert ScriptStore(kit.home).get_script(TOGGLE_SCRIPT)["enabled"] is True
    assert _quota_held(kit.home) is False


def test_serial_extraction_and_delivery_controls_at_1gb_quota(kit):
    assert kit.service.toggle_asset("local/alice", "script", TOGGLE_SCRIPT, True)["enabled"] is True
    receipt = _commit(kit)
    _assert_published(kit, receipt)
    # 幂等重放只读原回执，同样先取配额再取提案锁，不重复发布。
    assert _commit(kit) == receipt
    assert kit.service.toggle_asset("local/alice", "script", TOGGLE_SCRIPT, False)["enabled"] is False
    assert _quota_held(kit.home) is False


def test_rollback_keeps_original_admission_until_finalization(kit, monkeypatch):
    real_write, real_rollback, observed = extract._write_record, extract._rollback_files, []

    def write_record(home, collection, value, id_key):
        if value.get("status") == "COMMITTED" and not observed:
            observed.append(("publication", _quota_held(kit.home)))
            raise OSError("simulated proposal publication failure")
        return real_write(home, collection, value, id_key)

    def rollback_files(root, files):
        observed.append(("rollback", _quota_held(kit.home)))
        return real_rollback(root, files)

    monkeypatch.setattr(extract, "_write_record", write_record)
    monkeypatch.setattr(extract, "_rollback_files", rollback_files)
    with pytest.raises(OSError, match="proposal publication failure"):
        _commit(kit)
    assert observed == [("publication", True), ("rollback", True)]
    assert _quota_held(kit.home) is False
    proposal = extract.get_extraction_proposal(kit.home, kit.draft["proposal_id"])
    assert proposal["status"] == "DRAFT" and proposal["revision"] == 1
    assert [row["id"] for row in ScriptStore(kit.home).list_scripts()] == [TOGGLE_SCRIPT]
    # 下一次普通提交从同一 DRAFT 重新发布，回滚未留下半提交登记。
    _assert_published(kit, _commit(kit))


def test_state_lock_default_order_and_foreign_admission_rejected(kit):
    store = SkillLearningStore(kit.home / "data" / "skill_learning", kit.home / "skills")
    with store.state_lock():
        assert _quota_held(kit.home) is True
    assert _quota_held(kit.home) is False
    with owner_quota_enforcer_from_policy(kit.home).admission() as admission:
        # 同 owner 准入原样复用，不再取不可重入配额锁；退出 state 后准入仍由调用方持有。
        with store.state_lock(admission=admission) as reused:
            assert reused is admission
        assert _quota_held(kit.home) is True
        with owner_quota_enforcer_from_policy(kit.other).admission() as foreign:
            with pytest.raises(RuntimeError, match="配额事务"):
                with store.state_lock(admission=foreign):
                    pytest.fail("foreign owner admission accepted")
    assert _quota_held(kit.home) is False



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r9_gateway_admission_cleanup.py
# R9-R1 P1-R9-21：Count0 准入记账后，移交原 executor 前的每个出口都恰好释放一次。
#
# 真实文件队列、claim_request 与 GatewayAdmission；按默认配置容量派发。只在原 claim/submit 调用点模拟
# 一次失败，不重置全局计数，也不以过期替代释放证据。
# ==========================================================================

EMPTY = {"total": 0, "per_user": {}, "per_conversation": {}}


class _Halt(BaseException):
    pass


def _paths(tmp_path: Path) -> GatewayPaths:
    root = tmp_path / "gw"
    return GatewayPaths(root=root, pid=root / "gateway.pid", adapter_pid=root / "adapter.pid",
                        state=root / "gateway_state.json", heartbeat=root / "gateway_heartbeat.json",
                        stop_request=root / "gateway_stop.request", log=root / "gateway.log",
                        inbox=root / "requests" / "pending", processing=root / "requests" / "processing",
                        done=root / "requests" / "done", failed=root / "requests" / "failed",
                        responses=root / "responses", history=root / "history.jsonl")


def _enqueue(paths: GatewayPaths, request_id: str, *, conversation: str = "conv-r9") -> dict:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    payload = {"id": request_id, "kind": "ask", "goal": "g", "user_id": "u-r9", "status": "queued",
               "created_at": time.time(),
               "conversation": {"canonical_user_id": "u-r9", "channel": "chat",
                                "channel_conversation_id": conversation, "channel_user_id": "u-r9"}}
    (paths.inbox / f"{request_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _keys(payload: dict) -> tuple[str, str]:
    return rw.request_user_key(payload), rw.request_conversation_key(payload)


@pytest.fixture()
def admission(monkeypatch):
    fresh = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", fresh)
    return fresh


@pytest.fixture()
def limits():
    configured = rw.AdmissionLimits.from_config(AgentConfig())
    assert configured.user_inflight > 1 and configured.global_inflight > 1
    return configured


def _claim_failing_once(monkeypatch, error):
    real, calls = rw.claim_request, []

    def claim(paths, request_path, **kwargs):
        calls.append(request_path.name)
        if len(calls) == 1:
            raise error
        return real(paths, request_path, **kwargs)

    monkeypatch.setattr(rw, "claim_request", claim)
    return calls


@pytest.mark.parametrize("error", [OSError("simulated claim transition failure"), _Halt()],
                         ids=["claim-oserror", "claim-base-exception"])
def test_single_claim_error_releases_admission_then_later_dispatch_proceeds(tmp_path, monkeypatch, admission,
                                                                           limits, error):
    paths = _paths(tmp_path)
    payload = _enqueue(paths, "req-r9-claim")
    calls = _claim_failing_once(monkeypatch, error)
    submitted = []
    with pytest.raises(type(error)):
        rw.dispatch_pending_requests(paths, limits, lambda claim, user, conv: submitted.append((claim, user, conv)))
    assert admission.snapshot() == EMPTY and submitted == []
    assert (paths.inbox / "req-r9-claim.json").exists()
    # 同一会话的下一轮普通派发不被泄漏的会话/用户坑挡住，并把责任移交给原 executor。
    assert rw.dispatch_pending_requests(paths, limits, lambda claim, user, conv: submitted.append((claim, user, conv))) == 1
    assert calls == ["req-r9-claim.json", "req-r9-claim.json"]
    (claim, user_key, conversation_key), = submitted
    assert (user_key, conversation_key) == _keys(payload) and claim.path == paths.processing / "req-r9-claim.json"
    assert admission.snapshot()["total"] == 1
    admission.release(user_key, conversation_key=conversation_key)  # 原 executor 的执行收尾
    assert admission.snapshot() == EMPTY


@pytest.mark.parametrize("error", [RuntimeError("simulated submit failure"), _Halt()],
                         ids=["submit-error", "submit-base-exception"])
def test_submit_failure_releases_exactly_once(tmp_path, admission, limits, error):
    paths = _paths(tmp_path)
    other = {"conversation": {"canonical_user_id": "u-r9", "channel": "chat",
                              "channel_conversation_id": "conv-r9-other", "channel_user_id": "u-r9"},
             "user_id": "u-r9"}
    other_user, other_conversation = _keys(other)
    # 同一用户另一会话仍在运行：重复释放会把它的坑一起退掉。
    assert admission.try_acquire(other_user, limits.user_inflight, limits.global_inflight,
                                 conversation_key=other_conversation)
    held = admission.snapshot()
    _enqueue(paths, "req-r9-submit")

    def submit(_claim, _user, _conversation):
        raise error

    with pytest.raises(type(error)):
        rw.dispatch_pending_requests(paths, limits, submit)
    assert admission.snapshot() == held
    # 已认领请求留在 processing 交恢复扫描，不回到 pending 被重复提交。
    assert (paths.processing / "req-r9-submit.json").exists()
    assert not (paths.inbox / "req-r9-submit.json").exists()


def test_absent_claim_releases_once_and_successful_submit_transfers_ownership(tmp_path, monkeypatch, admission,
                                                                           limits):
    paths = _paths(tmp_path)
    payload = _enqueue(paths, "req-r9-raced")
    real, calls = rw.claim_request, []

    def claim(claim_paths, request_path, **kwargs):
        calls.append(request_path.name)  # 第一次模拟别的扫描已抢先认领
        return None if len(calls) == 1 else real(claim_paths, request_path, **kwargs)

    monkeypatch.setattr(rw, "claim_request", claim)
    assert rw.dispatch_pending_requests(paths, limits, lambda *_args: pytest.fail("raced claim submitted")) == 0
    assert admission.snapshot() == EMPTY
    submitted = []
    assert rw.dispatch_pending_requests(paths, limits, lambda claim, user, conv: submitted.append((user, conv))) == 1
    # 提交成功后本函数不再释放；坑由原 executor 持有直到其收尾。
    assert submitted == [_keys(payload)] and admission.snapshot()["total"] == 1
    user_key, conversation_key = _keys(payload)
    assert admission.snapshot()["per_conversation"] == {conversation_key: 1}
    admission.release(user_key, conversation_key=conversation_key)
    assert admission.snapshot() == EMPTY



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r9_gateway_drain_auxiliary.py
# R9-R1 P1-R9-16：原审批桥正常消费后遗留的空锁设施不再阻断停止库存与排空快照；未结/活跃/危险布局仍阻断。
#
# 审批两端都是原实现：服务端 StreamApproval / serve_command_stream 等待并消费决定，客户端沿原
# gateway_chunk_path / command_approval_path 调 write_gateway_permission_decision 回写。HTTP 输出流是合成
# 对象；不启动 Gateway 服务、模型或 native 进程，停止事实由原 Count0 停止 writer 在临时 home 写入。
# ==========================================================================

EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
COMMAND_OWNER = OwnerIdentity("local", "main", "main")


def _request(request_id: str, permission_id: str) -> ToolApprovalRequest:
    return ToolApprovalRequest(
        permission_id=permission_id, request_id=request_id, tool_name="synthetic", round=1, call_index=0,
        title="合成审批", description="仅审批桥合同",
        binding={"tool_name": "synthetic", "run_id": "run-r9", "operation_id": "operation-r9",
                 "idempotency_key": "idempotency-r9", "args_hash": "a" * 64},
        options=({"id": "yes", "decision": "approved", "label": "同意"},
                 {"id": "no", "decision": "denied", "label": "拒绝"}),
    )


def _approve(chunk_path, permission: dict) -> None:
    shown = ToolApprovalRequest.from_mapping(permission)
    write_gateway_permission_decision(chunk_path, shown, ToolApprovalDecision(shown.permission_id, "approved"))


# 前台请求：原 StreamApproval 在 claimed chunk 旁等待，客户端沿 gateway_chunk_path 回写，原桥消费。
def _complete_foreground(paths, owner=None, request_id="gwreq-r9-foreground"):
    request = _request(request_id, "permission-foreground")
    chunk = claimed_request_chunk_path(paths.processing / f"{request_id}.json", request_id)
    requested, events, outcome = threading.Event(), [], {}

    def publish(event):
        events.append(event)
        if event["kind"] == "permission_requested":
            requested.set()

    approval = StreamApproval(chunk, publish, lambda: None)
    waiter = threading.Thread(target=lambda: outcome.update(
        approval.request(request.to_dict(), options=StreamApprovalRequestOptions(True))))
    waiter.start()
    try:
        assert requested.wait(5)
        _approve(gateway_chunk_path(paths, request_id), events[-1]["permission"])
    finally:
        waiter.join(10)
    assert not waiter.is_alive() and outcome["decision"] == "approved"
    return gateway_permission_decision_path(chunk, request)


class _CommandWire:
    """合成 HTTP 输出：只记录原命令流帧并在审批请求出现时通知客户端。"""

    def __init__(self):
        self.frames, self.requested = [], threading.Event()

    def write(self, raw):
        frame = json.loads(raw)
        self.frames.append(frame)
        if frame["kind"] == "permission_requested":
            self.requested.set()
        return len(raw)

    def flush(self):
        return None


# 宿主命令：原 serve_command_stream 在 HTTP 线程等待，TUI 沿 command_approval_path 回写，原桥消费。
def _complete_host_command(paths, owner=COMMAND_OWNER, request_id="command-r9"):
    wire = _CommandWire()
    handler = SimpleNamespace(wfile=wire, connection=SimpleNamespace(settimeout=lambda _seconds: None),
                              close_connection=False, send_response=lambda _code: None,
                              send_header=lambda *_args: None, end_headers=lambda: None)
    request = _request(request_id, "permission-command")

    def execute(request_permission, token):
        return {"decision": request_permission(request.to_dict(), cancellation_token=token)["decision"]}

    server = threading.Thread(target=serve_command_stream, args=(handler, paths, owner, request_id, execute))
    server.start()
    try:
        assert wire.requested.wait(5)
        permission = next(f for f in wire.frames if f["kind"] == "permission_requested")["payload"]["permission"]
        _approve(command_approval_path(paths, owner, request_id), permission)
    finally:
        server.join(10)
    assert not server.is_alive()
    assert [f["payload"] for f in wire.frames if f["kind"] == "result"] == [{"decision": "approved"}]
    return gateway_permission_decision_path(command_approval_path(paths, owner, request_id), request)


COMPLETIONS = {"foreground": _complete_foreground, "host-command": _complete_host_command}


def _settled_lock(decision):
    lock = decision.with_name(decision.name + ".lock")
    # 原桥消费后只删除决定本身；锁仍在，故两层哈希目录不能被 rmdir。
    assert not decision.exists() and lock.is_file() and lock.read_bytes() == b""
    assert sorted(p.name for p in decision.parent.iterdir()) == [lock.name]
    return lock


# 原 Count0 正常停止 writer：完成审批后才记录停止事实，与真实先跑后停顺序一致。
def _stop_after(tmp_path, monkeypatch, complete):
    home = tmp_path / "home"
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(f'model_backend: echo\nmy_agent_home: "{home}"\ngateway_worker_process_count: 0\n')
    args = Namespace(config=config_path, workspace_root="")
    config = common.agent_configuration(args)[0]
    paths = _cold_gateway_paths(args)
    owner = resolve_owner_home(home)
    owner.home_dir.mkdir(parents=True)
    RuntimeRepository(runtime_db_path(owner.home_dir))
    paths.processing.mkdir(parents=True, exist_ok=True)
    decision = complete(paths, owner.identity)
    identity = {"host_id": "fixture-host", "pid": os.getpid(), "start_time": "fixture-birth"}
    context = SimpleNamespace(paths=paths, config=config, config_path=config_path, process_identity=identity,
                              process_started_at=1.0, coordinator_worker_count=0, control_plane=None)
    cleanup = {"drain_complete": True, "alive_threads": [], "interrupted_model_calls": 0,
               "host_shutdown_executors": 0, "surviving_background_sessions": []}
    monkeypatch.setattr(gateway_process, "log_gateway_event", lambda *_: None)
    with standalone_lifecycle_guard(paths.root) as held:
        gateway_process._record_gateway_service_termination(
            paths, None, os.getpid(),
            gateway_process._GatewayServiceTermination("stopped", "planned_stop", "fixture", "", 0), context=context)
        fact = record_standalone_stop(context, held, cleanup)
    return SimpleNamespace(home=home, config=config, paths=paths, fact=fact, decision=decision)


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / "gateway"
    root.mkdir(mode=0o700)
    value = gateway_paths_from_root(root)
    value.processing.mkdir(parents=True, mode=0o700)
    return value


def _sources(paths):
    namespaces, files = {}, {}
    inventory._gateway_sources(paths, namespaces, files)
    return namespaces, files


@pytest.mark.parametrize("kind", sorted(COMPLETIONS))
def test_normal_stop_after_completed_approval_records_stable_settled_lock(tmp_path, monkeypatch, kind):
    case = _stop_after(tmp_path, monkeypatch, COMPLETIONS[kind])
    lock = _settled_lock(case.decision)
    stop = case.fact["standalone_stop"]
    assert stop["blockers"] == [] and stop["sources"] is not None
    ref = "gateway/" + "/".join(lock.relative_to(case.paths.root).parts)
    current = inventory.standalone_inventory(case.home, case.config, case.paths)
    # 已结空锁带原身份目录链进入摘要；后继迁移重算与停止事实一致，锁本身不被删除或改写。
    assert current["files"][ref] == EMPTY_DIGEST
    assert all("gateway/" + "/".join(parent.relative_to(case.paths.root).parts) in current["namespaces"]
               for parent in (lock.parent, lock.parent.parent))
    assert inventory.standalone_inventory(case.home, case.config, case.paths) == current
    assert responsibility_digest(current) == stop["sources"]["digest"]
    assert lock.is_file() and lock.read_bytes() == b""


def test_drain_snapshot_classifies_completed_approval_facilities_and_keeps_requests(paths):
    locks = [_settled_lock(complete(paths)) for complete in COMPLETIONS.values()]
    scope = CoordinatorStopScope(2, "c-r9")
    snapshot = durable_drain_snapshot(paths, 2)
    # 原审批/命令设施只核身份，不变成 processing 责任；同一停止观察可重复确认。
    assert snapshot[0][1][1] == ()
    assert durable_items(scope, snapshot) == ()
    assert same_durable_snapshot(snapshot, durable_drain_snapshot(paths, 2))
    # 真实 processing 请求仍是排空责任，设施分类不吞业务项。
    (paths.processing / "gwreq-r9-live.json").write_text(json.dumps({"id": "gwreq-r9-live"}))
    items = durable_items(scope, durable_drain_snapshot(paths, 2))
    assert [(item.item_ref, item.state) for item in items] == [("requests/processing/gwreq-r9-live.json", "processing")]
    assert all(lock.is_file() and lock.read_bytes() == b"" for lock in locks)


def _pending_decision(paths, kind):
    if kind == "foreground":
        request, chunk = _request("gwreq-r9-pending", "permission-pending"), gateway_chunk_path(paths, "gwreq-r9-pending")
    else:
        request = _request("command-r9-pending", "permission-pending")
        chunk = command_approval_path(paths, COMMAND_OWNER, "command-r9-pending")
    write_gateway_permission_decision(chunk, request, ToolApprovalDecision(request.permission_id, "approved"))
    return gateway_permission_decision_path(chunk, request)


@pytest.mark.parametrize("kind", sorted(COMPLETIONS))
def test_pending_decision_and_unknown_entries_keep_blocking(paths, kind):
    decision = _pending_decision(paths, kind)
    with pytest.raises(StandaloneMigrationRejected) as pending:
        _sources(paths)
    assert pending.value.reason == "processing_auxiliary_unsettled"
    assert pending.value.ref == "/".join(decision.relative_to(paths.root).parts)
    decision.unlink()
    lock = decision.with_name(decision.name + ".lock")
    stage = decision.with_name(f".{decision.name}.{'0' * 32}.tmp")
    stage.write_bytes(b"")
    with pytest.raises(StandaloneMigrationRejected, match="processing_auxiliary_unsettled"):
        _sources(paths)
    stage.unlink()
    assert _sources(paths)[1]["gateway/" + "/".join(lock.relative_to(paths.root).parts)] == EMPTY_DIGEST
    # 宿主命令目录从不写 approval.chunks.jsonl；前台决定桶不接受非哈希名称。
    unknown = (command_approval_path(paths, COMMAND_OWNER, "command-r9-pending") if kind == "host-command"
               else decision.parent.parent / "not-a-request-bucket")
    unknown.write_bytes(b"")
    with pytest.raises(StandaloneMigrationRejected) as unexpected:
        _sources(paths)
    assert (unexpected.value.reason, unexpected.value.ref) == (
        "processing_auxiliary_unsettled", "/".join(unknown.relative_to(paths.root).parts))


@pytest.mark.parametrize("kind", sorted(COMPLETIONS))
def test_live_lock_holder_and_nonempty_lock_keep_blocking(paths, kind):
    decision = COMPLETIONS[kind](paths)
    lock = _settled_lock(decision)
    # 原锁原语持有即活跃 writer/consumer；探测不等待也不改动锁。
    with locked_json_path(decision):
        with pytest.raises(StandaloneMigrationRejected) as held:
            _sources(paths)
    assert (held.value.reason, held.value.ref) == (
        "processing_approval_lock_unsettled", "/".join(lock.relative_to(paths.root).parts))
    before = os.stat(lock)
    assert _sources(paths)[1]
    lock.write_bytes(b"x")
    with pytest.raises(StandaloneMigrationRejected, match="processing_approval_lock_unsettled"):
        _sources(paths)
    assert (os.stat(lock).st_dev, os.stat(lock).st_ino) == (before.st_dev, before.st_ino)


@pytest.mark.parametrize("kind", sorted(COMPLETIONS))
@pytest.mark.parametrize("unsafe", ["symlink-lock", "hardlink-lock", "symlink-bucket"])
def test_unsafe_links_keep_blocking(paths, tmp_path, kind, unsafe):
    lock = _settled_lock(COMPLETIONS[kind](paths))
    outside = tmp_path / "outside"
    outside.mkdir()
    if unsafe == "symlink-lock":
        lock.unlink()
        (outside / "target.lock").write_bytes(b"")
        lock.symlink_to(outside / "target.lock")
    elif unsafe == "hardlink-lock":
        os.link(lock, outside / "alias.lock")
    else:
        bucket = lock.parent
        bucket.rename(outside / "bucket")
        bucket.symlink_to(outside / "bucket", target_is_directory=True)
    with pytest.raises(OSError):
        _sources(paths)



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py
# LLM: 真实 P3/P5 worker claim、原 background_request_scope、原租约/WAL/提交与 _finish；只替换来源 HTTP 抓取、
#   父 Audit 任务观察与 Audit 岗位通知这三处外部协作者，每个场景至多一次普通模拟 I/O 错误或一次 typed 权限变化。
# 模块用途: 回归 P1-R9-15——收割提交后通知失败仍在 finally 内执行原租约收尾，typed 未知/过期/冲突不删租约、不新领租约。
# ==========================================================================

_ENVELOPE = {
    "mode": "cursor", "record_boundary": "array_item", "record_list_key": "items",
    "cursor_field": "next_cursor", "cursor_semantics": "next_position",
    "request": {"method": "GET", "cursor_binding": {"location": "query", "name": "since", "initial": 0},
                "page_size_binding": {"location": "query", "name": "limit"}},
    "valid": True,
}


# LLM: 与原 stamped_request 同一字段与指纹顺序，只在指纹前放入原 producer 冻结的 harvester 来源引用。
# 函数用途: 构造携带精确 background_source 的已盖戳合成请求。
def _stamped_harvester_request(owner, ref):
    request = {"id": "harvest-request", "kind": "ask", "prompt": "合成收割请求", "status": "pending",
               "user_id": "synthetic-user", "metadata": {"provider": "synthetic", "channel": "synthetic",
               "user_id": "synthetic-user", "chat_id": "chat-a", "background_source": ref},
               "conversation": {"channel": "synthetic", "canonical_user_id": "synthetic-user",
                                "channel_conversation_id": "chat-a"}}
    request[REQUEST_OWNER_KEY] = asdict(owner)
    request["metadata"]["gateway_admin_binding_version"] = "binding-v1"
    request["request_fingerprint"] = gateway_request_fingerprint(request, request["id"])
    request["request_fingerprint_schema"] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    return freeze_request_route(request, owner, RouteStamp(2, "c-p5", 100.0, "binding-v1"))


# LLM: 真实 owner home、真实保证档 watch 快照与原 freeze_source 引用；请求经 P3 预留、P5 快照与原 claim 入口认领。
# 函数用途: 准备一拍原请求收割所需的 claim、来源引用、agent 与抓取计数。
@pytest.fixture
def harvest_case(tmp_path, monkeypatch):
    configure_canonical_background_contracts()
    monkeypatch.setattr(execution, "registry", ws.WatchRegistry())
    owner = OwnerIdentity.provider_user("synthetic", "alice")
    root = tmp_path / "home"
    agent = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root, owner)))
    owner_home = agent.home_paths.owner_home_dir
    state = ws.new_state(owner_home, "http://src.example/pull", {"full_read_per_pull": 4})
    state.source_envelope = dict(_ENVELOPE)
    state.audit_guarantee = True
    state.owner_id = agent.home_paths.owner_id
    ws.persist_state(state)
    ref = freeze_source(root, owner, owner_home / "watch_state" / f"{state.watch_id}.json", "harvester")
    request = _stamped_harvester_request(owner, ref)
    launch = mode.WorkerLaunch(WorkerPartition(request["route"]["worker"], 2, "c-p5"), tmp_path / "gateway", root, 101)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    paths = gateway_paths_from_root(launch.gateway_root)
    ensure_gateway_folders(paths)
    path = paths.inbox / f"{request['id']}.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    snapshot = freeze_claim_snapshot(freeze_worker_bootstrap(launch), ClaimCandidate(path, request, reservation_for(request)))
    reservation = reservation_file(paths, request["id"])
    reservation.parent.mkdir(parents=True, exist_ok=True)
    reservation.write_bytes(snapshot.reservation_bytes)
    claim = claim_request(paths, path, worker_snapshot=snapshot)
    assert claim is not None
    current = json.loads(claim.path.read_text(encoding="utf-8"))
    binding = bind_claimed_request(snapshot, claim, current)
    fetches = []

    def fetch(_tool, request_obj):
        fetches.append(request_obj)
        return True, {"items": [{"seq": n, "kind": "beat", "note": f"n{n}"} for n in range(3)],
                      "next_cursor": 3, "has_more": False}, ""

    monkeypatch.setattr(WatchStreamTool, "_fetch_json", fetch)
    monkeypatch.setattr(harvest_parent, "observe_harvest_parent", lambda *_args: None)
    monkeypatch.setattr(execution, "_parent_active", lambda *_args: True)
    context = {"agent": agent, "request": current, "request_path": claim.path, "request_id": claim.request_id}
    return SimpleNamespace(agent=agent, binding=binding, context=context, ref=ref, root=root,
                           state=state, fetches=fetches)


# LLM: 只在原 worker 执行域与原后台请求域内调用产品入口；返回值或异常原样交给断言。
# 函数用途: 以原运输作用域执行一拍收割。
def _run(case):
    with worker_execution_scope(case.binding, case.agent), bridge.background_request_scope(case.context):
        return execution.execute_harvester_source(case.agent, case.ref, read_source(case.root, case.ref))


# 函数用途: 返回该 watch 的原租约文件路径。
def _harvester_lease(case) -> Path:
    return hv._lease_path(case.state)


# 函数用途: 读取盘上已提交的 watch 快照。
def _committed(case):
    return ws.load_state(case.state.owner_home, case.state.watch_id)


# LLM: 通知一次普通失败：已提交游标/spool 保持，原租约仍按 finally 收尾释放，对外异常就是通知失败本身。
# 函数用途: 验证通知失败不遗留租约、不回滚提交、不被收尾吞掉。
def test_notification_failure_still_releases_original_lease(harvest_case, monkeypatch):
    def failing_wake(_agent, _state):
        assert _harvester_lease(harvest_case).exists()
        raise OSError(errno.EIO, "simulated I/O error")

    monkeypatch.setattr(source_worker, "wake_audit_source_worker", failing_wake)
    with pytest.raises(OSError, match="simulated I/O error"):
        _run(harvest_case)

    assert not _harvester_lease(harvest_case).exists()
    committed = _committed(harvest_case)
    assert committed.cursor == 3 and committed.spool_seq >= 1
    assert hv._read_harvest_transaction(committed) is None
    assert len(harvest_case.fetches) == 1


# LLM: 收尾删除租约遇一次普通 I/O 错误：沿同一原计划/作用域有界重试后释放，本拍结果照常 handled。
# 函数用途: 验证普通清理错误的有界重试。
def test_ordinary_cleanup_error_is_retried_with_same_plan(harvest_case, monkeypatch):
    monkeypatch.setattr(source_worker, "wake_audit_source_worker", lambda *_args: True)
    lease_path = _harvester_lease(harvest_case)
    original = Path.unlink
    calls = {"failed": 0}

    def flaky_unlink(self, *args, **kwargs):
        if self == lease_path and not calls["failed"]:
            calls["failed"] += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    result = _run(harvest_case)

    assert calls["failed"] == 1
    assert result == {"outcome": "handled", "status": "ok", "cursor": 3}
    assert not lease_path.exists() and _committed(harvest_case).cursor == 3


# LLM: 通知期间租约被接手者换代（冲突）：收尾核到非原租约即 typed 未知，不删接手者租约、不新领租约。
# 函数用途: 验证租约冲突不授权删除。
def test_lease_conflict_during_notification_is_typed_and_keeps_successor(harvest_case, monkeypatch):
    lease_path = _harvester_lease(harvest_case)
    successor = {}

    def takeover(_agent, _state):
        current = json.loads(lease_path.read_text(encoding="utf-8"))
        successor.update(current, lease_id="successor-lease")
        lease_path.write_text(json.dumps(successor), encoding="utf-8")
        return True

    monkeypatch.setattr(source_worker, "wake_audit_source_worker", takeover)
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        _run(harvest_case)

    assert json.loads(lease_path.read_text(encoding="utf-8")) == successor
    assert _committed(harvest_case).cursor == 3


# LLM: 通知期间原执行窗口过期（原 lifetime 失活）：收尾短门报 typed 未知，原租约记录保留给确实死者接手，不删除。
# 函数用途: 验证执行权过期不授权删除租约。
def test_expired_scope_during_notification_keeps_original_lease(harvest_case, monkeypatch):
    lease_path = _harvester_lease(harvest_case)
    held = {}

    def expire(_agent, _state):
        held["lease"] = lease_path.read_bytes()
        bridge.current_background_request().lifetime.active = False
        return True

    monkeypatch.setattr(source_worker, "wake_audit_source_worker", expire)
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        _run(harvest_case)

    assert lease_path.read_bytes() == held["lease"]
    assert _committed(harvest_case).cursor == 3
