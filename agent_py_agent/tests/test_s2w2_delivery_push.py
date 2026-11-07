"""S2-W2: wake the existing durable delivery worker; no second sender or terminal authority."""
from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.manager import _GatewayReplyPollClient
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import archive_request
from agent_py_agent.agent.gateway_parts.request_event_hub import RequestEventHub, bind_hub


# LLM: Use the actual store and worker, with a recorded mock adapter acknowledgement rather than a real external message.
# 函数用途: 构造保留认领/租约/已发账本的真实投递线程。
def worker_for(tmp_path, hub, poll, send):
    return GatewayReplyDeliveryWorker(GatewayReplyDeliveryStore(tmp_path / "delivery"),
        poll_response=poll, deliver_response=send, poll_interval=10, event_hub=hub)


# LLM: A hint cannot replace the durable read; a commit between read and wait must not be lost even with a ten-second fallback.
# 函数用途: 故意在本轮读后、等待前提交提示，验证200ms内唤醒并且最多发送一次。
def test_publish_between_read_and_wait_and_no_fake_terminal(tmp_path):
    hub = RequestEventHub(ring_events=8, topic_capacity=16, subscribers_per_topic=8, idle_seconds=1)
    checked, committed, sent = threading.Event(), threading.Event(), threading.Event()
    messages = []
    def poll(record):
        value = "durable result" if committed.is_set() else None
        checked.set()
        return value
    def send(record, response):
        messages.append(response)
        sent.set()
        return True
    worker = worker_for(tmp_path, hub, poll, send)
    worker.enqueue(PendingGatewayReply("r", "chat", "alice", "m", conversation_id="c"))
    worker.start()
    try:
        assert checked.wait(2)
        hub.publish(("request", "r"), "terminal", "r", {})
        assert not sent.wait(0.04), "notification alone was mistaken for a canonical terminal"
        start = time.monotonic()
        committed.set()
        hub.publish(("request", "r"), "terminal", "r", {})
        assert sent.wait(0.2)
        assert time.monotonic() - start < 0.2
        for _ in range(30):
            hub.publish(("request", "r"), "terminal", "r", {})
        time.sleep(0.03)
        assert messages == ["durable result"]
    finally:
        worker.stop(2)
    assert not worker._thread.is_alive()
    assert not worker._hub_subscriptions


# LLM: Closing transport observation must not cancel the durable request or need another thread per subscription.
# 函数用途: 验证原worker停止立即唤醒，未完成的持久监听仍可恢复。
def test_worker_stop_preserves_pending_record(tmp_path):
    hub = RequestEventHub(ring_events=8, topic_capacity=4, subscribers_per_topic=2, idle_seconds=1)
    checked = threading.Event()
    def poll(record):
        checked.set()
        return None
    worker = worker_for(tmp_path, hub, poll, lambda *_: True)
    worker.enqueue(PendingGatewayReply("r", "chat", "alice", "m"))
    worker.start()
    assert checked.wait(2)
    start = time.monotonic()
    worker.stop(1)
    assert not worker._thread.is_alive()
    assert time.monotonic() - start < 0.2
    assert [record.request_id for record in worker.store.pending()] == ["r"]


# LLM: A matching same-process service must use original progress/result projections, without a hidden urllib fallback.
# 函数用途: 从真实队列/终态文件对账并投递，用原归档提交自动唤醒，证明没有另造结果来源。
def test_in_process_projection_and_real_terminal_wakeup(tmp_path, monkeypatch):
    import agent_py_agent.agent.adapter.manager as manager
    import agent_py_agent.agent.gateway_parts.http_service as http_service
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses, paths.terminal):
        folder.mkdir(parents=True, exist_ok=True)
    record = paths.processing / "r.json"
    record.write_text(json.dumps({"id": "r", "execution_attempt_id": "attempt-a", "user_id": "alice", "channel": "chat"}))
    hub = RequestEventHub(ring_events=8, topic_capacity=16, subscribers_per_topic=8, idle_seconds=1)
    bind_hub(paths, hub)
    server = SimpleNamespace(server=SimpleNamespace(server_address=("127.0.0.1", 31415)), paths=paths,
        request_event_hub=hub, auth_middleware=None, agent=None, control_plane=None)
    monkeypatch.setattr(http_service, "_server_instance", server)
    http_calls = []
    def forbidden_http(*args, **kwargs):
        http_calls.append(args)
        raise AssertionError("in-process delivery used urllib")
    monkeypatch.setattr(manager, "open_authenticated_request", forbidden_http)
    client = _GatewayReplyPollClient(31415)
    client.credentials = SimpleNamespace(headers=lambda: {})
    assert client.in_process_hub() is hub
    pending = PendingGatewayReply("r", "chat", "alice", "m", conversation_id="c")
    assert client.poll_response(pending, 0) is None
    checked, sent = threading.Event(), threading.Event()
    messages = []
    def poll(record):
        value = client.poll_response(record, 0)
        checked.set()
        return value
    def send(record, response):
        messages.append(response)
        sent.set()
        return True
    worker = worker_for(tmp_path, client.in_process_hub, poll, send)
    worker.enqueue(pending)
    worker.start()
    try:
        assert checked.wait(2)
        start = time.monotonic()
        result = archive_request(paths, record, paths.done, "r", terminal_response={"ok": True, "response": "canonical result",
            "id": "r", "request_id": "r", "user_id": "alice", "channel": "chat", "execution_attempt_id": "attempt-a"})
        assert result.disposition.value in {"committed", "already_committed"}, result
        assert sent.wait(0.2)
        assert time.monotonic() - start < 0.2
        assert messages == ["canonical result"]
        assert not http_calls
    finally:
        worker.stop(2)


# LLM: An unavailable/disabled in-process hub retains the existing authenticated HTTP method unchanged.
# 函数用途: 反证关闭中心后偷偷绕过原HTTP路径。
def test_hub_off_preserves_http_poll_client(monkeypatch):
    import agent_py_agent.agent.adapter.manager as manager
    import agent_py_agent.agent.gateway_parts.http_service as http_service
    monkeypatch.setattr(http_service, "_server_instance", None)
    calls = []
    def opener(request, *, timeout):
        calls.append(request.full_url)
        result = io.BytesIO(b'{"ok":true,"response":"legacy HTTP"}')
        result.status = 200
        return result
    monkeypatch.setattr(manager, "open_authenticated_request", opener)
    client = _GatewayReplyPollClient(31415)
    client.credentials = SimpleNamespace(headers=lambda: {})
    assert client.poll_response(PendingGatewayReply("r", "chat", "alice", "m"), 0) == "legacy HTTP"
    assert calls == ["http://127.0.0.1:31415/result/r"]


# LLM: Repeated scans prune completed subscriptions, keeping wake bookkeeping bounded and preserving the store's existing authority.
# 函数用途: 用不活跃记录清理订阅，容量拒绝不改变持久投递记录。
def test_subscription_pruning_and_topic_limit(tmp_path):
    hub = RequestEventHub(ring_events=8, topic_capacity=2, subscribers_per_topic=1, idle_seconds=0)
    worker = worker_for(tmp_path, hub, lambda _: None, lambda *_: True)
    records = [PendingGatewayReply(f"r{i}", "chat", "alice", f"m{i}") for i in range(4)]
    worker._sync_hub_subscriptions(records)
    assert len(worker._hub_subscriptions) == 2
    worker._sync_hub_subscriptions([])
    assert not worker._hub_subscriptions
    assert not any(state.subscribers for state in hub._topics.values())
