"""后台来源共用路由合同：精确线程绑定优先，owner 按需读取，真实调用携带所选地址。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent.conversation import background_claim as claim_module
from agent.conversation.background_routing import (
    BackgroundRouteDependencies,
    default_route_target,
    owner_from_paths,
    resolve_background_route,
)
from agent.conversation.models import ChannelBinding, ConversationThread
from agent.conversation.runtime import BackgroundMainAgentScheduler


# LLM: helper 只提供路由所需的三个只读能力，不构造 scheduler 或缓存第二份线程状态。
# 函数用途: 为路由合同提供合成线程和 owner 事实，无文件、网络或持久化副作用。
def _routes(thread, *, paths=(), identity=("", "")):
    return BackgroundRouteDependencies(
        load_thread=lambda _thread_id: thread,
        owner_paths=lambda: iter(paths),
        owner_identity=lambda: identity,
    )


def test_observation_route_feishu_uses_open_id():
    thread = ConversationThread(
        thread_id="t1", canonical_user_id="u1",
        channel_bindings=[ChannelBinding(channel="feishu", channel_conversation_id="default",
                                          channel_user_id="ou_abc", canonical_user_id="ou_abc", thread_id="t1")],
    )
    channel, target = resolve_background_route(_routes(thread), "t1")
    assert channel == "feishu"
    assert target == "ou_abc"  # 飞书发到 open_id(receive_id_type=open_id),不是 "default"


def test_observation_route_no_binding_falls_back_internal():
    thread = ConversationThread(thread_id="t2", canonical_user_id="u1", channel_bindings=[])
    assert resolve_background_route(_routes(thread), "t2") == ("internal", "")


def test_observation_requires_main_agent_treated_urgent():
    """观察批叫回(真事件)必须走 urgent 上报分支,不落 default(拿 create_subagents 去重派工)。"""
    from agent.conversation.background_tool_policy import _is_urgent_wake
    req = SimpleNamespace(wake_signal={}, reason="observation_requires_main_agent")
    assert _is_urgent_wake(req) is True
    # 普通闲聊 reason 不误判成 urgent
    assert _is_urgent_wake(SimpleNamespace(wake_signal={}, reason="incoming_channel_message")) is False


def test_observation_route_prefers_owner_identity():
    """没有线程绑定时才使用 owner 的可外发身份。"""
    thread = ConversationThread(thread_id="tsub", canonical_user_id="u1", channel_bindings=[])
    routes = _routes(thread, identity=("feishu", "ou_owner"))
    assert resolve_background_route(routes, "tsub") == ("feishu", "ou_owner")


@pytest.mark.parametrize("owner_kind", ["users", "groups"])
def test_observation_route_parses_owner_home_path(owner_kind):
    """路径中的 owner 身份优先于属性，但不能覆盖更具体的线程绑定。"""
    thread = ConversationThread(thread_id="tsub", canonical_user_id="u1", channel_bindings=[])
    routes = _routes(
        thread,
        paths=(f"/owner-fixture/owners/providers/feishu/{owner_kind}/test-owner/conversations",),
        identity=("feishu", "attribute-owner"),
    )
    assert resolve_background_route(routes, "tsub") == ("feishu", "test-owner")


def test_owner_from_home_path_no_match_returns_empty():
    assert owner_from_paths(("", "/owner-fixture/threads")) == ("", "")


def test_thread_binding_skips_owner_reads():
    thread = ConversationThread(
        thread_id="t1", canonical_user_id="u1",
        channel_bindings=[
            ChannelBinding(channel="feishu", channel_conversation_id="chat-old", channel_user_id="old",
                           canonical_user_id="u1", thread_id="t1"),
            ChannelBinding(channel="feishu", channel_conversation_id="chat-new", channel_user_id="new",
                           canonical_user_id="u1", thread_id="t1"),
        ],
    )
    reads = []

    def forbidden_owner_read():
        reads.append("owner")
        raise AssertionError("有效线程绑定不能读取或被 owner 覆盖")

    routes = BackgroundRouteDependencies(
        load_thread=lambda _thread_id: thread,
        owner_paths=forbidden_owner_read,
        owner_identity=forbidden_owner_read,
    )
    assert resolve_background_route(routes, "t1") == ("feishu", "new")
    assert reads == []


def test_owner_path_skips_identity_and_later_path_reads():
    reads = []

    def owner_paths():
        reads.append("path")
        yield "/owner-fixture/owners/providers/feishu/users/path-owner"
        raise AssertionError("首个 canonical 路径已匹配，不得预读后续路径")

    def forbidden_identity():
        raise AssertionError("有效路径身份不应再查 owner 属性")

    routes = BackgroundRouteDependencies(
        load_thread=lambda _thread_id: None,
        owner_paths=owner_paths,
        owner_identity=forbidden_identity,
    )
    assert resolve_background_route(routes, "t1") == ("feishu", "path-owner")
    assert reads == ["path"]


def test_thread_load_failure_keeps_owner_route():
    def missing_thread(_thread_id):
        raise OSError("合成线程读取失败")

    routes = BackgroundRouteDependencies(
        load_thread=missing_thread,
        owner_paths=lambda: iter(()),
        owner_identity=lambda: ("feishu", "owner"),
    )
    assert resolve_background_route(routes, "missing") == ("feishu", "owner")


def test_local_owner_does_not_override_internal_binding():
    thread = ConversationThread(
        thread_id="t1", canonical_user_id="u1",
        channel_bindings=[ChannelBinding(channel="internal", channel_conversation_id="session", channel_user_id="",
                                         canonical_user_id="u1", thread_id="t1")],
    )
    routes = _routes(thread, identity=("local", "owner"))
    assert resolve_background_route(routes, "t1") == ("internal", "session")


def test_default_route_target_keeps_matching_then_last_binding_order():
    thread = ConversationThread(
        thread_id="t1", canonical_user_id="u1",
        channel_bindings=[
            ChannelBinding(channel="feishu", channel_conversation_id="first", channel_user_id="",
                           canonical_user_id="u1", thread_id="t1"),
            ChannelBinding(channel="internal", channel_conversation_id="last", channel_user_id="",
                           canonical_user_id="u1", thread_id="t1"),
        ],
    )
    assert default_route_target(thread, "feishu") == "first"
    assert default_route_target(thread, "other") == "last"
    empty_thread = ConversationThread(thread_id="t1", canonical_user_id="u1", channel_bindings=[])
    assert default_route_target(empty_thread, "internal") == "t1"


def test_run_wake_signal_passes_owner_route(monkeypatch):
    """实际 wake 入口必须把同一路由器的结果传到既有 claimed 执行链。"""

    captured = {}

    s = BackgroundMainAgentScheduler({
        "runtime": SimpleNamespace(
            agent=SimpleNamespace(home_paths=SimpleNamespace(owner_provider="feishu", owner_id="ou_owner")),
            run_once=lambda _params: None,
        ),
        "store": SimpleNamespace(
            claims=SimpleNamespace(),
            wakes=SimpleNamespace(mark_handled=lambda *a, **k: None),
            threads=SimpleNamespace(load=lambda _tid: None),
        ),
    })
    s._pre_wake_capability_sweep = lambda *a, **k: None
    s._wake_retry_after = {}
    s.scheduler_service = None

    def _fake_run_claimed(params):
        captured.update(params)
        return SimpleNamespace(
            thread_id=params["thread_id"],
            wake_handled=True,
        )

    monkeypatch.setattr(claim_module, "run_claimed", lambda _dependencies, params: _fake_run_claimed(params))

    signal = SimpleNamespace(
        reason="", urgency="urgent", thread_id="bg-main-thread-x", root_task_id="task-1", wake_signal_id="ws-1",
    )
    s._run_wake_signal(signal, now=123.0)
    assert captured.get("route_channel") == "feishu"
    assert captured.get("route_target") == "ou_owner"
    assert captured.get("reason") == "urgent_wake_signal"
