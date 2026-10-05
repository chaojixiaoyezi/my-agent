# LLM: 用真实 pending/sent 文件和 worker 入口注入崩溃，渠道和进程事实是假对象，不连接网络。
# 模块用途: 钉住外发意图、死亡证明、幂等恢复与旧 epoch 栅栏，不把租约到期当成死亡。
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import delivery
from agent_py_agent.agent.adapter.base import BaseChannelAdapter
from agent_py_agent.agent.adapter.delivery import (
    GatewayClaimLeaseConfig,
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.manager import _gateway_delivery_key
from agent_py_agent.agent.adapter.qq import QQAdapter
from agent_py_agent.agent.gateway_parts import daemon_metadata


class _Crash(BaseException):
    pass


# LLM: 模拟 provider 对稳定键的真实折叠；支持与不支持幂等时分别计数外部可见消息。
# 类用途: 在发送前或接受后崩溃，保留跨 worker 的外部效果供恢复断言。
class _Provider:
    provider_delivery_queryable = False
    provider_idempotency_window_seconds = 0.0

    def __init__(self, store, idempotent=True):
        self.store = store
        self.provider_idempotent_delivery = idempotent
        self.stage = ""
        self.attempts = []
        self.messages = {}
        self.records = []
        self.require_marker = False

    def send(self, record, text):
        self.records.append(record)
        marker = getattr(record, "dispatch", {})
        if self.require_marker:
            assert marker.get("state") == "dispatch-started"
            assert self.store.pending()[0] == record
            assert marker["claim_epoch"] == record.claim_epoch
            assert marker["owner"] == record.claim_owner
        phase = "progress" if text == "进度" else "final"
        key = _gateway_delivery_key(
            message_id=record.message_id, request_id=record.stable_id,
            phase=phase, progress_cursor=record.progress_cursor,
        )
        self.attempts.append(key)
        if self.stage == "before_accept":
            raise _Crash()
        visible_key = key if self.provider_idempotent_delivery else f"{key}:{len(self.attempts)}"
        self.messages.setdefault(visible_key, text)
        if self.stage == "after_accept":
            raise _Crash()
        return True


class _QueryableProvider(_Provider):
    provider_delivery_queryable = True

    def query_delivery(self, key):
        return any(attempt == key for attempt in self.attempts) and bool(self.messages)


@pytest.fixture
def world(monkeypatch):
    state = {"now": 1000.0, "live": True}
    monkeypatch.setattr(delivery, "time", SimpleNamespace(time=lambda: state["now"]))
    monkeypatch.setattr(daemon_metadata, "build_process_identity", lambda: {
        "host_id": "fixture-host", "pid": os.getpid(), "start_time": "fixture-start",
    })
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: state["live"])
    monkeypatch.setattr(daemon_metadata, "process_host_id", lambda: "fixture-host")
    return state


# LLM: 每次构建独立 store/worker 模拟重启，但外部 provider 保留同一份可见效果。
# 函数用途: 使用既有 worker 入口领取、发送及写回执，不手造状态文件。
def _worker(root, provider, owner="first"):
    store = GatewayReplyDeliveryStore(root)
    provider.store = store
    worker = GatewayReplyDeliveryWorker(
        store, poll_response=lambda _record: "最终回复", deliver_response=provider.send,
        lease=GatewayClaimLeaseConfig(owner=owner, ttl_seconds=5),
    )
    return worker


def _enqueue(worker):
    worker.enqueue(PendingGatewayReply("req-resume", "fixture", "user", "message"))


def _crash(worker, provider, stage):
    provider.stage = stage
    with pytest.raises(_Crash):
        worker.run_once()
    provider.stage = ""


def test_dispatch_is_durable_before_provider_io(tmp_path, world):
    provider = _Provider(None)
    provider.require_marker = True
    worker = _worker(tmp_path, provider)
    _enqueue(worker)
    assert worker.run_once() == 1
    assert len(provider.messages) == 1
    receipt = worker.store.terminal_receipt("req-resume")
    assert receipt["disposition"] == "sent"
    assert receipt["message_sequence"] == "final"
    assert receipt["claim_epoch"] == 1


@pytest.mark.parametrize("stage", ["before_accept", "after_accept"])
@pytest.mark.parametrize("live", [True, None])
def test_expired_dispatch_never_takes_over_live_or_unverifiable_owner(tmp_path, world, stage, live):
    provider = _Provider(None)
    first = _worker(tmp_path, provider)
    _enqueue(first)
    _crash(first, provider, stage)
    world.update(now=1010.0, live=live)
    second = _worker(tmp_path, provider, "second")
    assert second.run_once() == 0
    assert len(provider.attempts) == 1
    assert second.store.pending()[0].claim_epoch == 1


@pytest.mark.parametrize("stage", ["before_accept", "after_accept"])
def test_dead_dispatch_replays_same_provider_key_once(tmp_path, world, stage):
    provider = _Provider(None)
    first = _worker(tmp_path, provider)
    _enqueue(first)
    _crash(first, provider, stage)
    marker = getattr(first.store.pending()[0], "dispatch", {})
    assert marker.get("message_sequence") == "final"
    assert marker.get("payload_sha256")
    world.update(now=1010.0, live=False)
    second = _worker(tmp_path, provider, "second")
    assert second.run_once() == 1
    assert len(provider.messages) == 1
    assert provider.attempts[0] == provider.attempts[1]
    assert second.store.terminal_receipt("req-resume")["claim_epoch"] == 2


@pytest.mark.parametrize("stage", ["before_accept", "after_accept"])
def test_dead_non_queryable_dispatch_is_unknown_without_resend(tmp_path, world, stage):
    provider = _Provider(None, idempotent=False)
    first = _worker(tmp_path, provider)
    _enqueue(first)
    _crash(first, provider, stage)
    world.update(now=1010.0, live=False)
    second = _worker(tmp_path, provider, "second")
    assert second.run_once() == 0
    assert len(provider.attempts) == 1
    assert second.store.terminal_receipt("req-resume")["disposition"] == "unknown"
    assert second.run_once() == 0
    assert second.store.pending() == []


def test_receipt_write_failure_keeps_dispatch_for_cold_recovery(tmp_path, world, monkeypatch):
    provider = _Provider(None, idempotent=False)
    first = _worker(tmp_path, provider)
    _enqueue(first)

    def disk_failed(*args, **kwargs):
        raise OSError("fixture receipt disk failure")

    monkeypatch.setattr(first.store, "mark_terminal_claimed", disk_failed)
    assert first.run_once() == 0
    world.update(now=1010.0, live=False)
    second = _worker(tmp_path, provider, "second")
    assert second.run_once() == 0
    assert len(provider.messages) == 1
    assert second.store.terminal_receipt("req-resume")["disposition"] == "unknown"


def test_dispatch_write_failure_has_zero_external_effect_then_sends_once(tmp_path, world, monkeypatch):
    provider = _Provider(None)
    worker = _worker(tmp_path, provider)
    _enqueue(worker)
    write = delivery._atomic_write_json

    def before_dispatch(path, payload):
        if payload.get("dispatch"):
            raise _Crash()
        return write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(delivery, "_atomic_write_json", before_dispatch)
        with pytest.raises(_Crash):
            worker.run_once()
    assert provider.attempts == []
    world.update(now=1010.0, live=False)
    assert _worker(tmp_path, provider, "second").run_once() == 1
    assert len(provider.messages) == 1


def test_old_epoch_receipt_cannot_overwrite_new_unknown(tmp_path, world):
    provider = _Provider(None, idempotent=False)
    first = _worker(tmp_path, provider)
    _enqueue(first)
    _crash(first, provider, "after_accept")
    old_record = provider.records[-1]
    world.update(now=1010.0, live=False)
    second = _worker(tmp_path, provider, "second")
    assert second.run_once() == 0
    before = second.store.terminal_receipt("req-resume")
    assert first._record_sent(old_record) is False
    assert second.store.terminal_receipt("req-resume") == before
    assert before["disposition"] == "unknown"


def test_progress_and_final_have_distinct_durable_message_identities(tmp_path, world):
    provider = _Provider(None)
    provider.require_marker = True
    worker = _worker(tmp_path, provider)
    worker._poll_progress = lambda record: (["进度"] if record.progress_cursor == 0 else [], 3)
    worker._deliver_progress = provider.send
    _enqueue(worker)
    assert worker.run_once() == 1
    assert list(provider.messages.values()) == ["进度", "最终回复"]
    identities = [record.dispatch["message_sequence"] for record in provider.records]
    assert identities == ["progress:0", "final"]
    assert len(set(provider.attempts)) == 2
    assert worker.run_once() == 0


def test_provider_idempotency_window_expiry_becomes_unknown(tmp_path, world):
    provider = _Provider(None)
    provider.provider_idempotency_window_seconds = 3600.0
    first = _worker(tmp_path, provider)
    _enqueue(first)
    _crash(first, provider, "after_accept")
    world.update(now=4601.0, live=False)
    second = _worker(tmp_path, provider, "second")
    assert second.run_once() == 0
    assert len(provider.attempts) == 1
    assert second.store.terminal_receipt("req-resume")["disposition"] == "unknown"


def test_read_only_query_can_confirm_sent_without_replay(tmp_path, world):
    provider = _QueryableProvider(None, idempotent=False)
    first = _worker(tmp_path, provider)
    _enqueue(first)
    _crash(first, provider, "after_accept")
    world.update(now=1010.0, live=False)
    second = _worker(tmp_path, provider, "second")
    assert second.run_once() == 1
    assert len(provider.attempts) == 1
    assert second.store.terminal_receipt("req-resume")["disposition"] == "sent"


def test_permission_error_is_not_owner_death(tmp_path, world, monkeypatch):
    provider = _Provider(None)
    first = _worker(tmp_path, provider)
    _enqueue(first)
    _crash(first, provider, "after_accept")
    world.update(now=1010.0, live=False)

    def denied(_pid, _signal):
        raise PermissionError("fixture process inaccessible")

    monkeypatch.setattr(delivery.os, "kill", denied)
    second = _worker(tmp_path, provider, "second")
    assert second.run_once() == 0
    assert len(provider.attempts) == 1


def test_channel_recovery_capabilities_are_explicit_and_windowed():
    assert getattr(BaseChannelAdapter, "provider_idempotent_delivery", None) is False
    assert getattr(BaseChannelAdapter, "provider_delivery_queryable", None) is False
    assert QQAdapter.provider_idempotent_delivery is False
    assert FeishuAdapter.provider_idempotent_delivery is True
    assert getattr(FeishuAdapter, "provider_delivery_queryable", None) is False
    assert getattr(FeishuAdapter, "provider_idempotency_window_seconds", None) == 3600.0
