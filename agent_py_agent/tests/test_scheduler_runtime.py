from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.channels import (
    DeliveryContext,
    DeliveryReceipt,
    ReplyEnvelope,
)
from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _finish_scheduler_wake_claim,
    _run_params,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.scheduler.service import SchedulerRunClaim
from agent_py_agent.agent.settings import AgentConfig


class _Backend:
    name = "scheduler-test"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(
        self,
        prompt: str,
        on_chunk=None,
        on_thinking_delta=None,
        **_kwargs: object,
    ) -> ModelResponse:
        del on_chunk, on_thinking_delta
        self.prompts.append(prompt)
        return ModelResponse(text="已按计划完成。", backend=self.name)


def _create_job(agent: SimpleAgent, thread_id: str, name: str) -> None:
    agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name=name,
            prompt=f"执行 {name}",
            thread_id=thread_id,
            source_task_id="",
            schedule={
                "kind": "every",
                "every_seconds": 600,
                "anchor_at": 1_000,
                "timezone": "UTC",
            },
            misfire_grace_seconds=60,
            skill_refs=[],
            source_request_id=name,
            now=900,
        )
    )


# LLM: 仅替换外发回执，调度、任务、冻结信封及重投仍走真实实现；不会发送网络请求。
# 类用途: 记录失败和成功的投递尝试，让测试证明恢复没有重新生成回复。
class _RetryDelivery(FakeDeliveryService):
    # LLM: 每个隔离测试独立保存尝试和回执，首次拒收不伪造成功 receipt。
    # 函数用途: 创建可从拒收切换到成功的测试渠道。
    def __init__(self) -> None:
        super().__init__()
        self.status = "rejected"
        self.attempts: list[tuple[DeliveryContext, ReplyEnvelope, DeliveryReceipt]] = []

    # LLM: 部署声明与当前发送状态分离，拒收仍有原外发义务。
    # 函数用途: 声明本夹具的飞书渠道，避免把拒收误作未部署。
    def declares_channel(self, channel: str) -> bool:
        return channel == "feishu"

    # LLM: 假渠道与生产接口同样以结构化声明决定主动投递资格。
    # 函数用途: 为本测试已声明的渠道提供主动发送能力。
    def declared_proactive(self, channel: str) -> bool:
        return self.declares_channel(channel)

    # LLM: 回执完整保留原身份和正文；唯一副作用是进程内记录，没有真实外发。
    # 函数用途: 按测试状态拒收或接收同一冻结信封。
    def deliver(self, context: DeliveryContext, envelope: ReplyEnvelope) -> DeliveryReceipt:
        receipt = DeliveryReceipt(
            channel=context.channel, target=context.target, content=envelope.content,
            thread_id=context.thread_id, task_id=context.task_id,
            delivery_status=self.status, evidence_refs=envelope.evidence_refs,
            receipt_id="scheduled-receipt" if self.status == "sent" else "",
        )
        self.attempts.append((context, envelope, receipt))
        return receipt


# LLM: 仅在 pytest 临时目录通过原 create_job/enqueue 建立定时事实，不替换领取、状态或模型主循环。
# 函数用途: 为定时投递恢复测试准备真实调度入口及无网络模型、渠道替身。
def _scheduled_delivery_case(tmp_path):
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    backend = _Backend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "feishu",
        "channel_conversation_id": "chat-delivery", "channel_user_id": "open-1", "now": 900.0,
    })
    _create_job(agent, thread.thread_id, "delivery")
    agent.scheduler_service.enqueue_ready_runs(now=1_000)
    signal = store.wakes.pending()[0]
    channels = _RetryDelivery()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({
        "runtime": runtime, "store": store, "scheduler_service": agent.scheduler_service,
    })
    return SimpleNamespace(
        agent=agent, backend=backend, store=store, thread=thread, signal=signal,
        channels=channels, scheduler=scheduler,
    )


def test_scheduled_delivery_retry_uses_frozen_reply_without_second_model_turn(tmp_path) -> None:
    case = _scheduled_delivery_case(tmp_path)
    repository = case.agent.scheduler_repository
    task_id = case.signal.root_task_id

    assert case.scheduler.tick(now=1_000) == []
    assert len(case.backend.prompts) == len(case.channels.attempts) == 1
    assert case.store.tasks.load(task_id).status == "completed"
    queued = repository.get_active_run(task_id)
    assert queued["status"] == "queued" and queued["claim_id"] == ""
    pending = case.store.wakes.pending_one(case.signal.wake_signal_id)
    assert pending is not None
    frozen = pending.metadata["owner_delivery"]
    assert frozen["schema_version"] == "wake-owner-delivery.v2"
    assert (frozen["task_id"], frozen["reason"]) == (task_id, "scheduled_job_due")
    assert frozen["content"] == "已按计划完成。" and frozen["external_sent"] is False
    assert repository.history()[0] == []

    case.channels.status = "sent"
    reports = case.scheduler.tick(now=1_031)

    assert len(case.backend.prompts) == 1
    assert len(case.channels.attempts) == 2, "已完成业务仍欠原外发，只重投冻结信封"
    first, second = case.channels.attempts
    assert first[1] == second[1] and second[1].content == frozen["content"]
    assert (first[0].request_id, first[0].idempotency_key) == (
        second[0].request_id, second[0].idempotency_key,
    )
    assert (first[2].receipt_id, second[2].receipt_id) == ("", "scheduled-receipt")
    assert len(reports) == 1 and reports[0].wake_handled is True
    assert reports[0].task_status == "completed" and reports[0].delivery_status == "sent"
    assert reports[0].delivery_reason == "cached_owner_delivery_retry"
    assert case.store.tasks.load(task_id).status == "completed"
    assert case.store.wakes.pending_one(case.signal.wake_signal_id) is None
    assert repository.get_active_run(task_id) is None
    history, errors = repository.history()
    assert errors == [] and len(history) == 1
    assert history[0]["run_id"] == task_id and history[0]["status"] == "done"
    assert history[0]["response"] == frozen["content"]
    assert history[0]["delivery_status"] == "sent"
    assert history[0]["delivery_reason"] == "cached_owner_delivery_retry"
    assert history[0]["claim_id"] == ""
    final_rows = [row for row in case.store.messages.recent(case.thread.thread_id, limit=0) if row.content]
    assert len(final_rows) == 1 and final_rows[0].content == frozen["content"]
    assert final_rows[0].metadata["conversation_request_id"] == frozen["message_metadata"]["conversation_request_id"]

    assert case.scheduler.tick(now=1_062) == []
    assert len(case.backend.prompts) == 1 and len(case.channels.attempts) == 2
    assert repository.history()[0] == history


@pytest.mark.parametrize(("task_status", "expected", "invalid_frozen"), [
    ("completed", "done", ""), ("cancelled", "cancelled", ""), ("failed", "failed", ""),
    ("completed", "done", "schema"), ("completed", "done", "task_id"),
])
def test_scheduled_terminal_tick_without_valid_frozen_only_closes_history(
    tmp_path, task_status, expected, invalid_frozen,
) -> None:
    case = _scheduled_delivery_case(tmp_path)
    task_id = case.signal.root_task_id
    case.store.tasks.bind({"thread_id": case.thread.thread_id, "task_id": task_id, "goal": "原执行"})
    case.store.tasks.update_status({"task_id": task_id, "status": task_status, "now": 999.0})
    if invalid_frozen:
        case.store.wakes.cache_delivery(case.signal.wake_signal_id, {
            "schema_version": "invalid" if invalid_frozen == "schema" else "wake-owner-delivery.v2",
            "task_id": "other-task" if invalid_frozen == "task_id" else task_id,
            "reason": "scheduled_job_due", "content": "不得投递的旧载荷",
        })

    assert case.scheduler.tick(now=1_000) == []

    assert case.backend.prompts == [] and case.channels.attempts == []
    assert case.store.tasks.load(task_id).status == task_status
    assert case.agent.scheduler_repository.get_active_run(task_id) is None
    history, errors = case.agent.scheduler_repository.history()
    assert errors == [] and len(history) == 1
    assert history[0]["status"] == expected and history[0]["claim_id"] == ""
    assert history[0]["delivery_status"] == history[0]["response"] == ""
    assert case.store.wakes.pending_one(case.signal.wake_signal_id) is None
    assert case.scheduler.tick(now=1_031) == []
    assert case.backend.prompts == [] and case.channels.attempts == []


@pytest.mark.parametrize(("task_status", "expected"), [
    ("completed", "done"), ("cancelled", "cancelled"), ("failed", "failed"),
    ("", ""), ("unknown", ""),
])
def test_scheduler_finish_preserves_report_terminal_mapping(tmp_path, task_status, expected) -> None:
    case = _scheduled_delivery_case(tmp_path)
    service = case.agent.scheduler_service
    claim = service.claim_wake(case.signal, now=1_001).claim
    assert claim is not None
    # 空/未知报告不是新任务状态；原任务保持 completed，不能按缺失报告事实冒领完成。
    case.store.tasks.update_status({
        "task_id": claim.run_id, "status": task_status if expected else "completed", "now": 1_002,
    })
    report = BackgroundMainAgentReport(
        thread_id=case.thread.thread_id, task_id=claim.run_id, reason="scheduled_job_due",
        response="原冻结答复", route_channel="feishu", route_target="chat-delivery",
        created_at=1_003, delivery_status="suppressed", delivery_reason="cached_owner_delivery_retry",
        wake_handled=True, task_status=task_status,
    )

    returned = _finish_scheduler_wake_claim(
        case.scheduler, case.signal, report, claim=claim, now=1_003,
    )

    history, errors = case.agent.scheduler_repository.history()
    assert errors == []
    if expected:
        assert returned is report and len(history) == 1
        assert history[0]["status"] == expected and history[0]["claim_id"] == ""
        assert history[0]["delivery_status"] == "suppressed"
        assert case.agent.scheduler_repository.get_active_run(claim.run_id) is None
    else:
        assert returned is None and history == []
        queued = case.agent.scheduler_repository.get_active_run(claim.run_id)
        assert queued["status"] == "queued" and queued["claim_id"] == ""
        assert case.store.wakes.pending_one(case.signal.wake_signal_id) is not None
    assert case.backend.prompts == [] and case.channels.attempts == []


def test_durable_schedule_run_uses_stable_run_identity_and_full_owner_tools() -> None:
    wake = {
        "wake_signal_id": "wake-1",
        "metadata": {
            "scheduler_job_id": "job-1",
            "scheduler_run_id": "srun-1",
            "scheduler_prompt": "执行日报",
            "scheduler_skill_refs": [],
        },
    }
    request = BackgroundRunRequest(
        thread_id="thread-1",
        reason="scheduled_job_due",
        wake_signal=wake,
    )
    params = _run_params(request.thread_id, request)
    assert params.request_id == "srun-1"
    assert params.run_id == "srun-1"
    assert params.task_id == "srun-1"
    assert params.allowed_tools is None
    assert params.task_attributes == {
        "conversation_thread_id": "thread-1",
        "background_wake_signal_id": "wake-1",
        "scheduler_job_id": "job-1",
        "scheduler_run_id": "srun-1",
        "scheduler_trigger": "",
    }


def test_scheduler_wake_uses_run_id_as_durable_root_task(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-root-id",
            "channel_user_id": "user-1",
            "now": 900.0,
        }
    )
    _create_job(agent, thread.thread_id, "root-id")

    wake_ids = agent.scheduler_service.enqueue_ready_runs(now=1_000)

    assert len(wake_ids) == 1
    signals = store.wakes.pending()
    assert len(signals) == 1
    signal = signals[0]
    run_id = str(signal.metadata["scheduler_run_id"])
    assert signal.root_task_id == run_id
    assert run_id.startswith("srun_")


def test_two_due_jobs_in_one_thread_both_execute_and_close_history(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _Backend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 900.0,
        }
    )
    _create_job(agent, thread.thread_id, "first")
    _create_job(agent, thread.thread_id, "second")
    prepared_tasks = []
    snapshot_for_scope = agent.skill_snapshot_for_run_scope

    # LLM: 包裹同步快照入口后仍调用原实现，不替换权限裁决；backend worker 不拥有主线程的 RunParams。
    # 函数用途: 保证定时服务已在快照及模型之前完成准确绑定，两个任务不会共享同线程的旧链接。
    def observe_binding(workspace):
        params = agent._current_run_params
        link = store.tasks.load(params.task_id)
        assert link is not None and link.thread_id == thread.thread_id
        assert params.task_attributes["conversation_task_id"] == link.task_id
        assert link.status == "active"
        prepared_tasks.append(link.task_id)
        return snapshot_for_scope(workspace)

    monkeypatch.setattr(agent, "skill_snapshot_for_run_scope", observe_binding)
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": runtime,
            "store": store,
            "scheduler_service": agent.scheduler_service,
        }
    )

    reports = scheduler.tick(now=1_000)

    assert len(reports) == 2
    assert len(backend.prompts) == 2
    assert len(set(prepared_tasks)) == 2
    assert any("执行 first" in prompt for prompt in backend.prompts)
    assert any("执行 second" in prompt for prompt in backend.prompts)
    assert store.wakes.pending() == []
    history, errors = agent.scheduler_repository.history(limit=10)
    assert errors == []
    assert len(history) == 2
    assert {row["status"] for row in history} == {"done"}
    assert len(channels.adapter("internal").sent_messages) == 2


def test_scheduled_run_waits_for_same_task_terminal_before_history_close(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-waiting",
            "channel_user_id": "user-1",
            "now": 900.0,
        }
    )
    _create_job(agent, thread.thread_id, "child-backed")
    run = agent.scheduler_repository.reserve_due_runs(now=1_000)[0]
    claimed = agent.scheduler_repository.claim_run(
        str(run["run_id"]),
        lease_seconds=300,
        now=1_001,
    )
    assert claimed is not None
    claim = SchedulerRunClaim(
        run_id=str(run["run_id"]),
        claim_id=str(claimed["claim_id"]),
        run=claimed,
    )
    agent.scheduler_repository.mark_run_running(
        claim.run_id,
        claim.claim_id,
        now=1_002,
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": claim.run_id,
            "goal": "等待四个子代理并整合报告",
            "now": 1_002,
        }
    )

    report = BackgroundMainAgentReport(
        thread_id=thread.thread_id,
        task_id=claim.run_id,
        reason="scheduled_job_due",
        response="子代理仍在运行",
        route_channel="internal",
        route_target="",
        created_at=1_003,
        delivery_status="suppressed",
        delivery_reason="nonterminal_children",
        wake_handled=True,
        task_status="active",
    )
    returned = _finish_scheduler_wake_claim(
        SimpleNamespace(
            scheduler_service=agent.scheduler_service,
            _wake_retry_after={},
        ),
        SimpleNamespace(wake_signal_id="wake-waiting"),
        report,
        claim=claim,
        now=1_003,
    )

    assert returned is report
    waiting = agent.scheduler_repository.get_active_run(claim.run_id)
    assert waiting is not None
    assert waiting["status"] == "waiting"
    assert waiting["claim_id"] == ""
    history, errors = agent.scheduler_repository.history(limit=10)
    assert errors == []
    assert history == []
    active, errors = agent.scheduler_repository.active_runs_by_job()
    assert errors == []
    assert next(iter(active.values()))["status"] == "waiting"
    assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=1_004) is None

    store.tasks.update_status(
        {"task_id": claim.run_id, "status": "completed", "now": 1_005}
    )
    # Gateway 启动/轮询时走批量对账；这条路径保证一次生命周期通知丢失后，
    # 重启仍会从同一个 task link 收敛，而不是让 waiting 永久悬挂。
    settled = agent.scheduler_service.reconcile_waiting_runs(now=1_006)

    assert settled == [claim.run_id]
    assert agent.scheduler_repository.get_active_run(claim.run_id) is None
    history, errors = agent.scheduler_repository.history(limit=10)
    assert errors == []
    assert [row["status"] for row in history] == ["done"]


def test_scheduled_message_tool_delivery_is_mirrored_once_without_fallback_send(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "ou-user-1",
            "now": 900.0,
        }
    )
    delivery = {
        "schema_version": "message_tool_delivery.v1",
        "delivery_status": "sent",
        "source_owner_delivery": True,
        "channel": "feishu",
        "content": "提醒到啦：检查索引备份。",
        "receipt_id": "receipt-one",
        "deduplicated": False,
        "attachments": [],
    }

    def run(*_args, **_kwargs):
        return SimpleNamespace(
            response="模型最终又说了一遍，但这段不应再次外发。",
            archive_tool_calls=[{"tool": "send_message", "ok": True}],
            delivery_artifacts=[],
            message_tool_deliveries=[delivery],
        )

    agent.run = run
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    request = {
        "thread_id": thread.thread_id,
        "reason": "scheduled_job_due",
        "wake_signal": {
            "metadata": {
                "scheduler_job_id": "job-1",
                "scheduler_run_id": "srun-1",
                "scheduler_prompt": "提醒我检查索引备份",
            }
        },
        "now": 1_000.0,
    }

    first = runtime.run_once(request)
    second = runtime.run_once(request)

    assert first.delivery_status == second.delivery_status == "sent"
    assert first.delivery_reason == second.delivery_reason == "scheduled_message_tool_delivery"
    assert first.response == second.response == "提醒到啦：检查索引备份。"
    assert channels.adapter("feishu").sent_messages == []
    messages = store.messages.recent(thread.thread_id)
    assert [row.content for row in messages] == ["提醒到啦：检查索引备份。"]
    assert messages[0].metadata["message_tool_delivery"]["receipt_id"] == "receipt-one"
