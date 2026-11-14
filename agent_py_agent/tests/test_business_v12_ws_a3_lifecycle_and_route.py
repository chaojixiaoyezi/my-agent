# LLM: WS-A3 业务空间后台代理生命周期、退休清算、素材版本冻结与命名路由入口验收测试集。
#   覆盖 G1 纯文本任务收口与回报回执（V05-03/V10-03/ROUTE-01/05）、after_task 强制收敛（reuse/suspend/retire, V06-05/06/07）、
#   BIZ-009 退休义务完整核验与配额释放、V09-02/V11-03 资产接受版本冻结与候选版本 409 拒绝、
#   ROUTE-02 准入饱和阻塞报告（ADMISSION_LIMIT_REACHED）、ROUTE-03/06 命名后台代理查询与路由持久化、
#   以及 BIZ-001 隔离并发订单计算（A=4200, B=330）。只用 tmp 隔离 home。
# 模块用途: 验收后台生命周期、退休责任、资产冻结及命名路由全链路行为。
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.conversation.background_scheduling_view import (
    scheduling_view,
    task_result_view,
    task_scheduling_view,
)
from agent_py_agent.agent.conversation.background_team_lifecycle import (
    background_identity,
    background_identity_obligations,
    identity_outstanding,
    reconcile_background_identity,
)
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_assets import BusinessAssetService
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.conversation.route_mode import (
    route_setting,
    set_route_mode,
)
from agent_py_agent.agent.conversation.runtime import (
    BackgroundExecutionResult,
    BackgroundRunRequest,
    _close_out_session_task_turn,
)
from agent_py_agent.agent.conversation.session_task_report import (
    TaskTurnOutcome,
    close_out_turn,
    public_result_refs,
)
from agent_py_agent.agent.conversation.session_tasks import (
    SESSION_TASK_DONE,
    SESSION_TASK_FAILED,
    SessionTaskDraft,
    SessionTaskUpdate,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import (
    capture_im_dispatch,
)
from agent_py_agent.agent.gateway_parts.im_task_routing import (
    prepare_im_task_dispatch,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_business_v12_background_scheduling import (
    add_echo_profile,
    assigned_task,
    native,
    ok,
    scheduler_for,
    tick,
)


# LLM: 构造一个隔离的测试宿主和初始空间，供后台助手生命周期与资产测试使用。
# 函数用途: 返回初始化的 SimpleAgent 实例及关联 HostChannelActor。
def _make_agent_and_actor(tmp_path):
    home = tmp_path / "home"
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=True,
            business_spaces_enabled=True,
            memory_path="memory.jsonl",
            my_agent_home=str(home),
            max_background_agents=5,
        ),
        tmp_path / "workspace",
    )
    # 创建源会话线程
    source_thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-001",
            "channel": "local",
            "channel_conversation_id": "conv-001",
            "channel_user_id": "user-001",
            "owner_id": str(agent.home_paths.owner_id),
            "owner_home": str(agent.home_paths.owner_home_dir),
            "now": time.time(),
        }
    )
    actor = HostChannelActor(
        owner_id=str(agent.home_paths.owner_id),
        source_thread_id=source_thread.thread_id,
        entry_request_id="inv-001",
        channel="local",
        conversation_id="conv-001",
        actor_id="user-001",
    )
    return agent, actor


# LLM: 验证 G1 纯文本任务收口时持久化 reply_message_id 及任务元数据关联，并向 tasks.get 暴露完整 result + report。
# 函数用途: 验证纯文本交付收口时消息与回报关联，以及公开 result_refs 对绝对路径的脱敏。
def test_g1_text_only_closeout_and_reply_linkage(native):  # noqa: F811
    _, host = native
    add_echo_profile(host)
    agent, task_id = assigned_task(native, name="g1-worker")

    # 派发执行轮并模拟纯文本回复（0 文件产物）
    req = BackgroundRunRequest(thread_id=agent["agent_id"], task_id=task_id, reason="session_task")
    exec_result = BackgroundExecutionResult(
        response="Calculated final total: 4200 units",
        runtime_status="ok",
        tool_call_count=1,
        tool_success_count=1,
        material_progress_count=1,
        delivery_artifacts=(),
        message_tool_deliveries=(),
        operation_verification={},
        assistant_commentaries=(),
        display_snapshot={},
    )
    turn_id = "turn-" + task_id
    host.conversation_store.session_tasks.bind_turn(task_id, turn_id=turn_id)

    # fake执行回执不等于规范工作已结束；受管订单须复用宿主TaskLink的客观终态。
    host.conversation_store.tasks.bind({"thread_id": agent["agent_id"], "task_id": task_id,
                                        "goal": "compute order A total", "status": "active"})
    _close_out_session_task_turn(host, req, exec_result, turn_id=turn_id)
    assert host.conversation_store.session_tasks.require(task_id).status == "accepted"
    host.conversation_store.tasks.update_status({"thread_id": agent["agent_id"], "task_id": task_id,
                                                 "status": "completed"})

    # 执行收口
    _close_out_session_task_turn(host, req, exec_result, turn_id=turn_id)

    # 驱动真实后台交付收口（唯一的 canonical assistant 消息提交通道）
    runtime = BackgroundMainAgentRuntime(agent=host, store=host.conversation_store)
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.conversation.runtime import (
        _BackgroundSlicePlan,
        _complete_background_slice,
    )

    plan = _BackgroundSlicePlan(
        request=req,
        delivery_context=DeliveryContext(
            channel="local", target=agent["agent_id"], thread_id=agent["agent_id"]
        ),
        channel="local",
        target=agent["agent_id"],
        response=exec_result.response,
        deliver=True,
        delivery_reason="session_task",
        route_supports_transcript=True,
        counters=(
            exec_result.tool_call_count,
            exec_result.tool_success_count,
            exec_result.material_progress_count,
        ),
        runtime_status="ok",
    )
    report = _complete_background_slice(runtime, plan)

    # 验证 messages 中仅存在 1 条对应 assistant 消息（无重复 final 消息）
    page = host.conversation_store.messages.history_page_report(agent["agent_id"], limit=10)
    replies = [
        m
        for m in page.rows
        if m.role == "assistant"
        and m.content == "Calculated final total: 4200 units"
    ]
    assert len(replies) == 1
    assert replies[0].metadata.get("assistant_part_id") == "final"

    # 验证任务终态与元数据中持久化的 reply_message_id
    stored = host.conversation_store.session_tasks.require(task_id)
    assert stored.status == SESSION_TASK_DONE
    assert stored.summary == "Calculated final total: 4200 units"
    assert stored.result_refs == ()
    assert stored.metadata.get("reply_message_id") == report.message_id
    assert stored.metadata.get("reply_message_id") == replies[0].message_id
    assert stored.metadata.get("result_report_receipt")

    # 验证 tasks.get 暴露的结构化 result 与 report
    detail = ok(native, "tasks.get", {"task_id": task_id})
    assert detail["result"]["summary"] == "Calculated final total: 4200 units"
    assert detail["result"]["summary_truncated"] is False
    assert "report" in detail["result"]
    assert detail["result"]["report"]["guidance"] in {"pending", "submitted", "consumed"}

    # 验证 public_result_refs 在无产物时返回空列表及 None
    refs, cov = public_result_refs(stored)
    assert refs == []
    assert cov is None


# LLM: 验证 after_task 策略强制收敛（reuse / suspend / retire），任务结束后自动推进身份状态机。
# 函数用途: 验证 reuse 保持 available、suspend 推进到 suspended、retire 推进到 retired。
def test_after_task_enforcement_reuse_suspend_retire(tmp_path):
    agent, actor = _make_agent_and_actor(tmp_path)
    service = BackgroundAgentService(agent)

    # 1. reuse: 完成任务后依然保持 available
    worker_reuse = service.create(
        {"space_id": None, "name": "worker-reuse", "after_task": "reuse"},
        actor=actor,
        idempotency_key="create-reuse",
    )["agent"]
    assert worker_reuse["after_task"] == "reuse"
    assert worker_reuse["identity_state"] == "available"

    task1 = agent.conversation_store.session_tasks.create(
        SessionTaskDraft(actor.source_thread_id, worker_reuse["agent_id"], "task 1")
    )
    turn1 = "turn-reuse-1"
    agent.conversation_store.session_tasks.bind_turn(task1.task_id, turn_id=turn1)
    close_out_turn(agent, turn1, TaskTurnOutcome(ok=True, summary="done 1"))
    reconcile_background_identity(agent, worker_reuse["agent_id"])
    assert service.get(worker_reuse["agent_id"], actor=actor)["agent"]["identity_state"] == "available"

    # 2. suspend: 完成任务后自动推进到 suspended
    worker_suspend = service.create(
        {"space_id": None, "name": "worker-suspend", "after_task": "suspend"},
        actor=actor,
        idempotency_key="create-suspend",
    )["agent"]
    task2 = agent.conversation_store.session_tasks.create(
        SessionTaskDraft(actor.source_thread_id, worker_suspend["agent_id"], "task 2")
    )
    turn2 = "turn-suspend-2"
    agent.conversation_store.session_tasks.bind_turn(task2.task_id, turn_id=turn2)
    req2 = BackgroundRunRequest(thread_id=worker_suspend["agent_id"], task_id=task2.task_id, reason="session_task")
    exec2 = BackgroundExecutionResult(response="done 2", runtime_status="ok", tool_call_count=0,
                                     tool_success_count=0, material_progress_count=0, delivery_artifacts=(),
                                     message_tool_deliveries=(), operation_verification={}, assistant_commentaries=(), display_snapshot={})
    _close_out_session_task_turn(agent, req2, exec2, turn_id=turn2)
    assert service.get(worker_suspend["agent_id"], actor=actor)["agent"]["identity_state"] == "suspended"

    # 3. retire: 完成任务后推进到 retired 并释放配额
    worker_retire = service.create(
        {"space_id": None, "name": "worker-retire", "after_task": "retire"},
        actor=actor,
        idempotency_key="create-retire",
    )["agent"]
    task3 = agent.conversation_store.session_tasks.create(
        SessionTaskDraft(actor.source_thread_id, worker_retire["agent_id"], "task 3")
    )
    turn3 = "turn-retire-3"
    agent.conversation_store.session_tasks.bind_turn(task3.task_id, turn_id=turn3)
    req3 = BackgroundRunRequest(thread_id=worker_retire["agent_id"], task_id=task3.task_id, reason="session_task")
    exec3 = BackgroundExecutionResult(response="done 3", runtime_status="ok", tool_call_count=0,
                                     tool_success_count=0, material_progress_count=0, delivery_artifacts=(),
                                     message_tool_deliveries=(), operation_verification={}, assistant_commentaries=(), display_snapshot={})
    _close_out_session_task_turn(agent, req3, exec3, turn_id=turn3)
    assert service.get(worker_retire["agent_id"], actor=actor)["agent"]["identity_state"] == "retired"


# LLM: 验证 BIZ-009 退休义务完整核验（子代理运行、订阅、审批、未投递回报）及配额释放。
# 函数用途: 验证未结义务阻止 retired 推进至 retiring，义务清零后自动收敛为 retired。
def test_biz_009_retirement_obligations_and_quota_release(tmp_path):
    agent, actor = _make_agent_and_actor(tmp_path)
    service = BackgroundAgentService(agent)

    worker = service.create(
        {"space_id": None, "name": "worker-biz009"},
        actor=actor,
        idempotency_key="create-biz009",
    )["agent"]
    quota_before = service.quota_summary(actor=actor)
    assert quota_before["used"] == 1

    # 1. 模拟未投递任务回报义务：任务未回报时阻止退休
    task = agent.conversation_store.session_tasks.create(
        SessionTaskDraft(actor.source_thread_id, worker["agent_id"], "pending work")
    )
    retired = service.transition(
        worker["agent_id"], "retire", actor=actor,
        expected_revision=worker["revision"], idempotency_key="retire-biz009", disposition="drain"
    )
    assert retired["agent"]["identity_state"] == "retiring"
    assert task.task_id in retired["outstanding_refs"]

    # 2. 注入 host.background_identity_obligations 适配器测试自定义义务
    agent.background_identity_obligations = lambda host_obj, tid: [f"custom_sub:{tid}"]
    obs = background_identity_obligations(agent, worker["agent_id"])
    assert any("custom_sub" in ref for ref in obs)

    # 移除自定义义务并将任务收口
    agent.background_identity_obligations = None
    agent.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))

    # 义务结清后推进到 retired
    thread_retired = reconcile_background_identity(agent, worker["agent_id"])
    assert background_identity(thread_retired).identity_state == "retired"

    # 验证配额已自动释放
    quota_after = service.quota_summary(actor=actor)
    assert quota_after["used"] == 0


# LLM: 验证委派任务的未投递结果回报与未结唤醒准确阻止退休（保持 retiring），待交付结清后平滑收敛为 retired。
# 函数用途: 验证 delegated 任务在结果回报未入队 (undelivered_report) 或 wake 待处理 (undelivered_wake) 时保持 retiring，直到结案。
def test_delegated_report_and_wake_obligations_hold_retiring_until_delivered(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.execution_threads import (
        EXECUTION_ORIGIN_KEY,
        IM_TASK_EXECUTION_KEY,
    )
    from agent_py_agent.agent.conversation.session_task_report import (
        result_report_key,
    )

    agent, actor = _make_agent_and_actor(tmp_path)
    service = BackgroundAgentService(agent)

    worker = service.create(
        {"space_id": None, "name": "worker-delegated-retire", "after_task": "retire"},
        actor=actor,
        idempotency_key="create-delegated-retire",
    )["agent"]
    worker_id = worker["agent_id"]

    # 创建委派任务并绑定到执行线程
    source_thread = agent.conversation_store.threads.require(actor.source_thread_id)
    recent = source_thread.channel_bindings[0]
    binding = {
        "channel": recent.channel,
        "conversation_id": recent.channel_conversation_id,
        "actor_id": recent.channel_user_id,
    }
    task = agent.conversation_store.session_tasks.create(
        SessionTaskDraft(actor.source_thread_id, worker_id, "delegated work")
    )
    agent.conversation_store.threads.update_atomic(
        worker_id,
        lambda t: replace(
            t,
            metadata={
                **t.metadata,
                IM_TASK_EXECUTION_KEY: {
                    "mode": "delegated",
                    "session_task_id": task.task_id,
                    "root_task_id": task.task_id,
                },
                EXECUTION_ORIGIN_KEY: {
                    "source_thread_id": actor.source_thread_id,
                    "kind": "delegated",
                    "operation_key": "op-test-delegated",
                    "binding": binding,
                },
            },
        ),
    )

    # 任务已到终态但尚未写入结果回报 guidance
    agent.conversation_store.session_tasks.advance(
        task.task_id, SessionTaskUpdate(status="accepted")
    )
    agent.conversation_store.session_tasks.advance(
        task.task_id, SessionTaskUpdate(status=SESSION_TASK_DONE, summary="work complete")
    )

    # 1. 未投递结果回报时：阻止 retired，保持 retiring
    refs, _ = identity_outstanding(agent, agent.conversation_store.threads.require(worker_id))
    assert f"undelivered_report:{task.task_id}" in refs
    thread_retiring = reconcile_background_identity(agent, worker_id)
    assert background_identity(thread_retiring).identity_state == "retiring"
    assert service.get(worker_id, actor=actor)["agent"]["identity_state"] == "retiring"

    # 2. 模拟 guidance 已入队，但 wake 处于 pending 状态
    task = agent.conversation_store.session_tasks.require(task.task_id)
    key = result_report_key(task)
    agent.conversation_store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": actor.source_thread_id,
            "message": "Report complete",
        },
        dedupe_key=key,
    )
    agent.conversation_store.wakes.raise_signal(
        {
            "thread_id": actor.source_thread_id,
            "reason": "session_message",
            "urgency": "normal",
            "dedupe_key": key,
            "retain_handled": True,
        }
    )

    # 处于 undelivered_wake 状态：依然保持 retiring
    refs2, _ = identity_outstanding(agent, agent.conversation_store.threads.require(worker_id))
    assert f"undelivered_wake:{task.task_id}" in refs2
    thread_retiring2 = reconcile_background_identity(agent, worker_id)
    assert background_identity(thread_retiring2).identity_state == "retiring"

    # 3. 来源线程消费并 mark_handled 该 wake
    wake = next(w for w in agent.conversation_store.wakes.pending(limit=0) if w.dedupe_key == key)
    agent.conversation_store.wakes.mark_handled(wake.wake_signal_id)

    # 义务全部结清：自动推进至 retired 并释放配额
    refs3, _ = identity_outstanding(agent, agent.conversation_store.threads.require(worker_id))
    assert f"undelivered_report:{task.task_id}" not in refs3
    assert f"undelivered_wake:{task.task_id}" not in refs3
    assert refs3 == []

    thread_retired = reconcile_background_identity(agent, worker_id)
    assert background_identity(thread_retired).identity_state == "retired"
    assert service.get(worker_id, actor=actor)["agent"]["identity_state"] == "retired"
    assert service.quota_summary(actor=actor)["used"] == 0


# LLM: 验证 V09-02 素材按接受版本绑定，候选版本或跨空间资产直接 409 拒绝（effect_outcome='not_started'）。
# 函数用途: 校验 tasks.submit 中 asset_refs 的接受版本冻结与未接受候选版本拦截。
def test_asset_refs_accepted_version_freezing_and_409_rejection(tmp_path):
    agent, actor = _make_agent_and_actor(tmp_path)
    service = BackgroundAgentService(agent)

    # 开启业务空间并注册空间与资产
    def resolver(kind, ref, act):
        return {
            "owner_id": act.owner_id,
            "ref": ref,
            "artifact_id": "art-001",
            "version_ref": ref,
            "state": "available",
            "space_id": None,
            "project_ref": None,
        }

    space_service = BusinessSpaceService(
        agent.home_paths.owner_home_dir,
        owner_id=str(agent.home_paths.owner_id),
        enabled=True,
        reference_resolver=resolver,
    )
    agent.business_spaces_catalog = space_service

    # 写入初始空间和资产数据到 catalog.json
    catalog_path = agent.home_paths.owner_home_dir / "data" / "business" / "catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_data = {
        "schema_version": "business-catalog.v1",
        "owner_id": str(agent.home_paths.owner_id),
        "revision": 1,
        "spaces": {
            "space-alpha": {
                "owner_id": str(agent.home_paths.owner_id),
                "space_id": "space-alpha",
                "name": "Alpha Space",
                "purpose": "Alpha Space Purpose",
                "state": "active",
                "revision": 1,
                "mounts": [],
                "default_project_ref": None,
            },
            "space-beta": {
                "owner_id": str(agent.home_paths.owner_id),
                "space_id": "space-beta",
                "name": "Beta Space",
                "purpose": "Beta Space Purpose",
                "state": "active",
                "revision": 1,
                "mounts": [],
                "default_project_ref": None,
            },
        },
        "bindings": {},
        "assets": {
            "asset-accepted": {
                "owner_id": str(agent.home_paths.owner_id),
                "asset_id": "asset-accepted",
                "space_id": "space-alpha",
                "name": "Character Accepted",
                "kind": "character",
                "revision": 1,
                "accepted_version": "ver-accepted-v1",
                "state": "linked",
                "project_ref": None,
                "versions": [
                    {
                        "version_ref": "ver-accepted-v1",
                        "artifact_ref": {"artifact_id": "art-001", "version_ref": "ver-accepted-v1"},
                        "state": "accepted",
                        "created_at": "2026-11-10T00:00:00Z",
                    }
                ],
            },
            "asset-candidate-only": {
                "owner_id": str(agent.home_paths.owner_id),
                "asset_id": "asset-candidate-only",
                "space_id": "space-alpha",
                "name": "Character Draft",
                "kind": "character",
                "revision": 1,
                "accepted_version": None,  # 候选版本，尚未接受
                "state": "linked",
                "project_ref": None,
                "versions": [
                    {
                        "version_ref": "ver-draft-v1",
                        "artifact_ref": {"artifact_id": "art-002", "version_ref": "ver-draft-v1"},
                        "state": "candidate",
                        "created_at": "2026-11-10T00:00:00Z",
                    }
                ],
            },
            "asset-cross-space": {
                "owner_id": str(agent.home_paths.owner_id),
                "asset_id": "asset-cross-space",
                "space_id": "space-beta",  # 属于另一个空间
                "name": "Beta Character",
                "kind": "character",
                "revision": 1,
                "accepted_version": "ver-beta-v1",
                "state": "linked",
                "project_ref": None,
                "versions": [
                    {
                        "version_ref": "ver-beta-v1",
                        "artifact_ref": {"artifact_id": "art-003", "version_ref": "ver-beta-v1"},
                        "state": "accepted",
                        "created_at": "2026-11-10T00:00:00Z",
                    }
                ],
            },
        },
        "receipts": {},
    }
    catalog_path.write_text(json.dumps(catalog_data))

    # 1. 尝试绑定候选版本资产 -> 409 STATE_CONFLICT
    with pytest.raises(BusinessError) as exc_info:
        service.submit(
            {
                "space_id": "space-alpha",
                "instruction": "render character",
                "asset_refs": [{"artifact_id": "asset-candidate-only", "version_ref": "ver-draft-v1"}],
            },
            actor=actor,
            idempotency_key="sub-candidate-001",
        )
    assert exc_info.value.code == "STATE_CONFLICT"

    # 2. 尝试跨空间绑定资产 -> 409 SPACE_MISMATCH
    with pytest.raises(BusinessError) as exc_info:
        service.submit(
            {
                "space_id": "space-alpha",
                "instruction": "render character",
                "asset_refs": [{"artifact_id": "asset-cross-space", "version_ref": "ver-beta-v1"}],
            },
            actor=actor,
            idempotency_key="sub-cross-001",
        )
    assert exc_info.value.code == "SPACE_MISMATCH"

    # 3. 绑定正式接受版本资产 -> 成功并冻结 version_ref
    sub_res = service.submit(
        {
            "space_id": "space-alpha",
            "instruction": "render character",
            "asset_refs": [{"artifact_id": "asset-accepted", "version_ref": "ver-accepted-v1"}],
        },
        actor=actor,
        idempotency_key="sub-accepted-001",
    )
    assert sub_res["state"] == "accepted"
    task = agent.conversation_store.session_tasks.require(sub_res["task_id"])
    context = task.metadata.get("business_context") or {}
    assert "ver-accepted-v1" in context.get("asset_version_refs", ()) or "ver-accepted-v1" in task.metadata.get("business_context_intent", {}).get("requested_asset_version_refs", ())


# LLM: 验证 BIZ-001 并发后台订单计算隔离性（A=4200, B=330），确保各自摘要、结果和回执互不干扰。
# 函数用途: 验证并发任务各自冻结独立上下文和结果，杜绝跨任务状态串扰。
def test_biz_001_isolated_concurrent_order_totals_a4200_b330(tmp_path):
    agent, actor = _make_agent_and_actor(tmp_path)
    service = BackgroundAgentService(agent)

    worker_a = service.create({"space_id": None, "name": "order-calc-A"}, actor=actor, idempotency_key="create-ord-a")["agent"]
    worker_b = service.create({"space_id": None, "name": "order-calc-B"}, actor=actor, idempotency_key="create-ord-b")["agent"]

    task_a = agent.conversation_store.session_tasks.create(
        SessionTaskDraft(actor.source_thread_id, worker_a["agent_id"], "Calculate Order A total in cents")
    )
    task_b = agent.conversation_store.session_tasks.create(
        SessionTaskDraft(actor.source_thread_id, worker_b["agent_id"], "Calculate Order B total in cents")
    )

    turn_a = f"turn-{task_a.task_id}"
    turn_b = f"turn-{task_b.task_id}"
    agent.conversation_store.session_tasks.bind_turn(task_a.task_id, turn_id=turn_a)
    agent.conversation_store.session_tasks.bind_turn(task_b.task_id, turn_id=turn_b)

    req_a = BackgroundRunRequest(thread_id=worker_a["agent_id"], task_id=task_a.task_id, reason="session_task")
    exec_a = BackgroundExecutionResult(
        response="Order A total = 4200 cents", runtime_status="ok", tool_call_count=1,
        tool_success_count=1, material_progress_count=1, delivery_artifacts=(),
        message_tool_deliveries=(), operation_verification={}, assistant_commentaries=(), display_snapshot={}
    )
    _close_out_session_task_turn(agent, req_a, exec_a, turn_id=turn_a)

    req_b = BackgroundRunRequest(thread_id=worker_b["agent_id"], task_id=task_b.task_id, reason="session_task")
    exec_b = BackgroundExecutionResult(
        response="Order B total = 330 cents", runtime_status="ok", tool_call_count=1,
        tool_success_count=1, material_progress_count=1, delivery_artifacts=(),
        message_tool_deliveries=(), operation_verification={}, assistant_commentaries=(), display_snapshot={}
    )
    _close_out_session_task_turn(agent, req_b, exec_b, turn_id=turn_b)

    stored_a = agent.conversation_store.session_tasks.require(task_a.task_id)
    stored_b = agent.conversation_store.session_tasks.require(task_b.task_id)

    assert stored_a.summary == "Order A total = 4200 cents"
    assert stored_b.summary == "Order B total = 330 cents"

    view_a = task_result_view(agent.conversation_store, stored_a)
    view_b = task_result_view(agent.conversation_store, stored_b)

    assert "4200" in view_a["summary"] and "330" not in view_a["summary"]
    assert "330" in view_b["summary"] and "4200" not in view_b["summary"]


# LLM: 验证 ROUTE-01..06 准入饱和阻塞报告、命名代理查询、路由持久化和快照绑定。
# 函数用途: 验证 ADMISSION_LIMIT_REACHED 阻塞投影、按名称查询后台代理以及 /route 状态跨轮次持久化。
def test_route_01_through_06_lifecycle_and_entrypoints(tmp_path):
    agent, actor = _make_agent_and_actor(tmp_path)
    service = BackgroundAgentService(agent)

    # 1. ROUTE-02: 准入饱和阻塞报告 ADMISSION_LIMIT_REACHED
    task_dummy = SimpleNamespace(task_id="task-dummy", target_thread_id="thread-dummy", status="queued", failure_code="")
    sched = scheduling_view(
        agent.conversation_store,
        thread_id="thread-dummy",
        tasks=[task_dummy],
        admission_limit_reached=True,
    )
    assert sched["state"] == "queued"
    assert sched["reason_code"] == "ADMISSION_LIMIT_REACHED"

    task_sched = task_scheduling_view(agent.conversation_store, task_dummy, admission_limit_reached=True)
    assert task_sched["state"] == "queued"
    assert task_sched["reason_code"] == "ADMISSION_LIMIT_REACHED"

    # 2. ROUTE-03: 命名后台代理查询与过滤
    agent_info = service.create(
        {"space_id": None, "name": "3d-scene-renderer", "purpose": "render 3d scene"},
        actor=actor,
        idempotency_key="create-named-3d",
    )["agent"]

    # 通过名称过滤列表
    listed = service.list({"name": "3d-scene-renderer"}, actor=actor)
    assert len(listed["items"]) == 1
    assert listed["items"][0]["name"] == "3d-scene-renderer"

    # 未匹配名称返回空列表
    empty_list = service.list({"name": "non-existent-agent"}, actor=actor)
    assert len(empty_list["items"]) == 0

    # 通过名称直接在 _thread 中定位
    found_thread = service._thread("3d-scene-renderer", actor)
    assert found_thread.thread_id == agent_info["agent_id"]

    # 3. ROUTE-04 & ROUTE-06: 路由持久化与跨轮次设置保留
    source_thread = agent.conversation_store.threads.require(actor.source_thread_id)
    res_set = set_route_mode(agent, source_thread, True, actor_id="user-001")
    assert res_set["enabled"] is True

    # 跨轮次读取确认状态持久化
    res_get = route_setting(agent, source_thread, actor_id="user-001")
    assert res_get["enabled"] is True
    assert res_get["source"] == "thread"


# LLM: 验证命名后台代理通过唯一名称寻址执行全部生命周期操作（get / update / suspend / resume / model.get / model.set / retire）。
# 函数用途: 验证按名称寻址时内部正确解析为实际 thread_id，不因使用 name 导致状态机、配额或模型选择错位。
def test_named_background_agent_lifecycle_operations(tmp_path):
    agent, actor = _make_agent_and_actor(tmp_path)
    add_echo_profile(agent)
    service = BackgroundAgentService(agent)

    worker_name = "named-operator-x"
    created = service.create(
        {"space_id": None, "name": worker_name, "purpose": "Original Purpose"},
        actor=actor,
        idempotency_key="create-named-op",
    )["agent"]
    thread_id = created["agent_id"]
    rev = created["revision"]

    # 1. get by unique name
    got = service.get(worker_name, actor=actor)
    assert got["agent"]["thread_id"] == thread_id
    assert got["agent"]["name"] == worker_name

    # 2. update by unique name
    updated = service.update(
        worker_name,
        {"name": worker_name, "purpose": "Updated Purpose"},
        actor=actor,
        expected_revision=rev,
        idempotency_key="update-named-op",
    )
    assert updated["agent"]["purpose"] == "Updated Purpose"
    rev = updated["agent"]["revision"]

    # 3. suspend by unique name
    suspended = service.transition(
        worker_name,
        "suspend",
        actor=actor,
        expected_revision=rev,
        idempotency_key="suspend-named-op",
    )
    assert suspended["agent"]["identity_state"] == "suspended"
    rev = suspended["agent"]["revision"]

    # 4. resume by unique name
    resumed = service.transition(
        worker_name,
        "resume",
        actor=actor,
        expected_revision=rev,
        idempotency_key="resume-named-op",
    )
    assert resumed["agent"]["identity_state"] == "available"
    rev = resumed["agent"]["revision"]

    # 5. get_model by unique name
    model_view = service.get_model(worker_name, actor=actor)
    assert "model_profile_id" in model_view
    assert model_view["effective_at"] in {"current", "next_safe_turn"}

    # 6. set_model by unique name
    new_profile_id = add_echo_profile(agent, default=False)
    set_model_res = service.set_model(
        worker_name,
        new_profile_id,
        actor=actor,
        expected_revision=model_view["selection_revision"],
        idempotency_key="set-model-named-op",
    )
    assert set_model_res["adoption_status"] in {"applied", "unchanged"}
    after_model = service.get_model(worker_name, actor=actor)
    assert after_model["model_profile_id"] == new_profile_id

    # 7. retire by unique name
    retired = service.transition(
        worker_name,
        "retire",
        actor=actor,
        expected_revision=rev,
        idempotency_key="retire-named-op",
        disposition="drain",
    )
    assert retired["agent"]["identity_state"] in {"retiring", "retired"}
