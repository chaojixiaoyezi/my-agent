"""真机根 bug:观察批上报路(真事件→主代理→通道)原来不传 route → 默认 internal → 到不了
飞书。修:_observation_route 从线程 channel binding 取真实路由(飞书 p2p 用 open_id)。"""
from __future__ import annotations

from agent.conversation.models import ChannelBinding, ConversationThread
from agent.conversation.runtime import BackgroundMainAgentScheduler


class _MockStore:
    def __init__(self, thread):
        self._thread = thread

    def load_thread(self, thread_id):
        return self._thread


def _sched(thread):
    s = BackgroundMainAgentScheduler.__new__(BackgroundMainAgentScheduler)
    s.store = _MockStore(thread)
    return s


def test_observation_route_feishu_uses_open_id():
    thread = ConversationThread(
        thread_id="t1", canonical_user_id="u1",
        channel_bindings=[ChannelBinding(channel="feishu", channel_conversation_id="default",
                                          channel_user_id="ou_abc", canonical_user_id="ou_abc", thread_id="t1")],
    )
    channel, target = _sched(thread)._observation_route("t1")
    assert channel == "feishu"
    assert target == "ou_abc"  # 飞书发到 open_id(receive_id_type=open_id),不是 "default"


def test_observation_route_no_binding_falls_back_internal():
    thread = ConversationThread(thread_id="t2", canonical_user_id="u1", channel_bindings=[])
    assert _sched(thread)._observation_route("t2") == ("internal", "")


def test_observation_requires_main_agent_treated_urgent():
    """观察批叫回(真事件)必须走 urgent 上报分支,不落 default(拿 create_subagents 去重派工)。"""
    from types import SimpleNamespace

    from agent.conversation.runtime import _is_urgent_wake
    req = SimpleNamespace(wake_signal={}, reason="observation_requires_main_agent")
    assert _is_urgent_wake(req) is True
    # 普通闲聊 reason 不误判成 urgent
    assert _is_urgent_wake(SimpleNamespace(wake_signal={}, reason="incoming_channel_message")) is False
