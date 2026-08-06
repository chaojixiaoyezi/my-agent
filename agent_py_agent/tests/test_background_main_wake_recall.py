"""事件叫回半环:多 owner 唤醒消费(断裂A)+ 叫回产出投真渠道(断裂B)。

覆盖修复:
- 断裂A:scoped owner 的子代理把唤醒写进各自 owner 的 conversation_store,原后台调度器只 tick base
  → 永远消费不到。修复=后台值守按共享活跃登记表逐 owner tick。
- 断裂B:叫回后主代理产出以前塞 FakeDeliveryService + 路由到 internal → 发不到飞书。修复=统一
  DeliveryService + 路由把 internal 唤醒升级成对会话已绑飞书通道的主动外呼(open_id)。
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    ConversationStore,
)
from agent_py_agent.agent.conversation.models import ChannelBinding, ConversationThread
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _resolve_delivery_route
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import (
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
    build_default_channel_registry,
)
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
    """替身飞书 adapter:记录文本与原生媒体发送,不发网络。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.files: list[tuple[str, Path]] = []
        self.images: list[tuple[str, Path]] = []

    def send_message(self, user_id: str, message) -> bool:
        self.sent.append((user_id, str(getattr(message, "content", "") or "")))
        return True

    def send_file(
        self,
        user_id: str,
        path: Path,
        *,
        idempotency_key: str = "",
    ) -> bool:
        _ = idempotency_key
        self.files.append((user_id, path))
        return True

    def send_image(
        self,
        user_id: str,
        path: Path,
        *,
        idempotency_key: str = "",
    ) -> bool:
        _ = idempotency_key
        self.images.append((user_id, path))
        return True


def _feishu_thread() -> ConversationThread:
    binding = ChannelBinding(
        channel="feishu",
        channel_conversation_id="chat-1",
        channel_user_id="ou_open_id_1",
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
    assert target == "ou_open_id_1"  # 飞书 send 用 receive_id_type=open_id → 取 channel_user_id


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


def test_internal_wake_uses_registered_second_im_proactive_capability() -> None:
    binding = ChannelBinding(
        channel="second-im", channel_conversation_id="room-1", channel_user_id="user-1",
        canonical_user_id="u1", thread_id="t1", last_active_at=2.0,
    )
    thread = ConversationThread(thread_id="t1", canonical_user_id="u1", channel_bindings=(binding,))

    channel, target = _resolve_delivery_route(
        thread,
        BackgroundRunRequest(thread_id="t1", route_channel="internal"),
        supports_proactive=lambda name: name == "second-im",
    )

    assert (channel, target) == ("second-im", "user-1")


# ---------- 统一 DeliveryService(断裂B 的投递半边) ----------

def _service() -> DeliveryService:
    return DeliveryService(build_default_channel_registry(AgentConfig(feishu_app_id="", feishu_app_secret="")))


def _register_recording_adapter(service: DeliveryService, adapter: _RecordingFeishuAdapter) -> None:
    service.registry.register_adapter(
        "feishu",
        adapter,
        capabilities=ChannelCapabilities(text=True, reply=True, proactive=True, files=True, images=True),
    )


def _deliver(service: DeliveryService, *, channel: str = "feishu", target: str = "ou_open_id_1", content: str = ""):
    return service.deliver(
        DeliveryContext(channel=channel, target=target, mode="proactive"),
        ReplyEnvelope(content=content),
    )


def test_hub_internal_channel_is_noop() -> None:
    service = _service()
    receipt = _deliver(service, channel="internal", target="x", content="hi")
    assert receipt.channel == "internal" and receipt.delivery_status == "not_applicable"
    assert service.registry.adapter_for("internal") is None


def test_hub_feishu_sends_via_adapter() -> None:
    service = _service()
    adapter = _RecordingFeishuAdapter()
    _register_recording_adapter(service, adapter)
    receipt = _deliver(service, content="汇总内容")
    assert adapter.sent == [("ou_open_id_1", "汇总内容")]
    assert receipt.delivery_status == "sent" and receipt.error_code == ""


def test_hub_feishu_without_credentials_is_noop() -> None:
    service = _service()  # 无飞书凭据 → 建不出 adapter
    receipt = _deliver(service, content="x")
    assert receipt.target == "ou_open_id_1"
    assert receipt.delivery_status == "unavailable"
    assert receipt.error_code == "CHANNEL_ADAPTER_UNAVAILABLE"
    assert service.registry.adapter_for("feishu") is None  # 缓存 None,不每次重试、不崩


def test_hub_empty_content_is_not_sent() -> None:
    service = _service()
    adapter = _RecordingFeishuAdapter()
    _register_recording_adapter(service, adapter)
    _deliver(service, content="   ")
    assert adapter.sent == []  # 空内容不外发


def test_hub_rejects_invalid_feishu_open_id_before_adapter_and_deduplicates_log(caplog) -> None:
    service = _service()
    adapter = _RecordingFeishuAdapter()
    _register_recording_adapter(service, adapter)

    with caplog.at_level("WARNING"):
        first = _deliver(service, target="mon2", content="进度")
        second = _deliver(service, target="mon2", content="进度2")

    assert adapter.sent == []
    assert first.delivery_status == second.delivery_status == "rejected"
    assert first.error_code == second.error_code == "CHANNEL_TARGET_INVALID"
    assert caplog.text.count("CHANNEL_TARGET_INVALID") == 1
    assert "mon2" not in caplog.text


def test_channel_delivery_error_codes_have_recovery_contracts() -> None:
    expected = {
        "CHANNEL_TARGET_INVALID": False,
        "CHANNEL_DELIVERY_MODE_INVALID": False,
        "CHANNEL_PROACTIVE_UNSUPPORTED": False,
        "CHANNEL_ADAPTER_UNAVAILABLE": False,
        "CHANNEL_SEND_FAILED": True,
        "CHANNEL_SEND_EXCEPTION": True,
    }
    for code, retryable in expected.items():
        contract = error_contract(code)
        assert contract.code == code
        assert contract.retryable is retryable


# ---------- 端到端:叫回产出真投飞书(断裂B 全链) ----------

def test_wake_summary_delivered_to_feishu(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _CapturingBackend()
    store = ConversationStore(tmp_path / "conversations")
    service = DeliveryService(build_default_channel_registry(agent.config))
    adapter = _RecordingFeishuAdapter()
    _register_recording_adapter(service, adapter)
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=service)
    thread = store.get_or_create_thread(
        {"canonical_user_id": "u1", "channel": "feishu", "channel_conversation_id": "chat-1", "channel_user_id": "ou_open_id_1", "now": 10.0}
    )

    report = runtime.run_once({"thread_id": thread.thread_id, "reason": "subagent_runner_finished", "now": 20.0})

    assert report.route_channel == "feishu"
    assert report.route_target == "ou_open_id_1"
    assert adapter.sent and adapter.sent[0][0] == "ou_open_id_1"
    assert "整合汇总" in adapter.sent[0][1]


# ---------- 端到端:后台值守 tick scoped owner 并投递(断裂A + B) ----------

def test_supervisor_ticks_scoped_owner_wake_and_delivers(tmp_path) -> None:
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
    from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor
    from agent_py_agent.cli.models import GatewayRunContext

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
        {"canonical_user_id": "u1", "channel": "feishu", "channel_conversation_id": "chat-1", "channel_user_id": "ou_open_id_1", "now": 10.0}
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
        config_path=tmp_path / "config.yaml",
    )

    assert scoped.conversation_store.pending_wake_signals()  # 修前:这条唤醒 base 调度器看不到
    with patch("agent_py_agent.cli.gateway_loops.make_agent", return_value=base_agent):
        supervisor = _BackgroundMainSupervisor(context)
        adapter = _RecordingFeishuAdapter()
        _register_recording_adapter(supervisor._channels, adapter)  # 替身,免真发飞书
        supervisor.tick()  # 提交 owner tick(整合已并行化:每 owner 的 tick 丢线程池异步跑,不阻塞)
        import concurrent.futures as _cf

        _cf.wait(list(supervisor._inflight.values()), timeout=15)  # 等后台 owner tick 真跑完(生产靠多轮 tick 异步收割)
        ran = supervisor.tick()  # 下一轮 tick 收割完成的 owner 整合报告

    assert ran is True  # 收割到 owner 整合报告
    assert not scoped.conversation_store.pending_wake_signals()  # 断裂A:scoped owner 唤醒被消费
    assert adapter.sent and adapter.sent[0][0] == "ou_open_id_1"  # 断裂B:汇总主动外呼到该飞书用户


def test_supervisor_single_owner_only_ticks_base(tmp_path) -> None:
    # 未开 scoping / 无活跃 scoped owner:登记表空 → 后台只 tick base,绝不建 owner 池(行为不变)。
    from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor
    from agent_py_agent.cli.models import GatewayRunContext

    base_agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", gateway_request_poll_interval=1, gateway_heartbeat_interval=5),
        tmp_path,
    )
    base_agent.backend = _CapturingBackend()
    context = GatewayRunContext(
        agent=base_agent,
        paths=SimpleNamespace(),
        config_path=tmp_path / "config.yaml",
    )
    with patch("agent_py_agent.cli.gateway_loops.make_agent", return_value=base_agent):
        supervisor = _BackgroundMainSupervisor(context)
        assert supervisor.tick() is False  # base 无 due 事件 → 无报告
    assert supervisor._owner_pool is None  # 从未触碰 owner 池
    assert supervisor._owner_schedulers == {}


def test_supervisor_periodically_recovers_base_and_active_owner_watches(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.cli import gateway_loops

    base_home = tmp_path / "owners" / "local" / "main"
    scoped_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "u1"
    base = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=base_home))
    scoped = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=scoped_home))

    class Pool:
        @staticmethod
        def active_agents():
            return [scoped, scoped]

    recovered: list[object] = []
    monkeypatch.setattr(
        gateway_loops,
        "recover_active_audit_harvesters",
        lambda agent: recovered.append(agent) or 1,
    )
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = base
    supervisor._owner_pool = Pool()
    supervisor._watch_recovery_interval = 15.0
    supervisor._next_watch_recovery_at = 0.0

    assert supervisor._recover_active_watch_harvesters(now=100.0) == 2
    assert recovered == [base, scoped]
    assert supervisor._recover_active_watch_harvesters(now=114.9) == 0
    assert supervisor._recover_active_watch_harvesters(now=115.0) == 2


def test_blocked_base_tick_does_not_starve_scoped_owner_tick() -> None:
    """A long local/main goal cannot stop an IM owner from starting work."""
    from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor

    base_started = threading.Event()
    release_base = threading.Event()
    owner_ran = threading.Event()

    class BlockingBaseScheduler:
        def tick(self):
            base_started.set()
            release_base.wait(5)
            return []

    class OwnerScheduler:
        def tick(self):
            owner_ran.set()
            return []

    supervisor = object.__new__(_BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(
        config=SimpleNamespace(
            background_owner_workers=1,
            owner_maintenance_scan_interval_seconds=60,
        ),
    )
    supervisor._base_scheduler = BlockingBaseScheduler()
    supervisor._owner_schedulers = {1: OwnerScheduler()}
    supervisor._executor = None
    supervisor._inflight = {}
    supervisor._next_curator_run_at = 0.0
    supervisor._curator_inflight = {}
    supervisor._maybe_seed_wake_pending_owners = lambda: None
    supervisor._sync_owner_schedulers = lambda: None
    supervisor._recover_active_watch_harvesters = lambda: None
    supervisor._run_due_curators = lambda: None  # curator 调度独立单测覆盖,这里只测 tick 编排

    try:
        assert supervisor.tick() is False
        assert base_started.wait(1)
        assert owner_ran.wait(1)
        assert "base" in supervisor._inflight
        assert 1 in supervisor._inflight
    finally:
        release_base.set()
        supervisor.shutdown()


def test_internal_signal_not_pushed_to_user():
    """唤醒多次时，运行态和子代理信号不能被当消息主动外呼给用户。"""
    from agent_py_agent.agent.conversation import leads_with_internal_signal
    assert leads_with_internal_signal("[RUN_TOOL_EVIDENCE_BLOCKED] {...}") is True
    assert leads_with_internal_signal("[RUN_NONBLOCKING_YIELD]\n{...}") is True
    assert leads_with_internal_signal("  [SUBAGENT_WAITING]") is True
    assert leads_with_internal_signal("① 13×17=221 ② √256=16 汇总给你") is False
    assert leads_with_internal_signal("好的,已经帮你处理完了") is False


def test_gateway_channel_hub_sends_registered_attachment_with_native_file_api(tmp_path) -> None:
    """结构化附件不降级成服务器路径文字，而是调用飞书原生文件接口。"""
    from agent_py_agent.agent.conversation import ChannelAttachment

    artifact = tmp_path / "report.xlsx"
    artifact.write_bytes(b"xlsx")
    service = DeliveryService(build_default_channel_registry(SimpleNamespace()))
    adapter = _RecordingFeishuAdapter()
    _register_recording_adapter(service, adapter)

    receipt = service.deliver(
        DeliveryContext(channel="feishu", target="ou_open_id_1", mode="proactive"),
        ReplyEnvelope(
            attachments=(
                ChannelAttachment(
                    artifact_id="weekly_report",
                    path=str(artifact),
                    name=artifact.name,
                    kind="xlsx",
                ),
            ),
        )
    )

    assert receipt.delivery_status == "sent"
    assert receipt.attachment_ids == ("weekly_report",)
    assert adapter.sent == []
    assert adapter.files == [("ou_open_id_1", artifact)]


# ---------- 断裂A 硬护栏:请求路(worker)与后台值守(supervisor)对同一 owner 必须解析到同一磁盘会话库 ----------

def test_worker_and_supervisor_pools_resolve_identical_conversation_store_root(tmp_path) -> None:
    """真机 KeyError('unknown conversation thread') 的第一嫌疑是「两路 base agent 各建 owner 池 →
    同一 owner 的 conversation_store 落到不同磁盘目录」。这里用**两个独立 base agent**(生产实况:
    worker 与 supervisor 各自 make_agent、各自 owner 池)证明:同一 owner 解析出的会话库根**完全一致**,
    supervisor 池看得见 worker 建的线程,且跨池 claim→renew 全程无 KeyError。锁住这条一致性不许回退。"""
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool

    def base_config() -> AgentConfig:
        return AgentConfig(
            enable_tools=False, memory_path="memory.jsonl",
            gateway_per_user_owner_scoping=True, my_agent_home=str(tmp_path / "home"),
        )

    root = tmp_path / "ws"
    owner = OwnerIdentity.provider_user("feishu", "u1")

    # 两个独立 base agent + 各自独立 owner 池(等价于生产里 worker 线程与 supervisor 线程各建各的)。
    base_worker = SimpleAgent(base_config(), root)
    base_super = SimpleAgent(base_config(), root)
    scoped_worker = _owner_pool(base_worker).get(owner)
    scoped_super = _owner_pool(base_super).get(owner)

    assert scoped_worker is not scoped_super  # 不同实例(线程隔离)
    worker_root = Path(scoped_worker.conversation_store.root).resolve()
    super_root = Path(scoped_super.conversation_store.root).resolve()
    assert worker_root == super_root  # 同一磁盘会话库

    # worker 建线程 + claim;supervisor 池(独立实例、同磁盘根)看得见并能跨池续租,全程无 KeyError。
    worker_store = scoped_worker.conversation_store
    super_store = scoped_super.conversation_store
    thread = worker_store.get_or_create_thread(
        {"canonical_user_id": "u1", "channel": "feishu", "channel_conversation_id": "chat-1", "channel_user_id": "ou_open_id_1", "now": 10.0}
    )
    assert super_store.load_thread(thread.thread_id) is not None
    claim = super_store.claim_background_run({"thread_id": thread.thread_id, "reason": "wake_signal", "lease_seconds": 30, "now": 20.0})
    assert claim is not None
    renewed = super_store.renew_background_run_claim({"thread_id": thread.thread_id, "claim_id": claim["claim_id"], "lease_seconds": 30, "now": 21.0})
    assert renewed is not None
