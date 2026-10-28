# LLM: Wave9 B3 从真实线程文件、调度账本和投递入口核对继任与通知授权，不连接真实 IM。
# 模块用途: 防止原地新会话丢历史、停止后台或把告警投递给未认证对象。
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import store_threads
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.store import ConversationStore


@pytest.fixture
# LLM: 只在pytest临时owner内建原Store，不连接真实通道。
# 函数用途: 提供隔离会话账本。
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


@pytest.mark.parametrize("text,title", [("/new", ""), ("/new 下一件事", "下一件事"), ("/NEW 中文 标题", "中文 标题")])
# LLM: 使用原解析器与CLI序列化，验证/new不是模型普通消息。
# 函数用途: 覆盖大小写、空标题及中文标题。
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


@pytest.mark.parametrize("hops", range(1, 6))
# LLM: 一到五跳都由真实事务生成，不手写可绕过验证的目标选择。
# 函数用途: 验证ConversationStore和ThreadStore共享解析入口。
def test_same_scope_successor_chain(conversations, hops):
    first = current = thread(conversations)
    for _ in range(hops):
        current = successor(conversations, current)
    assert resolve(conversations, first.thread_id) == current.thread_id
    assert resolve(conversations.threads, first.thread_id) == current.thread_id


@pytest.mark.parametrize("tamper", ["owner", "home", "channel", "actor", "chat", "cycle", "missing"])
# LLM: 仅篡改临时记录，非法owner/home/三元组、环和缺失都必须拒绝。
# 函数用途: 逐项验证继任链安全边界。
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


@pytest.fixture
# LLM: 通过原认证和schedule构造IM计划，模型/运输替身限临时目录。
# 函数用途: 提供同owner的飞书与本地双线目标。
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


@pytest.mark.parametrize("mode", ["origin", "local", "none"])
# LLM: 原工具更新沿同一job和版本冲突合同，不创建第二计划。
# 函数用途: 验证旧三种模式的兼容更新和CAS。
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


# LLM: 相同旧ID只能按已发表scope重试，另一个绑定不得返回跨通道后继。
# 函数用途: 复现并防止旧ID重放跨范围。
def test_replayed_reset_cannot_switch_a_different_binding(conversations):
    old = thread(conversations)
    first = old.channel_bindings[0]
    other = replace(first, channel="chat", channel_user_id="terminal", channel_conversation_id="terminal")
    conversations.threads.update_atomic(old.thread_id, lambda row: replace(row, channel_bindings=(first, other)))
    new = conversations.threads.succeed(old.thread_id, binding=first)
    with pytest.raises(ValueError):
        conversations.threads.succeed(old.thread_id, binding=other)
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


@pytest.mark.parametrize("mode", ["bound", "multi"])
# LLM: 原sender和Store真实，只有IM运输替身，不向实际用户发送通知。
# 函数用途: 验证双线逐目标回执、本地留档和include_origin去重。
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


@pytest.mark.parametrize("foreground", [True, False])
# LLM: 原claim账真实领取，前台拒绝与后台保留不能用替身状态证明。
# 函数用途: 验证/new不停止活跃后台租约。
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


@pytest.mark.parametrize("fail_local", [True, False])
# LLM: 仅注入canonical写失败，真实outbox、冻结信封和渠道回执仍按原顺序运行。
# 函数用途: 验证本地优先及外发优先两种失败顺序均不丢结果、不重复外发。
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
    def unavailable(run_id, key, receipt):
        if receipt.get("state") == "sent":
            raise OSError("shared outbox unavailable")
        return save(run_id, key, receipt)
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
