"""显式宿主请求必须原子绑定真实运行；重送只读原绑定，不能生成新尝试。"""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
from dataclasses import asdict, replace

import pytest

from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.repository import RuntimeConflictError, RuntimeRepository


@pytest.fixture
def command_request():
    return HostCommandRequest("owner-a", "actor-a", "cli", "thread-a", "message-a", "install", "a" * 64)


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "runtime.db")


def test_register_pending_chain_and_read_from_fresh_repository(repo, command_request):
    assert repo.find_host_command(command_request) is None
    first = repo.register_host_command(command_request)
    replay = RuntimeRepository(repo.db_path).register_host_command(command_request)
    assert first.created and not replay.created
    assert replace(first, created=False) == replay == repo.find_host_command(command_request)
    assert repo.get_task(first.task_id)["owner_id"] == command_request.owner_id
    run = repo.agent_run_for_run_id(first.run_id)
    assert run["role"] == "host_command" and run["parent_agent_run_id"] == ""
    attempt = repo.current_attempt(first.agent_run_id)
    assert (attempt["attempt_id"], attempt["attempt_generation"], attempt["status"]) == (first.attempt_id, 1, "pending")
    assert json.loads(attempt["metadata_json"]) == {"lifecycle": "pending"}
    assert not repo.has_active_exec_lock(first.agent_run_id)
    assert [event["event_type"] for event in repo.events_for_attempt(first.attempt_id)] == [
        "task.created", "agent_run.created", "host_command.registered",
    ]
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM wake_queue").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_operations").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["command_name", "input_digest"])
def test_same_message_different_input_conflicts_without_new_chain(repo, command_request, field):
    first = repo.register_host_command(command_request)
    changed = replace(command_request, **{field: "b" * 64 if field == "input_digest" else "remove"})
    assert changed.operation_id == command_request.operation_id
    for operation in (repo.find_host_command, repo.register_host_command):
        with pytest.raises(RuntimeConflictError):
            operation(changed)
    assert repo.find_host_command(command_request).task_id == first.task_id
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["owner_id", "actor_id", "channel", "thread_id", "request_id"])
def test_distinct_authenticated_scope_never_replays_other_identity(repo, command_request, field):
    first = repo.register_host_command(command_request)
    changed = replace(command_request, **{field: "another"})
    assert repo.find_host_command(changed) is None
    second = repo.register_host_command(changed)
    assert second.task_id != first.task_id and second.run_id != first.run_id
    assert changed.operation_id != command_request.operation_id


@pytest.mark.parametrize("field,value", [
    ("request_id", ""), ("owner_id", " owner"), ("actor_id", "a\x00b"),
    ("channel", "a" * 1025), ("thread_id", "\ud800"), ("input_digest", "x" * 64),
])
def test_invalid_identity_is_rejected(command_request, field, value):
    with pytest.raises(ValueError):
        replace(command_request, **{field: value})


# LLM: 子进程只打开测试临时库并调用生产登记入口；不会运行代理、模型或真实工具。
# 函数用途: 同步竞争同一消息，验证 SQLite 跨进程互斥与原子复用。
def _register_worker(path, payload, barrier, queue):
    repository = RuntimeRepository(path)
    barrier.wait(timeout=10)
    queue.put(asdict(repository.register_host_command(HostCommandRequest(**payload))))


def test_parallel_processes_register_one_tree(repo, command_request):
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(4), ctx.Queue()
    workers = [ctx.Process(target=_register_worker, args=(repo.db_path, asdict(command_request), barrier, queue)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        rows = [queue.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        queue.close()
    assert sum(row["created"] for row in rows) == 1
    assert len({row["agent_run_id"] for row in rows}) == 1
    assert len({row["attempt_id"] for row in rows}) == 1
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == 3


def test_registration_event_failure_rolls_back_entire_chain(repo, command_request):
    with repo.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_registration BEFORE INSERT ON runtime_events "
                     "WHEN NEW.event_type='host_command.registered' "
                     "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.register_host_command(command_request)
    assert repo.find_host_command(command_request) is None
    with repo._runtime_connection() as conn:
        for table in ("tasks", "task_runs", "agent_runs", "agent_attempts", "runtime_events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("damage", [
    "UPDATE runtime_events SET event_type='other' WHERE event_type='host_command.registered'",
    "UPDATE tasks SET owner_id='another'", "UPDATE tasks SET thread_id='another'",
    "UPDATE task_runs SET metadata_json='{}'", "UPDATE task_runs SET metadata_json='not-json'",
    "UPDATE agent_runs SET role='main'", "UPDATE agent_runs SET parent_agent_run_id='another'",
    "UPDATE agent_runs SET task_run_id='missing'", "UPDATE agent_attempts SET attempt_generation=2",
    "DELETE FROM agent_attempts", "DELETE FROM task_runs", "DELETE FROM tasks",
])
def test_damaged_existing_registration_cannot_become_new_request(repo, command_request, damage):
    repo.register_host_command(command_request)
    with repo.transaction() as conn:
        conn.execute(damage)
    for operation in (repo.find_host_command, repo.register_host_command):
        with pytest.raises(RuntimeConflictError):
            operation(command_request)
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_terminal_replay_does_not_reopen_or_replace_first_attempt(repo, command_request, status):
    first = repo.register_host_command(command_request)
    repo.create_attempt(first.agent_run_id, reuse_pending=True, reject_running=True,
                        expected_pending_attempt_id=first.attempt_id)
    assert repo.settle_agent_run(agent_run_id=first.agent_run_id, attempt_id=first.attempt_id, status=status)["settled"]
    assert not repo.register_host_command(command_request).created
    with pytest.raises(RuntimeConflictError):
        repo.create_attempt(first.agent_run_id, reuse_pending=True, reject_running=True,
                            expected_pending_attempt_id=first.attempt_id)
    assert len(repo.attempts_for_run(first.agent_run_id)) == 1
    assert repo.get_attempt(first.attempt_id)["status"] == status
