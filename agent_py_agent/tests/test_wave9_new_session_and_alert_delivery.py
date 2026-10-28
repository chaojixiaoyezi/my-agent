# LLM: Wave9 B3 从真实线程文件、调度账本和投递入口核对继任与通知授权，不连接真实 IM。
# 模块用途: 防止原地新会话丢历史、停止后台或把告警投递给未认证对象。
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import store_threads
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.store import ConversationStore


# LLM: 只在pytest临时owner内建原Store，不连接真实通道。
# 函数用途: 提供隔离会话账本。
@pytest.fixture
def conversations(tmp_path):
    return ConversationStore(tmp_path / "owner" / "conversations")


# LLM: 测试身份显式冻结到原ThreadStore，不能借实际用户目录。
# 函数用途: 创建指定owner和通道范围的合成线程。
def thread(store, *, channel="feishu", actor="ou_owner", chat="oc_private", owner="local/main"):
    return store.threads.get_or_create({"canonical_user_id": "owner", "owner_id": owner,
        "owner_home": str(store.storage.root.parent), "channel": channel,
        "channel_user_id": actor, "channel_conversation_id": chat,
        "cwd": str(store.storage.root.parent), "title": "历史会话"})


# LLM: 必须调用真实继任事务，缺实现时明确失败，不用替身模拟成功。
# 函数用途: 创建并读取一个真实空白后继。
def successor(store, original):
    create = getattr(store.threads, "succeed", None)
    assert callable(create), "线程存储必须提供原地继任事务"
    return create(original.thread_id, title="新会话")


# LLM: 只调共享领域解析器，避免测试自行重建一条能通过的链。
# 函数用途: 校验准确线程的后继解析结果。
def resolve(store, identifier, **kwargs):
    resolver = getattr(store_threads, "resolve_thread_successor_chain", None)
    assert callable(resolver), "必须由共享领域函数解析显式后继链"
    return resolver(store, identifier, **kwargs)


# LLM: 使用原解析器与CLI序列化，验证/new不是模型普通消息。
# 函数用途: 覆盖大小写、空标题及中文标题。
@pytest.mark.parametrize("text,title", [("/new", ""), ("/new 下一件事", "下一件事"), ("/NEW 中文 标题", "中文 标题")])
def test_new_is_shared_typed_control(text, title):
    command = parse_conversation_control(text)
    assert command is not None and command.valid and command.kind == "new"
    assert command.value == title
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text
    assert _command_text(command) == f"/new {title}".rstrip()


# LLM: 不扩大命令词匹配，不把普通正文或clear命令冒充/new。
# 函数用途: 防止误识别其它输入。
def test_new_is_not_ordinary_prose_or_clear_alias():
    assert parse_conversation_control("先 /new 再工作") is None
    assert parse_conversation_control("/clear") is None
    assert parse_conversation_control("/newspaper") is None


# LLM: 比较真实落盘前后事实，旧任务归属保留，新线程不得携带历史或运行权限。
# 函数用途: 验证目录、模型、权限、包偏好继承和旧历史归档。
def test_new_preserves_preferences_not_history_or_execution(conversations):
    old = thread(conversations)
    old = conversations.threads.update_atomic(old.thread_id, lambda row: replace(row,
        model_profile_id="provider-model", model_selection_revision=1,
        model_selection_source="explicit", model_selection_last_explicit_revision=1,
        metadata={"provider": "provider", "model": "model", "permission_mode": "auto", "packs": ["pack-a"],
                  "untrusted_runtime_authority": "must-not-copy"},
        summary="旧上下文", task_ids=("background-child",), active_task_ids=("background-child",)))
    conversations.messages.append({"thread_id": old.thread_id, "role": "user", "content": "旧原话"})
    new = successor(conversations, old)
    archived = conversations.threads.require(old.thread_id)
    assert new.thread_id != old.thread_id and new.status == "active" and archived.status == "archived"
    assert archived.active_task_ids == ("background-child",)
    assert archived.metadata["successor_thread_id"] == new.thread_id and archived.metadata["succeeded_at"] > 0
    assert new.metadata["predecessor_thread_id"] == old.thread_id
    assert new.workspace_root == old.workspace_root and new.cwd == old.cwd
    assert new.model_profile_id == old.model_profile_id
    for key in ("provider", "model", "permission_mode", "packs"):
        assert new.metadata[key] == old.metadata[key]
    assert "untrusted_runtime_authority" not in new.metadata
    assert not new.task_ids and not new.active_task_ids and not new.summary
    assert not conversations.messages.recent(new.thread_id)
    assert conversations.messages.recent(old.thread_id)[0].content == "旧原话"
    current = conversations.threads.resolve(channel="feishu", channel_user_id="ou_owner", channel_conversation_id="oc_private")
    assert current.thread_id == new.thread_id


# LLM: 一到五跳都由真实事务生成，不手写可绕过验证的目标选择。
# 函数用途: 验证ConversationStore和ThreadStore共享解析入口。
@pytest.mark.parametrize("hops", range(1, 6))
def test_same_scope_successor_chain(conversations, hops):
    first = current = thread(conversations)
    for _ in range(hops):
        current = successor(conversations, current)
    assert resolve(conversations, first.thread_id) == current.thread_id
    assert resolve(conversations.threads, first.thread_id) == current.thread_id


# LLM: 仅篡改临时记录，非法owner/home/三元组、环和缺失都必须拒绝。
# 函数用途: 逐项验证继任链安全边界。
@pytest.mark.parametrize("tamper", ["owner", "home", "channel", "actor", "chat", "cycle", "missing"])
def test_successor_chain_fails_closed(conversations, tamper):
    old = thread(conversations)
    new = successor(conversations, old)
    if tamper == "owner":
        conversations.threads.update_atomic(new.thread_id, lambda row: replace(row, owner_id="other"))
    elif tamper == "home":
        conversations.threads.update_atomic(new.thread_id, lambda row: replace(row, owner_home=row.owner_home + "-other"))
    elif tamper in {"channel", "actor", "chat"}:
        field = {"channel": "channel", "actor": "channel_user_id", "chat": "channel_conversation_id"}[tamper]
        conversations.threads.update_atomic(new.thread_id, lambda row: replace(row,
            channel_bindings=(replace(row.channel_bindings[0], **{field: "other"}),)))
    else:
        target = old.thread_id if tamper == "cycle" else "thread-missing"
        conversations.threads.update_atomic(new.thread_id, lambda row: replace(row,
            metadata={**row.metadata, "successor_thread_id": target}))
    with pytest.raises(ValueError):
        resolve(conversations, old.thread_id)


# LLM: 重试旧ID只返回既有后继，有限解析不能静默截断。
# 函数用途: 验证幂等与显式跳数上限。
def test_chain_hop_limit_and_repeated_old_reset(conversations):
    first = current = thread(conversations)
    for _ in range(3):
        current = successor(conversations, current)
    with pytest.raises(ValueError):
        resolve(conversations, first.thread_id, max_hops=2)
    assert successor(conversations, first).thread_id == current.thread_id


# LLM: 直接CLI使用真实控制分发，停止资源回调不得被/new调用。
# 函数用途: 验证新会话与停止命令互不混用。
def test_direct_new_never_falls_into_stop(conversations):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )
    original = thread(conversations, channel="chat", actor="local-agent", chat="terminal-session")
    agent = SimpleNamespace(conversation_store=conversations)
    execution = ChatControlExecution(agent, False, ChatControlState(False, 0, "", 0, "terminal-session"))
    result = execute_chat_control(execution, ConversationControlCommand("new"))
    assert result.ok and result.details["predecessor_thread_id"] == original.thread_id
    assert result.details["background_tasks_continued"] is True


# LLM: 忙碌回合拒绝前后对比原文件，不用文案代替零副作用证据。
# 函数用途: 验证前台运行时/new不改线程。
def test_direct_new_busy_is_read_only(conversations):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )
    original = thread(conversations, channel="chat", actor="local-agent", chat="terminal-session")
    before = conversations.storage.thread_path(original.thread_id).read_bytes()
    execution = ChatControlExecution(SimpleNamespace(conversation_store=conversations), False,
        ChatControlState(True, 0, "运行中", 0, "terminal-session", request_id="foreground"))
    result = execute_chat_control(execution, ConversationControlCommand("new"))
    assert not result.ok and result.error_code == "NEW_SESSION_BUSY"
    assert conversations.storage.thread_path(original.thread_id).read_bytes() == before


# LLM: 固定run的执行身份，只允许通知沿同通道后继选择。
# 函数用途: 验证通知跟随且执行来源不迁移。
def test_origin_delivery_follows_new_without_moving_execution(conversations):
    from dataclasses import asdict

    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.scheduler.execution import delivery_thread, validate_delivery_origin
    original = thread(conversations)
    run = {"run_id": "srun_test", "thread_id": "thread-execution", "owner": {"id": original.owner_id},
           "delivery": {"mode": "origin"}, "execution_context": {"origin_thread_id": original.thread_id,
               "origin_bindings": [asdict(original.channel_bindings[0])]}}
    repository = SimpleNamespace(get_active_run=lambda _: run, event_run_is_current=lambda _: True)
    agent = SimpleNamespace(conversation_store=conversations, scheduler_repository=repository)
    request = SimpleNamespace(task_id="srun_test", thread_id="thread-execution")
    new = successor(conversations, original)
    projected = delivery_thread(agent, original, request)
    assert projected.thread_id == new.thread_id
    context = DeliveryContext("feishu", "ou_owner", thread_id=new.thread_id)
    assert validate_delivery_origin(agent, request, context)
    assert not validate_delivery_origin(agent, request, replace(context, thread_id=original.thread_id))
    assert run["thread_id"] == "thread-execution" and run["execution_context"]["origin_thread_id"] == original.thread_id


# LLM: 一条线程共享不同通道时只切换本次准确绑定。
# 函数用途: 防止/new改写另一chat或通道的入口。
def test_reset_does_not_rebind_another_channel(conversations):
    original = thread(conversations)
    original = conversations.threads.bind_channel({"thread_id": original.thread_id, "canonical_user_id": "owner",
        "channel": "chat", "channel_user_id": "local-agent", "channel_conversation_id": "terminal"})
    new = conversations.threads.succeed(original.thread_id, binding=original.channel_bindings[-1]) if hasattr(
        conversations.threads, "succeed") else successor(conversations, original)
    assert new.thread_id != original.thread_id
    assert conversations.threads.resolve(channel="chat", channel_user_id="local-agent", channel_conversation_id="terminal").thread_id == new.thread_id
    assert conversations.threads.resolve(channel="feishu", channel_user_id="ou_owner", channel_conversation_id="oc_private").thread_id == original.thread_id


# LLM: 在真实创建与原指针发表之间观察可见性，不替换读取合同。
# 函数用途: 验证未发表候选不会被load/list暴露。
def test_new_candidate_not_visible_before_publication(conversations, monkeypatch):
    old = thread(conversations)
    writer = store_threads.create_thread_atomic
    observed = []
    # LLM: 仅在原创建完成后插入观察，不提前发表successor指针。
    # 函数用途: 捕获事务中间态的真实可见性。
    def write_then_observe(threads, row):
        result = writer(threads, row)
        observed.append((threads.load(row.thread_id), [item.thread_id for item in threads.list(limit=0)]))
        return result
    monkeypatch.setattr(store_threads, "create_thread_atomic", write_then_observe)
    new = successor(conversations, old)
    assert observed == [(None, [old.thread_id])]
    assert conversations.threads.require(new.thread_id).metadata["predecessor_thread_id"] == old.thread_id


# LLM: 只替换前台窗口查询，线程及新会话控制仍运行原实现。
# 函数用途: 验证Gateway拒绝前台忙碌而不回收后台。
def test_new_gateway_entry_rejects_foreground_only(conversations, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
    from agent_py_agent.agent.gateway_parts import control_service
    original = thread(conversations)
    agent = SimpleNamespace(conversation_store=conversations)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda *_: agent)
    monkeypatch.setattr(control_service, "_live_window_requests", lambda *_: [])
    entry = getattr(control_service, "_execute_gateway_new_session", None)
    assert callable(entry), "Gateway必须在停止或模型分支之前分派/new"
    scope = SimpleNamespace(channel="feishu", user_id="ou_owner", conversation_id="oc_private")
    result = entry(agent, None, ConversationControlCommand("new"), scope)
    assert result.ok and result.details["predecessor_thread_id"] == original.thread_id
    monkeypatch.setattr(control_service, "_live_window_requests", lambda *_: [object()])
    busy = entry(agent, None, ConversationControlCommand("new"), scope)
    assert not busy.ok and busy.error_code == "NEW_SESSION_BUSY"


# LLM: 通过原认证和schedule构造IM计划，模型/运输替身限临时目录。
# 函数用途: 提供同owner的飞书与本地双线目标。
@pytest.fixture
def alert_case(tmp_path):
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
    from agent_py_agent.tests.test_scheduler_im_l_integration import im_case
    bind_admin_channel_identity(tmp_path / "home", "feishu", "ou_a")
    case = im_case(tmp_path, delivery={"mode": "origin"})
    case.local = case.store.threads.get_or_create({"canonical_user_id": "local-agent", "channel": "chat",
        "channel_user_id": "local-agent", "channel_conversation_id": "terminal-alerts",
        "owner_id": case.agent.home_paths.owner_id, "owner_home": str(case.agent.home_paths.owner_home_dir)})
    return case


# LLM: 检查真实工具目录schema，不以内部函数测试替代可发现性。
# 函数用途: 验证五种通知方式及新动作可发现。
def test_delivery_policy_schema_is_exposed():
    from agent_py_agent.agent.scheduler.tool import build_schedule_tool_model_spec
    props = build_schedule_tool_model_spec().input_schema["properties"]
    assert "delivery_policy" in props
    assert {"multi", "bound", "none"} <= set(props["delivery_policy"]["properties"]["mode"]["enum"])
    assert "update_delivery" in props["action"]["enum"]


# LLM: 使用原认证绑定目录，不手工计算或伪造binding_id。
# 函数用途: 返回当前owner可选择的真实目标引用。
def delivery_targets(case):
    from agent_py_agent.agent.scheduler import delivery_policy
    reader = getattr(delivery_policy, "available_delivery_targets", None)
    assert callable(reader), "必须公开当前owner已验证的投递绑定，而不是让模型猜地址"
    return reader(case.agent)


# LLM: 原工具更新沿同一job和版本冲突合同，不创建第二计划。
# 函数用途: 验证旧三种模式的兼容更新和CAS。
@pytest.mark.parametrize("mode", ["origin", "local", "none"])
def test_delivery_policy_update_uses_original_job_cas(alert_case, mode):
    import json

    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    case = alert_case
    policy = {"mode": mode}
    result = ScheduleTool(case.agent).execute({"action": "update_delivery", "job_id": case.job["job_id"],
        "expected_version": case.job["version"], "delivery_policy": policy})
    assert result.ok, result.output
    saved = case.agent.scheduler_repository.get_job(case.job["job_id"])
    assert saved["delivery"]["mode"] == mode
    assert saved["source_thread_id"] == case.job["source_thread_id"]
    assert saved["version"] == case.job["version"] + 1
    assert json.loads(result.output)["job"]["delivery_policy"]["mode"] == mode


# LLM: 非法目标保留准确错误码，拒绝后原job版本不能前进。
# 函数用途: 验证任意或跨owner收件人无法持久化。
def test_unverified_delivery_target_returns_typed_error_without_writes(alert_case):
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    case = alert_case
    before = case.agent.scheduler_repository.store_path.read_bytes()
    outcome = ScheduleTool(case.agent).execute({"action": "update", "job_id": case.job["job_id"],
        "expected_version": case.job["version"], "delivery_policy": {"mode": "bound", "targets": [
            {"target_kind": "channel_binding", "binding_id": "foreign", "channel": "feishu", "chat_id": "another-owner"}]}})
    assert not outcome.ok and outcome.error_code == "SCHEDULE_DELIVERY_TARGET_UNAUTHORIZED"
    assert case.agent.scheduler_repository.store_path.read_bytes() == before


# LLM: 使用原预留运行冻结策略，不从wake或模型正文补权限。
# 函数用途: 验证已认证多线策略完整进入run快照。
def test_verified_multi_target_policy_survives_reservation(alert_case):
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    case = alert_case
    choices = delivery_targets(case)
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]} for row in choices]
    assert {row["channel"] for row in choices} == {"chat", "feishu"}
    outcome = ScheduleTool(case.agent).execute({"action": "update", "job_id": case.job["job_id"],
        "expected_version": case.job["version"], "delivery_policy": {"mode": "multi", "targets": refs,
            "include_origin": True}})
    assert outcome.ok, outcome.output
    run = case.agent.scheduler_repository.reserve_manual_run(case.job["job_id"])
    assert run["delivery"]["mode"] == "multi" and run["delivery"]["include_origin"] is True
    assert run["delivery"]["targets"] == refs


# LLM: 测试真实管理员注销重绑代次，旧引用不能借同名账号继续使用。
# 函数用途: 验证认证撤销即时使旧通知绑定失效。
def test_rebound_admin_identity_revokes_old_binding_reference(alert_case):
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    from agent_py_agent.agent.user_space.admin_channel_identity import (
        bind_admin_channel_identity,
        remove_admin_channel_identity,
    )
    case = alert_case
    bind_admin_channel_identity(case.agent.home_paths.root, "feishu", "ou_destination")
    case.store.threads.get_or_create({"canonical_user_id": "owner", "channel": "feishu",
        "channel_user_id": "ou_destination", "channel_conversation_id": "oc_destination",
        "owner_id": case.agent.home_paths.owner_id, "owner_home": str(case.agent.home_paths.owner_home_dir)})
    selected = next(row for row in delivery_targets(case) if row["account_id"] == "ou_destination")
    remove_admin_channel_identity(case.agent.home_paths.root, "feishu", "ou_destination")
    result = ScheduleTool(case.agent).execute({"action": "update_delivery", "job_id": case.job["job_id"],
        "expected_version": case.job["version"], "delivery_policy": {"mode": "bound", "targets": [
            {"target_kind": "channel_binding", "binding_id": selected["binding_id"]}]}})
    assert not result.ok and result.error_code == "SCHEDULE_DELIVERY_TARGET_UNAUTHORIZED"


# LLM: 偏好从真实plugin_host_v2位置读取，旧安装来源保留但旧回执不能复制。
# 函数用途: 验证Web权限和能力包偏好继承。
def test_actual_portal_preferences_are_inherited_without_old_receipts(conversations):
    original = thread(conversations)
    original = conversations.threads.update_atomic(original.thread_id, lambda row: replace(row,
        metadata={"plugin_host_v2": {"revision": 1, "preferred_pack_ids": ["pack-a"], "permission_mode": "auto",
            "installation_ref": "installation", "workspace_ref": "default", "receipts": {"old": "old-receipt"}}}))
    new = successor(conversations, original)
    state = new.metadata.get("plugin_host_v2", {})
    assert state.get("preferred_pack_ids") == ["pack-a"] and state.get("permission_mode") == "auto"
    assert state.get("installation_ref") == "installation" and not state.get("receipts")


# LLM: 运行真实显示事件/reducer，不把清屏当取消业务或切换chat。
# 函数用途: 验证历史清空而终端通道身份不变。
def test_tui_reset_clears_view_not_session_binding():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    runtime = TuiRuntime("terminal-session")
    runtime.write_console("旧可见历史")
    runtime.background_message_cursor = 500
    reset = getattr(runtime, "publish_conversation_reset", None)
    assert callable(reset), "确认/new后必须清空TUI视图和旧历史游标"
    reset("thread-new-session")
    assert runtime.session_id == "terminal-session", "同通道UI身份不能换成另一个chat"
    assert not runtime.store.snapshot().stable_blocks and runtime.background_message_cursor == 0


# LLM: 同时检查中央动作schema和真实领域路由，不把目录声明当完整接线。
# 函数用途: 验证Web新会话及通知更新入口可达领域。
def test_web_new_action_is_really_routed():
    from agent_py_agent.agent.plugin_host.action_specs import action_spec
    from agent_py_agent.agent.plugin_host.gateway_composition import portal_target
    assert action_spec("conversations.threads.new") is not None
    assert portal_target("conversations.threads.new") is not None
    assert action_spec("monitors.delivery.update") is not None
    assert portal_target("monitors.delivery.update") is not None


# LLM: 先用原工具授权更新，再经原服务预留唤醒，保持执行身份真实。
# 函数用途: 为投递测试准备冻结策略和后台请求。
def prepared_alert(case, policy):
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    result = ScheduleTool(case.agent).execute({"action": "update_delivery", "job_id": case.job["job_id"],
        "expected_version": case.job["version"], "delivery_policy": policy})
    assert result.ok, result.output
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    return signal, BackgroundRunRequest(thread_id=signal.thread_id, task_id=signal.root_task_id,
        reason=signal.reason, wake_signal=signal.to_dict())


# LLM: R2允许其它已登记通道独立继任，但不允许它返回第一条通道后继，未登记绑定仍拒绝。
# 函数用途: 验证按绑定重放隔离与兼容默认指针。
def test_replayed_reset_cannot_switch_a_different_binding(conversations):
    old = thread(conversations)
    first = old.channel_bindings[0]
    other = replace(first, channel="chat", channel_user_id="terminal", channel_conversation_id="terminal")
    conversations.threads.update_atomic(old.thread_id, lambda row: replace(row, channel_bindings=(first, other)))
    new = conversations.threads.succeed(old.thread_id, binding=first)
    separate = conversations.threads.succeed(old.thread_id, binding=other)
    assert separate.thread_id != new.thread_id
    with pytest.raises(ValueError):
        conversations.threads.succeed(old.thread_id, binding=replace(other, channel_user_id="unbound"))
    assert resolve(conversations, old.thread_id) == new.thread_id


# LLM: 未登记binding在创建任何新线程前拒绝，不能先归档再发现非法链。
# 函数用途: 防止无绑定Web线程被注入IM地址。
def test_unbound_thread_rejects_injected_binding_before_publication(conversations):
    old = thread(conversations)
    binding = old.channel_bindings[0]
    conversations.threads.update_atomic(old.thread_id, lambda row: replace(row, channel_bindings=()))
    with pytest.raises(ValueError):
        conversations.threads.succeed(old.thread_id, binding=binding)
    assert conversations.threads.require(old.thread_id).status == "active"
    assert not conversations.threads.require(old.thread_id).metadata.get("successor_thread_id")


# LLM: 合成ID碰撞只发生在临时目录，排他创建失败不能删除既有文件。
# 函数用途: 验证失败清理只作用于本次确已创建的候选。
def test_create_collision_does_not_delete_existing_thread(conversations, monkeypatch):
    old = thread(conversations)
    victim = thread(conversations, actor="other", chat="other")
    monkeypatch.setattr(store_threads, "new_id", lambda _: victim.thread_id)
    with pytest.raises(FileExistsError):
        successor(conversations, old)
    assert conversations.threads.load(victim.thread_id) is not None
    assert conversations.threads.require(old.thread_id).status == "active"


# LLM: 在真实原子updater入口注入身份漂移，锁前快照不能替代锁内核验。
# 函数用途: 验证发表前重核最新owner。
def test_reset_rechecks_latest_identity_inside_publication_lock(conversations, monkeypatch):
    old = thread(conversations)
    update = conversations.threads.update_atomic
    monkeypatch.setattr(conversations.threads, "update_atomic", lambda identifier, updater:
        update(identifier, lambda latest: updater(replace(latest, owner_id="other-owner"))))
    with pytest.raises(ValueError):
        successor(conversations, old)
    assert conversations.threads.require(old.thread_id).status == "active"


# LLM: 幂等重试不免除原owner/范围/CAS验证，不能沿提前返回绕过调用者约束。
# 函数用途: 验证原ID重放仍执行锁内验证器。
def test_repeated_new_still_checks_current_authorization(conversations):
    old = thread(conversations)
    successor(conversations, old)
    # LLM: 只注入调用者锁内撤权检查，真实幂等分支必须调用它。
    # 函数用途: 稳定识别验证器被提前返回绕过。
    def reject(_latest):
        raise ValueError("CURRENT_AUTHORIZATION_REVOKED")
    with pytest.raises(ValueError, match="CURRENT_AUTHORIZATION_REVOKED"):
        conversations.threads.succeed(old.thread_id, validate=reject)


# LLM: 真实指针已发表后只让索引写失败；重复原ID必须幂等补索引。
# 函数用途: 验证发表后故障可恢复且不会另建分叉。
def test_reset_retry_repairs_binding_indexes(conversations, monkeypatch):
    old = thread(conversations)
    write = conversations.threads._write_binding_indexes
    monkeypatch.setattr(conversations.threads, "_write_binding_indexes", lambda *_: (_ for _ in ()).throw(OSError("index unavailable")))
    with pytest.raises(OSError):
        successor(conversations, old)
    new_id = conversations.threads.require(old.thread_id).metadata["successor_thread_id"]
    calls = []
    monkeypatch.setattr(conversations.threads, "_write_binding_indexes", lambda binding: (calls.append(binding.thread_id), write(binding)))
    assert successor(conversations, old).thread_id == new_id
    assert calls == [new_id]


# LLM: 原sender和Store真实，只有IM运输替身，不向实际用户发送通知。
# 函数用途: 验证双线逐目标回执、本地留档和include_origin去重。
@pytest.mark.parametrize("mode", ["bound", "multi"])
def test_real_delivery_records_every_target_and_deduplicates_origin(alert_case, mode):
    from agent_py_agent.agent.conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        record_background_response,
    )
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery
    case = alert_case
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]} for row in delivery_targets(case)]
    signal, request = prepared_alert(case, {"mode": mode, "targets": refs, "include_origin": True})
    channels = _RetryDelivery()
    channels.status = "sent"
    commit = record_background_response(BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "completed"),
        request, DeliveryContext("feishu", "ou_a", thread_id=case.thread.thread_id, task_id=request.task_id),
        "合成双线告警", deliver=True, delivery_reason="scheduled_job_due")
    assert commit.persisted and commit.delivery_status == "sent", case.agent.scheduler_repository.delivery_receipts(signal.root_task_id)
    assert len(channels.attempts) == 1, "include_origin与相同绑定应去重，本地留档不调用IM"
    assert any(row.content == "合成双线告警" for row in case.store.messages.recent(case.local.thread_id))
    reader = getattr(case.agent.scheduler_repository, "delivery_receipts", None)
    assert callable(reader), "原调度outbox必须保留每个目标的独立回执"
    receipts = reader(signal.root_task_id)
    assert len(receipts) == 2 and all(row["state"] == "committed" for row in receipts.values())
    assert {row["delivery_status"] for row in receipts.values()} == {"sent", "local_only"}


# LLM: 部分成功来自持久outbox，重试不得再次提交已完成本地路线。
# 函数用途: 验证仅重投失败的IM目标。
def test_partial_multiline_retry_never_resends_committed_target(alert_case):
    from agent_py_agent.agent.conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        frozen_owner_delivery,
        record_background_response,
    )
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery
    case = alert_case
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]} for row in delivery_targets(case)]
    signal, request = prepared_alert(case, {"mode": "multi", "targets": refs})
    channels = _RetryDelivery()
    dependencies = BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "completed")
    context = DeliveryContext("feishu", "ou_a", thread_id=case.thread.thread_id, task_id=request.task_id)
    first = record_background_response(dependencies, request, context, "等待单线重投", deliver=True, delivery_reason="scheduled_job_due")
    assert first.delivery_status != "sent" and first.outbox_frozen
    assert len(case.store.messages.recent(case.local.thread_id)) == 1
    cached = case.store.wakes.pending_one(signal.wake_signal_id).metadata["owner_delivery"]
    channels.status = "sent"
    second = record_background_response(dependencies, request, context, cached["content"], deliver=True,
        delivery_reason="cached_owner_delivery_retry", frozen_retry=frozen_owner_delivery(cached))
    assert second.delivery_status == "sent" and second.persisted
    assert len(case.store.messages.recent(case.local.thread_id)) == 1
    assert len(channels.attempts) == 2


# LLM: 真实后台调度器运行并按原30秒退避重试，模型及网络用可计数替身。
# 函数用途: 证明完成业务后只补投递、不再次执行模型。
def test_real_scheduler_multiline_retry_after_completed_task_does_not_rerun_model(alert_case):
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery
    case = alert_case
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]} for row in delivery_targets(case)]
    signal, _request = prepared_alert(case, {"mode": "multi", "targets": refs})
    channels = _RetryDelivery()
    runtime = BackgroundMainAgentRuntime(agent=case.agent, store=case.store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": case.store, "scheduler_service": case.agent.scheduler_service})
    scheduler.tick(now=case.job["next_run_at"])
    assert len(case.backend.prompts) == 1 and len(channels.attempts) == 1
    assert len(case.store.messages.recent(case.local.thread_id)) == 1
    assert case.store.wakes.pending_one(signal.wake_signal_id).metadata["owner_delivery"]
    channels.status = "sent"
    scheduler.tick(now=case.job["next_run_at"] + 31)
    assert len(case.backend.prompts) == 1 and len(channels.attempts) == 2
    assert len(case.store.messages.recent(case.local.thread_id)) == 1
    assert case.store.wakes.pending_one(signal.wake_signal_id) is None


# LLM: 预留后再真实/new，运输上下文和历史来源分别核验。
# 函数用途: 验证真实调度通知跟随后继而执行编号不变。
def test_real_scheduler_origin_follows_new_with_original_execution_identity(alert_case):
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery
    case = alert_case
    signal, _request = prepared_alert(case, {"mode": "origin"})
    before = case.agent.scheduler_repository.get_active_run(signal.root_task_id)
    new = successor(case.store, case.thread)
    channels = _RetryDelivery()
    channels.status = "sent"
    scheduler = BackgroundMainAgentScheduler({"runtime": BackgroundMainAgentRuntime(agent=case.agent, store=case.store, channels=channels),
        "store": case.store, "scheduler_service": case.agent.scheduler_service})
    scheduler.tick(now=case.job["next_run_at"])
    assert len(channels.attempts) == 1 and channels.attempts[0][0].thread_id == new.thread_id
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]["thread_id"] == before["thread_id"]
    assert history[0]["origin_thread_id"] == case.thread.thread_id


# LLM: 直接领域合同不冒充HTTP认证验收，真实owner/CAS由Handler核验。
# 函数用途: 验证Web新会话成功及旧修订拒绝。
def test_web_domain_new_carries_preferences_and_uses_cas(alert_case):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_host.thread_settings_v2 import (
        ThreadSettingsV2Handlers,
        _effective_revision,
    )
    case = alert_case
    context = SimpleNamespace(owner_home=SimpleNamespace(owner_id=case.agent.home_paths.owner_id,
        home_dir=case.agent.home_paths.owner_home_dir))
    handler = ThreadSettingsV2Handlers(case.store, context, home_paths=case.agent.home_paths)
    old = case.thread
    with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
        handler.new_thread({"thread_id": old.thread_id, "expected_revision": _effective_revision(old) + 1})
    assert case.store.threads.require(old.thread_id).status == "active"
    result = handler.new_thread({"thread_id": old.thread_id, "expected_revision": _effective_revision(old), "title": "Web新会话"})
    assert result["view_reset"] and result["background_tasks_continued"]
    assert result["thread"]["workspace_root"] == old.workspace_root


# LLM: Web沿原schedule工具和认证owner，不创建替代执行器。
# 函数用途: 验证通知控制使用同一仓库且跨owner拒绝。
def test_web_delivery_domain_uses_original_repository_and_rejects_other_owner(alert_case):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_host.monitor_delivery_v2 import delivery_targets as web_targets
    from agent_py_agent.agent.plugin_host.monitor_delivery_v2 import update_delivery
    case = alert_case
    context = SimpleNamespace(owner_home=SimpleNamespace(owner_id=case.agent.home_paths.owner_id,
        home_dir=case.agent.home_paths.owner_home_dir))
    assert len(web_targets(context, {}, agent=case.agent)["delivery_targets"]) == 2
    updated = update_delivery(context, {"monitor_id": case.job["job_id"], "expected_version": case.job["version"],
        "delivery_policy": {"mode": "none"}}, agent=case.agent)
    assert updated["job"]["delivery_policy"] == {"mode": "none"}
    context.owner_home.owner_id = "other"
    with pytest.raises(HostAPIError, match="HOST_NOT_READY"):
        web_targets(context, {}, agent=case.agent)


# LLM: 第二条规则的非法目标也须拒绝，不能只校验公共默认或第一条规则。
# 函数用途: 防止规则覆盖绕过通知授权。
def test_rule_override_cannot_store_unverified_target(alert_case):
    from agent_py_agent.agent.scheduler.delivery_policy import ScheduleDeliveryTargetUnauthorized
    case = alert_case
    rule = {"id": "rule1", "name": "检查", "schedule": case.job["schedule"], "use_llm": True, "prompt": "合成检查",
        "delivery": {"mode": "bound", "targets": [{"target_kind": "channel_binding", "binding_id": "foreign-binding"}]}}
    with pytest.raises(ScheduleDeliveryTargetUnauthorized):
        case.agent.scheduler_repository.update_job(case.job["job_id"], patch={"rules": [
            {**rule, "id": "safe-rule", "delivery": {"mode": "origin"}}, rule]}, expected_version=case.job["version"])
    assert case.agent.scheduler_repository.get_job(case.job["job_id"])["version"] == case.job["version"]


# LLM: 明确整体通知更新可清除旧覆盖，但不得重置规则运行时统计或周期。
# 函数用途: 验证监控全部规则采用新通知方式。
def test_update_delivery_switches_all_rules_without_resetting_execution_state(alert_case):
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    case = alert_case
    rule = {"id": "rule1", "name": "检查", "schedule": case.job["schedule"], "use_llm": True,
        "prompt": "合成检查", "delivery": {"mode": "origin"}}
    monitor = case.agent.scheduler_repository.update_job(case.job["job_id"], patch={"rules": [rule]}, expected_version=case.job["version"])
    outcome = ScheduleTool(case.agent).execute({"action": "update_delivery", "job_id": monitor["job_id"],
        "expected_version": monitor["version"], "delivery_policy": {"mode": "local"}})
    assert outcome.ok, outcome.output
    updated = case.agent.scheduler_repository.get_job(monitor["job_id"])
    assert all((row.get("delivery") or updated["delivery"])["mode"] == "local" for row in updated["rules"])
    assert updated["rules"][0]["next_run_at"] == monitor["rules"][0]["next_run_at"]


# LLM: 原claim账真实领取，前台拒绝与后台保留不能用替身状态证明。
# 函数用途: 验证/new不停止活跃后台租约。
@pytest.mark.parametrize("foreground", [True, False])
def test_new_rejects_foreground_claim_but_keeps_background_claim(conversations, foreground):
    old = thread(conversations)
    claim = conversations.claims.acquire({"thread_id": old.thread_id, "task_id": "foreground" if foreground else "background",
        "reason": "request", "recover_same_task_only": foreground})
    assert claim is not None
    if foreground:
        with pytest.raises(ValueError, match="NEW_SESSION_BUSY"):
            successor(conversations, old)
        assert conversations.threads.require(old.thread_id).status == "active"
    else:
        assert successor(conversations, old).thread_id != old.thread_id
    assert conversations.claims.load(old.thread_id)["claim_id"] == claim["claim_id"]
    assert conversations.claims.load(old.thread_id)["status"] == "running"


# LLM: 完整生成16及17跳边界，超过上限不得返回截断目标。
# 函数用途: 验证继任链硬上限。
def test_successor_chain_has_exact_sixteen_hop_boundary(conversations):
    old = current = thread(conversations)
    for _ in range(16):
        current = successor(conversations, current)
    assert resolve(conversations, old.thread_id) == current.thread_id
    successor(conversations, current)
    with pytest.raises(ValueError, match="HOP_LIMIT"):
        resolve(conversations, old.thread_id)


# LLM: 仅注入canonical写失败，真实outbox、冻结信封和渠道回执仍按原顺序运行。
# 函数用途: 验证本地优先及外发优先两种失败顺序均不丢结果、不重复外发。
@pytest.mark.parametrize("fail_local", [True, False])
def test_target_canonical_failure_keeps_complete_retry_and_never_resends_sent_route(alert_case, monkeypatch, fail_local):
    from agent_py_agent.agent.conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        frozen_owner_delivery,
        record_background_response,
    )
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery
    case = alert_case
    selected = sorted(delivery_targets(case), key=lambda row: row["local"] != fail_local)
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]} for row in selected]
    signal, request = prepared_alert(case, {"mode": "multi", "targets": refs})
    channels = _RetryDelivery()
    channels.status = "sent"
    dependencies = BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "completed")
    context = DeliveryContext("feishu", "ou_a", thread_id=case.thread.thread_id, task_id=request.task_id)
    append = case.store.messages.append_once
    failed_thread = case.local.thread_id if fail_local else case.thread.thread_id
    # LLM: 只阻断指定线程写入，恢复后仍调用原append_once去重。
    # 函数用途: 稳定复现运输与canonical之间的失败窗口。
    def failing_append(params, **kwargs):
        if params["thread_id"] == failed_thread:
            raise OSError("canonical unavailable")
        return append(params, **kwargs)
    monkeypatch.setattr(case.store.messages, "append_once", failing_append)
    # 单目标错误现在隔离成结构化欠账；原不丢结果/不重发断言保留，并要求健康目标本次就完成。
    first = record_background_response(dependencies, request, context, "故障后完整重投", deliver=True, delivery_reason="scheduled_job_due")
    assert not first.persisted and first.delivery_status == "failed" and first.outbox_frozen
    healthy_thread = case.thread.thread_id if fail_local else case.local.thread_id
    assert len(case.store.messages.recent(healthy_thread)) == 1
    cached = case.store.wakes.pending_one(signal.wake_signal_id).metadata.get("owner_delivery")
    assert cached and cached["content"] == "故障后完整重投", "本地canonical失败前也必须冻结整封结果，不能丢掉已生成回复"
    monkeypatch.setattr(case.store.messages, "append_once", append)
    result = record_background_response(dependencies, request, context, cached["content"], deliver=True,
        delivery_reason="cached_owner_delivery_retry", frozen_retry=frozen_owner_delivery(cached))
    assert result.persisted and result.delivery_status == "sent"
    assert len(channels.attempts) == 1, "原外发已有回执时只能补canonical，不能再次发送"
    assert len(case.store.messages.recent(case.local.thread_id)) == 1


# LLM: 第二个远端也先经过真实管理员绑定，只在临时owner建立，不使用任意收件人。
# 函数用途: 建立可测试部分成功的两个已认证远端。
def second_alert_remote(case):
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
    bind_admin_channel_identity(case.agent.home_paths.root, "feishu", "ou_b")
    case.store.threads.get_or_create({"canonical_user_id": "owner", "channel": "feishu", "channel_user_id": "ou_b",
        "channel_conversation_id": "oc_b", "owner_id": case.agent.home_paths.owner_id,
        "owner_home": str(case.agent.home_paths.owner_home_dir)})


# LLM: 只封装原领域调用及运输替身，不自行生成回执或替换outbox。
# 函数用途: 准备多目标故障测试的真实投递调用。
def alert_attempt(case, selected):
    from agent_py_agent.agent.conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        record_background_response,
    )
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]} for row in selected]
    signal, request = prepared_alert(case, {"mode": "multi", "targets": refs})
    channels = _RetryDelivery()
    channels.status = "sent"
    dependencies = BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "completed")
    context = DeliveryContext("feishu", "ou_a", thread_id=case.thread.thread_id, task_id=request.task_id)
    # LLM: 所有写入仍经过原公开sender，仅简化重复参数。
    # 函数用途: 执行一次同run的投递或冻结重试。
    def attempt(body="原始业务结果", **kwargs):
        return record_background_response(dependencies, request, context, body, deliver=True,
            delivery_reason="scheduled_job_due", **kwargs)
    return SimpleNamespace(signal=signal, request=request, channels=channels, attempt=attempt, context=context)


# LLM: 真实远端A已成功、B失败，local_only不能消除B的外发义务。
# 函数用途: 防止部分成功被仅本地留档升级成全量sent。
def test_remote_local_only_cannot_settle_external_debt(alert_case, monkeypatch):
    case = alert_case
    second_alert_remote(case)
    flow = alert_attempt(case, sorted((row for row in delivery_targets(case) if not row["local"]), key=lambda row: row["account_id"]))
    deliver = flow.channels.deliver
    # LLM: 运输替身按准确目标返回结果，不修改调度授权或canonical。
    # 函数用途: 制造一条成功一条失败。
    def partial(context, envelope):
        flow.channels.status = "sent" if context.target == "ou_a" else "failed"
        return deliver(context, envelope)
    monkeypatch.setattr(flow.channels, "deliver", partial)
    assert not flow.attempt().persisted
    result = flow.attempt(local_transcript_only=True)
    assert not result.persisted and result.delivery_status == "failed"
    receipts = case.agent.scheduler_repository.delivery_receipts(flow.signal.root_task_id)
    assert sum(row["state"] == "committed" for row in receipts.values()) == 1


# LLM: 本地目标确实提交canonical，但不能对外报告external_delivery。
# 函数用途: 核对全本地计划的提交分类。
def test_all_local_targets_are_not_external_delivery(alert_case):
    flow = alert_attempt(alert_case, [row for row in delivery_targets(alert_case) if row["local"]])
    result = flow.attempt()
    assert result.persisted and result.delivery_status == "local_only"
    assert result.commit_kind != "external_delivery" and not flow.channels.attempts


# LLM: 故障只注入指定canonical线程，允许测试旧路径抛异常和新路径结构化未完成。
# 函数用途: 保留原append并产生确定的单目标磁盘失败。
def block_alert_canonical(case, monkeypatch, thread_id):
    append = case.store.messages.append_once
    # LLM: 不阻断其它健康线程或原job outbox。
    # 函数用途: 模拟某一路历史暂不可写。
    def blocked(params, **kwargs):
        if params["thread_id"] == thread_id:
            raise OSError("target canonical unavailable")
        return append(params, **kwargs)
    monkeypatch.setattr(case.store.messages, "append_once", blocked)
    return append


# LLM: 已发送回执只能补原canonical；撤销绑定后绝不能新发，也不应阻止历史补账。
# 函数用途: 验证发送后撤权仍可无网络补档。
def test_sent_target_repairs_canonical_after_binding_revocation(alert_case, monkeypatch):
    from agent_py_agent.agent.conversation.background_delivery import frozen_owner_delivery
    from agent_py_agent.agent.user_space.admin_channel_identity import remove_admin_channel_identity
    case = alert_case
    flow = alert_attempt(case, [row for row in delivery_targets(case) if not row["local"]])
    append = block_alert_canonical(case, monkeypatch, case.thread.thread_id)
    try:
        first = flow.attempt()
        assert not first.persisted
    except OSError:
        pass
    cached = case.store.wakes.pending_one(flow.signal.wake_signal_id).metadata["owner_delivery"]
    assert len(flow.channels.attempts) == 1 and not case.store.messages.recent(case.thread.thread_id)
    remove_admin_channel_identity(case.agent.home_paths.root, "feishu", "ou_a")
    monkeypatch.setattr(case.store.messages, "append_once", append)
    result = flow.attempt(cached["content"], frozen_retry=frozen_owner_delivery(cached))
    assert result.persisted and result.delivery_status == "sent"
    assert len(flow.channels.attempts) == 1 and len(case.store.messages.recent(case.thread.thread_id)) == 1


# LLM: A的运输回执可脱敏正文，B必须从不可变原结果投影，不能复用A的目标freeze。
# 函数用途: 检查跨目标冻结内容污染及局部异常隔离。
def test_target_freeze_never_becomes_another_targets_business_input(alert_case, monkeypatch):
    from agent_py_agent.agent.conversation.background_delivery import frozen_owner_delivery
    case = alert_case
    flow = alert_attempt(case, sorted(delivery_targets(case), key=lambda row: row["local"]))
    deliver = flow.channels.deliver
    monkeypatch.setattr(flow.channels, "deliver", lambda context, envelope: replace(deliver(context, envelope), content="仅A的目标投影"))
    append = block_alert_canonical(case, monkeypatch, case.thread.thread_id)
    try:
        flow.attempt("不可变原始结果")
    except OSError:
        pass
    cached = case.store.wakes.pending_one(flow.signal.wake_signal_id).metadata["owner_delivery"]
    monkeypatch.setattr(case.store.messages, "append_once", append)
    result = flow.attempt(cached["content"], frozen_retry=frozen_owner_delivery(cached))
    assert result.persisted and len(flow.channels.attempts) == 1
    assert [row.content for row in case.store.messages.recent(case.local.thread_id)] == ["不可变原始结果"]
    assert [row.content for row in case.store.messages.recent(case.thread.thread_id)] == ["仅A的目标投影"]


# LLM: 一个目标持续canonical失败不能饿死其余已授权路线，完成状态仍必须失败。
# 函数用途: 验证同一次调用就推进健康目标，不等待失败路线恢复。
def test_persistent_target_failure_does_not_starve_healthy_target(alert_case, monkeypatch):
    case = alert_case
    flow = alert_attempt(case, sorted(delivery_targets(case), key=lambda row: row["local"]))
    block_alert_canonical(case, monkeypatch, case.thread.thread_id)
    try:
        result = flow.attempt()
        assert not result.persisted and result.delivery_status == "failed"
    except OSError:
        pass
    assert [row.content for row in case.store.messages.recent(case.local.thread_id)] == ["原始业务结果"]
    assert len(flow.channels.attempts) == 1


# LLM: 主动告警必须清空其它目标的回复/进度句柄，原task身份保持不变。
# 函数用途: 验证完整DeliveryContext不会串用旧消息路由。
def test_bound_target_rebuilds_proactive_transport_context(alert_case):
    from agent_py_agent.agent.scheduler.alert_delivery import alert_delivery_targets
    case = alert_case
    second_alert_remote(case)
    flow = alert_attempt(case, [row for row in delivery_targets(case) if row["account_id"] == "ou_b"])
    context = replace(flow.context, mode="reply", conversation_id="old-chat", reply_to="old-message", progress_handle="old-progress")
    run = case.agent.scheduler_repository.get_active_run(flow.signal.root_task_id)
    [(target, local)] = alert_delivery_targets(case.agent, flow.request, context, run).values()
    assert not local and target.target == "ou_b" and target.task_id == flow.request.task_id
    assert target.mode == "proactive" and target.conversation_id == "oc_b"
    assert not target.reply_to and not target.progress_handle


# LLM: 持久sent回执不授权写入现已属于另一owner的线程。
# 函数用途: 验证补档同样检查当前canonical owner/home。
def test_admitted_sent_receipt_cannot_write_foreign_owner_thread(alert_case):
    from agent_py_agent.agent.scheduler.alert_delivery import validate_alert_delivery
    case = alert_case
    flow = alert_attempt(case, [row for row in delivery_targets(case) if not row["local"]])
    assert flow.attempt().persisted
    run = case.agent.scheduler_repository.get_active_run(flow.signal.root_task_id)
    case.store.threads.update_atomic(case.thread.thread_id, lambda row: replace(row, owner_id="foreign-owner"))
    assert not validate_alert_delivery(case.agent, flow.request, flow.context, run, admitted_completion=True)


# LLM: 共享回执写失败不属于可隔离目标错误，必须停止其它外发；原业务结果已先冻到wake。
# 函数用途: 验证全局outbox故障不会被吞掉后继续运输。
def test_shared_outbox_failure_stops_other_transports(alert_case, monkeypatch):
    case = alert_case
    second_alert_remote(case)
    flow = alert_attempt(case, sorted((row for row in delivery_targets(case) if not row["local"]), key=lambda row: row["account_id"]))
    save = case.agent.scheduler_repository.record_delivery_receipt
    # LLM: 仅在发送事实落账时使共享仓库失败，初始业务快照仍用真实原仓库。
    # 函数用途: 定位发送后全局账本失败窗口。
    def unavailable(run_id, key, receipt, *, event_key=None):
        if receipt.get("state") == "sent":
            raise OSError("shared outbox unavailable")
        return save(run_id, key, receipt, event_key=event_key)
    monkeypatch.setattr(case.agent.scheduler_repository, "record_delivery_receipt", unavailable)
    with pytest.raises(OSError, match="shared outbox unavailable"):
        flow.attempt()
    assert len(flow.channels.attempts) == 1 and flow.channels.attempts[0][0].target == "ou_a"
    assert case.store.wakes.pending_one(flow.signal.wake_signal_id).metadata["owner_delivery"]["content"] == "原始业务结果"


# LLM: 撤销未发送A不能阻止仍认证B，且不能为了B跳过A的撤权检查。
# 函数用途: 验证逐目标当前授权隔离。
def test_revoked_unsent_target_does_not_block_authorized_target(alert_case):
    from agent_py_agent.agent.user_space.admin_channel_identity import remove_admin_channel_identity
    case = alert_case
    flow = alert_attempt(case, sorted(delivery_targets(case), key=lambda row: row["local"]))
    remove_admin_channel_identity(case.agent.home_paths.root, "feishu", "ou_a")
    result = flow.attempt()
    assert not result.persisted and result.delivery_status == "failed" and not flow.channels.attempts
    assert len(case.store.messages.recent(case.local.thread_id)) == 1
    receipts = case.agent.scheduler_repository.delivery_receipts(flow.signal.root_task_id)
    assert any(row.get("error_code") == "SCHEDULE_DELIVERY_TARGET_UNAUTHORIZED" for row in receipts.values())


# LLM: 已发后/new只补旧canonical，不向新线程再发，owner及绑定仍必须准确。
# 函数用途: 验证先发送、后继任、再补档的顺序。
def test_sent_target_repairs_original_canonical_after_new(alert_case, monkeypatch):
    from agent_py_agent.agent.conversation.background_delivery import frozen_owner_delivery
    case = alert_case
    flow = alert_attempt(case, [row for row in delivery_targets(case) if not row["local"]])
    append = block_alert_canonical(case, monkeypatch, case.thread.thread_id)
    assert not flow.attempt().persisted
    cached = case.store.wakes.pending_one(flow.signal.wake_signal_id).metadata["owner_delivery"]
    new = successor(case.store, case.thread)
    monkeypatch.setattr(case.store.messages, "append_once", append)
    result = flow.attempt(cached["content"], frozen_retry=frozen_owner_delivery(cached))
    assert result.persisted and result.delivery_status == "sent" and len(flow.channels.attempts) == 1
    assert len(case.store.messages.recent(case.thread.thread_id)) == 1
    assert not case.store.messages.recent(new.thread_id)


# LLM: 原输入通道的transcript标志不能替代新目标自身能力；原sender仍真实运行。
# 函数用途: 防止本地入口能力被错误继承到远端路线。
def test_remote_target_recomputes_transcript_capability(alert_case, monkeypatch):
    from agent_py_agent.agent.conversation import background_delivery
    flow = alert_attempt(alert_case, [row for row in delivery_targets(alert_case) if not row["local"]])
    sender = background_delivery._record_background_owner_response
    observed = []
    # LLM: 只捕获实际参数，不重建投递或替代回执。
    # 函数用途: 检查远端目标收到自己的路线能力。
    def capture(*args, **kwargs):
        observed.append(kwargs["route_supports_transcript"])
        return sender(*args, **kwargs)
    monkeypatch.setattr(background_delivery, "_record_background_owner_response", capture)
    assert flow.attempt(route_supports_transcript=True).persisted
    assert observed == [None]


# LLM: 完成后的派生outbox不能无限保留整封正文；未完成债务和当前运行必须完整保留。
# 函数用途: 验证有界回执保留及仅完成后压缩信封。
def test_completed_alert_outbox_is_bounded_without_dropping_pending_debt():
    from agent_py_agent.agent.scheduler import repository
    compact = getattr(repository, "_compact_delivery_outbox", None)
    assert callable(compact), "多线outbox必须有完成后压缩和有界保留"
    outbox = {f"run-{index}": {"target": {"state": "committed", "run_delivery_complete": True, "updated_at": index,
        "receipt_id": f"receipt-{index}", "business_result": {"content": "大业务结果"}, "frozen": {"content": "大投影"}}}
        for index in range(140)}
    pending = {"target": {"state": "sent", "frozen": {"content": "未补档"}, "business_result": {"content": "原结果"}}}
    outbox["old-pending"] = pending
    outbox["partially-registered"] = {"target": {"state": "committed", "business_result": {"content": "还有尚未登记的目标"}}}
    original_pending = repr(pending)
    compact(outbox, "run-139")
    assert repr(outbox["old-pending"]) == original_pending and "run-139" in outbox
    assert outbox["partially-registered"]["target"]["business_result"]["content"] == "还有尚未登记的目标"
    complete = [rows for rows in outbox.values() if all(row.get("run_delivery_complete") for row in rows.values())]
    assert len(complete) == 128
    assert all("frozen" not in row and "business_result" not in row for rows in complete for row in rows.values())
    assert outbox["run-139"]["target"]["receipt_id"] == "receipt-139"


# LLM: 原线程的用户选定目录不能被本地Agent启动目录覆盖；真实控制端与Store执行继任。
# 函数用途: 复现Direct/new在继任前改写cwd的问题。
def test_r2_direct_new_preserves_selected_directories(conversations):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )
    original = thread(conversations, channel="chat", actor="local-agent", chat="terminal-r2")
    original = conversations.threads.update_atomic(original.thread_id, lambda row: replace(row,
        cwd="/selected/project/sub", workspace_root="/selected/project"))
    agent = SimpleNamespace(conversation_store=conversations, root="/agent/startup")
    result = execute_chat_control(ChatControlExecution(agent, False, ChatControlState(False, 0, "", 0, "terminal-r2")),
        ConversationControlCommand("new"))
    assert result.ok
    for identifier in (original.thread_id, result.details["thread_id"]):
        saved = conversations.threads.require(identifier)
        assert (saved.cwd, saved.workspace_root) == (original.cwd, original.workspace_root)


# LLM: 经过真实Direct slash、控制事务和worker共享历史，不用只调用显示清屏代替入口验收。
# 函数用途: 验证Direct/new立即清屏清历史，下一轮不会注入旧正文。
def test_r2_direct_tui_new_clears_worker_history_and_display(conversations):
    import threading

    from agent_py_agent.cli.chat_parts.tui import _tui_handle_command
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_worker import (
        TuiWorkerConfig,
        _build_turn_inject,
        _worker_history_context,
    )
    thread(conversations, channel="chat", actor="local-agent", chat="terminal-r2")
    agent = SimpleNamespace(conversation_store=conversations)
    runtime = TuiRuntime("terminal-r2")
    runtime.write_console("旧显示内容")
    runtime.background_message_cursor = 500
    history, outputs = [("旧用户正文", "旧助手正文")], ["旧助手正文"]
    lock = threading.Lock()
    cfg = TuiWorkerConfig(jobs=None, state_lock=lock, is_running_ref=[False], pending_jobs_ref=[0],
        running_prompt_ref=[""], running_request_id_ref=[""], running_started_at_ref=[0.0], agent=agent, args=None,
        paths=None, use_gateway=False, conversation_history=history, history_lock=threading.Lock(),
        build_history_context=lambda: repr(history) if history else "", assistant_outputs=outputs,
        last_token_estimate_ref=[0], stop_event=threading.Event(), current_session_id="terminal-r2", tui_runtime=runtime)
    params = SimpleNamespace(user="/new", agent=agent, args=SimpleNamespace(memory_limit=10), runtime_inject=[],
        prompt_files=[], use_gateway=False, plugin_client=None, plugin_revision="", command_interaction=None,
        current_session_id="terminal-r2", state_lock=lock, is_running_ref=cfg.is_running_ref, pending_jobs_ref=cfg.pending_jobs_ref,
        running_prompt_ref=cfg.running_prompt_ref, running_request_id_ref=cfg.running_request_id_ref,
        running_started_at_ref=cfg.running_started_at_ref, local_run_ref=cfg.local_run_ref,
        assistant_outputs=outputs, tui_runtime=runtime)
    assert _tui_handle_command(params=params)
    assert not runtime.store.snapshot().stable_blocks and runtime.background_message_cursor == 0
    assert history == [] and outputs == []
    assert _worker_history_context(cfg) == ""
    assert "旧用户正文" not in repr(_build_turn_inject([], _worker_history_context(cfg)))


# LLM: HTTP在途时真正执行reset再返回旧页；旧notice、游标、seen和过程页都不能再发表。
# 函数用途: 复现Gateway旧poll覆盖新空白会话的问题。
def test_r2_tui_discards_poll_started_before_reset():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_gateway_background_snapshot
    runtime = TuiRuntime("terminal-r2")
    seen, cursor = set(), [0]
    # LLM: 只替代HTTP边界并在响应前触发实际reset，后续消费仍走真实代码。
    # 函数用途: 以确定时序交付过期快照。
    def fetch(*_args, **_kwargs):
        runtime.publish_conversation_reset("thread-new")
        return {"ok": True, "thread_id": "thread-old", "cursor": 500, "event_cursor": 0, "event_stream_id": "",
            "transcript_events": [], "notices": [{"schema_version": "background_message.v1", "display_kind": "assistant_response",
                "thread_id": "thread-old", "message_id": "msg-old", "content": "过期poll正文"}]}
    agent = SimpleNamespace(request_background_notices=fetch)
    assert _consume_gateway_background_snapshot(agent, "terminal-r2", runtime, [None], seen, cursor)
    assert runtime.background_message_cursor == 0 and not runtime.store.snapshot().stable_blocks
    assert not seen and cursor == [0]


# LLM: 在首次采样字段读出后精确启动另一线程reset；锁内只等待对方尝试，锁外则等reset完成，不用sleep赌时序。
# 函数用途: 同时核对两种poll原子采样、锁外IO，以及旧页不能回写游标500。
@pytest.mark.parametrize("transport", ["gateway", "local"])
def test_r3_poll_captures_cursor_and_generation_under_reset_lock(alert_case, monkeypatch, transport):
    import threading

    from agent_py_agent.agent.conversation import background_transcript, message_stream
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_threading import (
        _consume_gateway_background_snapshot,
        _consume_local_background_snapshot,
    )
    start, attempted, finished = threading.Event(), threading.Event(), threading.Event()
    samples, errors, requests, transcripts = [], [], [], []
    # LLM: 只探测属性读取边界；仍执行真实runtime锁、reset、快照提交，不替代被测同步机制。
    # 类用途: 让reset确定落在旧实现的两次无锁字段读取之间。
    class ProbedRuntime(TuiRuntime):
        # LLM: 先保存实际属性值，再通知reset线程；读锁状态只用于测试握手，绝不改变生产锁行为。
        # 函数用途: 精确记录首次采样是否持有真实reset锁。
        def __getattribute__(self, name):
            value = super().__getattribute__(name)
            if name in {"background_message_cursor", "conversation_reset_generation"} and self.__dict__.get("probe_armed"):
                self.probe_armed = False
                locked = self._lock._is_owned()
                samples.append((name, locked))
                start.set()
                assert attempted.wait(5), "reset线程未到达握手点"
                if not locked:
                    assert finished.wait(5), "无锁采样期间reset未完成"
            return value
    runtime = ProbedRuntime("terminal-alerts")
    runtime.background_message_cursor = 500
    runtime.background_event_stream_id = "stream-r3-old"
    seen, cursor = set(), [7]
    # LLM: 原reset必须沿真实RLock执行；线程异常显式交回主测试，结束前join，不遗留后台线程。
    # 函数用途: 在采样首字段之后尝试切换会话。
    def reset():
        try:
            assert start.wait(5), "poll未到达采样点"
            attempted.set()
            runtime.publish_conversation_reset("thread-r3-new")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    worker = threading.Thread(target=reset)
    worker.start()
    runtime.probe_armed = True
    # LLM: 只替代IO边界；若生产代码把HTTP或文件读取放进reset锁，此握手会直接失败而非等待超时掩盖。
    # 函数用途: 证明采样锁已释放并让旧页在reset完成后返回。
    def io_boundary():
        assert not runtime._lock._is_owned(), "IO不得持有reset锁"
        assert finished.wait(5), "IO前reset仍被采样锁阻塞"
    # LLM: 服务端返回合法空页及请求位置；旧游标不能被配上reset后的新代次重新接纳。
    # 函数用途: 模拟Gateway对游标500的迟到响应。
    def fetch(_session_id, *, after, event_after, event_stream_id):
        io_boundary()
        requests.append((after, event_after, event_stream_id))
        return {"ok": True, "thread_id": "thread-r3-old", "cursor": 500, "event_cursor": 7,
            "event_stream_id": "stream-r3-old", "transcript_events": [], "notices": []}
    # LLM: 本地页同样只返回请求位置，消息解析与提交仍由原poll执行。
    # 函数用途: 记录本地文件页读取使用的采样游标。
    def read_page(_store, _thread_id, *, after, **_kwargs):
        io_boundary()
        requests.append(after)
        return [], 500, True
    try:
        if transport == "gateway":
            result = _consume_gateway_background_snapshot(SimpleNamespace(request_background_notices=fetch),
                "terminal-alerts", runtime, [None], seen, cursor)
        else:
            resolve = alert_case.store.threads.resolve_report
            read_transcript = background_transcript.read_background_transcript_events
            # LLM: 保留真实过程流读取，额外核对reset后的IO仍只使用同代捕获的位置与流身份。
            # 函数用途: 防止过程游标或stream在锁外重新采样。
            def read_events(*args, **kwargs):
                io_boundary()
                transcripts.append((kwargs["after"], kwargs["stream_id"]))
                return read_transcript(*args, **kwargs)
            # LLM: 原线程解析是本地首个文件IO；进入它之前也必须已经完成原子采样并释放锁。
            # 函数用途: 保留真实线程解析，同时核对锁外读取边界。
            def resolve_report(**kwargs):
                io_boundary()
                return resolve(**kwargs)
            monkeypatch.setattr(alert_case.store.threads, "resolve_report", resolve_report)
            monkeypatch.setattr(background_transcript, "read_background_transcript_events", read_events)
            monkeypatch.setattr(message_stream, "read_background_response_page", read_page)
            result = _consume_local_background_snapshot(alert_case.agent, alert_case.store,
                "terminal-alerts", runtime, [None], seen, cursor)
    finally:
        start.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert result and runtime.background_message_cursor == 0
    assert samples and all(locked for _name, locked in samples)
    assert requests == ([(500, 7, "stream-r3-old")] if transport == "gateway" else [500])
    assert transcripts == ([(7, "stream-r3-old")] if transport == "local" else [])
    assert runtime.background_event_stream_id == "" and cursor == [7] and not seen
    assert not runtime.store.snapshot().stable_blocks


# LLM: 两个通道都真实登记到同一线程；各自new只能移动自己的索引，重复旧ID必须幂等。
# 函数用途: 复现一个通道继任后另一个通道无法new的问题。
def test_r2_each_bound_channel_can_succeed_independently(conversations):
    old = thread(conversations)
    old = conversations.threads.bind_channel({"thread_id": old.thread_id, "canonical_user_id": "owner", "channel": "chat",
        "channel_user_id": "local-agent", "channel_conversation_id": "terminal-r2"})
    feishu, chat = old.channel_bindings
    new_chat = conversations.threads.succeed(old.thread_id, binding=chat)
    error = ""
    try:
        new_feishu = conversations.threads.succeed(old.thread_id, binding=feishu)
    except ValueError as exc:
        error = str(exc)
    assert not error, error
    assert new_feishu.thread_id != new_chat.thread_id
    for binding, expected in ((chat, new_chat), (feishu, new_feishu)):
        current = conversations.threads.resolve(channel=binding.channel, channel_user_id=binding.channel_user_id,
            channel_conversation_id=binding.channel_conversation_id)
        assert current.thread_id == expected.thread_id
        assert conversations.threads.succeed(old.thread_id, binding=binding).thread_id == expected.thread_id
    saved = conversations.threads.require(old.thread_id)
    assert saved.metadata["succession_by_binding"]["\x1f".join((chat.channel, chat.channel_user_id, chat.channel_conversation_id))] == new_chat.thread_id


# LLM: 通过真实Web删除入口移除空闲前任，新线程已发表的身份不再依赖前任文件永久存在。
# 函数用途: 验证删除归档前任不会隐藏活动后继。
def test_r2_deleting_archived_predecessor_keeps_successor_visible(alert_case):
    from agent_py_agent.agent.plugin_host.thread_settings_v2 import ThreadSettingsV2Handlers
    case = alert_case
    new = successor(case.store, case.thread)
    context = SimpleNamespace(owner_home=SimpleNamespace(owner_id=case.agent.home_paths.owner_id,
        home_dir=case.agent.home_paths.owner_home_dir))
    result = ThreadSettingsV2Handlers(case.store, context, home_paths=case.agent.home_paths).delete_thread({"thread_id": case.thread.thread_id})
    assert result["deleted"] and case.store.threads.load(case.thread.thread_id) is None
    visible = case.store.threads.load(new.thread_id)
    assert visible is not None and visible.status == "active"
    assert visible.metadata["succession_published"] is True
    assert new.thread_id in {row.thread_id for row in case.store.threads.list(limit=0)}


# LLM: 非通知字段在任何来源准备/写入之前拒绝，保留原版本和文件字节，不能抛AttributeError。
# 函数用途: 复现update_delivery误入schedule/rules准备对象的问题。
@pytest.mark.parametrize("extra", [{"rules": []}, {"schedule_kind": "every", "every_seconds": 60}, {"prompt": "不能混改"}, {"cron_expr": "* * * * *"}])
def test_r2_update_delivery_rejects_unrelated_fields(alert_case, extra):
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    case = alert_case
    before = case.agent.scheduler_repository.store_path.read_bytes()
    error = ""
    try:
        result = ScheduleTool(case.agent).execute({"action": "update_delivery", "job_id": case.job["job_id"],
            "expected_version": case.job["version"], "delivery_policy": {"mode": "none"}, **extra})
    except AttributeError as exc:
        error = str(exc)
    assert not error, error
    assert not result.ok and result.error_code == "SCHEDULE_DELIVERY_UPDATE_FIELDS_INVALID"
    assert case.agent.scheduler_repository.store_path.read_bytes() == before


# LLM: 实际安装、令牌、issuer、门户与scheduler都真实，只有模型和IM运输不启动；无当前model run。
# 函数用途: 复现冷Host v2请求缺少模型上下文无法更新本人监控的问题。
def test_r2_cold_host_delivery_update_uses_authenticated_owner(alert_case):
    import json

    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.plugin_host.composition import HostComposition
    from agent_py_agent.agent.plugin_host.contracts import (
        HostDomainPorts,
        decode_call,
        dispatch_call,
    )
    from agent_py_agent.agent.plugin_host.conversations import ConversationDomain
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_host.gateway_composition import GatewayPortalDomain
    from agent_py_agent.agent.plugin_manifest_host_v9 import host_capabilities
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant
    case = alert_case
    caps = sorted(host_capabilities())
    home, _install, entry, ref = active_grant(case.agent.home_paths.root, caps)
    assert home.owner_id == case.agent.home_paths.owner_id and home.home_dir == case.agent.home_paths.owner_home_dir
    api.set_host_api_base("http://127.0.0.1:8420")
    host = None
    try:
        token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
        host = HostComposition(capability_policy=lambda: frozenset(caps))
        conversation = ConversationDomain(lambda _: case.store, host.cursors, default_thread=lambda *_: False)
        paths = gateway_paths_from_root(home.data_dir / "gateway")
        ensure_gateway_folders(paths)
        resources = SimpleNamespace(store_for=lambda _: case.store, paths_for=lambda _: paths,
            server=SimpleNamespace(agent=case.agent))
        conversation.portal_v2 = GatewayPortalDomain(resources, conversation)
        host.ports = HostDomainPorts(conversation=conversation)
        source = host.issuer.ingress(token)
        call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "monitors.delivery.update",
            "idempotency_key": "cold-delivery-update-r2-0001", "payload": {"monitor_id": case.job["job_id"],
                "expected_version": case.job["version"], "delivery_policy": {"mode": "none"}}}).encode())
        authorized = host.issuer.authorize(source, call)
        case.agent._current_run_params = None
        error = ""
        try:
            result = dispatch_call(authorized, host.ports)
        except HostAPIError as exc:
            error = f"{exc.code}; cause={exc.__cause__!r}; context={exc.__context__!r}"
        assert not error, error
        assert result.data["job"]["delivery_policy"] == {"mode": "none"}
        assert case.agent.scheduler_repository.get_job(case.job["job_id"])["version"] == case.job["version"] + 1
        assert case.agent._current_run_params is None
    finally:
        if host is not None:
            host.close()
        api.set_host_api_base(None)


# LLM: 原prepare/reserve/wake和sender均真实运行，只把外部IM运输换为可计数协议替身。
# 函数用途: 为R2事件去重及路由测试准备同一run的原入口。
def r2_delivery(case, policy=None):
    from agent_py_agent.agent.conversation.background_delivery import BackgroundDeliveryDependencies
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]}
        for row in sorted(delivery_targets(case), key=lambda row: row["channel"] != "feishu")]
    signal, request = prepared_alert(case, policy or {"mode": "multi", "targets": refs})
    channels = _RetryDelivery()
    channels.status = "sent"
    return SimpleNamespace(signal=signal, request=request, channels=channels,
        context=DeliveryContext("feishu", "ou_a", thread_id=case.thread.thread_id, task_id=request.task_id,
            idempotency_key="r2-first-event"),
        dependencies=BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "running"))


# LLM: 两个真实wake共用run_id但事件身份不同；同事件重试仍不得第二次运输或落重复消息。
# 函数用途: 复现run级outbox吞掉第二条通知的问题。
def test_r2_different_events_in_same_run_deliver_once_each(alert_case):
    from agent_py_agent.agent.conversation.background_delivery import record_background_response
    case = alert_case
    call = r2_delivery(case)
    second = case.store.wakes.raise_signal({"thread_id": call.request.thread_id, "root_task_id": call.request.task_id,
        "reason": call.request.reason, "dedupe_key": "r2-second-notification", "summary": "第二条通知"})
    request_b = replace(call.request, wake_signal=second.to_dict())
    context_b = replace(call.context, idempotency_key="r2-second-event")
    for request, context, content in ((call.request, call.context, "事件A正文"), (request_b, context_b, "事件B正文")):
        result = record_background_response(call.dependencies, request, context, content, deliver=True, delivery_reason="scheduled_job_due")
        assert result.persisted and result.delivery_status == "sent"
    assert len(call.channels.attempts) == 2
    for request, context, content in ((call.request, call.context, "事件A正文"), (request_b, context_b, "事件B正文")):
        assert record_background_response(call.dependencies, request, context, content, deliver=True,
            delivery_reason="scheduled_job_due").persisted
    assert len(call.channels.attempts) == 2
    assert {row.content for row in case.store.messages.recent(case.local.thread_id)} == {"事件A正文", "事件B正文"}


# LLM: 工具回执已经核验后进入正式镜像入口，只应补canonical和其它目标，不能把主通道改成未发送。
# 函数用途: 复现multi镜像重复外发已经成功的主通道。
def test_r2_multiline_mirror_keeps_existing_primary_send_receipt(alert_case):
    from agent_py_agent.agent.conversation.background_delivery import mirror_message_tool_deliveries
    case = alert_case
    call = r2_delivery(case)
    result = mirror_message_tool_deliveries(call.dependencies, call.request, call.context,
        [{"content": "主通道已经发出", "receipt_id": "primary-existing-receipt"}], delivery_reason="scheduled_message_tool_delivery")
    assert result.persisted and result.delivery_status == "sent"
    assert call.channels.attempts == []
    receipts = case.agent.scheduler_repository.delivery_receipts(call.request.task_id)
    primary = next(row for row in receipts.values() if not row["local"])
    assert primary["external_sent"] is True and primary["receipt_id"] == "primary-existing-receipt"
    assert any(row.content == "主通道已经发出" for row in case.store.messages.recent(case.local.thread_id))


# LLM: 可选flag是严格布尔值；不强制给旧策略补键，以兼容原投影/摘要形状。
# 函数用途: 固定跟随开关的公开策略合同。
@pytest.mark.parametrize("follow", [True, False])
def test_r2_delivery_policy_accepts_follow_latest_boolean(follow):
    from agent_py_agent.agent.scheduler.delivery_policy import normalize_delivery
    from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
    error, policy = "", None
    try:
        policy = normalize_delivery({"mode": "origin", "follow_latest_channel_session": follow})
    except ScheduleValidationError as exc:
        error = str(exc)
    assert not error, error
    assert policy == {"mode": "origin", "follow_latest_channel_session": follow}


# LLM: 两通道分别继任后按监控冻结的feishu三元组走链；关闭跟随则仍落原线程，不动run来源。
# 函数用途: 核验per-binding继任与固定会话通知。
@pytest.mark.parametrize("follow", [True, False])
def test_r2_origin_follow_flag_with_two_channel_successors(alert_case, follow):
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.scheduler.execution import delivery_thread, validate_delivery_origin
    case = alert_case
    original = case.store.threads.bind_channel({"thread_id": case.thread.thread_id, "canonical_user_id": "owner", "channel": "chat",
        "channel_user_id": "local-agent", "channel_conversation_id": "r2-multichannel-source"})
    _signal, request = prepared_alert(case, {"mode": "origin", "follow_latest_channel_session": follow})
    case.store.threads.succeed(original.thread_id, binding=original.channel_bindings[1])
    new = case.store.threads.succeed(original.thread_id, binding=original.channel_bindings[0])
    run = case.agent.scheduler_repository.get_active_run(request.task_id)
    actual = delivery_thread(case.agent, case.store.threads.require(run["thread_id"]), request)
    expected = new.thread_id if follow else original.thread_id
    assert actual.thread_id == expected
    assert validate_delivery_origin(case.agent, request, DeliveryContext("feishu", "ou_a", thread_id=expected, task_id=request.task_id))
    assert run["origin_thread_id"] == original.thread_id


# LLM: 固定绑定的多线策略在run首次准备时冻结目标线程；/new只移动通道索引，不移动固定canonical归属。
# 函数用途: 验证多线关闭跟随后仍写原本地目标。
def test_r2_multiline_pins_target_thread_when_follow_disabled(alert_case):
    from agent_py_agent.agent.conversation.background_delivery import record_background_response
    case = alert_case
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]} for row in delivery_targets(case)]
    call = r2_delivery(case, {"mode": "multi", "targets": refs, "follow_latest_channel_session": False})
    new_local = successor(case.store, case.local)
    result = record_background_response(call.dependencies, call.request, call.context, "固定旧本地目标", deliver=True,
        delivery_reason="scheduled_job_due")
    assert result.persisted and result.delivery_status == "sent"
    assert any(row.content == "固定旧本地目标" for row in case.store.messages.recent(case.local.thread_id))
    assert not case.store.messages.recent(new_local.thread_id)


# LLM: 通知接管保留原管理员认证代次，flag只改变canonical线程，不改变接管来源或接收地址。
# 函数用途: 验证关闭跟随也适用于notification_binding。
def test_r2_notification_binding_can_remain_pinned(alert_case):
    from agent_py_agent.agent.scheduler.execution import delivery_thread
    from agent_py_agent.agent.scheduler.notification_binding import SCHEMA
    from agent_py_agent.agent.user_space.admin_channel_identity import find_admin_channel_identity
    case = alert_case
    repository = case.agent.scheduler_repository
    identity = find_admin_channel_identity(case.agent.home_paths.root, "feishu", "ou_a")
    binding = {"schema_version": SCHEMA, "version": 1, "owner": repository.owner, "source_thread_id": case.thread.thread_id,
        "binding": {"channel": "feishu", "actor_id": "ou_a", "conversation_id": "oc_group"},
        "admin_bound_at": identity.bound_at, "request_id": "r2-notification-binding"}
    case.job = repository.update_notification_binding(case.job["job_id"], binding, expected_version=case.job["version"])
    _signal, request = prepared_alert(case, {"mode": "origin", "follow_latest_channel_session": False})
    successor(case.store, case.thread)
    run = repository.get_active_run(request.task_id)
    assert delivery_thread(case.agent, case.store.threads.require(run["thread_id"]), request).thread_id == case.thread.thread_id


# LLM: 按实际运输/本地canonical与精确重试核语义；fallback成功不能再重试更早的失败路线。
# 函数用途: 验证主线、双线和按序失败转移模式。
@pytest.mark.parametrize("mode,status,local_count", [("primary_only", "sent", 0), ("dual_deliver", "sent", 1),
    ("fallback_on_failure", "sent", 0), ("fallback_on_failure", "failed", 1), ("fallback_on_failure", "local_only", 1)])
def test_r2_routing_modes_have_distinct_delivery_semantics(alert_case, mode, status, local_count):
    from agent_py_agent.agent.conversation.background_delivery import record_background_response
    case = alert_case
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]}
        for row in sorted(delivery_targets(case), key=lambda row: row["channel"] != "feishu")]
    call = r2_delivery(case, {"mode": mode, "targets": refs})
    call.channels.status = status
    first = record_background_response(call.dependencies, call.request, call.context, "按策略路由", deliver=True, delivery_reason="scheduled_job_due")
    assert first.persisted
    assert len(call.channels.attempts) == 1
    assert len(case.store.messages.recent(case.local.thread_id)) == local_count
    assert record_background_response(call.dependencies, call.request, call.context, "按策略路由", deliver=True,
        delivery_reason="scheduled_job_due").persisted
    assert len(call.channels.attempts) == 1


# LLM: severity只读结构化wake；受限与不受限目标同时存在，不根据正文或分数自动扩大目标。
# 函数用途: 验证精确等级列表、最低等级及无过滤目标的路由。
@pytest.mark.parametrize("severity,expected", [("info", set()), ("warning", {"ou_b"}), ("high", {"ou_a"}),
    ("critical", {"ou_a"}), ("", set())])
def test_r2_severity_routing_filters_targets(alert_case, severity, expected):
    from agent_py_agent.agent.conversation.background_delivery import record_background_response
    case = alert_case
    second_alert_remote(case)
    choices = delivery_targets(case)
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"],
        **({"min_severity": "high"} if row["account_id"] == "ou_a" else
            {"severities": ["warning"]} if row["account_id"] == "ou_b" else {})} for row in choices]
    call = r2_delivery(case, {"mode": "severity_routed", "targets": refs})
    signal = case.store.wakes.raise_signal({"thread_id": call.request.thread_id, "root_task_id": call.request.task_id,
        "reason": call.request.reason, "severity": severity, "dedupe_key": f"r2-severity-{severity or 'unset'}"})
    request = replace(call.request, wake_signal=signal.to_dict())
    result = record_background_response(call.dependencies, request, call.context, "有等级的告警", deliver=True, delivery_reason="scheduled_job_due")
    assert result.persisted
    assert {attempt[0].target for attempt in call.channels.attempts} == expected
    assert len(case.store.messages.recent(case.local.thread_id)) == 1


# LLM: 所有通知目标被等级过滤时仍保留内部业务结果，不能无休止重试或把未发送伪报为sent。
# 函数用途: 验证severity无匹配时静默留档而不外发。
def test_r2_severity_without_matching_targets_keeps_internal_result(alert_case):
    from agent_py_agent.agent.conversation.background_delivery import record_background_response
    case = alert_case
    remote = next(row for row in delivery_targets(case) if row["channel"] == "feishu")
    call = r2_delivery(case, {"mode": "severity_routed", "targets": [{"target_kind": "channel_binding",
        "binding_id": remote["binding_id"], "min_severity": "high"}]})
    result = record_background_response(call.dependencies, call.request, call.context, "未到通知等级但要保留", deliver=True,
        delivery_reason="scheduled_job_due")
    assert result.persisted and result.delivery_status == "local_only"
    assert not call.channels.attempts
    assert any(row.content == "未到通知等级但要保留" for row in case.store.messages.recent(call.request.thread_id))


# LLM: Direct当前绑定即使坏账指向外来owner也不能继任；原记录与全部字节在拒绝后保持。
# 函数用途: 防止更换查找路径时漏掉Direct入口的owner/home门。
def test_r2_direct_new_rejects_cross_owner_bound_record(alert_case):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )
    case = alert_case
    case.store.threads.update_atomic(case.local.thread_id, lambda row: replace(row, owner_id="foreign-owner", owner_home="/foreign-home"))
    before = case.store.storage.thread_path(case.local.thread_id).read_bytes()
    result = execute_chat_control(ChatControlExecution(case.agent, False, ChatControlState(False, 0, "", 0, "terminal-alerts")),
        ConversationControlCommand("new"))
    assert not result.ok and result.error_code == "THREAD_SUCCESSOR_OWNER_MISMATCH"
    assert case.store.storage.thread_path(case.local.thread_id).read_bytes() == before


# LLM: 主通道已送达但canonical欠账时，不得再发送fallback；只阻断原消息写入，不改sender或outbox。
# 函数用途: 复现canonical异常路径越过已发送事实继续投下一线的问题。
def test_r2_fallback_waits_for_sent_primary_canonical_repair(alert_case, monkeypatch):
    from agent_py_agent.agent.conversation.background_delivery import (
        frozen_owner_delivery,
        record_background_response,
    )
    case = alert_case
    second_alert_remote(case)
    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]}
        for row in sorted((row for row in delivery_targets(case) if not row["local"]), key=lambda row: row["account_id"])]
    call = r2_delivery(case, {"mode": "fallback_on_failure", "targets": refs})
    append = block_alert_canonical(case, monkeypatch, case.thread.thread_id)
    result = record_background_response(call.dependencies, call.request, call.context, "主线已发等待补账", deliver=True,
        delivery_reason="scheduled_job_due")
    assert not result.persisted and result.delivery_status == "failed"
    assert [attempt[0].target for attempt in call.channels.attempts] == ["ou_a"]
    receipts = case.agent.scheduler_repository.delivery_receipts(call.request.task_id)
    assert len(receipts) == 1
    receipt = next(iter(receipts.values()))
    assert receipt["external_sent"] and receipt["state"] == "sent"
    monkeypatch.setattr(case.store.messages, "append_once", append)
    cached = case.store.wakes.pending_one(call.signal.wake_signal_id).metadata["owner_delivery"]
    # wake首次业务快照不可覆盖；准确已发事实保存在原目标outbox，而非把整封wake升级成所有目标已发。
    assert receipt["frozen"]["external_sent"] and receipt["frozen"]["receipt_id"] == receipt["receipt_id"]
    repaired = record_background_response(call.dependencies, call.request, call.context, cached["content"], deliver=True,
        delivery_reason="cached_owner_delivery_retry", frozen_retry=frozen_owner_delivery(cached))
    assert repaired.persisted and repaired.delivery_status == "sent"
    assert [attempt[0].target for attempt in call.channels.attempts] == ["ou_a"]


# LLM: 本地文件页读取期间执行真实继任并reset；去重窗口和游标必须整页拒绝，再正常读到新会话第一页。
# 函数用途: 复现Direct本地poll恢复旧历史的问题。
def test_r2_local_tui_discards_inflight_page_after_new(alert_case, monkeypatch):
    from agent_py_agent.agent.conversation import message_stream
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_local_background_snapshot
    case = alert_case
    runtime = TuiRuntime("terminal-alerts")
    seen, cursor, calls = set(), [0], []
    # LLM: 仅替代消息IO边界；reset和原绑定读取均真实，第二拍核实after游标没有跳过新消息。
    # 函数用途: 精确制造旧页在reset后返回的时序。
    def read_page(_store, thread_id, *, after, **_kwargs):
        calls.append((thread_id, after))
        if len(calls) == 1:
            new = successor(case.store, case.local)
            runtime.publish_conversation_reset(new.thread_id)
            content, identifier, offset = "旧本地页", case.local.thread_id, 500
        else:
            content, identifier, offset = "新会话第一页", thread_id, 1
        return [{"schema_version": "background_message.v1", "display_kind": "assistant_response", "thread_id": identifier,
            "message_id": f"msg-{offset}", "content": content}], offset, True
    monkeypatch.setattr(message_stream, "read_background_response_page", read_page)
    assert _consume_local_background_snapshot(case.agent, case.store, "terminal-alerts", runtime, [None], seen, cursor)
    assert not runtime.store.snapshot().stable_blocks and runtime.background_message_cursor == 0 and not seen
    assert _consume_local_background_snapshot(case.agent, case.store, "terminal-alerts", runtime, [None], seen, cursor)
    assert calls[1][1] == 0 and runtime.background_message_cursor == 1
    assert "新会话第一页" in repr(runtime.store.snapshot().stable_blocks)
    assert "旧本地页" not in repr(runtime.store.snapshot().stable_blocks)


# LLM: 真实OwnerIdentity、Agent、creator及群权限，只有已认证Host上下文的读取端口替代；不冒充完整HTTP发行链。
# 函数用途: 验证provider用户/群成员冷上下文采用实际身份字段，并保留creator与CAS拒绝。
@pytest.mark.parametrize("group,caller,stale,allowed", [(False, "ou_a", False, True), (True, "ou_a", False, True),
    (True, "ou_b", False, False), (False, "ou_a", True, False)])
def test_r2_provider_host_context_keeps_native_identity_and_guards(tmp_path, monkeypatch, group, caller, stale, allowed):
    import json

    from agent_py_agent.agent.conversation.host_actor import actor_from_scope
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.plugin_host import conversations as host_conversations
    from agent_py_agent.agent.plugin_host.contracts import OwnerReadContext
    from agent_py_agent.agent.plugin_host.monitor_delivery_v2 import _host_schedule_context
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    config = AgentConfig(my_agent_home=str(tmp_path / "home"), enable_tools=False, orphan_supervision_interval_seconds=0,
        my_agent_owner_provider="feishu", my_agent_owner_kind="group" if group else "user", my_agent_owner_id="oc_group" if group else "ou_a")
    agent = SimpleAgent(config, tmp_path)
    original = agent.conversation_store.threads.get_or_create({"canonical_user_id": "owner", "channel": "feishu",
        "channel_user_id": "ou_a", "channel_conversation_id": "oc_group" if group else "oc_private",
        "owner_id": agent.home_paths.owner_id, "owner_home": str(agent.home_paths.owner_home_dir)})
    scope = SimpleNamespace(channel="feishu", channel_user_id="ou_a", channel_conversation_id="oc_group" if group else "oc_private")
    actor = actor_from_scope(agent, scope, original.thread_id, "create-provider-monitor")
    agent._current_run_params = SimpleNamespace(host_channel_actor=actor, request_id="create-provider-monitor",
        task_attributes={"conversation_thread_id": original.thread_id})
    if group:
        from agent_py_agent.tests.test_group_authorization import (
            grant_synthetic_group_monitor_capability,
        )
        grant_synthetic_group_monitor_capability(agent, actor, members={"ou_a", "ou_b"})
    created = ScheduleTool(agent).execute({"action": "create", "name": "Provider监控", "prompt": "整理本人的资料",
        "schedule_kind": "every", "every_seconds": 600})
    assert created.ok, created.output
    job = json.loads(created.output)["job"]
    identity = OwnerIdentity.provider_group("feishu", "oc_group") if group else OwnerIdentity.provider_user("feishu", "ou_a")
    source = SimpleNamespace(actor=OwnerIdentity.local_main(), source=SimpleNamespace(caller_owner=OwnerIdentity.provider_user("feishu", caller)))
    context = OwnerReadContext(identity, resolve_owner_home(agent.home_paths.root, identity), source)
    authorized = SimpleNamespace(call=SimpleNamespace(idempotency_key="host-provider-update-r2"))
    monkeypatch.setattr(host_conversations, "read_context", lambda _: context)
    agent._current_run_params = None
    before = agent.scheduler_repository.store_path.read_bytes()
    error = ""
    try:
        scoped, authorizer = _host_schedule_context(agent, context, {"monitor_id": job["job_id"]}, authorized)
        result = ScheduleTool(scoped, host_authorizer=authorizer).execute({"action": "update_delivery", "job_id": job["job_id"],
            "expected_version": job["version"] + int(stale), "delivery_policy": {"mode": "none"}})
    except AttributeError as exc:
        error = str(exc)
    assert not error, error
    assert result.ok is allowed, result.output
    assert agent._current_run_params is None
    if not allowed:
        # 非创建者只有已证离群时的pause/cancel/delete例外；update_delivery原合同直接拒绝scope。
        assert result.error_code == ("SCHEDULER_CONFLICT" if stale else "CHANNEL_ACTOR_SCOPE_MISMATCH")
        assert agent.scheduler_repository.store_path.read_bytes() == before
