"""精确任务查询只读目标链接，历史坏账仍留给全量审计入口。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore, store_tasks
from agent_py_agent.agent.conversation.models import ThreadTaskLink
from agent_py_agent.agent.conversation.task_runtime_state import _task_link


def _store_and_thread(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {"canonical_user_id": "cat3-owner", "channel": "internal"}
    )
    return store, thread


def test_exact_lookup_reads_one_link_among_500_historical_tasks(tmp_path, monkeypatch):
    store, thread = _store_and_thread(tmp_path)
    task_ids = tuple(f"task-{index:04d}" for index in range(500))
    for task_id in task_ids:
        link = ThreadTaskLink(
            thread_id=thread.thread_id, task_id=task_id, goal="历史任务", status="done"
        )
        store.storage.task_path(task_id).write_text(
            json.dumps(link.to_dict()), encoding="utf-8"
        )
    store.storage.thread_path(thread.thread_id).write_text(
        json.dumps(replace(thread, task_ids=task_ids).to_dict()), encoding="utf-8"
    )
    assert len(store.threads.require(thread.thread_id).task_ids) == 500

    reads = []
    original = store_tasks._read_task_link

    def counted_read(path, task_id, **kwargs):
        reads.append(path)
        return original(path, task_id, **kwargs)

    def forbidden_history_read(*_args, **_kwargs):
        pytest.fail("精确查询不得加载线程历史任务或遍历链接")

    monkeypatch.setattr(store_tasks, "_read_task_link", counted_read)
    monkeypatch.setattr(store.tasks, "list_report", forbidden_history_read)
    monkeypatch.setattr(store.tasks, "_require_thread", forbidden_history_read)
    errors = []

    selected = _task_link(store, thread.thread_id, task_ids[-1], errors)

    assert selected is not None and selected.task_id == task_ids[-1]
    assert reads == [store.storage.task_path(task_ids[-1])]
    assert errors == []


def test_exact_lookup_rejects_task_from_another_thread(tmp_path):
    store, thread = _store_and_thread(tmp_path)
    link = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "foreign-task"})
    errors = []

    assert store.tasks.link_report("another-thread", link.task_id) == (None, None)
    assert _task_link(store, "another-thread", link.task_id, errors) is None
    assert errors == []


@pytest.mark.parametrize("payload", ["not-json", "[]", '{"task_id": "different-task"}'])
def test_target_corruption_reaches_load_errors(tmp_path, payload):
    store, thread = _store_and_thread(tmp_path)
    link = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "broken-task"})
    path = store.storage.task_path(link.task_id)
    path.write_text(payload, encoding="utf-8")
    errors = []

    assert _task_link(store, thread.thread_id, link.task_id, errors) is None
    assert len(errors) == 1
    assert errors[0]["context"] == "conversation.task_link.read"
    assert errors[0]["task_id"] == link.task_id
    assert errors[0]["path"] == str(path)
    assert errors[0]["error_type"] in {"JSONDecodeError", "ValueError"}


def test_unrelated_corruption_only_appears_in_full_history_report(tmp_path):
    store, thread = _store_and_thread(tmp_path)
    good = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "good-task"})
    bad = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "bad-task"})
    store.storage.task_path(bad.task_id).write_text("not-json", encoding="utf-8")
    errors = []

    assert _task_link(store, thread.thread_id, good.task_id, errors) == good
    assert errors == []
    links, history_errors = store.tasks.list_report(thread.thread_id)
    assert links == [good]
    assert [error["task_id"] for error in history_errors] == [bad.task_id]
    assert store.tasks.list(thread.thread_id) == [good]


@pytest.mark.parametrize("delete_existing", [False, True])
def test_missing_target_returns_none_without_error(tmp_path, delete_existing):
    store, thread = _store_and_thread(tmp_path)
    if delete_existing:
        store.tasks.bind({"thread_id": thread.thread_id, "task_id": "missing-task"})
        store.storage.task_path("missing-task").unlink()
    errors = []

    assert store.tasks.link_report(thread.thread_id, "missing-task") == (None, None)
    assert _task_link(store, thread.thread_id, "missing-task", errors) is None
    assert errors == []


@pytest.mark.parametrize("task_id", ["wanted-task", "missing-task"])
def test_list_report_only_test_double_keeps_exact_match_and_errors(task_id):
    wanted = SimpleNamespace(task_id="wanted-task")
    error = {"context": "test-double", "task_id": "broken-task"}
    calls = []

    def list_report(thread_id):
        calls.append(thread_id)
        return [SimpleNamespace(task_id="other-task"), wanted], [error, "bad", None]

    store = SimpleNamespace(tasks=SimpleNamespace(list_report=list_report))
    previous = {"context": "previous-error"}
    errors = [previous]

    selected = _task_link(store, "thread", task_id, errors)

    assert selected is (wanted if task_id == wanted.task_id else None)
    assert calls == ["thread"]
    assert errors == [previous, error]
    assert errors[-1] is error


def test_list_only_test_double_remains_supported():
    wanted = SimpleNamespace(task_id="wanted-task")
    calls = []

    def list_links(thread_id):
        calls.append(thread_id)
        return [wanted]

    errors = []
    store = SimpleNamespace(tasks=SimpleNamespace(list=list_links))

    assert _task_link(store, "thread", wanted.task_id, errors) is wanted
    assert calls == ["thread"]
    assert errors == []


def test_exact_report_preserves_error_and_does_not_fall_back():
    error = {"context": "exact-read", "task_id": "broken-task"}
    calls = []

    def link_report(thread_id, task_id):
        calls.append((thread_id, task_id))
        return None, error

    def forbidden_history_read(*_args):
        pytest.fail("精确查询返回坏账时不得回退到全历史")

    store = SimpleNamespace(
        tasks=SimpleNamespace(link_report=link_report, list_report=forbidden_history_read)
    )
    errors = []

    assert _task_link(store, "thread", "broken-task", errors) is None
    assert calls == [("thread", "broken-task")]
    assert errors == [error]
    assert errors[0] is error


def test_exact_report_exception_is_reported_without_history_fallback():
    def broken_report(_thread_id, _task_id):
        raise OSError("target unreadable")

    store = SimpleNamespace(tasks=SimpleNamespace(link_report=broken_report))
    errors = []

    assert _task_link(store, "thread", "task", errors) is None
    assert len(errors) == 1
    assert errors[0]["context"] == "task_runtime_state.task_link"
    assert errors[0]["error_type"] == "OSError"
