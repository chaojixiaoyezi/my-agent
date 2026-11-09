"""RES-W5：真实会话读取降级，丢失链接不修索引，损坏活跃链接仍拒绝。"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_history_seed import (
    prepare_background_history_or_raise,
)
from agent_py_agent.agent.conversation.history_seed import seed_text_messages
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_context as context_module
from agent_py_agent.agent.gateway_parts.request_context import GatewayConversationLoadRequest
from agent_py_agent.agent.gateway_parts.request_errors import (
    ConversationPersistenceError,
    ConversationTaskBindingError,
)
from agent_py_agent.agent.gateway_parts.request_execution import _require_gateway_conversation_ready
from agent_py_agent.agent.settings import AgentConfig


@pytest.fixture
def case(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                   auto_save_memory=False), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "res-w5", "channel": "chat", "cwd": str(tmp_path),
        "owner_id": agent.home_paths.owner_id, "owner_home": str(agent.home_paths.owner_home_dir),
    })
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "preserved history"})
    request = {"working_dir": str(tmp_path), "conversation": {"thread_id": thread.thread_id}}
    return SimpleNamespace(agent=agent, store=store, thread=thread, request=request)


def _context(case):
    # 空request_id不更新用户活动；可直接比较读路径前后的完整线程字节。
    return context_module.gateway_conversation_context(GatewayConversationLoadRequest(
        case.agent, case.request, "", "continue", defer_compact=True))


def _link(case, task_id, status):
    workspace = Path(case.agent.home_paths.owner_home_dir) / "tasks" / task_id
    workspace.mkdir(parents=True)
    return case.store.tasks.bind({"thread_id": case.thread.thread_id, "task_id": task_id,
                                  "status": status, "task_path": str(workspace), "goal": task_id})


def _events(caplog, code):
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith(code + " ")]


def _warning_ids(context):
    return {row.get("task_id") or row.get("id") for row in context.load_warnings}


def test_corrupt_goal_degrades_with_one_body_free_warning(case, caplog):
    path = case.store.storage.goal_path(case.thread.thread_id)
    path.write_text("{PRIVATE_GOAL_PAYLOAD", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        context = _context(case)
    assert context.load_errors == ()
    assert context.thread_goal is None
    assert context.load_warnings
    assert len(_events(caplog, "GATEWAY_CONTEXT_DEGRADED")) == 1
    assert "PRIVATE_GOAL_PAYLOAD" not in caplog.text
    assert path.read_text(encoding="utf-8") == "{PRIVATE_GOAL_PAYLOAD"
    _require_gateway_conversation_ready(case.request, context)


def test_corrupt_historical_link_keeps_readable_workspace_candidates(case, caplog):
    _link(case, "old-corrupt", "completed")
    _link(case, "old-readable", "completed")
    _link(case, "live", "active")
    _context(case)  # 先加载，确保缓存不掩盖随后出现的损坏。
    case.store.storage.task_path("old-corrupt").write_text("{broken", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        context = _context(case)
    assert context.load_errors == ()
    assert "old-corrupt" in _warning_ids(context)
    assert context.named_work == ()
    assert context.workspace_task.task_id == "live"
    assert {row["task_id"] for row in context.recent_task_workspaces} == {"old-readable"}
    assert len(_events(caplog, "GATEWAY_CONTEXT_DEGRADED")) == 1
    _require_gateway_conversation_ready(case.request, context)


@pytest.mark.parametrize("fault", ["corrupt", "identity"])
def test_bad_active_link_stays_blocking_in_both_contexts(case, fault):
    link = _link(case, "live", "active")
    _context(case)
    path = case.store.storage.task_path("live")
    payload = {**link.to_dict(), "task_id": "foreign-task"}
    path.write_text("{broken" if fault == "corrupt" else json.dumps(payload), encoding="utf-8")
    context = _context(case)
    assert any(row.get("task_id") == "live" for row in context.load_errors)
    with pytest.raises(ConversationPersistenceError):
        _require_gateway_conversation_ready(case.request, context)
    _bundle, errors = case.store.context_bundle_report(case.thread.thread_id)
    assert any(row.get("task_id") == "live" for row in errors)


def test_torn_observation_with_workspace_lineage_is_only_a_warning(case, caplog):
    _link(case, "live", "active")
    path = case.store.storage.observation_path(case.thread.thread_id)
    path.write_text("{torn observation\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        context = _context(case)
    assert context.workspace_task.task_id == "live"
    assert context.load_errors == ()
    assert any(row.get("context") == "conversation.observations.window" for row in context.load_warnings)
    assert len(_events(caplog, "GATEWAY_CONTEXT_DEGRADED")) == 1
    assert path.read_text(encoding="utf-8") == "{torn observation\n"
    _require_gateway_conversation_ready(case.request, context)


@pytest.mark.parametrize("missing", ["directory", "active", "historical"])
def test_deleted_links_degrade_without_repair_and_background_keeps_history(case, caplog, missing):
    _link(case, "old", "completed")
    _link(case, "live", "active")
    assert _context(case).workspace_task.task_id == "live"
    thread_path = case.store.storage.thread_path(case.thread.thread_id)
    before = thread_path.read_bytes()
    if missing == "directory":
        shutil.rmtree(case.store.storage.tasks_dir)
        lost = {"old", "live"}
    else:
        task_id = "live" if missing == "active" else "old"
        case.store.storage.task_path(task_id).unlink()
        lost = {task_id}
    with caplog.at_level(logging.WARNING):
        context = _context(case)
    assert context.load_errors == ()
    assert _warning_ids(context) == lost
    if missing != "historical":
        assert context.workspace_task is None
        assert context.cwd == str(case.agent.root)
    assert len(_events(caplog, "GATEWAY_CONTEXT_DEGRADED")) == 1
    _require_gateway_conversation_ready(case.request, context)
    assert thread_path.read_bytes() == before

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        bundle, errors = case.store.context_bundle_report(case.thread.thread_id)
    assert errors == []
    assert {row["task_id"] for row in bundle["tasks"]}.isdisjoint(lost)
    events = _events(caplog, "TASK_LINK_DEGRADED")
    assert len(events) == 1 and "context=conversation.context_bundle" in events[0]
    assert all(task_id in events[0] for task_id in lost)
    request = SimpleNamespace(thread_id=case.thread.thread_id, task_id="", reason="scheduled_progress_report",
                              wake_signal=None, conversation_turn_id="background-turn")
    result = prepare_background_history_or_raise(case.agent, case.store,
                                               case.store.threads.require(case.thread.thread_id), request)
    assert result.status == "ready"
    assert ("user", "preserved history") in seed_text_messages(result.seed)
    assert thread_path.read_bytes() == before
    assert case.store.threads.require(case.thread.thread_id).active_task_ids == ("live",)
    assert all(not case.store.storage.task_path(task_id).exists() for task_id in lost)
    if missing == "directory":
        assert not case.store.storage.tasks_dir.exists()


@pytest.mark.parametrize("deferred", [False, True])
def test_compact_reset_source_thread_reaches_prepare_and_progress(case, monkeypatch, deferred):
    source = context_module.load_conversation_compact_source(case.agent, case.store, case.thread)
    reset_thread = replace(case.thread, compact_generation=case.thread.compact_generation + 1)
    source = replace(source, thread=reset_thread)
    monkeypatch.setattr(context_module, "load_conversation_compact_source", lambda *_a, **_k: source)
    seen = []

    def progress(_sink, *, store, thread):
        assert store is case.store
        seen.append(("progress", thread))

    def prepare(_agent, store, thread, *, options):
        assert store is case.store and options.source is source
        seen.append(("prepare", thread))
        return SimpleNamespace(thread=thread, messages=source.messages, trigger_tokens=source.policy.trigger_tokens,
                               recent_operation_evidence=source.recent_operation_evidence, compacted=False)

    monkeypatch.setattr(context_module, "_gateway_compact_progress_callback", progress)
    monkeypatch.setattr(context_module, "prepare_conversation_context", prepare)
    inputs = GatewayConversationLoadRequest(case.agent, case.request, "", "continue", defer_compact=deferred)
    loaded = context_module._load_gateway_compact_context(inputs, case.store, case.thread)
    assert loaded[0] is reset_thread
    assert seen == ([] if deferred else [("progress", reset_thread), ("prepare", reset_thread)])


@pytest.mark.parametrize("error", [
    {"context": "store-level", "error_type": "OSError"},
    {"context": "store-level", "error_type": "FileNotFoundError"},
])
def test_task_store_errors_without_identity_stay_blocking(case, monkeypatch, error):
    def report(_thread_id, *, classify_missing=False):
        return ([], [error], []) if classify_missing else ([], [error])

    monkeypatch.setattr(case.store.tasks, "list_report", report)
    monkeypatch.setattr(case.store.tasks, "context_report",
                        lambda *_args, **_kwargs: ([], [error], [], {"status": "unknown", "history_complete": False}))
    context = _context(case)
    assert error in context.load_errors
    with pytest.raises(ConversationPersistenceError):
        _require_gateway_conversation_ready(case.request, context)
    _bundle, errors = case.store.context_bundle_report(case.thread.thread_id)
    assert error in errors


@pytest.mark.parametrize("binding", ["request", "goal"])
def test_deleted_exact_binding_falls_back_without_repair(case, binding):
    task_id = "live"
    if binding == "goal":
        goal = case.store.goals.create({"thread_id": case.thread.thread_id, "objective": "keep serving"})
        task_id = goal.task_id
    else:
        case.request["conversation_runtime"] = {"thread_id": case.thread.thread_id, "task_id": task_id}
    _link(case, task_id, "active")
    assert _context(case).workspace_task.task_id == task_id
    thread_path = case.store.storage.thread_path(case.thread.thread_id)
    before = thread_path.read_bytes()
    case.store.storage.task_path(task_id).unlink()
    context = _context(case)
    assert context.load_errors == ()
    assert task_id in _warning_ids(context)
    assert context.workspace_task is None
    assert context.cwd == str(case.agent.root)
    _require_gateway_conversation_ready(case.request, context)
    assert thread_path.read_bytes() == before
    assert not case.store.storage.task_path(task_id).exists()


def test_unknown_exact_binding_still_fails_closed(case):
    case.request["conversation_runtime"] = {"thread_id": case.thread.thread_id, "task_id": "unknown"}
    with pytest.raises(ConversationTaskBindingError):
        _context(case)


def test_degrade_preserves_plain_list_behavior():
    error = {"context": "context-only", "error_type": "OSError"}
    plain = []
    context_module._degrade(plain, error)
    assert plain == [error]
    classified = context_module._GatewayLoadErrors()
    context_module._degrade(classified, error)
    assert classified == [] and classified.warnings == [error]


def test_missing_history_index_error_keeps_identity_free_diagnostic_blocking(case):
    import shutil

    from agent_py_agent.agent.conversation.task_history import history_root

    _link(case, "live", "active")
    root = history_root(case.store.storage.thread_path(case.thread.thread_id))
    shutil.rmtree(root)
    _links, errors, warnings = case.store.tasks.list_report(case.thread.thread_id, classify_missing=True)
    assert not warnings
    assert errors and errors[0]["error_type"] == "FileNotFoundError" and not errors[0].get("task_id")
    _bundle, context_errors = case.store.context_bundle_report(case.thread.thread_id)
    assert errors == context_errors
