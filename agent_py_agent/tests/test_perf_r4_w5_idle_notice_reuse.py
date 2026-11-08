# LLM: PERF-R4-W5 回归：事件中心启用时，空闲 TUI 的无等待通知轮询（游标不变、上次结果为空、owner 主题无新事件、
#   未超 gateway_idle_notice_reuse_seconds）直接复用上次空投影，O(1) 返回；任何 owner 事件、游标变化、非空结果、超时、
#   配置为 0 或事件中心关闭都回到原完整读取，且事件中心关闭时仍返回原对象。
# 模块用途: 用真实 RequestEventHub 与读口替身固定复用合同，不起 Gateway、不读 owner 数据。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import http_handlers
from agent_py_agent.agent.gateway_parts.request_event_hub import RequestEventHub


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.control_service.resolve_gateway_scope_owner",
                        lambda agent, scope: "owner-identity")
    monkeypatch.setattr("agent_py_agent.agent.user_space.owner_resolver.resolve_owner_home",
                        lambda root, owner: SimpleNamespace(owner_id="o1"))
    http_handlers._IDLE_NOTICE_REUSE.clear()
    hub = RequestEventHub(ring_events=8, topic_capacity=16, subscribers_per_topic=4, idle_seconds=60)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_idle_notice_reuse_seconds=120.0),
                            home_paths=SimpleNamespace(root="/home"))
    scope = SimpleNamespace(user_id="u", channel="chat", conversation_id="c1", all_user_access=False)
    calls = []
    payload = {"ok": True, "notices": [], "cursor": 7, "transcript_events": [], "event_cursor": 3,
               "agent_permission_requests": [], "active_task_count": 0, "events_truncated": False}

    def reader(agent, **kwargs):
        calls.append(kwargs)
        return dict(payload)

    def read(**overrides):
        kwargs = {"scope": scope, "after": 7, "event_after": 3, "event_stream_id": "s", **overrides}
        return http_handlers._read_client_notices_with_wait(reader, agent, hub=hub, **kwargs)

    yield SimpleNamespace(hub=hub, agent=agent, calls=calls, payload=payload, read=read)
    http_handlers._IDLE_NOTICE_REUSE.clear()


def test_idle_poll_reuses_until_owner_event(setup):
    first = setup.read()
    assert setup.read() == first and len(setup.calls) == 1
    setup.hub.publish(("owner", "o1"), "terminal", "req1", {})
    setup.read()
    assert len(setup.calls) == 2
    setup.read()
    assert len(setup.calls) == 2


def test_cursor_change_nonempty_result_and_expiry_reread(setup, monkeypatch):
    setup.read()
    setup.read(after=9)
    assert len(setup.calls) == 2
    setup.payload["notices"] = [{"id": "n1"}]
    setup.read(after=11)
    setup.read(after=11)
    assert len(setup.calls) == 4
    setup.payload["notices"] = []
    setup.read(after=13)
    clock = http_handlers.time.monotonic() + 121
    monkeypatch.setattr(http_handlers.time, "monotonic", lambda: clock)
    setup.read(after=13)
    assert len(setup.calls) == 6


def test_disabled_or_hubless_keeps_original_reader(setup):
    setup.agent.config.gateway_idle_notice_reuse_seconds = 0
    setup.read()
    setup.read()
    assert len(setup.calls) == 2
    result = {"notices": [], "cursor": 7}
    assert http_handlers._read_client_notices_with_wait(lambda agent, **kw: result, object(), hub=None, after=7) is result
