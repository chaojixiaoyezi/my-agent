"""从真实后台提交入口核对外发索引、原会话同步与重投。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_delivery import (
    BackgroundDeliveryDependencies,
    FrozenOwnerDelivery,
    frozen_owner_delivery,
    record_background_response,
)
from agent_py_agent.agent.conversation.channels import DeliveryContext
from agent_py_agent.agent.conversation.execution_threads import ensure_execution_thread
from agent_py_agent.agent.conversation.models import ChannelBinding, ConversationThread
from agent_py_agent.agent.conversation.outbound_message_index import OutboundMessageIndex
from agent_py_agent.agent.conversation.store import ConversationStore


class _Transport:
    def __init__(self, status="sent", *, components=()):
        self.status, self.components, self.calls = status, components, []

    def declares_channel(self, channel):
        return channel == "feishu"

    def declared_proactive(self, channel):
        return channel == "feishu"

    def deliver(self, context, envelope):
        self.calls.append((context, envelope))
        return SimpleNamespace(content=envelope.content, receipt_id="transport-receipt",
            channel=context.channel, target=context.target, evidence_refs=envelope.evidence_refs,
            delivery_status=self.status, component_receipts=self.components)


def _setup(tmp_path, *, status="sent", components=()):
    store = ConversationStore(tmp_path / "conversations")
    source = ConversationThread(thread_id="thread-origin", canonical_user_id="owner-a",
        owner_id="owner-a", owner_home=str(tmp_path),
        channel_bindings=(ChannelBinding(channel="feishu", channel_conversation_id="oc_main",
                                        channel_user_id="ou_main", canonical_user_id="owner-a", thread_id="thread-origin"),))
    store.threads.write(source)
    agent = SimpleNamespace(conversation_store=store,
        home_paths=SimpleNamespace(owner_id="owner-a", owner_home_dir=tmp_path, owner_kind="personal"))
    execution = ensure_execution_thread(agent, source.thread_id, "operation-a", kind="scheduled")
    wake = store.wakes.raise_signal({"thread_id": execution.thread_id, "reason": "scheduled_progress_report",
        "root_task_id": "task-a", "evidence_refs": ["artifact://report-a"],
        "metadata": {"workflow_run_id": "workflow-run-a"}})
    request = SimpleNamespace(thread_id=execution.thread_id, task_id="task-a", reason=wake.reason,
        conversation_turn_id="turn-a", wake_signal=wake.to_dict())
    transport = _Transport(status, components=components)
    deps = BackgroundDeliveryDependencies(agent, store, transport, lambda request: "active")
    context = DeliveryContext(channel="feishu", target="ou_main", conversation_id="oc_main",
        thread_id=execution.thread_id, task_id=request.task_id, idempotency_key="delivery-a")
    return deps, request, context, source


def _send(deps, request, context, **options):
    return record_background_response(deps, request, context, "主动报告：第 2 条已完成。", deliver=True,
                                      delivery_reason="non_subagent_completion", **options)


def test_success_binds_canonical_message_and_syncs_origin_once(tmp_path):
    deps, request, context, origin = _setup(tmp_path)
    first = _send(deps, request, context)
    row, errors = deps.store.messages.by_id_report(context.thread_id, first.message_id)
    assert errors == []
    assert row.channel_message_id == "transport-receipt"
    assert row.metadata["workflow_run_id"] == "workflow-run-a"
    indexed = OutboundMessageIndex(tmp_path).lookup("transport-receipt", channel="feishu", chat_id="oc_main")
    assert indexed["conversation_message_id"] == first.message_id
    assert indexed["workflow_run_id"] == "workflow-run-a"
    notices = deps.store.messages.recent(origin.thread_id, limit=10)
    assert len(notices) == 1
    assert "第 2 条已完成" in notices[0].content
    assert notices[0].metadata["origin_notice"]["conversation_message_id"] == first.message_id
    assert notices[0].metadata["origin_notice"]["evidence_refs"] == ["artifact://report-a"]
    _send(deps, request, context, frozen_retry=FrozenOwnerDelivery(content=first.content,
        external_sent=True, receipt_id="transport-receipt"))
    assert len(deps.channels.calls) == 1
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1


def test_component_provider_ids_not_internal_receipt_hash_are_indexed(tmp_path):
    components = ({"phase": "send_confirmed", "effect_outcome": "confirmed", "provider_message_id": "om_provider_a"},
                  {"phase": "send_confirmed", "effect_outcome": "confirmed", "provider_message_id": "om_provider_b"},
                  {"phase": "unknown", "effect_outcome": "unknown", "provider_message_id": "om_uncertain"})
    deps, request, context, _ = _setup(tmp_path, components=components)
    commit = _send(deps, request, context)
    row, _ = deps.store.messages.by_id_report(context.thread_id, commit.message_id)
    assert row.channel_message_id == "om_provider_a"
    assert row.metadata["channel_message_ids"] == ["om_provider_a", "om_provider_b"]
    index = OutboundMessageIndex(tmp_path)
    assert index.lookup("om_provider_b", channel="feishu", chat_id="oc_main")["receipt_id"] == "transport-receipt"
    assert index.lookup("om_uncertain", channel="feishu", chat_id="oc_main") is None
    assert index.lookup("transport-receipt", channel="feishu", chat_id="oc_main") is None


@pytest.mark.parametrize("status", ["failed", "unknown", "rejected", "suppressed"])
def test_unsuccessful_send_has_no_index_or_delivered_notice(tmp_path, status):
    deps, request, context, origin = _setup(tmp_path, status=status)
    _send(deps, request, context)
    assert OutboundMessageIndex(tmp_path).lookup("transport-receipt", channel="feishu", chat_id="oc_main") is None
    assert deps.store.messages.recent(origin.thread_id, limit=10) == []


def test_index_failure_after_send_uses_original_frozen_receipt_for_repair(tmp_path, monkeypatch):
    deps, request, context, origin = _setup(tmp_path)
    original = OutboundMessageIndex.record
    def fail(*args, **kwargs):
        raise OSError("simulated local write error")
    monkeypatch.setattr(OutboundMessageIndex, "record", fail)
    with pytest.raises(OSError):
        _send(deps, request, context)
    pending = deps.store.wakes.pending_one(request.wake_signal["wake_signal_id"])
    frozen = pending.metadata["owner_delivery"]
    assert frozen["external_sent"] is True
    monkeypatch.setattr(OutboundMessageIndex, "record", original)
    commit = _send(deps, request, context, frozen_retry=frozen_owner_delivery(frozen))
    assert commit.persisted
    assert len(deps.channels.calls) == 1
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1


def test_same_origin_full_message_is_not_duplicated_by_summary(tmp_path):
    deps, request, context, origin = _setup(tmp_path)
    context = replace(context, thread_id=origin.thread_id)
    commit = _send(deps, request, context)
    rows = deps.store.messages.recent(origin.thread_id, limit=10)
    assert [row.message_id for row in rows] == [commit.message_id]


def test_target_mode_notice_uses_original_thread_not_newest_binding(tmp_path):
    deps, request, context, origin = _setup(tmp_path)
    changed = ConversationThread(thread_id="thread-new-chat", canonical_user_id="owner-a",
        owner_id="owner-a", owner_home=str(tmp_path),
        channel_bindings=(ChannelBinding(channel="feishu", channel_conversation_id="oc_other", channel_user_id="ou_other", canonical_user_id="owner-a", thread_id="thread-new-chat"),))
    deps.store.threads.write(changed)
    _send(deps, request, replace(context, target="oc_explicit", conversation_id="oc_explicit"))
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1
    assert deps.store.messages.recent(changed.thread_id, limit=10) == []
    assert OutboundMessageIndex(tmp_path).lookup("transport-receipt", channel="feishu", chat_id="oc_explicit") is not None


def test_tool_mirror_indexes_every_confirmed_provider_message_without_resending(tmp_path):
    from agent_py_agent.agent.conversation.background_delivery import mirror_message_tool_deliveries

    deps, request, context, origin = _setup(tmp_path)
    deliveries = tuple({'content': f'正文 {n}', 'receipt_id': f'local-receipt-{n}',
        'component_receipts': [{'phase': 'send_confirmed', 'effect_outcome': 'confirmed',
                                'provider_message_id': f'om_tool_{n}'}]} for n in range(2))
    commit = mirror_message_tool_deliveries(deps, request, context, deliveries,
                                           delivery_reason='scheduled_message_tool_delivery')
    assert deps.channels.calls == []
    index = OutboundMessageIndex(tmp_path)
    for n in range(2):
        entry = index.lookup(f'om_tool_{n}', channel='feishu', chat_id='oc_main')
        assert entry is not None and entry['conversation_message_id'] == commit.message_id
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1


def test_origin_notice_failure_repairs_after_restart_without_external_send(tmp_path, monkeypatch):
    deps, request, context, origin = _setup(tmp_path)
    append = deps.store.messages.append_once

    def fail_notice(payload, **kwargs):
        if payload['thread_id'] == origin.thread_id:
            raise OSError('simulated origin notice failure')
        return append(payload, **kwargs)

    monkeypatch.setattr(deps.store.messages, 'append_once', fail_notice)
    with pytest.raises(OSError, match='origin notice'):
        _send(deps, request, context)
    pending = deps.store.wakes.pending_one(request.wake_signal['wake_signal_id'])
    frozen = frozen_owner_delivery(pending.metadata['owner_delivery'])
    assert frozen.external_sent is True
    # 重启式重新打开所有本地Store，而不是复用旧Store的缓存。
    reopened = ConversationStore(deps.store.storage.root)
    deps = replace(deps, store=reopened)
    deps.agent.conversation_store = reopened
    commit = _send(deps, request, context, frozen_retry=frozen)
    assert commit.persisted and len(deps.channels.calls) == 1
    assert len(reopened.messages.recent(origin.thread_id, limit=10)) == 1


# LLM: 运输替身只确认一次外发；canonical与索引、原会话均使用真实临时Store，不声称飞书公网可达。
# 函数用途: 验证没有chat ID的actor目标不能在已发送后因群地址解析失败。
@pytest.mark.parametrize("target", ["ou_main", "user@example.invalid"])
def test_actor_target_without_conversation_id_commits_once(tmp_path, target):
    deps, request, context, origin = _setup(tmp_path)
    context = replace(context, target=target, conversation_id="")
    commit = _send(deps, request, context)
    assert commit.persisted and len(deps.channels.calls) == 1
    row, errors = deps.store.messages.by_id_report(context.thread_id, commit.message_id)
    assert errors == [] and row.metadata["channel_message_ids"] == ["transport-receipt"]
    indexed = OutboundMessageIndex(tmp_path).lookup("transport-receipt", channel="feishu", chat_id=target)
    assert indexed is not None and indexed["conversation_message_id"] == commit.message_id
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1
    _send(deps, request, context, frozen_retry=FrozenOwnerDelivery(
        content=commit.content, external_sent=True, receipt_id="transport-receipt"))
    assert len(deps.channels.calls) == 1
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1


# LLM: 证据列表来自冻结信封，canonical必须完整保存；索引预算不足不得变成重发或丢失原会话通知。
# 函数用途: 反证超过32条证据时确定性投影错误不破坏已完成交付。
def test_oversized_evidence_projection_does_not_fail_committed_delivery(tmp_path, caplog):
    deps, request, context, origin = _setup(tmp_path)
    refs = tuple(f"artifact://report-{number}" for number in range(33))
    commit = _send(deps, request, context, frozen_retry=FrozenOwnerDelivery(content="主动报告", evidence_refs=refs))
    assert commit.persisted and len(deps.channels.calls) == 1
    row, errors = deps.store.messages.by_id_report(context.thread_id, commit.message_id)
    assert errors == [] and row.metadata["evidence_refs"] == list(refs)
    assert OutboundMessageIndex(tmp_path).lookup("transport-receipt", channel="feishu", chat_id="oc_main") is None
    notices = deps.store.messages.recent(origin.thread_id, limit=10)
    assert len(notices) == 1 and notices[0].metadata["origin_notice"]["evidence_refs"] == list(refs)
    assert any("canonical" in record.getMessage() for record in caplog.records)
    _send(deps, request, context, frozen_retry=FrozenOwnerDelivery(
        content=commit.content, external_sent=True, receipt_id="transport-receipt", evidence_refs=refs))
    assert len(deps.channels.calls) == 1
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1


# LLM: 回执索引拒绝重绑仍有效，投递层只能隔离确定性投影错误，不能覆盖旧绑定或重发新正文。
# 函数用途: 验证冲突索引保留原绑定，而canonical与原会话通知仍正常完成。
def test_receipt_rebinding_projection_does_not_fail_committed_delivery(tmp_path, caplog):
    deps, request, context, origin = _setup(tmp_path)
    index = OutboundMessageIndex(tmp_path)
    existing = {"schema_version": "outbound_message.v1", "channel": "feishu", "chat_id": "oc_main",
        "channel_message_id": "transport-receipt", "thread_id": "thread-existing",
        "conversation_message_id": "message-existing", "task_id": None, "workflow_run_id": None,
        "evidence_refs": [], "receipt_id": "receipt-existing", "sent_at": datetime.now(timezone.utc).isoformat()}
    index.record(existing)
    commit = _send(deps, request, context)
    assert commit.persisted and len(deps.channels.calls) == 1
    row, errors = deps.store.messages.by_id_report(context.thread_id, commit.message_id)
    assert errors == [] and row.channel_message_id == "transport-receipt"
    assert index.lookup("transport-receipt", channel="feishu", chat_id="oc_main") == existing
    notices = deps.store.messages.recent(origin.thread_id, limit=10)
    assert len(notices) == 1 and notices[0].metadata["origin_notice"]["conversation_message_id"] == commit.message_id
    assert any("canonical" in record.getMessage() for record in caplog.records)
    _send(deps, request, context, frozen_retry=FrozenOwnerDelivery(
        content=commit.content, external_sent=True, receipt_id="transport-receipt"))
    assert len(deps.channels.calls) == 1
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1


# LLM: 批次内一条回执的确定性校验错误不能遮住另一条成功回执；只替换单条索引入口，真实存储仍写其它记录。
# 函数用途: 验证确定性异常按回执隔离且不吞掉后续有效组件。
@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_deterministic_projection_error_keeps_other_component_ids(tmp_path, monkeypatch, caplog, error_type):
    components = tuple({"phase": "send_confirmed", "effect_outcome": "confirmed", "provider_message_id": value}
                       for value in ("om_conflict", "om_valid"))
    deps, request, context, origin = _setup(tmp_path, components=components)
    record = OutboundMessageIndex.record

    # LLM: 只注入一个组件的确定性错误；其它组件继续进入原索引实现，不使用宽泛参数转发。
    # 函数用途: 模拟局部投影拒绝，不改变运输或canonical写入。
    def reject_one(self, entry, *, now=None):
        if entry["channel_message_id"] == "om_conflict":
            raise error_type("deterministic projection rejection")
        return record(self, entry, now=now)

    monkeypatch.setattr(OutboundMessageIndex, "record", reject_one)
    commit = _send(deps, request, context)
    assert commit.persisted and len(deps.channels.calls) == 1
    indexed = OutboundMessageIndex(tmp_path).lookup("om_valid", channel="feishu", chat_id="oc_main")
    assert indexed is not None and indexed["conversation_message_id"] == commit.message_id
    assert len(deps.store.messages.recent(origin.thread_id, limit=10)) == 1
    assert any("canonical" in entry.getMessage() for entry in caplog.records)
