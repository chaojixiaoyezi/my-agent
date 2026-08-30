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


def test_observation_route_prefers_owner_identity():
    """第3层根修:观察挂子代理线程(无binding)时,按 owner 身份(feishu/ou_xxx)取路由,不回落 internal。"""
    from types import SimpleNamespace
    thread = ConversationThread(thread_id="tsub", canonical_user_id="u1", channel_bindings=[])
    s = BackgroundMainAgentScheduler.__new__(BackgroundMainAgentScheduler)
    s.store = _MockStore(thread)
    s.runtime = SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(owner_provider="feishu", owner_id="ou_owner")))
    assert s._observation_route("tsub") == ("feishu", "ou_owner")


def test_observation_route_parses_owner_home_path():
    """第5层根修:scoped scheduler 的 owner 身份属性没设时,直接从 owner home 路径解析 provider+open_id。
    (真机疑点:观察批在 scoped scheduler 跑,agent.home_paths 属性可能为空 → 回落 internal → 到不了飞书。)"""
    from types import SimpleNamespace
    thread = ConversationThread(thread_id="tsub", canonical_user_id="u1", channel_bindings=[])
    s = BackgroundMainAgentScheduler.__new__(BackgroundMainAgentScheduler)
    # store 根落在 owner home 子树,身份属性缺失(owner_provider/owner_id 都空)
    s.store = SimpleNamespace(
        load_thread=lambda _tid: thread,
        root="/root/.my-agent/owners/providers/feishu/users/ou_1be76a133/threads",
    )
    s.runtime = SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(owner_provider="", owner_id="")))
    assert s._observation_route("tsub") == ("feishu", "ou_1be76a133")


def test_owner_from_home_path_no_match_returns_empty():
    """路径里没有 owners/providers 结构(如单租户/admin)→ 返回空,由上层回落 binding/internal。"""
    from types import SimpleNamespace
    s = BackgroundMainAgentScheduler.__new__(BackgroundMainAgentScheduler)
    s.store = SimpleNamespace(root="/root/.my-agent/threads")
    s.runtime = SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(owner_home="", root="")))
    assert s._owner_from_home_path() == ("", "")


def test_run_wake_signal_passes_owner_route():
    """真机第5层根修:wake signal 路径(真正的投递路径,先于观察批消费并连带标 observation handled)
    必须给 _run_claimed 传 owner 投递路由,否则回落 internal → 真事件叫回后上报到不了飞书。"""
    from types import SimpleNamespace

    from agent.conversation.runtime import BackgroundMainAgentScheduler

    captured = {}

    s = BackgroundMainAgentScheduler.__new__(BackgroundMainAgentScheduler)
    s.runtime = SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(owner_provider="feishu", owner_id="ou_owner")))
    s.store = SimpleNamespace(mark_wake_signal_handled=lambda *a, **k: None)
    s._observation_route = lambda _tid: ("feishu", "ou_owner")
    s._pre_wake_capability_sweep = lambda *a, **k: None
    s._wake_retry_after = {}
    s.scheduler_service = None

    def _fake_run_claimed(params):
        captured.update(params)
        return SimpleNamespace(
            thread_id=params["thread_id"],
            wake_handled=True,
        )

    s._run_claimed = _fake_run_claimed

    signal = SimpleNamespace(
        reason="", urgency="urgent", thread_id="bg-main-thread-x", root_task_id="task-1", wake_signal_id="ws-1",
    )
    s._run_wake_signal(signal, now=123.0)
    assert captured.get("route_channel") == "feishu"
    assert captured.get("route_target") == "ou_owner"
    assert captured.get("reason") == "urgent_wake_signal"
