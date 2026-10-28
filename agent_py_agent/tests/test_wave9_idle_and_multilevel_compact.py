# LLM: 压缩策略、闲置代次和管理授权用真实临时存储验证；摘要/网络边界可替换，不接生产账号或服务。
# 模块用途: 回归Wave9的四层压缩策略、用户活动CAS及闲置维护安全门。
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings.config import AgentConfig


# LLM: 新配置必须存在于真实dataclass，不能只给运行时getattr默认值制造配置已接线的假象。
# 函数用途: 钉住闲置开关和新的管理员默认比例。
def test_idle_compact_configuration_defaults():
    config = AgentConfig()
    assert getattr(config, "memory_compact_idle_trigger_percent", None) == 60
    assert getattr(config, "memory_compact_idle_seconds", None) == 7200
    assert getattr(config, "memory_compact_trigger_percent", None) == 80
    assert getattr(config, "force_compact_trigger_percent", None) is False


@pytest.mark.parametrize("forced,locked,user,thread,expected,source", [
    (True, False, 65, 70, 80, "admin_forced"),
    (True, True, 65, 70, 80, "admin_forced"),
    (False, True, 65, 70, 65, "admin_forced"),
    (False, True, None, 70, 80, "admin_forced"),
    (False, False, 65, 70, 70, "thread"),
    (False, False, 65, None, 65, "user"),
    (False, False, None, None, 80, "admin_default"),
])
# LLM: 四层策略必须返回同一完整结构；全局强制优先个人强制，强制无专属值时只能锁管理员默认。
# 函数用途: 验证管理员强制、会话、用户、管理员默认的优先顺序。
def test_compact_threshold_precedence(forced, locked, user, thread, expected, source):
    from agent_py_agent.agent.settings import config_layers

    resolve = getattr(config_layers, "resolve_compact_trigger_policy", None)
    assert callable(resolve), "compact policy must use a shared structured resolver"
    result = resolve(admin_default_percent=80, admin_forced=forced,
                     thread_override_percent=thread,
                     user_policy={"default_trigger_percent": user, "force_locked": locked})
    assert result == {"effective_trigger_percent": expected, "effective_source": source,
                      "force_locked": forced or locked, "user_default_percent": user,
                      "thread_override_percent": thread, "admin_default_percent": 80}


@pytest.mark.parametrize("value,expected", [(-1, 50), (49, 50), (50, 50), (95, 95), (100, 95), (True, 80)])
# LLM: 有效百分比夹在50..95，布尔不是管理员数值；失效值不能变成100或解除安全阈值。
# 函数用途: 钉住新四层解析器的有效范围。
def test_compact_policy_clamps_percent(value, expected):
    from agent_py_agent.agent.settings import config_layers

    resolve = getattr(config_layers, "resolve_compact_trigger_policy", None)
    assert callable(resolve)
    assert resolve(admin_default_percent=value)["effective_trigger_percent"] == expected


# LLM: 冻结对象持有本回合的解析结果，之后配置或外部metadata改变不能修改已经得到的策略快照。
# 函数用途: 验证运行时快照携带来源及闲置设置且不可变。
def test_runtime_policy_freezes_resolved_thread_threshold():
    from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy

    config = AgentConfig()
    agent = SimpleNamespace(config=config)
    thread = SimpleNamespace(metadata={"settings": {"compact_trigger_percent": 72}})
    import inspect

    assert "thread" in inspect.signature(runtime_compact_policy).parameters
    policy = runtime_compact_policy(agent, thread=thread)
    assert policy.trigger_percent == 72 and policy.effective_source == "thread"
    assert policy.idle_trigger_percent == 60 and policy.idle_seconds == 7200
    config.memory_compact_trigger_percent = 90
    thread.metadata["settings"]["compact_trigger_percent"] = 85
    assert policy.trigger_percent == 72
    with pytest.raises(FrozenInstanceError):
        policy.trigger_percent = 85


# LLM: 每个测试拥有原ConversationStore，metadata写入沿原update_atomic，不依赖模拟字典CAS。
# 函数用途: 建立没有任何后台工作的真实临时会话。
@pytest.fixture
def conversation(tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conversations")
    row = store.threads.create(canonical_user_id="test-user")
    return store, row


# LLM: 明确用户活动在同一线程锁内增代；后台摘要updated_at不能刷新用户发言时间，相同请求重读不增代。
# 函数用途: 区分用户活动与普通元数据写，并检查相同输入去重。
def test_user_activity_is_explicit_and_not_summary_activity(conversation):
    store, row = conversation
    record = getattr(store.threads, "record_user_activity", None)
    assert callable(record), "user activity needs its own atomic metadata transition"
    first = record(row.thread_id, request_id="input-one", now=100.0)
    assert first.metadata["last_user_activity_at"] == 100.0
    assert first.metadata["user_activity_generation"] == 1
    duplicate = record(row.thread_id, request_id="input-one", now=200.0)
    assert duplicate.metadata["user_activity_generation"] == 1
    summary = store.threads.update_summary(row.thread_id, "metadata only", now=300.0)
    assert summary.metadata["last_user_activity_at"] == 100.0
    second = record(row.thread_id, request_id="input-two", now=400.0)
    assert second.metadata["user_activity_generation"] == 2


# LLM: 必须在原update_compact_state锁内拒绝新活动代，旧候选不能推进head、游标或覆盖新turn。
# 函数用途: 让用户在预检后发言，再验证闲置摘要的真实CAS失败。
def test_idle_commit_cas_does_not_clobber_concurrent_user_turn(conversation):
    from agent_py_agent.agent.conversation.models import ConversationCompactCommit

    store, row = conversation
    record = getattr(store.threads, "record_user_activity", None)
    assert callable(record)
    old = record(row.thread_id, request_id="before", now=100.0)
    new = record(row.thread_id, request_id="during", now=200.0)
    commit = ConversationCompactCommit(summary="stale idle summary", operation_evidence={},
        checkpoint_id="checkpoint-fixture", compacted_through_message_id="message-fixture",
        compacted_through_byte_offset=1, source_messages=1, source_tool_pairs=0)
    with pytest.raises(RuntimeError, match="user activity"):
        store.threads.update_compact_state(row.thread_id, commit=commit,
            expected_generation=old.compact_generation,
            expected_user_activity_generation=old.metadata["user_activity_generation"], idle_compact=True,
            expected_message_tail=store.messages.tail_marker_report(row.thread_id)[0])
    actual = store.threads.require(row.thread_id)
    assert actual.to_dict() == new.to_dict()


# LLM: 动作登记只是第一关，后续用真实Host授权验证，不能仅凭方法名存在声称接口可用。
# 函数用途: 确认四个治理动作进入现有静态目录。
def test_host_compact_governance_actions_are_registered():
    from agent_py_agent.agent.plugin_host.action_specs_v2_ext import V2_ACTIONS

    assert {"users.managed.compact_policy.get", "users.managed.compact_policy.set",
            "users.managed.compact_policy.batch_set", "settings.compact_policy.admin_force.set"} <= V2_ACTIONS


# LLM: 账号沿真实本地生命周期登记，全部在pytest根内，不用伪造home identity绕开治理授权。
# 函数用途: 准备两个真实受管用户和主管理员治理服务。
@pytest.fixture
def governance(tmp_path):
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService

    root = tmp_path / "host"
    users = LocalUserStore(root)
    for name in ("alice", "bob"):
        LocalUserLifecycle(users).create(name, idempotency_key=f"compact-fixture-{name}")
    return ManagedUserGovernanceService(root), users


# LLM: 方法必须实际走原治理版本/CAS；省略字段不清除比例，显式None恢复继承，旧版本不得覆盖。
# 函数用途: 验证单用户压缩策略、锁定合并及并发版本。
def test_managed_compact_policy_preserves_cas_and_explicit_reset(governance):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    service, _users = governance
    setter = getattr(service, "set_user_compact_policy", None)
    assert callable(setter)
    first = setter("alice", {"default_trigger_percent": 65}, expected_revision=0)
    second = setter("alice", {"force_locked": True}, expected_revision=first["revision"])
    assert second["default_trigger_percent"] == 65 and second["force_locked"] is True
    with pytest.raises(HostAPIError) as error:
        setter("alice", {"default_trigger_percent": 75}, expected_revision=first["revision"])
    assert error.value.code == "REVISION_CONFLICT"
    reset = setter("alice", {"default_trigger_percent": None}, expected_revision=second["revision"])
    assert reset["default_trigger_percent"] is None and reset["force_locked"] is True


# LLM: 批量先核全体目标，all仅是管理员领域选择器；写入结果逐用户标记而不宣称跨文件事务。
# 函数用途: 回归显式列表、全部用户和带越界目标的零写入拒绝。
def test_managed_compact_batch_validates_all_targets_before_writing(governance):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    service, _users = governance
    setter = getattr(service, "batch_set_users_compact_policy", None)
    assert callable(setter)
    with pytest.raises(HostAPIError):
        setter({"default_trigger_percent": 70}, user_ids=["alice", "providers/foreign/user"])
    assert service.get_user_compact_policy("alice")["revision"] == 0
    result = setter({"default_trigger_percent": 68}, user_ids=["alice", "bob"])
    assert result["atomic"] is False
    assert all(item["status"] == "applied" for item in result["items"])
    setter({"default_trigger_percent": None}, scope="all_managed_users")
    assert all(service.get_user_compact_policy(uid)["default_trigger_percent"] is None for uid in ("alice", "bob"))


# LLM: 全局控制权仅属于真实local/main，普通用户即使持有服务对象也不能设置全局强制。
# 函数用途: 验证全局强制的权限边界和原子修订。
def test_global_compact_force_is_admin_only_and_revisioned(governance):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    service, _users = governance
    setter = getattr(service, "set_global_compact_force_policy", None)
    assert callable(setter)
    result = setter(True, trigger_percent=85, expected_revision=0)
    assert result["force_compact_trigger_percent"] is True and result["trigger_percent"] == 85
    outsider = ManagedUserGovernanceService(service.root_dir, actor_owner=OwnerIdentity.local_user("alice"))
    with pytest.raises(HostAPIError) as error:
        outsider.set_global_compact_force_policy(False)
    assert error.value.status == 403


@pytest.mark.parametrize("scenario,expected", [
    ("idle_disabled", "disabled"), ("seconds_disabled", "disabled"),
    ("not_idle", "not_idle"), ("below_threshold", "below_threshold"),
    ("already_compacted", "already_compacted"), ("foreground", "foreground_active"),
    ("subagent", "subagent_active"), ("process", "background_process_active"),
    ("regular_compacted", "regular_compacted"), ("eligible", "compacted"),
])
# LLM: 状态读取及模型边界可替换，时间/代次/原子提交使用真实ThreadStore；不声称此测试调用真实摘要模型。
# 函数用途: 覆盖闲置压缩五道闸和成功后同代次幂等。
def test_idle_compact_guards_and_generation_idempotency(conversation, tmp_path, monkeypatch, scenario, expected):
    import importlib.util
    from dataclasses import replace

    spec = importlib.util.find_spec("agent_py_agent.agent.gateway_parts.compact_operations")
    assert spec is not None, "idle compaction needs a guarded maintenance entry"
    from agent_py_agent.agent.conversation.models import ConversationCompactCommit
    from agent_py_agent.agent.gateway_parts import compact_operations as idle

    store, row = conversation
    metadata = {"last_user_activity_at": 100.0, "user_activity_generation": 1}
    if scenario == "already_compacted":
        metadata["last_idle_compacted_activity_gen"] = 1
        metadata["idle_auto_compact"] = {"last_idle_compacted_activity_gen": 1,
            "message_tail": store.messages.tail_marker_report(row.thread_id)[0]}
    if scenario == "regular_compacted":
        metadata["last_compacted_user_activity_gen"] = 1
        metadata["last_compacted_message_tail"] = store.messages.tail_marker_report(row.thread_id)[0]
    row = store.threads.update_atomic(row.thread_id, lambda current: replace(current, metadata=metadata))
    config = AgentConfig()
    if scenario == "idle_disabled":
        config.memory_compact_idle_trigger_percent = 0
    if scenario == "seconds_disabled":
        config.memory_compact_idle_seconds = 0
    agent = SimpleNamespace(config=config, home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    busy = {"foreground": "foreground_active", "subagent": "subagent_active", "process": "background_process_active"}.get(scenario, "")
    monkeypatch.setattr(idle, "idle_compact_busy_state", lambda *_: busy)
    tokens = 599 if scenario == "below_threshold" else 850 if scenario == "regular_compacted" else 650
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=tokens, context_window_tokens=1000))
    calls = []

    # LLM: 只替换模型准备边界，但提交仍走产品CAS且消费真实options，重复压缩不能由假函数自行去重。
    # 函数用途: 用可控摘要请求驱动同一原子写口。
    def compact(_agent, _store, thread, *, options):
        calls.append(options)
        commit = ConversationCompactCommit(summary="idle summary", operation_evidence={}, checkpoint_id="idle-fixture",
            compacted_through_message_id="idle-message", compacted_through_byte_offset=1, source_messages=1, source_tool_pairs=0)
        updated = store.threads.update_compact_state(thread.thread_id, commit=commit,
            expected_generation=thread.compact_generation,
            expected_user_activity_generation=options.expected_user_activity_generation, idle_compact=options.idle_compact,
            expected_message_tail=options.expected_message_tail)
        return SimpleNamespace(compacted=True, thread=updated)

    monkeypatch.setattr(idle, "prepare_conversation_context", compact)
    result = idle.maybe_auto_compact_idle_session(agent, store, row, now=7299.0 if scenario == "not_idle" else 7300.0)
    assert result["reason"] == expected
    assert len(calls) == int(scenario == "eligible")
    if scenario == "eligible":
        updated = store.threads.require(row.thread_id)
        assert updated.metadata["last_idle_compacted_activity_gen"] == 1
        assert idle.maybe_auto_compact_idle_session(agent, store, updated, now=15000.0)["reason"] == "already_compacted"
        assert len(calls) == 1


# LLM: 实际CAS冲突属于让位而非模型失败；测试在摘要期间加入新用户消息，必须保留新活动并不标记闲置完成。
# 函数用途: 验证闲置入口干净放弃并发用户的新回合。
def test_idle_entry_aborts_when_user_speaks_during_summary(conversation, tmp_path, monkeypatch):
    import importlib.util

    assert importlib.util.find_spec("agent_py_agent.agent.gateway_parts.compact_operations") is not None
    from agent_py_agent.agent.conversation.models import ConversationCompactCommit
    from agent_py_agent.agent.gateway_parts import compact_operations as idle

    store, row = conversation
    row = store.threads.record_user_activity(row.thread_id, request_id="old-input", now=100.0)
    agent = SimpleNamespace(config=AgentConfig(), home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    monkeypatch.setattr(idle, "idle_compact_busy_state", lambda *_: "")
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=650, context_window_tokens=1000))

    # LLM: 用户写和摘要提交使用同一真实Store，不能用mock返回False冒充原子竞争已经被处理。
    # 函数用途: 固定预检后、CAS前的用户发言交错。
    def racing_summary(_agent, _store, thread, *, options):
        store.threads.record_user_activity(row.thread_id, request_id="new-input", now=7301.0)
        commit = ConversationCompactCommit(summary="stale", operation_evidence={}, checkpoint_id="idle-fixture",
            compacted_through_message_id="idle-message", compacted_through_byte_offset=1, source_messages=1, source_tool_pairs=0)
        store.threads.update_compact_state(thread.thread_id, commit=commit, expected_generation=thread.compact_generation,
            expected_user_activity_generation=options.expected_user_activity_generation, idle_compact=True,
            expected_message_tail=options.expected_message_tail)

    monkeypatch.setattr(idle, "prepare_conversation_context", racing_summary)
    result = idle.maybe_auto_compact_idle_session(agent, store, row, now=7300.0)
    latest = store.threads.require(row.thread_id)
    assert result["reason"] == "activity_changed" and not result["compacted"]
    assert latest.compact_generation == 0 and latest.summary == ""
    assert latest.metadata["user_activity_generation"] == 2
    assert "last_idle_compacted_activity_gen" not in latest.metadata


@pytest.mark.parametrize("text,value,valid", [("50", "50", True), ("95", "95", True),
    ("default", "default", True), ("49", "49", False), ("96", "96", False), ("", "", False), ("80 extra", "80 extra", False)])
# LLM: 仅显式threshold子命令产生设置操作，旧自定义摘要说明仍保持run，非法比例不发摘要请求。
# 函数用途: 钉住最小命令扩展及50..95/default语法。
def test_compact_threshold_command_parses_separately(text, value, valid):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control

    command = parse_conversation_control("/compact threshold " + text)
    assert command.operation == "threshold" and command.value == value and command.valid is valid


# LLM: 设置写口读取新鲜管理员政策，不得沿本回合冻结的旧force=False绕过新锁；写入仍由原Store原子执行。
# 函数用途: 验证管理员锁定时用户设置和恢复默认都返回403。
@pytest.mark.parametrize("value", [70, None])
def test_thread_threshold_write_respects_current_admin_lock(conversation, value):
    from agent_py_agent.agent.conversation import compact_threshold_policy as policy
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    setter = getattr(policy, "thread_with_compact_threshold", None)
    assert callable(setter)
    store, row = conversation
    agent = SimpleNamespace(config=AgentConfig())
    with policy.compact_policy_scope(agent, row):
        agent.config.force_compact_trigger_percent = True
        with pytest.raises(HostAPIError) as error:
            store.threads.update_atomic(row.thread_id, lambda latest: setter(agent, latest, value))
        assert error.value.code == "COMPACT_POLICY_LOCKED_BY_ADMIN" and error.value.status == 403
    assert "settings" not in store.threads.require(row.thread_id).metadata


# LLM: 实际gateway预检发生在等待车道前，重复加载同一request只记一次活动，不从updated_at猜输入。
# 函数用途: 验证真实用户活动接线而不只测试Store新方法。
def test_gateway_preflight_records_user_activity_before_lane_wait(conversation, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_context

    store, row = conversation
    agent = SimpleNamespace(conversation_store=store)
    monkeypatch.setattr(request_context, "gateway_request_workspace_scope", lambda *_: ("", ()))
    monkeypatch.setattr(request_context, "_gateway_effective_learning_source", lambda *_: None)
    request = request_context.GatewayConversationLoadRequest(agent, {"conversation": {"thread_id": row.thread_id}}, "real-input", "hello", None)
    request_context.preflight_gateway_conversation(request)
    first = store.threads.require(row.thread_id)
    assert first.metadata.get("user_activity_generation") == 1
    request_context.preflight_gateway_conversation(request)
    assert store.threads.require(row.thread_id).metadata["user_activity_generation"] == 1


# LLM: 用真实Store并进入实际主回合prompt scope；仅替换无关技能装配，证明live/finalization不会重读新设置。
# 函数用途: 检查一次真实执行范围内比例被冻结，离开后恢复最新策略。
def test_main_turn_scope_freezes_thread_policy(conversation, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
    from agent_py_agent.agent.capability import method_carry

    store, row = conversation
    row = store.threads.update_atomic(row.thread_id, lambda old: replace(old, metadata={"settings": {"compact_trigger_percent": 72}}))
    agent = SimpleNamespace(config=AgentConfig(), conversation_store=store)
    monkeypatch.setattr(runtime_mixin, "_turn_skill_snapshot", lambda *_: None)
    monkeypatch.setattr(method_carry, "freeze_conversation_methods", lambda *_: None)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": row.thread_id})
    with runtime_mixin.current_prompt_scope(agent, "input", params):
        assert runtime_compact_policy(agent).trigger_percent == 72
        store.threads.update_atomic(row.thread_id, lambda old: replace(old, metadata={"settings": {"compact_trigger_percent": 85}}))
        assert runtime_compact_policy(agent).trigger_percent == 72
    assert runtime_compact_policy(agent, thread=store.threads.require(row.thread_id)).trigger_percent == 85


# LLM: 沿真实Host领域/owner检查/Store，模型运行时仅作配置依赖，不发模型或网络请求。
# 函数用途: 验证Host会话比例写入、生效字段和管理员403拒绝。
def test_host_thread_threshold_updates_and_admin_lock(tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.plugin_host import contracts
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_host.thread_settings_v2 import ThreadSettingsV2Handlers
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    owner = resolve_owner_home(tmp_path, OwnerIdentity.local_main())
    owner.home_dir.mkdir(parents=True)
    paths = home_paths_with_owner(home_paths(tmp_path), owner)
    store = ConversationStore(owner.data_dir / "conversations")
    row = store.threads.create(owner_id=owner.owner_id, owner_home=str(owner.home_dir))
    context = contracts.OwnerReadContext(owner.identity, owner, None)
    agent = SimpleNamespace(config=AgentConfig(), home_paths=paths)
    handler = ThreadSettingsV2Handlers(store, context, agent=agent, home_paths=paths)
    result = handler.update_thread_settings({"thread_id": row.thread_id, "compact_trigger_percent": 70})
    assert result.get("compact_trigger_percent") == 70
    assert result["effective_compact_trigger_percent"] == 70 and result["effective_compact_source"] == "thread"
    agent.config.force_compact_trigger_percent = True
    with pytest.raises(HostAPIError) as error:
        handler.update_thread_settings({"thread_id": row.thread_id, "compact_trigger_percent": None})
    assert error.value.code == "COMPACT_POLICY_LOCKED_BY_ADMIN" and error.value.status == 403


# LLM: 运行覆盖层无权修改全局强制，原base是唯一管理员配置来源；批量/全局治理另走宿主接口。
# 函数用途: 阻止owner运行配置解除管理员压缩锁。
def test_runtime_owner_layer_cannot_unlock_admin_compact_policy():
    from agent_py_agent.agent.settings.runtime_scope_config import (
        RuntimeConfigLayer,
        merge_runtime_config_layers,
    )

    config = AgentConfig(force_compact_trigger_percent=True)
    layer = RuntimeConfigLayer("owner", "test-owner-config", {"force_compact_trigger_percent": False})
    assert merge_runtime_config_layers(config, [layer]).values["force_compact_trigger_percent"] is True


# LLM: 使用真实线程目录和显式易失游标，测试每拍最多两个且后续能访问剩余线程；摘要边界不代表模型实测。
# 函数用途: 钉住维护吞吐和公平游标，避免只检查最新两条会话。
def test_idle_maintenance_is_bounded_and_round_robin(conversation, tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import compact_operations as idle

    tick = getattr(idle, "idle_compact_maintenance_tick", None)
    assert callable(tick)
    store, row = conversation
    extra = [store.threads.create(canonical_user_id="user") for _ in range(3)]
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    calls = []
    monkeypatch.setattr(idle, "maybe_auto_compact_idle_session", lambda _a, _s, t, **_: calls.append(t.thread_id) or {"compacted": False})
    cursor = {}
    tick(agent, cursor=cursor, now=10000.0)
    assert len(calls) == 2
    tick(agent, cursor=cursor, now=10060.0)
    assert len(calls) == 4 and set(calls) == {row.thread_id, *(r.thread_id for r in extra)}


@pytest.mark.parametrize("forced,expected,source", [(True, 80, "admin_forced"), (False, 94, "user")])
# LLM: 来源元数据必须跨多次叠加保留管理员原值，不能把owner比例冒充强制值或管理员默认。
# 函数用途: 复现独立评审指出的强制比例绕过，并核未强制时owner默认生效。
def test_owner_threshold_preserves_trusted_admin_default(forced, expected, source):
    from agent_py_agent.agent.conversation.compact_threshold_policy import (
        resolve_thread_compact_policy,
    )
    from agent_py_agent.agent.settings.runtime_scope_config import (
        RuntimeConfigLayer,
        merge_runtime_config_layers,
    )

    base = AgentConfig(force_compact_trigger_percent=forced)
    merged = merge_runtime_config_layers(base, [RuntimeConfigLayer("owner", "owner-config", {"memory_compact_trigger_percent": 94})])
    scoped = AgentConfig(**merged.values, config_sources=merged.sources)
    repeated = merge_runtime_config_layers(scoped, [RuntimeConfigLayer("task", "other", {"tool_read_max_chars": 2000})])
    policy = resolve_thread_compact_policy(SimpleNamespace(config=AgentConfig(**repeated.values, config_sources=repeated.sources)))
    assert (policy["effective_trigger_percent"], policy["effective_source"], policy["admin_default_percent"]) == (expected, source, 80)


@pytest.mark.parametrize("patch", [{"default_trigger_percent": True}, {"default_trigger_percent": 49},
    {"force_locked": "false"}, {"surprise": 80}])
# LLM: 非法嵌套字段应在Host原schema门拒绝，不进入幂等写口后变成未知500。
# 函数用途: 验证两个压缩写动作使用闭合严格类型的政策schema。
def test_compact_governance_host_schema_rejects_bad_patch(patch):
    from agent_py_agent.agent.plugin_host.action_specs import action_spec
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    for action in ("users.managed.compact_policy.set", "users.managed.compact_policy.batch_set"):
        with pytest.raises(HostAPIError, match="INVALID_SCHEMA"):
            action_spec(action).validate_payload({"policy_patch": patch})


# LLM: 新维护操作先取最新线程并冻结本次政策，模型摘要前后不重读后来设置；模型边界替身明确标记。
# 函数用途: 复现过期线程比例与维护摘要中途策略变化。
def test_idle_uses_latest_policy_and_freezes_summary(conversation, tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
    from agent_py_agent.agent.gateway_parts import compact_operations as idle

    store, stale = conversation
    latest = store.threads.update_atomic(stale.thread_id, lambda row: replace(row, updated_at=100.0,
        metadata={"settings": {"compact_trigger_percent": 70}, "last_compacted_user_activity_gen": 0,
                  "last_compacted_message_tail": store.messages.tail_marker_report(stale.thread_id)[0]}))
    agent = SimpleNamespace(config=AgentConfig(), home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    monkeypatch.setattr(idle, "idle_compact_busy_state", lambda *_: "")
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=750, context_window_tokens=1000))
    monkeypatch.setattr(idle, "prepare_conversation_context", lambda *_, **__: SimpleNamespace(compacted=False))
    assert idle.maybe_auto_compact_idle_session(agent, store, stale, now=7300.0)["reason"] == "regular_compacted"
    latest = store.threads.update_atomic(latest.thread_id, lambda row: replace(row, metadata={"last_user_activity_at": 100.0}))

    # LLM: 用量读取之后改变部署比例，摘要仍须消费操作开始时冻结的80%。
    # 函数用途: 固定维护预检和摘要之间的政策改变。
    def inspect(*_):
        agent.config.memory_compact_trigger_percent = 90
        return SimpleNamespace(projected_tokens=650, context_window_tokens=1000)

    # LLM: 读取真实冻结解析器，不以捕获的旧变量冒充运行策略。
    # 函数用途: 在摘要边界断言预检到摘要共享同一比例。
    def compact(*_, **__):
        assert runtime_compact_policy(agent, thread=latest).trigger_percent == 80
        return SimpleNamespace(compacted=False)

    monkeypatch.setattr(idle, "inspect_conversation_context", inspect)
    monkeypatch.setattr(idle, "prepare_conversation_context", compact)
    idle.maybe_auto_compact_idle_session(agent, store, latest, now=7300.0)


@pytest.mark.parametrize("current", [float("nan"), float("inf")])
# LLM: 测试显式now健壮性，不宣称生产时钟会返回NaN；非有限时刻不得绕过闲置时长。
# 函数用途: 验证维护时间失效时失败关闭。
def test_idle_rejects_nonfinite_now(conversation, tmp_path, monkeypatch, current):
    from agent_py_agent.agent.gateway_parts import compact_operations as idle

    store, row = conversation
    row = store.threads.record_user_activity(row.thread_id, now=100)
    agent = SimpleNamespace(config=AgentConfig(), home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    monkeypatch.setattr(idle, "idle_compact_busy_state", lambda *_: "")
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=650, context_window_tokens=1000))
    monkeypatch.setattr(idle, "prepare_conversation_context", lambda *_, **__: SimpleNamespace(compacted=True))
    assert idle.maybe_auto_compact_idle_session(agent, store, row, now=current)["reason"] == "activity_unknown"


# LLM: Host grant是原授权交集，all不能扩成全部用户；全局写必须明确通配权限，显式越权整批拒绝。
# 函数用途: 用真实受管用户记录核Host批量和全局授权而不启动HTTP服务器。
def test_host_compact_batch_scope_and_global_authority(governance):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_host.governance_delivery_v2_handlers import (
        call_governance_delivery,
        preflight_governance_delivery,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    service, _ = governance
    authorized = SimpleNamespace(binding=SimpleNamespace(grant={"manageable_user_ids": ["alice"]}))
    context = SimpleNamespace(identity=OwnerIdentity.local_main(), owner_home=SimpleNamespace(root=service.root_dir))
    result = call_governance_delivery("users.managed.compact_policy.batch_set", {
        "scope": "all_managed_users", "policy_patch": {"default_trigger_percent": 69}}, authorized, context)
    assert result.http_status == 200
    assert service.get_user_compact_policy("alice")["default_trigger_percent"] == 69
    assert service.get_user_compact_policy("bob")["revision"] == 0
    with pytest.raises(HostAPIError) as error:
        preflight_governance_delivery("settings.compact_policy.admin_force.set", {}, authorized, context)
    assert error.value.status == 403
    with pytest.raises(HostAPIError):
        call_governance_delivery("users.managed.compact_policy.batch_set", {
            "user_ids": ["alice", "bob"], "policy_patch": {"force_locked": True}}, authorized, context)
    assert service.get_user_compact_policy("alice")["revision"] == 1


# LLM: 原模型scope是Gateway预检到收口的外层边界；只替换后端依赖构造，不替换政策解析/线程存储。
# 函数用途: 核预检与主回合共享冻结比例，不因管理员中途更改而漂移。
def test_outer_model_scope_freezes_preflight_policy(conversation, tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
    from agent_py_agent.agent.settings import model_scope
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id

    store, row = conversation

    # LLM: 仅使用与真实Agent同一descriptor和真实Store，未装配模型网络依赖。
    # 类用途: 观察原模型依赖范围与压缩快照的嵌套。
    class Agent:
        config = model_scope.ModelScopedAttribute("config")

    agent = Agent()
    agent.config = AgentConfig()
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_home_dir=tmp_path, config_dir=tmp_path / "config")
    thread_model_profile_id(agent, row.thread_id)
    monkeypatch.setattr(model_scope, "selected_model_config", lambda a, **_: a.config)
    monkeypatch.setattr(model_scope, "prepare_model_dependencies", lambda a, c, **_: model_scope.ModelScopeDependencies(a, (("config", c),)))
    with model_scope.selected_model_scope(agent, thread_id=row.thread_id):
        assert runtime_compact_policy(agent, thread=row).trigger_percent == 80
        agent.config.memory_compact_trigger_percent = 90
        assert runtime_compact_policy(agent, thread=row).trigger_percent == 80


# LLM: 管理员强制是宿主管理边界，模型参数工具不能自助关闭；常用推荐必须指向真实新运行配置。
# 函数用途: 防止参数中心绕过强制，并钉住新比例的可发现性。
def test_compact_parameter_registry_authority_and_current_key():
    from agent_py_agent.agent.settings.parameter_registry import listed_parameters

    parameters = listed_parameters()
    assert not parameters["force_compact_trigger_percent"].writable
    assert parameters["memory_compact_trigger_percent"].common


@pytest.mark.parametrize("race", [False, True])
# LLM: 真实Agent/原消息库/原prepare/检查点/CAS全链，仅摘要provider为本地假后端、用量为固定观察，不证明真实供应商链路。
# 函数用途: 验证空闲options穿过原摘要实现且并发发言不会计失败或发布旧摘要。
def test_idle_original_summary_chain_and_user_race(tmp_path, monkeypatch, race):
    from agent_py_agent.agent.gateway_parts import compact_operations as idle
    from agent_py_agent.agent.gateway_parts.request_history import (
        append_gateway_conversation_message,
    )
    from agent_py_agent.tests.test_gateway_conversation_compact import (
        _agent,
        _context,
        _request,
        _SummaryBackend,
    )

    agent = _agent(tmp_path, context_tokens=1_000_000)
    context = _context(agent, _request(), "idle-create", "start")
    for index in range(3):
        for role in ("user", "assistant"):
            append_gateway_conversation_message(agent, {}, context, request_id=f"seed-{index}-{role}", role=role, content="需要保留的原文材料")
    store = agent.conversation_store
    row = store.threads.record_user_activity(context.thread_id, request_id="idle-last", now=100.0)
    before = store.storage.message_path(row.thread_id).read_bytes()
    backend = _SummaryBackend()
    generate = backend.generate

    # LLM: 只在原模型调用时插入实际Store活动变更，不替换commit或interrupt检查。
    # 函数用途: 固定用户在真实摘要期间插话的交错。
    def summary(*args, **kwargs):
        if race:
            store.threads.record_user_activity(row.thread_id, request_id="idle-racing-user", now=7400.0)
        return generate(*args, **kwargs)

    backend.generate = summary
    agent.backend = backend
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=650000, context_window_tokens=1000000))
    result = idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)
    latest = store.threads.require(row.thread_id)
    assert backend.prompts and result["compacted"] is (not race)
    assert latest.compact_generation == int(not race) and latest.compact_consecutive_failures == 0
    assert store.storage.message_path(row.thread_id).read_bytes() == before
    if race:
        assert result["reason"] == "activity_changed" and latest.summary == ""
        assert "last_idle_compacted_activity_gen" not in latest.metadata
    else:
        assert latest.summary and latest.metadata["last_idle_compacted_activity_gen"] == row.metadata["user_activity_generation"]


# LLM: 解析器和真实控制执行共享命令；只限定身份/线程定位，设置操作不得调用摘要链。
# 函数用途: 核CLI比例设置、default重置和锁定错误实际执行分支。
def test_compact_threshold_control_executes_without_summary(conversation, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import control_service as control

    store, row = conversation
    agent = SimpleNamespace(config=AgentConfig(), conversation_store=store)
    monkeypatch.setattr(control, "_request_agent_for_scope", lambda *_: agent)
    monkeypatch.setattr(control, "_conversation_thread_for_scope", lambda *_: store.threads.require(row.thread_id))
    scope = control.GatewayControlScope("user", "tui", "thread")
    for value, expected in (("71", 71), ("default", 80)):
        result = control._execute_compact_control(agent, None, parse_conversation_control("/compact threshold " + value), scope)
        assert result.ok and result.details["compact_policy"]["effective_trigger_percent"] == expected
    agent.config.force_compact_trigger_percent = True
    assert control._execute_compact_control(agent, None, parse_conversation_control("/compact threshold 75"), scope).error_code == "COMPACT_POLICY_LOCKED_BY_ADMIN"


@pytest.mark.parametrize("canonical,expected", [(False, 73), (True, 82)])
# LLM: 旧YAML只在未明确配置新键时迁移，新旧同时出现以新键为准，不改变本轮冻结对象。
# 函数用途: 防止现有部署压缩配置升级后被默默丢弃。
def test_legacy_compact_yaml_migration(tmp_path, canonical, expected):
    from agent_py_agent.agent.conversation.compact_threshold_policy import (
        resolve_thread_compact_policy,
    )
    from agent_py_agent.agent.settings.config import load_config

    path = tmp_path / "agent.yaml"
    path.write_text("memory_compact_auto_trigger_percent: 73\n" + ("memory_compact_trigger_percent: 82\n" if canonical else ""))
    config = load_config(path)
    assert resolve_thread_compact_policy(SimpleNamespace(config=config))["effective_trigger_percent"] == expected


# LLM: 无同owner执行器时仍须检查宿主管理配置；不能为读设置构造Agent，也不能借main执行器执行用户任务。
# 函数用途: 核Host冷管理设置实际消费部署强制而非默认force=False。
def test_cold_host_thread_setting_preserves_admin_force(tmp_path):
    import inspect

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.plugin_host.contracts import OwnerReadContext
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_host.gateway_composition import GatewayPortalDomain
    from agent_py_agent.agent.plugin_host.thread_settings_v2 import ThreadSettingsV2Handlers
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    assert "compact_config" in inspect.signature(ThreadSettingsV2Handlers).parameters
    owner = resolve_owner_home(tmp_path, OwnerIdentity.local_main())
    owner.home_dir.mkdir(parents=True)
    store = ConversationStore(owner.data_dir / "conversations")
    row = store.threads.create(owner_id=owner.owner_id, owner_home=str(owner.home_dir))
    scope = {"scope": "owner_all", "created_by_installation": True}
    binding = SimpleNamespace(installation_ref="cold-fixture", grant={"threads": scope})
    authorized = SimpleNamespace(binding=binding, resource_scope={"threads": scope},
                                 call=SimpleNamespace(action="conversations.threads.settings.update"))
    context = OwnerReadContext(owner.identity, owner, binding)
    resources = SimpleNamespace(server=SimpleNamespace(agent=SimpleNamespace(config=AgentConfig(force_compact_trigger_percent=True))),
                                store_for=lambda _: store)
    portal = GatewayPortalDomain.__new__(GatewayPortalDomain)
    portal.resources = resources
    with pytest.raises(HostAPIError) as error:
        portal._call_class(authorized, (ThreadSettingsV2Handlers, "update_thread_settings"), context,
                           {"thread_id": row.thread_id, "compact_trigger_percent": 72})
    assert error.value.status == 403 and error.value.code == "COMPACT_POLICY_LOCKED_BY_ADMIN"


# LLM: 命令和Host共用线程设置修订，不能让旧Host CAS在CLI设置后仍覆盖新偏好。
# 函数用途: 验证纯比例更新递增既有线程修订。
def test_threshold_snapshot_advances_settings_revision(conversation):
    from agent_py_agent.agent.conversation.compact_threshold_policy import (
        thread_with_compact_threshold,
    )
    from agent_py_agent.agent.plugin_host.conversations import thread_revision

    store, row = conversation
    agent = SimpleNamespace(config=AgentConfig())
    updated = store.threads.update_atomic(row.thread_id, lambda old: thread_with_compact_threshold(agent, old, 74))
    assert thread_revision(updated) > thread_revision(row)


# LLM: 同一活动代两个候选并发时失败的是旧候选，不是模型；原Store仍保留胜出检查点且拒绝缺失CAS输入。
# 函数用途: 钉住并发闲置幂等和必需的活动代参数。
def test_idle_commit_requires_generation_and_cleanly_loses_race(conversation):
    from agent_py_agent.agent.conversation.models import ConversationCompactCommit
    from agent_py_agent.agent.conversation.store_threads import IdleCompactActivityChanged

    store, row = conversation
    commit = ConversationCompactCommit(summary="summary", operation_evidence={}, checkpoint_id="winner",
        compacted_through_message_id="message", compacted_through_byte_offset=1, source_messages=1, source_tool_pairs=0)
    with pytest.raises(ValueError):
        store.threads.update_compact_state(row.thread_id, commit=commit, expected_generation=0, idle_compact=True)
    winner = store.threads.update_compact_state(row.thread_id, commit=commit, expected_generation=0,
        idle_compact=True, expected_user_activity_generation=0,
        expected_message_tail=store.messages.tail_marker_report(row.thread_id)[0])
    with pytest.raises(IdleCompactActivityChanged):
        store.threads.update_compact_state(row.thread_id, commit=commit, expected_generation=0,
            idle_compact=True, expected_user_activity_generation=0,
            expected_message_tail=store.messages.tail_marker_report(row.thread_id)[0])
    assert store.threads.require(row.thread_id).to_dict() == winner.to_dict()


# LLM: 真实维护controller/闲置入口/原摘要/CAS，只有本地provider和时钟观察为测试依赖；不启动服务或等待两小时。
# 函数用途: 防止功能只停留在可直接调用的helper而未接入实际Gateway维护拍。
def test_native_gateway_maintenance_tick_compacts_idle_thread(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import compact_operations as idle
    from agent_py_agent.agent.gateway_parts.request_history import (
        append_gateway_conversation_message,
    )
    from agent_py_agent.cli.gateway_loops import _GatewayOwnerMaintenanceController
    from agent_py_agent.tests.test_gateway_conversation_compact import (
        _agent,
        _context,
        _request,
        _SummaryBackend,
    )

    agent = _agent(tmp_path, context_tokens=1000000)
    agent.backend = _SummaryBackend()
    context = _context(agent, _request(), "tick-create", "start")
    for role in ("user", "assistant"):
        append_gateway_conversation_message(agent, {}, context, request_id="tick-seed-" + role, role=role, content="维护摘要的原始资料")
    row = agent.conversation_store.threads.record_user_activity(context.thread_id, now=100.0)
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=650000, context_window_tokens=1000000))
    controller = _GatewayOwnerMaintenanceController(SimpleNamespace(agent=agent))
    controller._startup_started_at = 0
    controller.tick(now=7400.0)
    updated = agent.conversation_store.threads.require(row.thread_id)
    assert updated.compact_generation == 1 and agent.backend.calls == 1
    assert updated.metadata["last_idle_compacted_activity_gen"] == row.metadata["user_activity_generation"]
