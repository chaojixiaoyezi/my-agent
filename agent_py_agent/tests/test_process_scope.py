from dataclasses import FrozenInstanceError, replace

import pytest

from agent_py_agent.agent.tooling.process_scope import (
    ProcessAccessScope,
    ProcessExecutionScope,
    process_access_scope,
)


def test_execution_identity_is_frozen_and_distinct_from_access_fallback(tmp_path):
    run_scope = {
        "owner_home": str(tmp_path / "owner"), "owner_id": "owner-a",
        "root_task_id": "task-a", "root_run_id": "root-run", "run_id": "child-run",
        "task_id": "authority:other-task", "attempt_id": "attempt-a",
    }
    access = process_access_scope(run_scope, tmp_path / "wrong-owner")
    execution = ProcessExecutionScope.from_run_scope(run_scope, tmp_path / "wrong-owner")

    assert access == ProcessAccessScope("owner-a", "task-a", str(tmp_path / "owner"))
    assert execution == ProcessExecutionScope(
        str(tmp_path / "owner"), "", "task-a", "child-run", "attempt-a",
    )
    run_scope.update(session_id="later-thread", root_task_id="later-task", attempt_id="later-attempt")
    assert execution.thread_id == "" and execution.root_task_id == "task-a"
    with pytest.raises(FrozenInstanceError):
        execution.attempt_id = "later-attempt"
    assert execution.attempt_id == "attempt-a"


def test_access_fallback_does_not_invent_execution_identity(tmp_path):
    scope = {"owner_id": "a", "root_run_id": "root-run", "task_id": "authority-task"}
    assert process_access_scope(scope, tmp_path).conversation_id == "root-run"
    assert ProcessExecutionScope.from_run_scope(scope, tmp_path) == ProcessExecutionScope(str(tmp_path))
    assert ProcessExecutionScope.from_run_scope(None, "") == ProcessExecutionScope()
    assert not process_access_scope(None).is_bound()


@pytest.mark.parametrize("field", ["owner_home", "thread_id", "root_task_id", "run_id", "attempt_id"])
def test_all_stop_identity_dimensions_must_match(tmp_path, field):
    execution = ProcessExecutionScope(str(tmp_path), "thread", "task", "run", "attempt")
    assert execution.matches(execution)
    assert not execution.matches(replace(execution, **{field: "other"}))


def test_task_stop_spans_attempts_but_run_stop_can_be_narrowed(tmp_path):
    old = ProcessExecutionScope(str(tmp_path), "thread", "task", "run-a", "attempt-a")
    newer = replace(old, attempt_id="attempt-b")
    child = replace(old, run_id="child", attempt_id="child-attempt")
    root = ProcessExecutionScope(str(tmp_path), "thread", "task")
    assert all(resource.matches(root) for resource in (old, newer, child))
    exact_attempt = ProcessExecutionScope(str(tmp_path), run_id="run-a", attempt_id="attempt-a")
    assert old.matches(exact_attempt)
    assert not newer.matches(exact_attempt) and not child.matches(exact_attempt)


@pytest.mark.parametrize("target", [
    ProcessExecutionScope(),
    ProcessExecutionScope(thread_id="thread", root_task_id="task", run_id="run"),
    ProcessExecutionScope(owner_home="owner", thread_id="thread"),
    ProcessExecutionScope(owner_home="owner", root_task_id="task"),
    ProcessExecutionScope(owner_home="owner", attempt_id="attempt"),
])
def test_partial_stop_target_never_selects_resources(target):
    execution = ProcessExecutionScope("owner", "thread", "task", "run", "attempt")
    assert not execution.matches(target)
    assert not ProcessExecutionScope().matches(target)
