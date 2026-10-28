# LLM: Round2用真实Store/摘要提交链验证只读、审批和消息竞争；只有供应商和容量观察为本地替身。
# 模块用途: 防止闲置维护覆盖新消息，或把查询、等待确认和坏账当可压缩状态。
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import compact_operations as idle
from agent_py_agent.agent.gateway_parts import control_service as control
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.tests.test_gateway_conversation_compact import (
    _agent,
    _context,
    _request,
    _SummaryBackend,
)


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 在测试临时目录创建真实会话存储与初始线程。
@pytest.fixture
def conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    return store, store.threads.create(canonical_user_id="user")


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证只读status命令经CLI运输后不变成摘要请求。
def test_status_parser_preserves_cli_transport():
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text

    command = parse_conversation_control("/compact status")
    assert command.valid and command.operation == "status"
    assert _command_text(command) == "/compact status"
    assert parse_conversation_control("/compact preserve status evidence").operation == "run"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证比例设置与重置命令经CLI运输往返保留参数。
@pytest.mark.parametrize("value", ["reset", "default", "71"])
def test_threshold_cli_transport_roundtrip(value):
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text

    command = parse_conversation_control("/compact threshold " + value)
    restored = parse_conversation_control(_command_text(command))
    assert restored.operation == command.operation == "threshold"
    assert restored.value == command.value == value


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证Gateway状态查询不调用模型、不写线程也不增压缩代数。
def test_gateway_status_is_read_only_without_model_or_generation(conversation, monkeypatch):
    store, row = conversation
    row = store.threads.update_atomic(row.thread_id, lambda t: replace(t, metadata={
        "settings": {"compact_trigger_percent": 71},
        "idle_auto_compact": {"last_idle_compacted_activity_gen": 3},
    }))
    agent = SimpleNamespace(config=AgentConfig(), conversation_store=store)
    monkeypatch.setattr(control, "_request_agent_for_scope", lambda *_: agent)
    monkeypatch.setattr(control, "_conversation_thread_for_scope", lambda *_: store.threads.require(row.thread_id))
    monkeypatch.setattr(control, "_execute_registered_compact_control", lambda *_: pytest.fail("status ran summary"))
    before = store.storage.thread_path(row.thread_id).read_bytes()
    result = control._execute_compact_control(agent, None, parse_conversation_control("/compact status"),
        control.GatewayControlScope("user", "chat", "conversation"))
    assert result.ok
    policy = result.details["compact_policy"]
    assert policy == {"effective_percent": 71, "source": "thread", "admin_forced": False,
        "thread_override": 71, "user_override": None, "admin_default": 80}
    assert result.details["idle_auto_compact"]["last_idle_compacted_activity_gen"] == 3
    assert result.details["compact_generation"] == row.compact_generation
    assert store.storage.thread_path(row.thread_id).read_bytes() == before


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证CLI状态分派只读取线程及有效压缩策略。
def test_cli_status_dispatch_is_read_only(conversation):
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )

    store, row = conversation
    agent = SimpleNamespace(config=AgentConfig(), conversation_store=store)
    before = store.storage.thread_path(row.thread_id).read_bytes()
    result = execute_chat_control(ChatControlExecution(agent, False,
        ChatControlState(False, 0, "", 0, row.thread_id)), parse_conversation_control("/compact status"))
    assert result.ok and result.details["compact_policy"]["effective_percent"] == 80
    assert store.storage.thread_path(row.thread_id).read_bytes() == before


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证重置阈值删除覆盖项但不执行摘要。
@pytest.mark.parametrize("reset", ["reset", "default"])
def test_threshold_reset_clears_override_without_summary(conversation, monkeypatch, reset):
    store, row = conversation
    row = store.threads.update_atomic(row.thread_id, lambda t: replace(t, metadata={"settings": {"compact_trigger_percent": 71}}))
    agent = SimpleNamespace(config=AgentConfig(), conversation_store=store)
    monkeypatch.setattr(control, "_request_agent_for_scope", lambda *_: agent)
    monkeypatch.setattr(control, "_conversation_thread_for_scope", lambda *_: store.threads.require(row.thread_id))
    command = parse_conversation_control("/compact threshold " + reset)
    assert command.valid
    result = control._execute_compact_control(agent, None, command, control.GatewayControlScope("user", "chat", "conversation"))
    assert result.ok and result.details["compact_policy"]["effective_trigger_percent"] == 80
    latest = store.threads.require(row.thread_id)
    assert "compact_trigger_percent" not in latest.metadata["settings"]
    assert latest.compact_generation == row.compact_generation


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证阻塞子代理和未决能力申请都使会话保持忙态。
@pytest.mark.parametrize("status,requests,expected", [
    ("BLOCKED", [], "subagent_blocked"), ("WAITING_APPROVAL", [], "pending_approval"),
    ("DONE", [SimpleNamespace(status="OPEN")], "pending_approval"),
    ("BLOCKED", [SimpleNamespace(status="OPEN")], "pending_approval"),
])
def test_blocked_and_open_capability_requests_are_busy(conversation, tmp_path, monkeypatch, status, requests, expected):
    store, row = conversation
    run = SimpleNamespace(status=status, conversation_thread_id=row.thread_id, capability_requests=requests)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
        subagents=SimpleNamespace(list_runs=lambda: [run],
            list_runs_report=lambda: SimpleNamespace(runs=[run], load_errors=[])))
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})
    assert idle.idle_compact_busy_state(agent, store, row) == expected


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证子代理持久化损坏不被当作空闲。
def test_corrupted_subagent_persistence_is_not_idle(tmp_path):
    agent = _agent(tmp_path, context_tokens=1000000)
    store = agent.conversation_store
    row = store.threads.create()
    task_file = agent.subagents.persistence.workspace / "broken-run" / "task.json"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("{invalid json", encoding="utf-8")
    assert agent.subagents.list_runs_report().load_errors
    assert idle.idle_compact_busy_state(agent, store, row) == "activity_unknown"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证活动claim或任务读取失败时闲置判断失败关闭。
@pytest.mark.parametrize("source", ["claims", "tasks"])
def test_activity_store_errors_fail_closed(conversation, tmp_path, monkeypatch, source):
    store, row = conversation
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), subagents=SimpleNamespace(
        list_runs=lambda: [], list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])))
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})
    if source == "tasks":
        monkeypatch.setattr(store.tasks, "list_report", lambda *_: ([], [{"code": "READ_FAILED"}]))
    else:
        monkeypatch.setattr(store.claims, "load_report", lambda *_, **__: ({}, {"code": "READ_FAILED"}))
    assert idle.idle_compact_busy_state(agent, store, row) == "activity_unknown"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证工具审批、确认卡及损坏审批记录都不能被视为空闲。
@pytest.mark.parametrize("kind", ["tool", "confirmation", "corrupt_approval"])
def test_pending_approval_records_are_busy(conversation, tmp_path, monkeypatch, kind):
    import json

    store, row = conversation
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), subagents=SimpleNamespace(
        list_runs=lambda: [], list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])))
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})
    if kind == "confirmation":
        path = tmp_path / "data/private_interactions/model-control.json"
        payload = {"kind": "model_configuration", "status": "active", "thread_id": row.thread_id}
    else:
        path = store.storage.root / "subagent_tool_approvals/root/approval.json"
        payload = {"schema_version": "subagent_tool_approval.v1", "status": "pending", "thread_id": row.thread_id}
    path.parent.mkdir(parents=True)
    path.write_text("{" if kind == "corrupt_approval" else json.dumps(payload))
    before = path.read_bytes()
    assert idle.idle_compact_busy_state(agent, store, row) == ("activity_unknown" if kind == "corrupt_approval" else "pending_approval")
    assert path.read_bytes() == before


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证确认卡过期检查只读，坏时刻保留未知状态。
@pytest.mark.parametrize("expires,expected", [(99.0, ""), (101.0, "pending_approval"),
    ("bad", "activity_unknown"), (float("nan"), "activity_unknown")])
def test_confirmation_expiry_is_read_only(conversation, tmp_path, monkeypatch, expires, expected):
    import json

    store, row = conversation
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), subagents=SimpleNamespace(
        list_runs=lambda: [], list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])))
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})
    monkeypatch.setattr(idle.time, "time", lambda: 100.0)
    path = tmp_path / "data/private_interactions/confirmation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"kind": "model_configuration", "thread_id": row.thread_id,
        "status": "active", "expires_at": expires}))
    before = path.read_bytes()
    assert idle.idle_compact_busy_state(agent, store, row) == expected
    assert path.read_bytes() == before


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证不符合闲置维护条件时不会迁移空模型选择。
@pytest.mark.parametrize("scenario", ["disabled", "not_idle", "busy", "below_threshold"])
def test_ineligible_native_maintenance_never_migrates_empty_model(tmp_path, monkeypatch, scenario):
    agent = _agent(tmp_path, context_tokens=1000000)
    # 独立原Store没有默认模型回调，保留真实旧会话的空模型选择。
    store = ConversationStore(tmp_path / "old-conversations")
    agent.conversation_store = store
    row = store.threads.create()
    row = store.threads.update_atomic(row.thread_id, lambda t: replace(t, updated_at=100.0))
    assert not row.model_profile_id
    if scenario == "disabled":
        agent.config.memory_compact_idle_trigger_percent = 0
    monkeypatch.setattr(idle, "idle_compact_busy_state", lambda *_: "pending_approval" if scenario == "busy" else "")
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=1, context_window_tokens=1000000))
    before = store.storage.thread_path(row.thread_id).read_bytes()
    idle.idle_compact_maintenance_tick(agent, cursor={}, now=101.0 if scenario == "not_idle" else 7400.0)
    assert store.storage.thread_path(row.thread_id).read_bytes() == before


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 准备真实消息与用户活动基线，仅摘要provider和用量观察使用本地替身。
@pytest.fixture
def summary_case(tmp_path, monkeypatch):
    agent = _agent(tmp_path, context_tokens=1000000)
    context = _context(agent, _request(), "create", "start")
    store = agent.conversation_store
    for index in range(3):
        for role in ("user", "assistant"):
            store.messages.append({"thread_id": context.thread_id, "role": role, "content": f"原始材料{index}"})
    row = store.threads.record_user_activity(context.thread_id, now=100.0)
    agent.backend = _SummaryBackend()
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=650000, context_window_tokens=1000000))
    return agent, store, row


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证任何新角色消息追加都使旧闲置幂等标记失效。
@pytest.mark.parametrize("role", ["assistant", "user", "tool"])
def test_message_append_after_idle_compact_invalidates_idempotency(summary_case, monkeypatch, role):
    agent, store, row = summary_case
    assert idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)["compacted"]
    latest = store.threads.require(row.thread_id)
    metadata = latest.metadata.get("idle_auto_compact", {})
    assert metadata.get("last_idle_compacted_message_id")
    assert metadata.get("last_idle_compacted_message_count") == 6
    assert idle.maybe_auto_compact_idle_session(agent, store, latest, now=7500.0)["reason"] == "already_compacted"
    store.messages.append({"thread_id": row.thread_id, "role": role, "content": "新消息不经preflight"})
    assert store.threads.require(row.thread_id).metadata["user_activity_generation"] == row.metadata["user_activity_generation"]
    monkeypatch.setattr(idle, "prepare_conversation_context", lambda *_, **__: SimpleNamespace(compacted=False))
    assert idle.maybe_auto_compact_idle_session(agent, store, latest, now=7600.0)["reason"] != "already_compacted"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证变化后的消息超过普通阈值时确实进入准备链。
def test_changed_messages_above_regular_threshold_are_actually_prepared(summary_case, monkeypatch):
    agent, store, row = summary_case
    assert idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)["compacted"]
    store.messages.append({"thread_id": row.thread_id, "role": "assistant", "content": "超普通阈值的新消息"})
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=850000, context_window_tokens=1000000))
    calls = []
    monkeypatch.setattr(idle, "prepare_conversation_context", lambda *_, **__: calls.append(True) or SimpleNamespace(compacted=False))
    idle.maybe_auto_compact_idle_session(agent, store, row, now=7600.0)
    assert calls == [True]


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证摘要期间追加或同尺寸改写消息会中止CAS且不计模型失败。
@pytest.mark.parametrize("change", ["assistant", "user", "tool", "modify"])
def test_message_changes_during_summary_abort_cas_without_failure(summary_case, change):
    agent, store, row = summary_case
    generate = agent.backend.generate

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 在真实摘要调用期间追加或改写消息，保留原生成响应供CAS竞争。
    def racing_summary(*args, **kwargs):
        if change == "modify":
            path = store.storage.message_path(row.thread_id)
            before = path.read_bytes()
            path.write_bytes(before.replace("原始".encode(), "修改".encode()))
            assert path.stat().st_size == len(before)
        else:
            store.messages.append({"thread_id": row.thread_id, "role": change, "content": "摘要期间插入"})
        return generate(*args, **kwargs)

    agent.backend.generate = racing_summary
    result = idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)
    latest = store.threads.require(row.thread_id)
    assert result.get("cas_aborted") is True and not result["compacted"]
    assert latest.compact_generation == row.compact_generation
    assert latest.compact_consecutive_failures == 0 and latest.summary == ""


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证最后预检之后到达的新消息仍被存储CAS拦住。
def test_message_arriving_after_last_guard_is_rejected_by_store_cas(summary_case, monkeypatch):
    agent, store, row = summary_case
    original = store.threads.update_compact_state

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 在最后提交前追加真实消息，再调用原线程CAS写口。
    def before_cas(*args, **kwargs):
        store.messages.append({"thread_id": row.thread_id, "role": "assistant", "content": "最后检查之后追加"})
        return original(*args, **kwargs)

    monkeypatch.setattr(store.threads, "update_compact_state", before_cas)
    result = idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)
    assert result.get("cas_aborted") is True and not result["compacted"]
    latest = store.threads.require(row.thread_id)
    assert latest.compact_generation == 0 and latest.compact_consecutive_failures == 0


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证其它线程的子代理阻塞不污染本线程闲置状态。
def test_subagent_thread_attributes_keep_other_threads_independent(conversation, tmp_path, monkeypatch):
    store, row = conversation
    run = SimpleNamespace(status="BLOCKED", attributes={"conversation_thread_id": "thread-other"}, capability_requests=[])
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), subagents=SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(runs=[run], load_errors=[])))
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})
    assert idle.idle_compact_busy_state(agent, store, row) == ""


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证普通压缩记录实际消息基线，供闲置重复压缩门使用。
def test_regular_compact_records_message_baseline_for_idle_guard(summary_case, monkeypatch):
    agent, store, row = summary_case
    result = idle.prepare_conversation_context(agent, store, row, options=idle.ConversationCompactOptions(force=True))
    assert result.compacted
    marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert not errors and result.thread.metadata.get("last_compacted_message_tail") == marker
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=850000, context_window_tokens=1000000))
    assert idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)["reason"] == "regular_compacted"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证普通压缩不会宣称处理冻结来源之外的新消息。
@pytest.mark.parametrize("explicit_source", [False, True])
def test_regular_compact_never_claims_messages_outside_frozen_source(summary_case, monkeypatch, explicit_source):
    from agent_py_agent.agent.conversation import compact as compact_chain

    agent, store, row = summary_case
    before_marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert not errors
    load = compact_chain.load_conversation_compact_source

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 冻结真实摘要来源后追加消息，证明候选不能覆盖新消息。
    def frozen_then_append(*args, **kwargs):
        source = load(*args, **kwargs)
        message = store.messages.append({"thread_id": row.thread_id, "role": "assistant", "content": "来源冻结后追加"})
        assert message.message_id not in {item.message_id for item in source.messages}
        return source

    if explicit_source:
        options = idle.ConversationCompactOptions(force=True, source=frozen_then_append(agent, store, row))
    else:
        monkeypatch.setattr(compact_chain, "load_conversation_compact_source", frozen_then_append)
        options = idle.ConversationCompactOptions(force=True)
    result = idle.prepare_conversation_context(agent, store, row, options=options)
    assert result.compacted
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=850000, context_window_tokens=1000000))
    calls = []
    monkeypatch.setattr(idle, "prepare_conversation_context", lambda *_, **__: calls.append(True) or SimpleNamespace(compacted=False))
    idle.maybe_auto_compact_idle_session(agent, store, result.thread, now=7600.0)
    assert calls == [True], "未摘要的新消息不能被regular_compacted门挡住"
    assert result.thread.metadata.get("last_compacted_message_tail") == (None if explicit_source else before_marker)


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证损坏消息尾标记报告错误，而非伪装成空历史。
def test_corrupt_message_marker_is_not_an_empty_tail(conversation):
    store, row = conversation
    path = store.storage.message_path(row.thread_id)
    path.write_bytes(b"{broken\n")
    marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert errors and not marker
    assert path.read_bytes() == b"{broken\n"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证闲置维护每拍按实际配置上限处理线程。
@pytest.mark.parametrize("limit", [0, 1, 3])
def test_idle_maintenance_reads_configured_limit(conversation, tmp_path, monkeypatch, limit):
    store, _ = conversation
    for _ in range(4):
        store.threads.create()
    config = AgentConfig()
    assert getattr(config, "memory_compact_idle_max_per_tick", None) == 2
    config.memory_compact_idle_max_per_tick = limit
    agent = SimpleNamespace(config=config, conversation_store=store, home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    calls = []
    monkeypatch.setattr(idle, "maybe_auto_compact_idle_session", lambda *args, **__: calls.append(args[-1].thread_id) or {"compacted": False})
    report = idle.idle_compact_maintenance_tick(agent, cursor={})
    assert len(calls) == report["checked"] == limit


# LLM: 只替换候选容量观察，摘要、用户活动、消息追加和线程失败账均走真实入口；不调用真实供应商。
# 函数用途: 验证候选计量期间出现活动或消息变化时干净让位，无竞争的超量候选仍记一次失败。
@pytest.mark.parametrize("change", ["none", "activity", "message"])
def test_idle_oversized_candidate_measurement_failure_cas(summary_case, monkeypatch, change):
    from agent_py_agent.agent.conversation import compact as compact_chain
    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError

    agent, store, row = summary_case
    # 原话备份使用正式零预算配置，候选无需再收缩重测，确保竞争落在最后一次容量观察中。
    agent.config.compact_landmark_max_tokens = 0
    measured = []

    # LLM: 在摘要完成后的真实计量调用内制造竞争，不替换中断检查、失败记录、线程锁或CAS。
    # 函数用途: 将候选测为超量，并在首次最终计量时登记活动或仅追加规范消息。
    def measure(_agent, summary, *_args, **_kwargs):
        if summary:
            if not measured:
                if change == "activity":
                    store.threads.record_user_activity(row.thread_id, now=7401.0)
                elif change == "message":
                    store.messages.append({"thread_id": row.thread_id, "role": "assistant", "content": "最终计量期间追加"})
            measured.append(True)
            return 2_000_000
        return 650_000

    monkeypatch.setattr(compact_chain, "_projected_context_tokens", measure)
    if change == "none":
        with pytest.raises(ConversationCompactError) as failure:
            idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)
        assert failure.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    else:
        result = idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)
        assert result.get("cas_aborted") is True and not result["compacted"]
        assert result["reason"] == "activity_changed"
    assert measured, "竞争必须发生在摘要后的候选计量，而非摘要调用前"
    latest = store.threads.require(row.thread_id)
    assert latest.compact_consecutive_failures == (1 if change == "none" else 0)
    assert latest.compact_failure_code == ("COMPACT_CANDIDATE_TOO_LARGE" if change == "none" else "")
    assert latest.compact_generation == row.compact_generation and latest.summary == row.summary
    assert latest.compact_checkpoint_id == row.compact_checkpoint_id
    assert latest.metadata["user_activity_generation"] == row.metadata["user_activity_generation"] + (change == "activity")


# LLM: 仅在失败存储入口前插入真实竞争，调用原失败方法；必须验证锁内CAS，不能只靠计量后的取消检查。
# 函数用途: 验证最后一次取消预检之后到达的活动或消息不会污染压缩失败账。
@pytest.mark.parametrize("change", ["activity", "message"])
def test_idle_oversized_failure_recording_rechecks_store_cas(summary_case, monkeypatch, change):
    from agent_py_agent.agent.conversation import compact as compact_chain

    agent, store, row = summary_case
    agent.config.compact_landmark_max_tokens = 0
    original = store.threads.record_compact_failure
    attempted = []
    monkeypatch.setattr(compact_chain, "_projected_context_tokens", lambda _agent, summary, *_, **__: 2_000_000 if summary else 650_000)

    # LLM: 保留真实原子写口；活动改变只走线程活动登记，消息改变不增活动代，二者必须分别被CAS拒绝。
    # 函数用途: 在进入原失败记录之前注入一次迟到变化。
    def before_failure(*args, **kwargs):
        attempted.append(True)
        if change == "activity":
            store.threads.record_user_activity(row.thread_id, now=7401.0)
        else:
            store.messages.append({"thread_id": row.thread_id, "role": "assistant", "content": "失败写入口前追加"})
        return original(*args, **kwargs)

    monkeypatch.setattr(store.threads, "record_compact_failure", before_failure)
    result = idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)
    latest = store.threads.require(row.thread_id)
    assert attempted == [True] and result.get("cas_aborted") is True and not result["compacted"]
    assert latest.compact_consecutive_failures == 0 and latest.compact_failure_code == ""
    assert latest.compact_generation == row.compact_generation and latest.summary == row.summary


# LLM: 直接调用真实失败存储门，不提供外围取消回调；必须拒绝过期活动、消息或压缩代，且已有失败数不能被清零。
# 函数用途: 验证失败CAS本身保留最新状态，不依赖Gateway把异常掩盖成成功。
@pytest.mark.parametrize("change", ["activity", "message", "rewrite", "generation", "corrupt"])
def test_idle_failure_store_refuses_stale_baseline(summary_case, change):
    from agent_py_agent.agent.conversation.store_threads import IdleCompactActivityChanged

    _agent_value, store, row = summary_case
    marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert not errors
    store.threads.update_atomic(row.thread_id, lambda t: replace(t, compact_consecutive_failures=2,
        compact_failure_code="COMPACT_PREVIOUS_FAILURE", compact_failure_updated_at=120.0))
    if change == "activity":
        store.threads.record_user_activity(row.thread_id, now=7401.0)
    elif change == "message":
        store.messages.append({"thread_id": row.thread_id, "role": "tool", "content": "规范消息单独变化"})
    elif change == "generation":
        store.threads.update_atomic(row.thread_id, lambda t: replace(t, compact_generation=t.compact_generation + 1))
    else:
        path = store.storage.message_path(row.thread_id)
        data = path.read_bytes()
        path.write_bytes(data.replace("原始".encode(), "改写".encode()) if change == "rewrite" else data + b"{broken\n")
    before = store.storage.thread_path(row.thread_id).read_bytes()
    with pytest.raises(IdleCompactActivityChanged):
        store.threads.record_compact_failure(row.thread_id, failure_code="COMPACT_CANDIDATE_TOO_LARGE",
            expected_generation=row.compact_generation, expected_user_activity_generation=row.metadata["user_activity_generation"],
            expected_message_tail=marker, now=7402.0)
    assert store.storage.thread_path(row.thread_id).read_bytes() == before
    latest = store.threads.require(row.thread_id)
    assert latest.compact_consecutive_failures == 2 and latest.compact_failure_code == "COMPACT_PREVIOUS_FAILURE"


# LLM: 子进程只操作测试会话；真实append写完仍持原消息双层锁，用事件控制解锁，不模拟锁或供应商。
# 函数用途: 为跨进程回归提供先写入、后放锁的消息写者。
def _append_with_held_message_lock(root, thread_id, written, release):
    from contextlib import contextmanager

    from agent_py_agent.agent.common import json_io

    store = ConversationStore(root)
    message_path = store.storage.message_path(thread_id)
    original = json_io._locked_json_path

    # LLM: 包装原锁但不替换其线程锁或OS排他锁；只在真实写入结束、原锁退出之前暂停。
    # 函数用途: 暴露消息已落盘且追加锁仍被持有的确定观察点。
    @contextmanager
    def hold_after_write(path, **kwargs):
        with original(path, **kwargs):
            yield
            if path == message_path:
                written.set()
                assert release.wait(20), "父进程未按约释放真实消息锁"

    json_io._locked_json_path = hold_after_write
    try:
        store.messages.append({"thread_id": thread_id, "role": "assistant", "content": "跨进程持锁追加"})
    finally:
        json_io._locked_json_path = original


# LLM: spawn进程走真实append，父进程非阻塞探针确认OS锁确实繁忙后再等待原阻塞锁；不用sleep猜竞争时序。
# 函数用途: 证明失败记录等到消息写者放锁后会重读指纹并拒绝旧候选，原有失败数不增不清零。
def test_idle_failure_cas_waits_for_real_message_append_lock(summary_case, monkeypatch):
    import multiprocessing
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.conversation.store_threads import IdleCompactActivityChanged

    _agent_value, store, row = summary_case
    marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert not errors
    store.threads.update_atomic(row.thread_id, lambda t: replace(t, compact_consecutive_failures=2))
    context = multiprocessing.get_context("spawn")
    written, release = context.Event(), context.Event()
    contended = threading.Event()
    writer = context.Process(target=_append_with_held_message_lock,
        args=(str(store.storage.root), row.thread_id, written, release))
    original = json_io.locked_json_path

    # LLM: 探针与真正等待都调用原双层锁，只有非阻塞取得真实OS锁失败后才通知主线程释放写者。
    # 函数用途: 记录本次失败存储确实遇到了另一进程持有的消息锁。
    @contextmanager
    def observe_contention(path, **kwargs):
        if path == store.storage.message_path(row.thread_id):
            with pytest.raises(BlockingIOError):
                with original(path, blocking=False):
                    pass
            contended.set()
        with original(path, **kwargs):
            yield

    writer.start()
    try:
        assert written.wait(15), "子进程未完成真实append"
        monkeypatch.setattr(json_io, "locked_json_path", observe_contention)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(store.threads.record_compact_failure, row.thread_id,
                failure_code="COMPACT_CANDIDATE_TOO_LARGE", expected_generation=row.compact_generation,
                expected_user_activity_generation=row.metadata["user_activity_generation"], expected_message_tail=marker)
            try:
                assert contended.wait(10), "失败写口没有竞争真实消息锁"
            finally:
                release.set()
            with pytest.raises(IdleCompactActivityChanged):
                future.result(timeout=15)
    finally:
        release.set()
        writer.join(timeout=15)
        if writer.is_alive():
            writer.terminate()
            writer.join(timeout=5)
    assert writer.exitcode == 0
    latest = store.threads.require(row.thread_id)
    assert latest.compact_generation == row.compact_generation and latest.compact_consecutive_failures == 2


# LLM: 在真实消息指纹校验后、原线程事务读取前暂停失败写者，主线程沿真实活动写口先赢，不替换CAS回调。
# 函数用途: 证明失败事务会读取最新活动代，而不是持消息锁前捕获的旧线程快照。
def test_idle_failure_cas_reads_latest_activity_after_message_check(summary_case, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.conversation.store_threads import IdleCompactActivityChanged

    _agent_value, store, row = summary_case
    marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert not errors
    store.threads.update_atomic(row.thread_id, lambda t: replace(t, compact_consecutive_failures=2))
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    original = store.threads.update_atomic

    # LLM: 只暂停工作线程的原事务入口；主线程活动登记继续使用同一原子更新实现及真实线程文件锁。
    # 函数用途: 精确控制活动代先提交、旧失败后读取的时序。
    def wait_before_transaction(*args, **kwargs):
        if threading.get_ident() != main_thread:
            entered.set()
            assert release.wait(15), "失败事务未被释放"
        return original(*args, **kwargs)

    monkeypatch.setattr(store.threads, "update_atomic", wait_before_transaction)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(store.threads.record_compact_failure, row.thread_id,
            failure_code="COMPACT_CANDIDATE_TOO_LARGE", expected_generation=row.compact_generation,
            expected_user_activity_generation=row.metadata["user_activity_generation"], expected_message_tail=marker)
        try:
            assert entered.wait(10), "失败记录未到达线程事务"
            store.threads.record_user_activity(row.thread_id, now=7401.0)
        finally:
            release.set()
        with pytest.raises(IdleCompactActivityChanged):
            future.result(timeout=15)
    latest = store.threads.require(row.thread_id)
    assert latest.compact_generation == row.compact_generation and latest.compact_consecutive_failures == 2
    assert latest.metadata["user_activity_generation"] == row.metadata["user_activity_generation"] + 1
