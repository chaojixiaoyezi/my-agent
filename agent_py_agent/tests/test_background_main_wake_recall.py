"""事件叫回半环:多 owner 唤醒消费(断裂A)+ 叫回产出投真渠道(断裂B)。

覆盖修复:
- 断裂A:scoped owner 的子代理把唤醒写进各自 owner 的 conversation_store,原后台调度器只 tick base
  → 永远消费不到。修复=后台值守按共享活跃登记表逐 owner tick。
- 断裂B:叫回后主代理产出以前塞 FakeChannelHub + 路由到 internal → 发不到飞书。修复=真渠道枢纽
  GatewayChannelHub + 路由把 internal 唤醒升级成对会话已绑飞书通道的主动外呼(open_id)。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    ChannelSendRequest,
    ConversationStore,
)
from agent_py_agent.agent.conversation.models import ChannelBinding, ConversationThread
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _resolve_delivery_route
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.channel_delivery import GatewayChannelHub
from agent_py_agent.agent.owner_scoped_pool import shared_active_owner_registry
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


class _CapturingBackend:
    name = "capturing"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="子代理跑完了，这是主代理的整合汇总。", backend=self.name)


class _RecordingFeishuAdapter:
    """替身飞书 adapter:只记录 send_message,不发网络(注入 hub._adapters 免真外呼)。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_message(self, user_id: str, message) -> bool:
        self.sent.append((user_id, str(getattr(message, "content", "") or "")))
        return True


def _feishu_thread() -> ConversationThread:
    binding = ChannelBinding(
        channel="feishu",
        channel_conversation_id="chat-1",
        channel_user_id="open-id-1",
        canonical_user_id="u1",
        thread_id="t1",
        last_active_at=5.0,
    )
    return ConversationThread(thread_id="t1", canonical_user_id="u1", channel_bindings=(binding,))


# ---------- 路由升级(断裂B 的路由半边) ----------

def test_internal_wake_upgrades_to_feishu_open_id() -> None:
    # 子代理完成走 internal 路由;会话绑过飞书 → 升级成对该飞书用户 open_id 的主动外呼。
    channel, target = _resolve_delivery_route(_feishu_thread(), BackgroundRunRequest(thread_id="t1", route_channel="internal"))
    assert channel == "feishu"
    assert target == "open-id-1"  # 飞书 send 用 receive_id_type=open_id → 取 channel_user_id


def test_explicit_feishu_route_is_preserved() -> None:
    # 进度策略显式配了 feishu+目标 → 原样保留(不改既有语义)。
    channel, target = _resolve_delivery_route(
        _feishu_thread(), BackgroundRunRequest(thread_id="t1", route_channel="feishu", route_target="chat-1")
    )
    assert channel == "feishu"
    assert target == "chat-1"


def test_internal_thread_without_pushable_binding_stays_internal() -> None:
    binding = ChannelBinding(
        channel="internal", channel_conversation_id="thread-1", channel_user_id="u1",
        canonical_user_id="u1", thread_id="t1", last_active_at=1.0,
    )
    thread = ConversationThread(thread_id="t1", canonical_user_id="u1", channel_bindings=(binding,))
    channel, target = _resolve_delivery_route(thread, BackgroundRunRequest(thread_id="t1", route_channel="internal"))
    assert channel == "internal"  # 无可外呼绑定 → 行为不变(单机/CLI 不受影响)
    assert target == "thread-1"


# ---------- 真渠道枢纽 GatewayChannelHub(断裂B 的投递半边) ----------

def _hub() -> GatewayChannelHub:
    return GatewayChannelHub(AgentConfig(feishu_app_id="", feishu_app_secret=""))


def test_hub_internal_channel_is_noop() -> None:
    hub = _hub()
    receipt = hub.send(ChannelSendRequest(channel="internal", target="x", content="hi"))
    assert receipt.channel == "internal"
    assert hub._adapters == {}  # 内部通道不建任何 adapter、不外发


def test_hub_feishu_sends_via_adapter() -> None:
    hub = _hub()
    adapter = _RecordingFeishuAdapter()
    hub._adapters["feishu"] = adapter  # 预置替身
    hub.send(ChannelSendRequest(channel="feishu", target="open-id-1", content="汇总内容"))
    assert adapter.sent == [("open-id-1", "汇总内容")]


def test_hub_feishu_without_credentials_is_noop() -> None:
    hub = _hub()  # 无飞书凭据 → 建不出 adapter
    receipt = hub.send(ChannelSendRequest(channel="feishu", target="open-id-1", content="x"))
    assert receipt.target == "open-id-1"
    assert hub._adapters.get("feishu") is None  # 缓存 None,不每次重试、不崩


def test_hub_empty_content_is_not_sent() -> None:
    hub = _hub()
    adapter = _RecordingFeishuAdapter()
    hub._adapters["feishu"] = adapter
    hub.send(ChannelSendRequest(channel="feishu", target="open-id-1", content="   "))
    assert adapter.sent == []  # 空内容不外发


# ---------- 端到端:叫回产出真投飞书(断裂B 全链) ----------

def test_wake_summary_delivered_to_feishu(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _CapturingBackend()
    store = ConversationStore(tmp_path / "conversations")
    hub = GatewayChannelHub(agent.config)
    adapter = _RecordingFeishuAdapter()
    hub._adapters["feishu"] = adapter
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=hub)
    thread = store.get_or_create_thread(
        {"canonical_user_id": "u1", "channel": "feishu", "channel_conversation_id": "chat-1", "channel_user_id": "open-id-1", "now": 10.0}
    )

    report = runtime.run_once({"thread_id": thread.thread_id, "reason": "subagent_runner_finished", "now": 20.0})

    assert report.route_channel == "feishu"
    assert report.route_target == "open-id-1"
    assert adapter.sent and adapter.sent[0][0] == "open-id-1"
    assert "整合汇总" in adapter.sent[0][1]


# ---------- 端到端:后台值守 tick scoped owner 并投递(断裂A + B) ----------

def test_supervisor_ticks_scoped_owner_wake_and_delivers(tmp_path) -> None:
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
    from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor
    from agent_py_agent.cli.models import GatewayRunContext, GatewayRunOptions

    config = AgentConfig(
        enable_tools=False,
        memory_path="memory.jsonl",
        gateway_per_user_owner_scoping=True,
        gateway_request_poll_interval=1,
        gateway_heartbeat_interval=5,
        my_agent_home=str(tmp_path / "home"),
    )
    base_agent = SimpleAgent(config, tmp_path)
    base_agent.backend = _CapturingBackend()

    # 用后台循环将复用的同一个池,预建 scoped owner 作用域 agent,并在其 store 造一个"子代理完成"唤醒。
    owner = OwnerIdentity.provider_user("feishu", "u1")
    scoped = _owner_pool(base_agent).get(owner)
    scoped.backend = _CapturingBackend()
    sthread = scoped.conversation_store.get_or_create_thread(
        {"canonical_user_id": "u1", "channel": "feishu", "channel_conversation_id": "chat-1", "channel_user_id": "open-id-1", "now": 10.0}
    )
    observation = scoped.conversation_store.append_observation(
        {"thread_id": sthread.thread_id, "event_type": "subagent_runner_finished", "summary": "子代理已完成，请整合。", "urgency": "normal", "requires_main_agent": True, "now": 11.0}
    )
    scoped.conversation_store.raise_wake_signal(
        {"thread_id": sthread.thread_id, "observation": observation, "reason": "subagent_runner_finished", "now": 11.0}
    )
    # 请求路会把活跃 owner 登记进共享表;这里手动模拟(context.agent 即 base_agent)。
    shared_active_owner_registry(base_agent).record(owner)

    context = GatewayRunContext(
        agent=base_agent,
        paths=SimpleNamespace(),
        options=GatewayRunOptions(
            mutate_state=False, start_runners=False, planner=False, interval=1.0,
            max_runners=0, limit=0, max_cycles=0, max_cards=0, reviewer="", instruction="", probe=False,
        ),
        config_path=tmp_path / "config.yaml",
        note="", take_over_by="", locked_files=[], force_lock=False,
    )

    assert scoped.conversation_store.pending_wake_signals()  # 修前:这条唤醒 base 调度器看不到
    with patch("agent_py_agent.cli.gateway_loops.make_agent", return_value=base_agent):
        supervisor = _BackgroundMainSupervisor(context)
        adapter = _RecordingFeishuAdapter()
        supervisor._channels._adapters["feishu"] = adapter  # 替身,免真发飞书
        ran = supervisor.tick()

    assert ran is True
    assert not scoped.conversation_store.pending_wake_signals()  # 断裂A:scoped owner 唤醒被消费
    assert adapter.sent and adapter.sent[0][0] == "open-id-1"  # 断裂B:汇总主动外呼到该飞书用户


def test_supervisor_single_owner_only_ticks_base(tmp_path) -> None:
    # 未开 scoping / 无活跃 scoped owner:登记表空 → 后台只 tick base,绝不建 owner 池(行为不变)。
    from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor
    from agent_py_agent.cli.models import GatewayRunContext, GatewayRunOptions

    base_agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", gateway_request_poll_interval=1, gateway_heartbeat_interval=5),
        tmp_path,
    )
    base_agent.backend = _CapturingBackend()
    context = GatewayRunContext(
        agent=base_agent,
        paths=SimpleNamespace(),
        options=GatewayRunOptions(
            mutate_state=False, start_runners=False, planner=False, interval=1.0,
            max_runners=0, limit=0, max_cycles=0, max_cards=0, reviewer="", instruction="", probe=False,
        ),
        config_path=tmp_path / "config.yaml",
        note="", take_over_by="", locked_files=[], force_lock=False,
    )
    with patch("agent_py_agent.cli.gateway_loops.make_agent", return_value=base_agent):
        supervisor = _BackgroundMainSupervisor(context)
        assert supervisor.tick() is False  # base 无 due 事件 → 无报告
    assert supervisor._owner_pool is None  # 从未触碰 owner 池
    assert supervisor._owner_schedulers == {}
