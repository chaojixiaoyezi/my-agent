"""W0 四条跨片闭环：真实临时账本与入口，仅提供方/业务计算和时钟使用替身。

接口沿 W12 已冻结的增量字段，不在测试中伪造任务书里的新 DTO 类或补接生产路由。
本地组合结果不代表真实 Gateway 换进程、飞书运输或线上模型已验收。
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import delivery, feishu
from agent_py_agent.agent.adapter.delivery import GatewayReplyDeliveryStore
from agent_py_agent.agent.adapter.feishu_progress_card import render_progress_card
from agent_py_agent.agent.adapter.ingress import GatewayIngressStore
from agent_py_agent.agent.adapter.manager import ChannelManager, GatewayAskSubmission
from agent_py_agent.agent.adapter.progress_draft import GatewayProgressBatch
from agent_py_agent.agent.adapter.protocol import IncomingMessage
from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.runtime.loop_models import RecoveryEligibility
from agent_py_agent.agent.assets.descriptor import execution_definition_ref as asset_definition_ref
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.conversation import background_claim, runtime
from agent_py_agent.agent.conversation.background_recovery import BackgroundRecoveryGuard
from agent_py_agent.agent.conversation.channels import DeliveryReceipt
from agent_py_agent.agent.delivery.execution_view import project_execution_view
from agent_py_agent.agent.gateway_parts import recovery, restart_service
from agent_py_agent.agent.gateway_parts.io import (
    read_json_file,
    write_gateway_request,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.status_rendering import gateway_readiness
from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.scheduler.execution_policy import (
    execution_definition_ref as scheduler_definition_ref,
)
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore, execution_receipt
from agent_py_agent.cli import gateway_restart_handover as handover
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)
from agent_py_agent.tests.test_memory_promotion_v2 import _explicit
from agent_py_agent.tests.test_plan_w2_recovery_budget import _scheduler
from agent_py_agent.tests.test_plan_w8_schedule_target import asset_case
from agent_py_agent.tests.test_q4_foreground_recovery_regressions import _q4_case


class Clock:
    """只替换墙钟，不让排空、心跳或轮询测试依赖实际 sleep。"""

    # LLM: 测试时钟只保存合成时间，由 clock fixture 注入，不创建线程或定时任务。
    # 函数用途: 给各闭环提供可手动前进的时间。
    def __init__(self, now=1000.0):
        self.now = now


# LLM: 自动前置校验所列真实组件的导入位置；不修改 sys.path，不用旧检出结果代替当前版本。
# 函数用途: 防止虚拟环境的 editable 安装把测试带到另一棵代码树。
@pytest.fixture(scope="module", autouse=True)
def current_source_tree():
    """明确验收当前工作树；editable 安装或安全路径模式不得把业务导入带到旧 checkout。"""
    root = Path(__file__).resolve().parents[2]
    for module in (delivery, feishu, runtime_mixin, background_claim, runtime, recovery, restart_service,
                   handover, sys.modules[render_progress_card.__module__], sys.modules[WorkflowRunStore.__module__]):
        assert Path(module.__file__).resolve().is_relative_to(root), module.__file__


# LLM: 每例替换并恢复墙钟，前后清空进程内排空门；不启动或停止真实服务。
# 函数用途: 隔离测试时间与排空状态，让各组合链独立重跑。
@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(delivery.time, "time", lambda: value.now)
    restart_service.reset_restart_drain_state()
    yield value
    restart_service.reset_restart_drain_state()


# LLM: 仅构造合成结构化事件及一条非工具噪声，保持跨刷新 call_id 稳定。
# 函数用途: 提供可辨别真实步骤与完成宣言的测试输入。
def _tool_events(request_id, phase):
    """正文含完成宣言的噪声不得进入工具步骤，原 call_id 在刷新间保持不变。"""
    return [
        {"kind": "tool_progress", "request_id": request_id, "progress": {
            "call_id": f"w0-call-{index}", "tool": "read_file", "phase": phase,
            "ok": True if phase == "finished" else None,
            "handler_executed": True, "duration_ms": index + 10,
        }} for index in range(5)
    ] + [{"kind": "assistant_commentary", "text": "已完成全部工作", "tool": "invented"}]


# LLM: 保留真实受理、草稿、worker、投递及 renderer；只替换 Gateway/飞书网络边界。
# 函数用途: 验证同一入站话题从受理到卡片刷新和最终正文的本地闭环。
def test_inbound_progress_card_and_final_keep_original_topic(tmp_path, monkeypatch, clock):
    """W12/W5/W6：受理先落盘，进度同卡编辑，最终回执仍属于原话题。"""
    calls, receipts, submissions = [], [], []

    # LLM: 捕获真实 adapter 请求参数并返回合成消息 ID，不访问飞书。
    # 函数用途: 记录实际准备外发的卡片和最终正文。
    def provider(method, path, body, _token):
        calls.append((method, path, dict(body)))
        identity = "om_w0_card" if body.get("msg_type") == "interactive" else "om_w0_final"
        return {"code": 0, "data": {"message_id": identity}}

    monkeypatch.setattr(feishu, "_feishu_msg_api_with_retry", provider)
    monkeypatch.setattr(feishu, "_feishu_msg_api", provider)
    adapter = feishu.FeishuAdapter({"feishu_session_lock_enabled": False})
    monkeypatch.setattr(adapter, "_get_tenant_access_token", lambda: "synthetic-only")
    manager = ChannelManager(delivery_state_dir=tmp_path, reply_policy="thread", progress_render_kind="card")
    manager.register_adapter(adapter)
    status, = manager._delivery_registry.runtime_snapshot()
    assert status.installed and status.configured and status.capabilities.reply_policy
    assert not status.ready  # REST 替身返回成功不等于真实长连接健康或平台可达。
    assert status.to_dict()["health"]["state"] != "healthy"
    request_id = "w0-inbound"

    # LLM: 仅替换 Gateway 提交边界，受理落盘和原 worker 仍真实执行。
    # 函数用途: 确认先持久接收，再提交唯一原请求。
    def submit(*args):
        submissions.append(args)
        return GatewayAskSubmission(request_id)

    monkeypatch.setattr(manager, "_submit_gateway_payload", submit)
    original_deliver = manager._delivery_service.deliver

    # LLM: 必须调用原投递方法，仅旁观返回的真实 DeliveryReceipt，不伪造发送成功。
    # 函数用途: 对照最终组件回执与重新读取的耐久记录。
    def observe_receipt(*args, **kwargs):
        receipt = original_deliver(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(manager._delivery_service, "deliver", observe_receipt)
    message = IncomingMessage("feishu", "ou_w0_actor", "读取原话题资料", "om_w0_inbound",
        conversation_id="oc_w0_group:thread:omt_w0_provider", metadata={
            "feishu_chat_type": "group", "feishu_chat_id": "oc_w0_group",
            "feishu_thread_id": "omt_w0_provider", "feishu_root_id": "om_w0_root",
            "feishu_parent_id": "om_w0_parent"})
    assert manager.route_message(message)
    durable_ingress = GatewayIngressStore(tmp_path).records()
    assert len(durable_ingress) == 1 and not submissions
    assert durable_ingress[0].provider_message_id == message.message_id
    assert manager._delivery_worker._process_record(durable_ingress[0], clock.now) == 1
    pending, = GatewayReplyDeliveryStore(tmp_path).pending()
    assert len(submissions) == 1 and pending.request_id == request_id
    assert (pending.message_id, pending.provider_thread_id, pending.root_id, pending.parent_id) == (
        message.message_id, "omt_w0_provider", "om_w0_root", "om_w0_parent")
    assert pending.reply_policy == "thread" and pending.conversation_id == message.conversation_id

    current = SimpleNamespace(final=None, phase="started", revision=1)

    # LLM: 替换进度拉取网络边界，使用真实 W6 投影与公共批次协议，不补接生产 renderer。
    # 函数用途: 将合成开始或完成事件交给原进度刷新入口。
    def progress(row):
        view = project_execution_view(_tool_events(row.request_id, current.phase),
            request_id=row.request_id, source_revision=current.revision)
        assert len(view["steps"]) == 5 and view["total_steps"] is None
        return GatewayProgressBatch((), current.revision, "stream", True,
            steps=tuple(view["steps"]), render_kind="card", source_revision=current.revision)

    manager._reply_delivery._poll_progress = progress
    manager._poll_gateway_once = lambda _row, interval: current.final
    assert manager._reply_delivery.run_once() == 0
    first, = GatewayReplyDeliveryStore(tmp_path).pending()
    assert first.progress_draft["display_message_id"] == "om_w0_card"
    assert first.progress_cursor == 1
    expected_cards = [render_progress_card({"request_id": request_id, **first.progress_draft})]
    clock.now += 2
    current.phase, current.revision, current.final = "finished", 2, "原话题的最终业务结果"
    assert manager._reply_delivery.run_once() == 1
    terminal = GatewayReplyDeliveryStore(tmp_path).terminal_receipt(request_id)
    assert terminal["disposition"] == "sent" and terminal["request_id"] == request_id
    assert terminal["reply_policy"] == "thread" and terminal["reply_to"] == message.message_id
    assert not GatewayReplyDeliveryStore(tmp_path).pending()
    assert terminal["progress_draft"]["source_revision"] == 2
    durable_steps = terminal["progress_draft"]["steps"]
    assert [step["step_id"] for step in durable_steps] == [f"w0-call-{index}" for index in range(5)]
    assert all(step["phase"] == "finished" and step["ok"] is True for step in durable_steps)
    expected_cards.append(render_progress_card({"request_id": request_id, **terminal["progress_draft"]}))
    creates = [row for row in calls if row[0] == "POST" and row[2].get("msg_type") == "interactive"]
    edits = [row for row in calls if row[0] == "PATCH"]
    finals = [row for row in calls if row[0] == "POST" and row[2].get("msg_type") == "text"]
    assert len(creates) == len(edits) == len(finals) == 1
    assert calls.index(creates[0]) < calls.index(edits[0]) < calls.index(finals[0])
    assert json.loads(finals[0][2]["content"])["text"] == current.final
    assert creates[0][1] == finals[0][1] == "/im/v1/messages/om_w0_inbound/reply"
    assert creates[0][2]["reply_in_thread"] is finals[0][2]["reply_in_thread"] is True
    assert edits[0][1].endswith("/om_w0_card")
    final_receipt, = receipts
    assert isinstance(final_receipt, DeliveryReceipt) and final_receipt.delivery_status == "sent"
    assert final_receipt.component_receipts == tuple(terminal["component_receipts"])
    assert terminal["component_receipts"][0]["provider_message_id"] == "om_w0_final"
    assert manager._reply_delivery.run_once() == 0
    assert len([row for row in calls if row[0] == "POST" and row[2].get("msg_type") == "text"]) == 1
    # 不替换生产 renderer：外发实际卡片必须与 W6 同一快照投影相等，否则保留真实跨片失败。
    actual_cards = [json.loads(row[2]["content"]) for row in (*creates, *edits)]
    assert actual_cards == expected_cards, "W5 实际投递未消费 W6 有界 Card 2.0 renderer"
    assert all(card["schema"] == "2.0" for card in actual_cards)
    assert "invented" not in json.dumps(actual_cards, ensure_ascii=False)


# LLM: 以真实排空门、请求文件及 RuntimeRepository 验证同代接班；进程存活使用观察替身。
# 函数用途: 检查排空与重开存储后，原请求和前台接手责任仍可续收。
def test_restart_under_load_preserves_request_and_foreground_handoff(tmp_path, monkeypatch, clock):
    """W12/W1/W2：排空门、最终清理、同代就绪及新实例重读均不得丢原恢复责任。"""
    agent, repo, record, _operation, params = _q4_case(tmp_path)
    bound = runtime_mixin._auto_recover_unknown_for_foreground_user_turn(agent, repo,
        repo.get_agent_run(record["agent_run_id"]), params)
    handoff_ref = json.loads(json.dumps(bound.foreground_recovery_handoff_ref))
    assert handoff_ref["attempt_ids"] == [record["attempt_id"]]
    paths = gateway_paths_from_root(tmp_path / "gateway")
    ensure_gateway_folders(paths)
    payload = {"id": params.request_id, "kind": "ask", "prompt": "继续核实原操作",
        "channel": "tui", "user_id": agent.home_paths.owner_id, "status": "processing",
        "created_at": 900.0, "started_at": 900.0, "lease_heartbeat_at": 900.0,
        "metadata": {"run_id": params.run_id, "task_id": params.task_id,
                     "conversation_thread_id": handoff_ref["thread_id"]}}
    request_path = write_gateway_request(paths, payload)
    request_path.replace(paths.processing / request_path.name)
    identity = {"pid": os.getpid(), "start_time": "w0-predecessor", "host_id": "fixture"}
    context = GatewayRunContext(agent, paths, tmp_path / "agent.yaml",
        process_identity=identity, process_started_at=900.0)
    monkeypatch.setattr(handover, "log_gateway_event", lambda *_args: None)
    request = restart_service.submit_restart_request(paths, target_pid=os.getpid(),
        requester={"thread_id": handoff_ref["thread_id"],
                   "conversation_store_root": str(agent.conversation_store.storage.root)},
        reason="合成负载接班", cooldown_seconds=0, now=clock.now)["request"]
    phases = []
    with ExitStack() as inflight:
        assert inflight.enter_context(restart_gate.tool_execution_admission({
            "operation_id": "w0-draining", "run_id": params.run_id, "request_id": params.request_id}))

        # LLM: 观察真实 drain 阶段并释放测试中的在途工具上下文，不改写门控结果。
        # 函数用途: 证明有负载时先关门，再等原工具排空。
        def progress(facts):
            phases.append(facts["phase"])
            assert (paths.processing / request_path.name).exists()
            if facts["phase"] == "tool_drain":
                assert restart_gate.tool_gate_closed() and restart_gate.executing_tool_count() == 1
                inflight.close()

        drained = restart_service.run_restart_drain(request, active_turns=lambda: 0,
            turn_wait_seconds=0, drain_timeout_seconds=1, on_progress=progress)
    assert drained["ok"] and phases == ["turn_wait", "tool_drain"]
    assert restart_gate.executing_tool_count() == 0 and restart_gate.tool_gate_closed()
    marker = handover._mark_drained_restart(context, request, drained)["restart"]
    assert restart_service.consume_restart_marker(paths, old_pid_alive=lambda _pid: False) is None
    write_json_file_atomic(paths.state, {"pid": os.getpid(), "status": "stopped",
        "started_at": 900.0, "process_identity": identity})
    cleanup = handover.finalize_restart_cleanup(context, marker, {
        "drain_complete": True, "alive_threads": [], "background_blockers": [], "unresolved_operation_refs": []})
    assert cleanup["status"] == "clean_drain" and cleanup["handover_allowed"]
    assert restart_service.consume_restart_marker(paths, old_pid_alive=lambda _pid: True) is None
    successor_marker = restart_service.consume_restart_marker(paths, old_pid_alive=lambda _pid: False)
    assert successor_marker["request_id"] == request["request_id"]
    report = recovery.recover_gateway_processing_requests_report(paths, startup=True, planned_restart=True)
    assert report.load_errors == [] and report.summary["requeued"] == 1
    recovered = read_json_file(paths.inbox / request_path.name)
    assert recovered["id"] == params.request_id and recovered["metadata"] == payload["metadata"]
    assert recovered.get("unplanned_resume_count", 0) == payload.get("unplanned_resume_count", 0)

    successor = RuntimeRepository(repo.db_path)
    agent.subagents.runtime_db = successor
    assert successor.foreground_recovery_handoff_valid(handoff_ref,
        thread_id=handoff_ref["thread_id"], attempt_ids=handoff_ref["attempt_ids"])
    assert not RecoveryEligibility.from_block(successor.main_agent_recovery_block_for_task(params.task_id)).eligible
    rebound = runtime_mixin._auto_recover_unknown_for_foreground_user_turn(agent, successor,
        successor.get_agent_run(record["agent_run_id"]), params)
    assert rebound.foreground_recovery_handoff_ref == handoff_ref
    assert len(successor.attempts_for_run(record["agent_run_id"])) == 1
    generation = {**identity, "start_time": "w0-successor"}
    write_json_file_atomic(paths.state, {"pid": os.getpid(), "status": "running", "started_at": clock.now,
        "process_identity": generation, "restart_request_id": request["request_id"]})
    assert not gateway_readiness(paths, pid=os.getpid(), process_alive=True,
        expected_generation=identity, restart_request_id=request["request_id"]).ready
    notified = handover.announce_restart_completed(agent, successor_marker, requeued=1,
        paths=paths, expected_generation=generation)
    assert notified["ready_observation"] and notified["notification_results"][0]["status"] == "written"
    runtime_mixin._acknowledge_foreground_recovery_notice(agent, rebound, SimpleNamespace(runtime_status="ok"))
    assert successor.main_agent_recovery_block_for_task(params.task_id) is None
    assert recovery.recover_gateway_processing_requests(paths, startup=True, planned_restart=True)["requeued"] == 0


# LLM: 所有记忆与证据依赖仅落在合成 owner 根，保留真实批次服务和持久账本。
# 函数用途: 为健康后台任务组装同一 owner 的记忆晋升入口。
def _memory_service(agent):
    """候选、正式记忆、证据及健康任务共用同一 owner 根和会话库。"""
    from agent_py_agent.agent.capability.persona_repository import PersonaRepository
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
    from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
    from agent_py_agent.agent.memory_store.promotion import (
        ConversationMessageEvidenceVerifier,
        MemoryPromotionDependencies,
        MemoryPromotionService,
    )

    owner = agent.home_paths.owner_home_dir
    return MemoryPromotionService(dependencies=MemoryPromotionDependencies(
        candidates=CandidateService(owner / "memory/candidates.jsonl"),
        long_term=JsonlMemory(owner / "memory/long_term/memory.jsonl", ops_path=owner / "memory/ops.jsonl"),
        persona=PersonaRepository(owner_home=owner, soul_path=owner / "SOUL.md",
            user_path=owner / "USER.md", agents_path=owner / "AGENTS.md"),
        lessons=LessonRepository(owner / "memory/lessons", owner / "memory/routing/INDEX.md"),
        hot=HotRuleRepository(owner / "memory-hot.md"),
        message_verifier=ConversationMessageEvidenceVerifier(agent.conversation_store),
        tool_verifier=SimpleNamespace(verify=lambda _ref: pytest.fail("用户消息候选不应请求工具证据")),
    ))


class MaintenanceTool(BaseTool):
    """由真实工具执行器认领；业务回调执行真实候选批次和有界 SQLite 维护。"""

    # LLM: 注册测试工具规范与回调，不提前执行业务，也不自行填写操作账。
    # 函数用途: 构造交给原工具执行器的有界维护工具。
    def __init__(self, callback):
        self.model_spec = make_test_model_spec("w0_maintenance")
        self.runtime_policy = make_test_runtime_policy("mutating")
        self.callback = callback
        self.calls = 0

    # LLM: 由原执行器调用，返回真实批次/维护结果；成功沿 ToolHandlerOutcome 的原协议。
    # 函数用途: 统计实际执行次数并运行候选晋升和存储清理。
    def execute(self, _params):
        self.calls += 1
        # 工具成功沿原 ok=True/空 effect_outcome；不能混用工作流回执的 confirmed 枚举。
        return ToolHandlerOutcome("w0_maintenance", True, json.dumps(self.callback()), phase="execution")


# LLM: 两种恢复屏障分别运行真实 claim、候选服务、SQLite 作用域和操作账；仅调度业务入口用替身。
# 函数用途: 证明阻断任务不吞来源，健康任务能执行并分拍完成维护。
@pytest.mark.parametrize("pending_handoff", [False, True], ids=["unknown", "pending-handoff"])
def test_recovery_barrier_allows_healthy_batch_and_bounded_maintenance(tmp_path, monkeypatch, clock, pending_handoff):
    """W12/W2/W3/W4：同 owner 阻断任务不消费来源，健康 claim 实际执行并落操作账。"""
    from agent_py_agent.agent.conversation import learning_source
    from agent_py_agent.agent.memory_store import candidates as candidate_ledger

    agent, repo, record, _operation, params = _q4_case(tmp_path)
    if pending_handoff:
        runtime_mixin._auto_recover_unknown_for_foreground_user_turn(agent, repo,
            repo.get_agent_run(record["agent_run_id"]), params)
    scheduler, _calls = _scheduler(agent)
    guard = BackgroundRecoveryGuard(lambda: repo.main_agent_recovery_block_for_task)
    original_block = guard.block_for_task(params.task_id)
    assert original_block and not RecoveryEligibility.from_block(original_block).eligible
    signal = scheduler.store.wakes.raise_signal({"thread_id": params.task_attributes["conversation_thread_id"],
        "root_task_id": params.task_id, "reason": "thread_goal_continue", "now": clock.now})
    healthy = repo.record_run_creation(owner_id=agent.home_paths.owner_id, run_id="w0-healthy-run",
        conversation_task_id="w0-healthy-task", thread_id=signal.thread_id, role="main")
    scheduler.store.tasks.bind({"thread_id": signal.thread_id, "task_id": "w0-healthy-task", "goal": "批量维护"})
    service = _memory_service(agent)
    source = scheduler.store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "internal",
        "channel_conversation_id": "w0-memory", "channel_user_id": "user-1", "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir), "learning_source": learning_source.user_dialogue_projection()})
    candidates = [_explicit(service, scheduler.store, source, content=f"可验证的独立事实 {index}",
        subject_key=f"w0.fact.{index}", promotion_target="") for index in range(3)]
    writes, promotions, steps, executions = [], [], [], []
    original_write = candidate_ledger.write_private_text_file_atomic_unlocked

    # LLM: 调用原原子写方法后仅统计候选账落盘次数，不绕过持久化失败。
    # 函数用途: 检查整个批次而不是每个候选各自重复落盘。
    def observe_write(path, text, **kwargs):
        result = original_write(path, text, **kwargs)
        if path == service.candidates.path:
            writes.append(text)
        return result

    monkeypatch.setattr(candidate_ledger, "write_private_text_file_atomic_unlocked", observe_write)
    local = LocalStore(agent.home_paths.owner_home_dir / "local.db", enable_fts=False)
    for index in range(8):
        local.record_event("w0", payload={"index": index, "padding": "x" * 80})

    # LLM: 真正执行候选批次与首拍清理，返回有界扫描事实，不伪造成功计数。
    # 函数用途: 将记忆和存储两个片的实际业务放入健康任务。
    def maintenance():
        promotions.extend(service.promote_batch(candidates, reviewer="w0", automatic=True))
        report = local.prune_events_step(before=clock.now + 1, keep_last=2, max_delete=2, max_bytes=128)
        steps.append(report)
        return {"candidate_ids": [row.candidate_id for row in promotions], "scanned_bytes": report["scanned_bytes"]}

    tool = MaintenanceTool(maintenance)
    operations = ManagedOperationStore(repo)
    operation_id = f"tool_call:{healthy['attempt_id']}:w0-maintenance-call"

    # LLM: 调度器已认领后进入原工具执行器和同 owner 操作账，执行失败必须使测试失败。
    # 函数用途: 核对健康认领确实触发工具而非只返回一个空结果。
    def execute_work(request):
        executions.append(request)
        with sqlite_connection_scope(True):
            execution = execute_canonical_test_call(tmp_path, tools={"w0_maintenance": tool},
                tool_name="w0_maintenance", arguments={}, run_id="w0-healthy-run", attempt_id=healthy["attempt_id"],
                call_id="w0-maintenance-call", operation_id=operation_id, operation_store=operations,
                operation_store_required=True, operation_owner_id=agent.home_paths.owner_id,
                trusted_run_context={"run_scope": {"task_id": "w0-healthy-task"}})
        if not execution.result.ok:
            pytest.fail(json.dumps({"calls": tool.calls, "output": execution.result.output,
                "details": execution.result.metadata.get("handler_details")}, ensure_ascii=False))
        assert execution.result.handler_executed
        return SimpleNamespace()

    scheduler.runtime.run_once = execute_work
    monkeypatch.setattr(background_claim, "ConversationRunClaimHeartbeat",
        lambda _request: SimpleNamespace(start=lambda: None, stop=lambda: None))
    dependencies = runtime._background_claim_dependencies(scheduler)
    for tick in range(3):
        assert background_claim.run_claimed(dependencies, {"thread_id": signal.thread_id, "task_id": params.task_id,
            "reason": signal.reason, "wake_signal": signal, "now": clock.now + tick}) is None
        assert scheduler.store.wakes.pending_one(signal.wake_signal_id) is not None
    assert not executions and tool.calls == 0
    assert background_claim.run_claimed(dependencies, {"thread_id": signal.thread_id, "task_id": "w0-healthy-task",
        "reason": "ordinary_task_resume", "now": clock.now + 4}) is not None
    assert len(executions) == tool.calls == 1
    assert len(promotions) == 3 and all(row.promoted and row.formal_memory_changed for row in promotions)
    assert len(writes) == 2 and len(service.long_term.all()) == 3
    durable = operations.get_tool_operation(owner_id=agent.home_paths.owner_id,
        run_id="w0-healthy-run", operation_id=operation_id)
    assert durable.status == "succeeded" and durable.attempt_id == healthy["attempt_id"]
    assert steps[0]["partial"] and steps[0]["scanned_bytes"] <= 128
    for _tick in range(64):
        if not steps[-1]["partial"]:
            break
        steps.append(LocalStore(local.db_path, enable_fts=False).prune_events_step(
            before=clock.now + 1, keep_last=2, max_delete=2, max_bytes=128))
    assert not steps[-1]["partial"] and all(row["scanned_bytes"] <= 128 for row in steps)
    assert sum(row["expired_rows"]["deleted"] for row in steps) == 2
    assert all(not row["physical_shrink"]["performed"] for row in steps)
    assert len(local.events_path.read_text().splitlines()) == 6
    assert guard.block_for_task(params.task_id) == original_block
    assert scheduler.store.wakes.pending_one(signal.wake_signal_id) is not None
    assert len(repo.attempts_for_run(record["agent_run_id"])) == 1


# LLM: 公开 workflow 定时目标驱动真实引擎和回执；仅模型提供方返回合成结构化结果。
# 函数用途: 分别验证本代收口与旧代迟到后新定义仍可独立运行。
@pytest.mark.parametrize("replace_definition", [False, True], ids=["current-generation", "late-old-generation"])
def test_scheduled_workflow_receipt_obeys_monitor_definition_generation(tmp_path, monkeypatch, clock, replace_definition):
    """W12/W7/W8：公开 workflow 目标进入真实执行器，迟到结果只进自己的历史。"""
    case = asset_case(tmp_path, "workflow", analysis=True)
    repo = case.agent.scheduler_repository
    target = case.job["execution_target"]
    definition = case.workflows.get_workflow(target["asset_id"]).to_dict()
    assert target["kind"] == "workflow" and target["execution_definition_ref"]
    model_calls, replacement = [], []

    # LLM: 模拟单次模型响应；旧代场景只在执行途中通过原 repository 更新下一次定义。
    # 函数用途: 让真实调度收口面对可控的定义代次变化。
    def provider(prompt, *, response_schema, **_kwargs):
        model_calls.append((prompt, response_schema))
        if replace_definition and not replacement:
            current = repo.get_job(case.job["job_id"])
            replacement.append(repo.update_job(current["job_id"], expected_version=current["version"],
                patch={"schedule": {"kind": "every", "every_seconds": 600, "anchor_at": 2000, "timezone": "UTC"}},
                now=1001))
        return ModelResponse(text='{"ok":true,"value":7}', backend="w0-fake")

    monkeypatch.setattr(case.agent.backend, "generate_structured", provider, raising=False)
    case.agent.scheduler_service.enqueue_ready_runs(now=clock.now)
    run, = repo.queued_runs(now=clock.now)
    frozen = json.loads(json.dumps(run))
    assert run["execution_target"] == target and run["definition_generation"] == case.job["definition_generation"]
    clock.now = 1001
    case.scheduler.tick(now=clock.now)
    history, errors = repo.history()
    assert not errors and len(history) == 1
    finished = history[0]
    assert finished["run_id"] == run["run_id"] and finished["status"] == "done", finished
    assert finished["execution_target"] == frozen["execution_target"]
    assert finished["model_call_count"] == len(model_calls) == 1 and not case.agent.backend.prompts
    data = finished["structured_result"]
    record = WorkflowRunStore(case.agent.home_paths.owner_home_dir).load_run(data["workflow_run_id"])
    assert WorkflowRunRecord.from_dict(json.loads(json.dumps(record.to_dict()))) == record
    receipt = execution_receipt(record)
    assert receipt["workflow_run_id"] == data["workflow_run_id"]
    # W7 的语义 hash 与 W8 的带 schema 冻结引用不是同一格式；分别核对同一份执行前快照。
    assert receipt["execution_definition_ref"] == data["execution_definition_ref"] == asset_definition_ref(definition, kind="workflow")
    assert finished["execution_definition_ref"] == target["execution_definition_ref"] == scheduler_definition_ref(
        "workflow", target["asset_id"], definition)
    assert receipt["phase"] == data["phase"] == "terminal"
    assert receipt["effect_outcome"] == data["effect_outcome"] == "confirmed"
    assert receipt["confirmed_step_refs"] == ("one",) and data["confirmed_step_refs"] == ["one"]
    assert record.final_output == {"ok": True, "value": 7} and record.status == "succeeded"
    assert not repo.queued_runs(now=1002) and not case.agent.conversation_store.wakes.pending()
    current = repo.get_job(case.job["job_id"])
    if replace_definition:
        assert current["definition_generation"] == run["definition_generation"] + 1
        # last_run_* 是实际完成的诊断投影；定义门只保护下一次计划、结果、退避与 checkpoint。
        assert current["last_run_status"] == "done"
        for key in ("next_run_at", "result", "precheck_checkpoint", "consecutive_failures", "backoff_until"):
            assert current.get(key) == replacement[0].get(key)
        assert not repo.reserve_due_runs(now=1999)
        clock.now = 2000
        case.agent.scheduler_service.enqueue_ready_runs(now=clock.now)
        successor, = repo.queued_runs(now=clock.now)
        assert successor["definition_generation"] == current["definition_generation"]
        assert successor["run_id"] != run["run_id"] and successor["scheduled_for"] == 2000
        case.scheduler.tick(now=2001)
        assert len(model_calls) == 2 and len(repo.history()[0]) == 2
    else:
        assert current["definition_generation"] == run["definition_generation"]
        assert current["last_run_status"] == "done" and current["next_run_at"] == 1600
