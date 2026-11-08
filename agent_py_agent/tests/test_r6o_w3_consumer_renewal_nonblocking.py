"""R6O-W3：审批消费者续租是尽力心跳；同一 root 的租约文件被他人持锁时立即返回，不让通知读请求排队。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation import agent_tool_approval as bridge


def _agent(tmp_path):
    return SimpleNamespace(conversation_store=SimpleNamespace(storage=SimpleNamespace(root=tmp_path)))


def test_renewal_returns_while_another_writer_holds_the_lease_lock(tmp_path):
    agent = _agent(tmp_path)
    target = tmp_path / "subagent_tool_approvals" / "lease-root" / ".consumer.json"
    target.parent.mkdir(parents=True)
    held, release, finished = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with locked_json_path(target):
            held.set()
            release.wait(30)

    def renew():
        bridge.renew_agent_tool_approval_consumer(agent, root_task_id="lease-root")
        finished.set()

    owner = threading.Thread(target=holder, daemon=True)
    owner.start()
    assert held.wait(10)
    waiter = threading.Thread(target=renew, daemon=True)
    waiter.start()
    try:
        assert finished.wait(5), "renewal queued behind the lease lock"
        assert not target.exists()
    finally:
        release.set()
        owner.join(10)
        waiter.join(10)
    # 锁释放后的下一次续租照常落盘。
    assert bridge.renew_agent_tool_approval_consumer(agent, root_task_id="lease-root") == target
    assert bridge._consumer_seen_at(target.parent, "lease-root") is not None
