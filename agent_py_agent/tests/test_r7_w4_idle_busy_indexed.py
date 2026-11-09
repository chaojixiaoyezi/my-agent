"""R7-W4：闲置压缩忙态与学习保护只读运行索引的当前集合/本会话谱系，不随 owner 全部历史 run 增长。"""
import json
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning_protection import learning_protection_for_agent
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import compact_operations as idle
from agent_py_agent.tests.test_subagent_listruns_cache import _selected_manager


def _write_run(manager, run_id, status, *, root, thread_id, attributes=None):
    path = manager.workspace / run_id / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": run_id, "goal": "fixture", "thought": "", "plan": [], "status": status,
        "root_id": root, "parent_id": root, "created_at": 1.0, "updated_at": 2.0,
        "attributes": {"conversation_thread_id": thread_id, **(attributes or {})},
    }), encoding="utf-8")
    old = time.time_ns() - 10_000_000_000
    os.utime(path, ns=(old, old))
    manager.persistence.run_index.upsert(manager.load(run_id))
    return path


@pytest.fixture
def case(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    row = replace(store.threads.create(canonical_user_id="user"), task_ids=("task-a",))
    manager = _selected_manager(tmp_path)
    for number in range(60):
        _write_run(manager, f"history-{number}", "DONE", root="task-old", thread_id="thread-other")
    loaded, original = [], manager.persistence._cached_run_ref

    def observed(run_id, path):
        loaded.append(run_id)
        return original(run_id, path)

    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), subagents=manager)
    return SimpleNamespace(store=store, row=row, manager=manager, agent=agent, loaded=loaded)


def test_terminal_blocked_run_of_this_thread_stays_busy_without_history_scan(case):
    _write_run(case.manager, "blocked-1", "BLOCKED", root="task-a", thread_id=case.row.thread_id)
    case.loaded.clear()
    assert idle.idle_compact_busy_state(case.agent, case.store, case.row) == "subagent_blocked"
    assert set(case.loaded) == {"blocked-1"}, "只应回读当前集合与本会话谱系，不扫描历史 run"


def test_active_run_of_this_thread_is_busy(case):
    _write_run(case.manager, "running-1", "RUNNING", root="task-a", thread_id=case.row.thread_id)
    case.loaded.clear()
    assert idle.idle_compact_busy_state(case.agent, case.store, case.row) == "subagent_active"
    assert set(case.loaded) == {"running-1"}


def test_other_threads_history_does_not_block_and_is_not_read(case):
    case.loaded.clear()
    assert idle.idle_compact_busy_state(case.agent, case.store, case.row) == ""
    assert case.loaded == []


def test_indexed_run_with_corrupt_canonical_fails_closed(case):
    path = _write_run(case.manager, "running-1", "RUNNING", root="task-a", thread_id=case.row.thread_id)
    path.write_text("{invalid json", encoding="utf-8")
    assert idle.idle_compact_busy_state(case.agent, case.store, case.row) == "activity_unknown"


def test_learning_protection_reads_current_set_only(case, tmp_path):
    _write_run(case.manager, "running-1", "RUNNING", root="task-a", thread_id=case.row.thread_id,
               attributes={"allowed_skills": ["owner:child"]})
    case.loaded.clear()
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            conversation_store=None, scheduler_repository=None, subagents=case.manager)
    learning_protection_for_agent(agent)
    assert set(case.loaded) == {"running-1"}
