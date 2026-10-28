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


@pytest.fixture
def conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    return store, store.threads.create(canonical_user_id="user")


def test_status_parser_preserves_cli_transport():
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text

    command = parse_conversation_control("/compact status")
    assert command.valid and command.operation == "status"
    assert _command_text(command) == "/compact status"
    assert parse_conversation_control("/compact preserve status evidence").operation == "run"


@pytest.mark.parametrize("value", ["reset", "default", "71"])
def test_threshold_cli_transport_roundtrip(value):
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text

    command = parse_conversation_control("/compact threshold " + value)
    restored = parse_conversation_control(_command_text(command))
    assert restored.operation == command.operation == "threshold"
    assert restored.value == command.value == value


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


def test_corrupted_subagent_persistence_is_not_idle(tmp_path):
    agent = _agent(tmp_path, context_tokens=1000000)
    store = agent.conversation_store
    row = store.threads.create()
    task_file = agent.subagents.persistence.workspace / "broken-run" / "task.json"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("{invalid json", encoding="utf-8")
    assert agent.subagents.list_runs_report().load_errors
    assert idle.idle_compact_busy_state(agent, store, row) == "activity_unknown"


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


def test_changed_messages_above_regular_threshold_are_actually_prepared(summary_case, monkeypatch):
    agent, store, row = summary_case
    assert idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)["compacted"]
    store.messages.append({"thread_id": row.thread_id, "role": "assistant", "content": "超普通阈值的新消息"})
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=850000, context_window_tokens=1000000))
    calls = []
    monkeypatch.setattr(idle, "prepare_conversation_context", lambda *_, **__: calls.append(True) or SimpleNamespace(compacted=False))
    idle.maybe_auto_compact_idle_session(agent, store, row, now=7600.0)
    assert calls == [True]


@pytest.mark.parametrize("change", ["assistant", "user", "tool", "modify"])
def test_message_changes_during_summary_abort_cas_without_failure(summary_case, change):
    agent, store, row = summary_case
    generate = agent.backend.generate

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


def test_message_arriving_after_last_guard_is_rejected_by_store_cas(summary_case, monkeypatch):
    agent, store, row = summary_case
    original = store.threads.update_compact_state

    def before_cas(*args, **kwargs):
        store.messages.append({"thread_id": row.thread_id, "role": "assistant", "content": "最后检查之后追加"})
        return original(*args, **kwargs)

    monkeypatch.setattr(store.threads, "update_compact_state", before_cas)
    result = idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)
    assert result.get("cas_aborted") is True and not result["compacted"]
    latest = store.threads.require(row.thread_id)
    assert latest.compact_generation == 0 and latest.compact_consecutive_failures == 0


def test_subagent_thread_attributes_keep_other_threads_independent(conversation, tmp_path, monkeypatch):
    store, row = conversation
    run = SimpleNamespace(status="BLOCKED", attributes={"conversation_thread_id": "thread-other"}, capability_requests=[])
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), subagents=SimpleNamespace(
        list_runs_report=lambda: SimpleNamespace(runs=[run], load_errors=[])))
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})
    assert idle.idle_compact_busy_state(agent, store, row) == ""


def test_regular_compact_records_message_baseline_for_idle_guard(summary_case, monkeypatch):
    agent, store, row = summary_case
    result = idle.prepare_conversation_context(agent, store, row, options=idle.ConversationCompactOptions(force=True))
    assert result.compacted
    marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert not errors and result.thread.metadata.get("last_compacted_message_tail") == marker
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: SimpleNamespace(projected_tokens=850000, context_window_tokens=1000000))
    assert idle.maybe_auto_compact_idle_session(agent, store, row, now=7400.0)["reason"] == "regular_compacted"


@pytest.mark.parametrize("explicit_source", [False, True])
def test_regular_compact_never_claims_messages_outside_frozen_source(summary_case, monkeypatch, explicit_source):
    from agent_py_agent.agent.conversation import compact as compact_chain

    agent, store, row = summary_case
    before_marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert not errors
    load = compact_chain.load_conversation_compact_source

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


def test_corrupt_message_marker_is_not_an_empty_tail(conversation):
    store, row = conversation
    path = store.storage.message_path(row.thread_id)
    path.write_bytes(b"{broken\n")
    marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert errors and not marker
    assert path.read_bytes() == b"{broken\n"


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
