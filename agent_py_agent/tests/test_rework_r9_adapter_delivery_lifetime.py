"""R9-R1 P1-R9-19：唯一 adapter 投递线程遇普通队列 OSError 后沿原可中断等待继续推进。

真实 GatewayIngressStore / GatewayReplyDeliveryWorker 与原 claim/epoch CAS；只替换外部回调，并在原私有
写入点模拟一次 IO 失败。推进全部由原后台线程完成，不手动重建 worker 或调用 run_once。
"""
from __future__ import annotations

import threading
import time

import pytest

from agent_py_agent.agent.adapter import ingress
from agent_py_agent.agent.adapter.delivery import (
    GatewayClaimLeaseConfig,
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
)
from agent_py_agent.agent.adapter.ingress import (
    GatewayAdapterDeliveryWorker,
    GatewayIngressAdvance,
    GatewayIngressStore,
    build_gateway_ingress_record,
)


def _record():
    return build_gateway_ingress_record(channel="feishu", user_id="ou-r9", conversation_id="oc-r9",
                                        provider_message_id="om-r9-1", content="hello", metadata={}, timestamp=1.0)


class _Callbacks:
    def __init__(self):
        self.submits = []
        self.handoffs = []

    def prepare(self, record):
        return {"kind": "ask", "prompt": record.content, "metadata": {}}

    def submit(self, record):
        self.submits.append((record.claim_epoch, time.time()))
        return {"request_id": f"req-r9-{len(self.submits)}", "status": "queued"}

    def advance(self, _record):
        return GatewayIngressAdvance("placeholder_ready", "typing")

    def handoff(self, record):
        self.handoffs.append(record.ingress_id)


def _worker(tmp_path, callbacks, *, ttl=None, poll_interval=0.02):
    reply = GatewayReplyDeliveryWorker(GatewayReplyDeliveryStore(tmp_path / "reply"),
                                       poll_response=lambda _pending: None,
                                       deliver_response=lambda _pending, _text: True)
    return GatewayAdapterDeliveryWorker(
        GatewayIngressStore(tmp_path / "delivery"), reply_worker=reply, prepare_payload=callbacks.prepare,
        submit_payload=callbacks.submit, advance_submission=callbacks.advance, handoff_reply=callbacks.handoff,
        poll_interval=poll_interval, lease=None if ttl is None else GatewayClaimLeaseConfig(ttl_seconds=ttl))


def _fail_write_once(monkeypatch, predicate):
    real, failures = ingress._write_private_json, []

    def write(path, payload):
        if not failures and predicate(payload):
            failures.append(dict(payload))
            raise OSError("simulated ingress queue write failure")
        return real(path, payload)

    monkeypatch.setattr(ingress, "_write_private_json", write)
    return failures


def _eventually(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _completed(worker):
    records = worker.store.records()
    return len(records) == 1 and records[0].state == "completed"


def test_claim_publication_failure_keeps_the_sole_poller_progressing(tmp_path, monkeypatch):
    callbacks = _Callbacks()
    worker = _worker(tmp_path, callbacks)
    failures = _fail_write_once(monkeypatch, lambda row: row.get("state") == "prepared" and row.get("claim_owner"))
    worker.enqueue(_record())
    worker.start()
    thread = worker._thread
    try:
        assert _eventually(lambda: _completed(worker)), "sole delivery thread stopped after a claim write failure"
        assert worker._thread is thread and thread.is_alive()
    finally:
        worker.stop()
    assert not thread.is_alive()
    # 失败的 claim 从未发布，外部效果只执行一次；后续由同一线程按原阶段与 epoch 推进到 completed。
    assert len(failures) == 1 and failures[0]["claim_epoch"] == 1
    assert len(callbacks.submits) == 1 and callbacks.handoffs == [_record().ingress_id]
    record, = worker.store.records()
    assert record.claim_owner == "" and record.claim_epoch > failures[0]["claim_epoch"]


def test_post_submit_publication_failure_keeps_original_lease_before_replay(tmp_path, monkeypatch):
    callbacks = _Callbacks()
    worker = _worker(tmp_path, callbacks, ttl=1.0)
    failures = _fail_write_once(monkeypatch, lambda row: row.get("state") == "submitted")
    worker.enqueue(_record())
    worker.start()
    thread = worker._thread
    try:
        assert _eventually(lambda: _completed(worker)), "sole delivery thread stopped after a commit write failure"
        assert worker._thread is thread and thread.is_alive()
    finally:
        worker.stop()
    assert len(failures) == 1
    # 提交结果未知：原租约未过期前不重投；过期后同一线程以更大 epoch 精确重放原阶段。
    (first_epoch, first_at), (replay_epoch, replay_at) = callbacks.submits
    assert replay_epoch > first_epoch and replay_at - first_at >= 0.9
    record, = worker.store.records()
    assert record.claim_owner == "" and record.submission["request_id"] == "req-r9-2"


def test_stop_interrupts_bounded_wait_and_base_exception_still_ends_loop(tmp_path, monkeypatch):
    callbacks = _Callbacks()
    worker = _worker(tmp_path, callbacks, poll_interval=30.0)
    worker.start()
    thread = worker._thread
    started = time.monotonic()
    worker.stop(timeout=5.0)
    assert not thread.is_alive() and time.monotonic() - started < 5.0

    class _Halt(BaseException):
        pass

    halted = _worker(tmp_path / "halt", callbacks)
    calls = []

    def halt():
        calls.append(threading.current_thread().name)
        raise _Halt()

    monkeypatch.setattr(halted, "run_once", halt)
    with pytest.raises(_Halt):
        halted._run()
    assert len(calls) == 1
