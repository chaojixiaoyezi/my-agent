"""R5-R1：一次最终落账 EIO 只留下原 wake 重投责任，不遗留执行片。"""
import errno
import json
from threading import current_thread
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    background_claim,
    wake_attempt_tracking,
)
from agent_py_agent.agent.conversation.background_delivery import cached_owner_delivery
from agent_py_agent.agent.conversation.background_shutdown import BackgroundShutdown
from agent_py_agent.agent.conversation.wake_poison import WAKE_CLAIM_YIELDED
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_background_owner_delivery_commit import (
    _ScriptedBackend,
    _ScriptedDelivery,
)
from agent_py_agent.tests.test_bounded_call_error_origin import held_call


@pytest.mark.parametrize("channel", ["tui", "feishu"])
@pytest.mark.parametrize("request_timeout", [0, 240])
def test_final_append_eio_recovers_once_without_retained_shutdown_slice(
    tmp_path, monkeypatch, channel, request_timeout,
):
    """只控制模型和运输边界；领取、冻结、退避、重投与停机核收均走原实现。"""
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", enable_tools=False, my_agent_home=str(tmp_path / "home"),
        memory_path="memory.jsonl", orphan_supervision_interval_seconds=0,
        request_timeout=request_timeout,
    ), tmp_path / "workspace")
    content = "原模型结果已生成，只需补齐最终会话记录。"
    agent.backend = backend = _ScriptedBackend(content)
    generate, workers = backend.generate, []

    def controlled_model(*args, **kwargs):
        workers.append(current_thread())
        return generate(*args, **kwargs)

    monkeypatch.setattr(backend, "generate", controlled_model)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    thread = store.threads.get_or_create({
        "canonical_user_id": "owner-r5", "channel": channel,
        "channel_conversation_id": "delivery-r5", "channel_user_id": "owner-r5",
    })
    task_id = "task-r5-delivery"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id,
                      "goal": "保留原结果并补齐会话记录", "status": "active"})
    channels = (_ScriptedDelivery(proactive=True, status="sent") if channel == "feishu"
                else DeliveryService(build_default_channel_registry(agent.config)))
    scheduler = BackgroundMainAgentScheduler({
        "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels),
        "store": store,
    })
    policy_failure = Mock(wraps=scheduler._record_policy_failure)
    monkeypatch.setattr(scheduler, "_record_policy_failure", policy_failure)
    signal = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": task_id,
        "reason": "scheduled_progress_report", "now": 2.0,
    })
    failure = OSError(errno.EIO, "one final canonical append failed")
    append, final_appends, frozen_at_failure = store.messages.append_once, [], []

    def fail_one_final_append(request, **kwargs):
        if request.get("metadata", {}).get("assistant_part_id") == "final":
            final_appends.append(request)
            if len(final_appends) == 1:
                pending = store.wakes.pending_one(signal.wake_signal_id)
                frozen_at_failure.append(cached_owner_delivery(pending))
                raise failure
        return append(request, **kwargs)

    monkeypatch.setattr(store.messages, "append_once", fail_one_final_append)
    verdict, facts = wake_attempt_tracking.verdict_for_attempt, []

    def observe_verdict(value):
        facts.append(value)
        return verdict(value)

    monkeypatch.setattr(wake_attempt_tracking, "verdict_for_attempt", observe_verdict)
    manager = BackgroundShutdown()
    with manager.observe(repo):
        with pytest.raises(OSError) as caught:
            scheduler.tick(now=20.0)
    assert caught.value is failure and caught.value.errno == errno.EIO
    # 真实 worker 的退出必须可观察，不能把逻辑结果当作物理退出证明。
    for worker in workers:
        if worker is not current_thread():
            worker.join(timeout=5)
            assert not worker.is_alive()
    original_claim = store.claims.load(thread.thread_id)
    pending = store.wakes.pending_one(signal.wake_signal_id)
    assert pending is not None and pending.wake_signal_id == signal.wake_signal_id
    frozen = cached_owner_delivery(pending)
    assert frozen == frozen_at_failure[0] and frozen["content"] == content
    assert frozen["canonical_pending"] is True
    assert frozen["external_sent"] is (channel == "feishu")
    assert len(backend.prompts) == len(final_appends) == 1
    assert not [row for row in store.messages.recent(thread.thread_id, limit=0)
                if row.metadata.get("assistant_part_id") == "final"]
    state, error = store.wakes.attempts.state_report(signal.wake_signal_id)
    assert error is None and state.total_count == state.same_cause_count == 1
    assert state.next_attempt_at > 20.0
    assert facts[0].error is failure and facts[0].delivery_frozen
    policy_failure.assert_not_called()

    # 不抹退避、不手工补账；推进到原两份重试时钟中较晚的时点。
    with manager.observe(repo):
        assert scheduler.tick(now=20.0) == []
    assert store.wakes.pending_one(signal.wake_signal_id) == pending
    assert len(backend.prompts) == len(final_appends) == 1
    due = max(state.next_attempt_at, scheduler._wake_retry_after.get(signal.wake_signal_id, 0.0))
    with manager.observe(repo):
        reports = scheduler.tick(now=due)
    assert len(reports) == 1 and reports[0].wake_handled
    assert reports[0].delivery_reason == "cached_owner_delivery_retry"
    assert len(backend.prompts) == 1 and len(final_appends) == 2
    final_rows = [row for row in store.messages.recent(thread.thread_id, limit=0)
                  if row.metadata.get("assistant_part_id") == "final"]
    assert len(final_rows) == 1 and final_rows[0].content == content
    assert store.wakes.pending(limit=0) == []
    if channel == "feishu":
        assert channels.sent == [content], "已外发的缓存只补 canonical，不得重复外发"

    assert manager.blockers() == [], "原交付已恢复，不能遗留原执行 claim 的停机阻塞"
    assert manager.slices == set(), "已完成执行片必须由原观察器正常核收"
    persisted = json.loads(store.storage.background_claim_path(thread.thread_id).read_text())
    assert persisted["claim_id"] == original_claim["claim_id"]
    assert persisted["status"] == persisted["phase"] == "finished"
    assert persisted["last_error"] == {"type": "OSError", "message": str(failure)}
    assert facts[0].claim_status == WAKE_CLAIM_YIELDED
    policy_failure.assert_not_called()
    with manager.observe(repo):
        assert scheduler.tick(now=due + 31.0) == []
    assert len(backend.prompts) == 1 and len(final_appends) == 2
    assert manager.blockers() == [] and manager.slices == set()
    manager.stop()
    assert manager.wait() == []


@pytest.fixture(params=["outcome", "bounded", "context", "tool"])
def unsettled_error(request, monkeypatch):
    if request.param == "bounded":
        # 使用原有界调用实际生成的异常；不伪造 worker 已退出的证明。
        with held_call(monkeypatch, key="r5-pending-delivery") as (error, _call):
            yield error
    else:
        yield {
            "outcome": RequestExecutionOutcomeUncertainError({
                "id": "original-r5-request", "execution_attempt_id": "original-r5-attempt",
                "lease_epoch": 1, "lease_owner": "original-r5-owner",
            }, "r5-pending-delivery"),
            "context": RequestExecutionContextUnavailable("original_context_unavailable"),
            "tool": ToolWriteContextUnavailable("original_tool_context_unavailable"),
        }[request.param]


def test_pending_delivery_does_not_settle_uncertain_execution(tmp_path, unsettled_error):
    """冻结载荷不能将执行不明或未确认 worker 洗成完成，原 claim 继续阻塞。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(owner_id="owner-r5")
    signal = store.wakes.raise_signal({"thread_id": thread.thread_id,
        "root_task_id": "uncertain-task", "reason": "scheduled_progress_report"})
    store.wakes.cache_delivery(signal.wake_signal_id, {
        "schema_version": "wake-owner-delivery.v2", "task_id": signal.root_task_id,
        "reason": signal.reason, "content": "原冻结结果仍不证明执行已结清。",
        "ownership": "local", "canonical_pending": True, "external_sent": False,
    })
    pending = store.wakes.pending_one(signal.wake_signal_id)
    assert cached_owner_delivery(pending) is not None
    policy_failures, pending_checks = [], []

    def fail(_kwargs):
        raise unsettled_error

    def pending_delivery(kwargs):
        pending_checks.append(kwargs)
        return cached_owner_delivery(store.wakes.pending_one(signal.wake_signal_id)) is not None

    dependencies = background_claim.BackgroundClaimDependencies(
        claims=store.claims, claim_scope_id=lambda thread_id, _task_id: thread_id,
        child_owns_task=lambda _task_id: False, terminal_task=lambda _kwargs: False,
        recovery_block=lambda _task_id: None, source_admission=lambda _wake: "",
        retire_source=lambda _kwargs: pytest.fail("uncertain source must remain pending"),
        run_once=fail, runtime_facts=lambda: {}, record_policy_failure=policy_failures.append,
        lease_seconds=30, heartbeat_interval_seconds=1, pending_delivery=pending_delivery,
    )
    manager = BackgroundShutdown()
    with manager.observe(None):
        with pytest.raises(type(unsettled_error)) as caught:
            background_claim.run_claimed(dependencies, {"thread_id": thread.thread_id,
                "task_id": signal.root_task_id, "wake_signal": signal, "reason": signal.reason})
    assert caught.value is unsettled_error
    claim = store.claims.load(thread.thread_id)
    assert claim["status"] == "running" and "finished_at" not in claim
    assert store.wakes.pending_one(signal.wake_signal_id) == pending
    assert not pending_checks and not policy_failures
    assert len(manager.slices) == 1
    assert manager.blockers() == [{"reason": "background_claim_unsettled", "ref": claim["claim_id"]}]
