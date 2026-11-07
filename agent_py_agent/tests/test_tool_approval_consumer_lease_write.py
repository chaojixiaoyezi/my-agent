"""审批消费者心跳按半个有效期限频写入，等待方仍读取持久化的在线时间。"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.conversation import agent_tool_approval as bridge
from agent_py_agent.tests.test_background_tool_approval import background

_FIXTURES = (background,)


@pytest.fixture
def lease_clock(monkeypatch):
    clock = SimpleNamespace(now=1_700_000_000.0, writes=[])
    write = bridge.write_json_file_atomic

    def write_and_stamp(path, payload, **kwargs):
        write(path, payload, **kwargs)
        if path.name == ".consumer.json":
            clock.writes.append(payload["seen_at"])
            # 显式时间与文件时间使用同一测试时钟，避免依赖真实等待和平台时间戳精度。
            os.utime(path, (clock.now, clock.now))

    monkeypatch.setattr(bridge, "write_json_file_atomic", write_and_stamp)
    monkeypatch.setattr(bridge.time, "time", lambda: clock.now)
    return clock


def _consumer_agent(tmp_path):
    return SimpleNamespace(conversation_store=SimpleNamespace(storage=SimpleNamespace(root=tmp_path)))


@pytest.mark.parametrize("explicit_seen_at", [False, True])
def test_consumer_burst_writes_once(tmp_path, lease_clock, explicit_seen_at):
    agent = _consumer_agent(tmp_path)
    start = lease_clock.now
    expected = tmp_path / "subagent_tool_approvals" / "lease-root" / ".consumer.json"

    for index in range(10):
        lease_clock.now = start + index / 10
        kwargs = {"seen_at": lease_clock.now} if explicit_seen_at else {}
        target = bridge.renew_agent_tool_approval_consumer(agent, root_task_id="lease-root", **kwargs)
        assert target == expected
        assert bridge._consumer_seen_at(target.parent, "lease-root") == start

    assert lease_clock.writes == [start]


@pytest.mark.parametrize("offset", [0.0, 0.01])
def test_consumer_renews_at_or_after_half_lease(tmp_path, lease_clock, offset):
    agent = _consumer_agent(tmp_path)
    start = lease_clock.now
    half_lease = bridge.AGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS / 2
    target = bridge.renew_agent_tool_approval_consumer(agent, root_task_id="lease-root")

    lease_clock.now = start + half_lease - 0.01
    assert bridge.renew_agent_tool_approval_consumer(agent, root_task_id="lease-root") == target
    assert lease_clock.writes == [start]
    assert bridge._consumer_seen_at(target.parent, "lease-root") == start

    lease_clock.now = start + half_lease + offset
    assert bridge.renew_agent_tool_approval_consumer(
        agent, root_task_id="lease-root", seen_at=lease_clock.now,
    ) == target
    assert lease_clock.writes == [start, lease_clock.now]
    assert bridge._consumer_seen_at(target.parent, "lease-root") == lease_clock.now


def test_missing_consumer_file_is_recreated_without_waiting(tmp_path, lease_clock):
    agent = _consumer_agent(tmp_path)
    start = lease_clock.now
    target = bridge.renew_agent_tool_approval_consumer(agent, root_task_id="lease-root")
    target.unlink()

    lease_clock.now += 0.1
    assert bridge.renew_agent_tool_approval_consumer(agent, root_task_id="lease-root") == target
    assert lease_clock.writes == [start, lease_clock.now]
    assert bridge._consumer_seen_at(target.parent, "lease-root") == lease_clock.now


def test_existing_waiter_stays_online_across_throttled_renewals(background, lease_clock, monkeypatch):
    ctx = background
    lease_clock.now = start = float(int(ctx.request.requested_at) + 1)
    handle = bridge.publish_agent_tool_approval(
        ctx.agent, run_id=ctx.task_id, thread_id=ctx.thread.thread_id, request_value=ctx.request,
    )
    target = bridge.renew_agent_tool_approval_consumer(ctx.agent, root_task_id=ctx.task_id)
    half_lease = bridge.AGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS / 2
    offsets = [index / 10 for index in range(1, 10)]
    offsets += [half_lease - 0.01, half_lease, 2 * half_lease - 0.01, 2 * half_lease,
                3 * half_lease - 0.01, 3 * half_lease]
    ticks = iter(offsets)
    observed = []

    def advance_and_renew(_interval):
        offset = next(ticks)
        lease_clock.now = start + offset
        assert bridge.renew_agent_tool_approval_consumer(ctx.agent, root_task_id=ctx.task_id) == target
        observed.append(offset)
        if offset == offsets[-1]:
            bridge.resolve_agent_tool_approval(
                ctx.agent, run_id=ctx.task_id, request_value=ctx.request,
                decision_value=ToolApprovalDecision(ctx.request.permission_id, "approved"),
            )

    monkeypatch.setattr(bridge.time, "sleep", advance_and_renew)
    decision = bridge.wait_for_agent_tool_approval(handle)

    assert decision.approved
    assert observed == offsets
    assert lease_clock.writes == [start + index * half_lease for index in range(4)]
    assert bridge._consumer_seen_at(target.parent, ctx.task_id) == start + 3 * half_lease
