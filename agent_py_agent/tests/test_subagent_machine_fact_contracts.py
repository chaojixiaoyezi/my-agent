"""Subagent machine-fact architecture contracts."""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
    dispatchable_tasks,
    find_reusable_work_scope_child,
)
from agent_py_agent.agent.subagents.services.base import CreateRunParams


def test_create_subagents_idempotency_does_not_case_coerce_status() -> None:
    task = SimpleNamespace(
        id="child-1",
        status="running",
        verification_status="unverified",
        parent_id="root",
        root_id="root",
        role="worker",
        agent_name="worker-a",
        task_dir="/tmp/task",
        allowed_write_roots=["/tmp/task"],
        context_packs=[],
        attributes={"work_scope_key": "scope-a"},
    )
    manager = SimpleNamespace(list_runs=lambda: [task])
    params = CreateRunParams(
        goal="do work",
        thought="",
        plan=[],
        role="worker",
        parent_id="root",
        root_id="root",
        agent_name="worker-b",
        attributes={"work_scope_key": "scope-a"},
    )

    assert find_reusable_work_scope_child(manager, params, "scope-a") is None
    assert dispatchable_tasks([SimpleNamespace(status="planning", verification_status="unverified")]) == []
