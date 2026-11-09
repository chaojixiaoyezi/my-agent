"""已知TaskLink身份的读者不能再遍历线程历史；坏目标仍保持原未知边界。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import run_task_workspace_writer as workspace
from agent_py_agent.agent.agent_core.tool_loop import completion
from agent_py_agent.agent.conversation import ConversationStore, runtime, store_tasks
from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id
from agent_py_agent.agent.ingestion.source_worker import audit_parent_reconcile_state

_READERS = (
    "workspace",
    "background_status",
    "background_link",
    "claim_scope",
    "audit_prepare",
    "audit_facts",
    "audit_parent",
)


def _case(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "isolated", "channel": "internal"})
    old = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "old"})
    store.tasks.update_status({"task_id": old.task_id, "status": "completed"})
    target = store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-target",
            "work_kind": "audit",
            "work_name": "exact-audit",
            "status": "active",
            "duration_seconds": 86400,
            "cancellation_scope": "detached",
        }
    )
    store.storage.task_path(old.task_id).write_text("{broken", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("exact reader traversed task history")

    for name in ("list_report", "list", "iter_task_ids"):
        monkeypatch.setattr(store.tasks, name, forbidden)
    calls, read = [], store_tasks._read_task_link

    def point(path, task_id, **kwargs):
        calls.append(task_id)
        return read(path, task_id, **kwargs)

    monkeypatch.setattr(store_tasks, "_read_task_link", point)
    return store, thread, target, calls


def _call(reader, store, thread_id, task_id):
    agent = SimpleNamespace(conversation_store=store)
    if reader == "workspace":
        return workspace._conversation_task_link(store, thread_id, task_id)
    if reader == "background_status":
        return runtime._background_task_link_status(
            agent, SimpleNamespace(thread_id=thread_id, task_id=task_id)
        )
    if reader == "background_link":
        return runtime._background_conversation_task_link(agent, thread_id, task_id)
    if reader == "claim_scope":
        return runtime._background_claim_scope_id(store, thread_id, task_id)
    if reader == "audit_prepare":
        return completion._matching_audit_prepare_link(store, thread_id, task_id, "exact-audit")
    if reader == "audit_facts":
        params = SimpleNamespace(
            task_id=task_id,
            task_attributes={
                "conversation_thread_id": thread_id,
                "conversation_task_id": task_id,
                completion.CONVERSATION_WORK_KIND_ATTR: "audit",
            },
        )
        return completion._active_named_audit_facts(agent, params)
    return audit_parent_reconcile_state(agent, task_id)


def _unavailable(reader, *, missing=False):
    if reader == "audit_parent":
        return False, "inactive" if missing else "unavailable"
    if reader == "audit_facts":
        return (
            {}
            if missing
            else {
                "task_id": "audit-target",
                "work_kind": "audit",
                "state_available": False,
                "continues_without_more_user_input": True,
            }
        )
    return "" if reader in {"background_status", "claim_scope"} else None


@pytest.mark.parametrize("reader", _READERS)
def test_exact_task_readers_ignore_unrelated_history_and_only_load_target(
    tmp_path, monkeypatch, reader
):
    store, thread, target, calls = _case(tmp_path, monkeypatch)
    result = _call(reader, store, thread.thread_id, target.task_id)
    if reader == "background_status":
        assert result == "active"
    elif reader == "claim_scope":
        assert result == detached_task_claim_scope_id(thread.thread_id, target.task_id)
    elif reader == "audit_facts":
        assert result["state_available"] is True and result["task_id"] == target.task_id
    elif reader == "audit_parent":
        assert result == (True, "active")
    else:
        assert result == target
    assert calls and set(calls) == {target.task_id} and len(calls) <= 2


@pytest.mark.parametrize("reader", _READERS)
@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_exact_task_readers_keep_missing_and_corrupt_boundaries(
    tmp_path, monkeypatch, reader, fault
):
    store, thread, target, _calls = _case(tmp_path, monkeypatch)
    path = store.storage.task_path(target.task_id)
    if fault == "missing":
        path.unlink()
    else:
        path.write_text("{broken target", encoding="utf-8")
    assert _call(reader, store, thread.thread_id, target.task_id) == _unavailable(
        reader, missing=fault == "missing"
    )


@pytest.mark.parametrize("reader", _READERS[:-1])
def test_exact_task_readers_reject_foreign_thread(tmp_path, monkeypatch, reader):
    store, _thread, target, _calls = _case(tmp_path, monkeypatch)
    other = store.threads.create(canonical_user_id="other")
    assert _call(reader, store, other.thread_id, target.task_id) == _unavailable(
        reader, missing=True
    )


_ACTIVE_READERS = ("named_kind", "named_select", "exact_audit", "audit_conflict")


def _active_call(reader, store, thread_id, target):
    from agent_py_agent.agent.conversation import audit_lifecycle, named_work

    if reader == "named_kind":
        return named_work._resolve_named_work_kind(store, thread_id, target.work_name)
    if reader == "named_select":
        return named_work._select_named_audit(store, thread_id, target.work_name)
    if reader == "exact_audit":
        return audit_lifecycle.resolve_exact_active_audit(store, thread_id, target.work_name)
    return store.audits._active_named_work_conflict(target, target.task_id)


@pytest.mark.parametrize("reader", _ACTIVE_READERS)
def test_active_named_readers_exclude_terminal_history(tmp_path, monkeypatch, reader):
    store, thread, target, calls = _case(tmp_path, monkeypatch)
    result = _active_call(reader, store, thread.thread_id, target)
    expected = {
        "named_kind": ("audit", ""),
        "named_select": (target.task_id, ""),
        "exact_audit": target,
        "audit_conflict": False,
    }
    assert result == expected[reader]
    assert calls == [target.task_id]


@pytest.mark.parametrize("reader", _ACTIVE_READERS)
def test_active_named_readers_reject_corrupt_active_link(tmp_path, monkeypatch, reader):
    from agent_py_agent.agent.conversation.audit_lifecycle import AuditLifecycleError
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    store, thread, target, _calls = _case(tmp_path, monkeypatch)
    store.storage.task_path(target.task_id).write_text("{broken", encoding="utf-8")
    if reader in {"named_kind", "named_select"}:
        assert _active_call(reader, store, thread.thread_id, target) == (
            "",
            "CONVERSATION_TASK_STATE_UNAVAILABLE",
        )
    else:
        with pytest.raises(AuditLifecycleError if reader == "exact_audit" else DataCorruptionError):
            _active_call(reader, store, thread.thread_id, target)
