"""R9-R1 P1-R9-18：飞书解锁续送只在下游受理后移出准确队首；拒收/异常保留队首有界退避，停止代次不复活。

真实 FeishuAdapter（原私聊密码库）与 FeishuUnlockQueue；下游为真实 ChannelManager.route_message 与持久入站
存储，只在原私有写入点模拟一次 IO 失败。运输启动被替换为空操作，不连接租户或网络。
"""
from __future__ import annotations

import threading
import time

import pytest

from agent_py_agent.agent.adapter import feishu_unlock_queue, ingress
from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.feishu_unlock_queue import FeishuUnlockQueue
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.adapter.protocol import IncomingMessage

pytestmark = pytest.mark.usefixtures("gateway_client_credential")

USER = "ou_owner"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    # 只缩短原有界退避常量；缺常量的实现照常运行，由各用例自身断言判定。
    monkeypatch.setattr(feishu_unlock_queue, "UNLOCK_RESUME_RETRY_INITIAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(feishu_unlock_queue, "UNLOCK_RESUME_RETRY_MAX_SECONDS", 0.05, raising=False)


def _message(message_id: str) -> IncomingMessage:
    return IncomingMessage(channel="feishu", user_id=USER, content=f"content-{message_id}", message_id=message_id,
                           conversation_id="oc_private", metadata={"feishu_chat_type": "p2p"})


def _eventually(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _locked_adapter(tmp_path, monkeypatch, *message_ids):
    # 原锁门入队：设置密码后让会话闲置超时，私聊消息经 _session_locked_gate 暂存。
    adapter = FeishuAdapter(config={"my_agent_home": str(tmp_path / "home")})
    monkeypatch.setattr(adapter, "_send_password_card", lambda *_args: True)
    monkeypatch.setattr(adapter, "_start_long_connection", lambda: None)
    adapter._unlock.set_password(USER, "SyntheticPassword123")
    adapter._unlock.store.record_activity(USER, now=time.time() - 4 * 60 * 60)
    messages = [_message(message_id) for message_id in message_ids]
    for message in messages:
        assert adapter._session_locked_gate(message) is True
    queue = adapter._pending_unlock_messages
    assert queue.depth(USER) == len(messages) and not queue.is_draining(USER)
    return adapter, queue, messages


def test_queue_keeps_exact_head_and_capacity_until_accepted():
    queue = FeishuUnlockQueue(per_user_cap=2, total_cap=2)
    first, second = _message("m1"), _message("m2")
    assert queue.enqueue_locked(first) == "added" and queue.enqueue_if_pending(second) == "added"
    assert queue.begin_drain(USER) is True
    assert queue.next_for_drain(USER) is first
    # 未受理：仍是同一准确队首，容量责任未释放，新消息不能挤占，也未登记为已续送。
    assert queue.next_for_drain(USER) is first and queue.depth(USER) == 2
    assert queue.enqueue_if_pending(_message("m3")) == "full"
    assert queue.enqueue_if_pending(_message("m1")) == "duplicate" and queue.was_resumed(first) is False
    queue.complete(first)
    assert queue.depth(USER) == 1 and queue.was_resumed(first) is True
    assert queue.next_for_drain(USER) is second
    queue.complete(second)
    assert queue.next_for_drain(USER) is None and queue.is_draining(USER) is False


@pytest.mark.parametrize("failure", ["ingress-write", "callback-error"])
def test_single_downstream_failure_retains_head_then_accepted_retry_keeps_fifo(tmp_path, monkeypatch, failure):
    adapter, queue, messages = _locked_adapter(tmp_path, monkeypatch, "m1", "m2", "m3")
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    manager.register_adapter(adapter)
    routed, failures = [], []
    real_write = ingress._write_private_json

    def write(path, payload):
        if failure == "ingress-write" and not failures and payload.get("provider_message_id") == "m1":
            failures.append(path)
            raise OSError("simulated ingress write failure")
        return real_write(path, payload)

    def route(message):
        if failure == "callback-error" and not failures:
            failures.append(message.message_id)
            routed.append((message.message_id, "error"))
            raise RuntimeError("simulated downstream error")
        result = manager.route_message(message)
        routed.append((message.message_id, result))
        return result

    monkeypatch.setattr(ingress, "_write_private_json", write)
    adapter.on_message(route)
    assert adapter._resume_pending_unlock_messages(USER) is True
    assert _eventually(lambda: not queue.is_draining(USER)), routed
    first_outcome = False if failure == "ingress-write" else "error"
    assert routed == [("m1", first_outcome), ("m1", True), ("m2", True), ("m3", True)]
    assert len(failures) == 1
    assert [row.provider_message_id for row in manager._delivery_worker.store.records()] == ["m1", "m2", "m3"]
    assert queue.depth(USER) == 0 and all(queue.was_resumed(message) for message in messages)
    assert adapter.stop(timeout=5.0).status == "exited"


def test_legacy_none_callback_is_acceptance(tmp_path, monkeypatch):
    adapter, queue, messages = _locked_adapter(tmp_path, monkeypatch, "m1", "m2")
    received = []
    adapter.on_message(received.append)
    assert adapter._resume_pending_unlock_messages(USER) is True
    assert _eventually(lambda: not queue.is_draining(USER))
    assert received == messages and queue.depth(USER) == 0
    assert adapter.stop(timeout=5.0).status == "exited"


def test_stop_interrupts_backoff_then_restart_resumes_in_new_generation(tmp_path, monkeypatch):
    adapter, queue, _messages = _locked_adapter(tmp_path, monkeypatch, "m1", "m2")
    adapter.start()
    attempts, accept = [], threading.Event()

    def downstream(message):
        attempts.append((message.message_id, threading.current_thread()))
        return accept.is_set()

    adapter.on_message(downstream)
    monkeypatch.setattr(feishu_unlock_queue, "UNLOCK_RESUME_RETRY_INITIAL_SECONDS", 30.0)
    monkeypatch.setattr(feishu_unlock_queue, "UNLOCK_RESUME_RETRY_MAX_SECONDS", 30.0)
    assert adapter._resume_pending_unlock_messages(USER) is True
    assert _eventually(lambda: len(attempts) == 1)
    old_thread, old_stop = attempts[0][1], adapter._unlock_drain_stop
    # 启动前已登记真实续送线程及其生命周期代次。
    assert adapter._unlock_drain_threads.get(old_thread) == adapter._generation_ref
    started = time.monotonic()
    assert adapter.stop(timeout=5.0).status == "exited"
    assert time.monotonic() - started < 5.0 and not old_thread.is_alive()
    # 停止代次：队首与容量保留、drain 所有权释放；重启前不能再起续送。
    assert queue.depth(USER) == 2 and not queue.is_draining(USER)
    assert adapter._resume_pending_unlock_messages(USER) is False
    adapter.start()
    assert old_stop.is_set() and adapter._unlock_drain_stop is not old_stop
    accept.set()
    assert adapter._resume_pending_unlock_messages(USER) is True
    assert _eventually(lambda: queue.depth(USER) == 0 and not queue.is_draining(USER))
    assert [message_id for message_id, _thread in attempts] == ["m1", "m1", "m2"]
    assert all(thread is not old_thread for _message_id, thread in attempts[1:])
    assert adapter.stop(timeout=5.0).status == "exited"


def test_unjoined_stopped_generation_blocks_restart_and_never_resumes(tmp_path, monkeypatch):
    adapter, queue, _messages = _locked_adapter(tmp_path, monkeypatch, "m1")
    adapter.start()
    entered, release, attempts = threading.Event(), threading.Event(), []

    def downstream(message):
        attempts.append(message.message_id)
        entered.set()
        assert release.wait(15)
        return False

    adapter.on_message(downstream)
    assert adapter._resume_pending_unlock_messages(USER) is True
    assert entered.wait(10)
    old_thread = next(iter(adapter._unlock_drain_threads))
    # 交接仍在进行：有界停止如实报告未确认，未 join 的旧代不能被新代覆盖重启。
    result = adapter.stop(timeout=0.2)
    assert result.status == "exit_unconfirmed" and result.reason_code == "CHANNEL_ADAPTER_STOP_FAILED"
    with pytest.raises(RuntimeError, match="CHANNEL_ADAPTER_STOP_FAILED"):
        adapter.start()
    release.set()
    # 旧代交接被拒后只在自己已置位的停止事件上退避，立即退出而不再重试或续送下一条。
    old_thread.join(10)
    assert not old_thread.is_alive() and attempts == ["m1"]
    assert queue.depth(USER) == 1 and not queue.is_draining(USER)
    adapter.start()
    assert attempts == ["m1"]
    assert adapter.stop(timeout=5.0).status == "exited"
