from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import dispatch_workflow_records as workflow_records
from agent_py_agent.agent.agent_core.dispatch_record_params import WorkflowRecordParams


# LLM: per-task workflow_mode=off must beat global dispatch workflow auto.
# 函数用途: 明确单文件 worker 创建时已被标成 workflow off，后续顶层 dispatch 不应重新给它套通用 workflow。
def test_build_workflow_records_respects_task_workflow_off(monkeypatch: pytest.MonkeyPatch) -> None:
    task = SimpleNamespace(
        id="worker-1",
        parent_id="",
        workflow_parent_run_id="",
        status="PLANNING",
        workflow_mode="off",
    )
    agent = SimpleNamespace()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("workflow planner should not run for task.workflow_mode=off")

    monkeypatch.setattr(workflow_records, "_try_workflow_plan", fail_if_called)

    result = workflow_records.build_workflow_records(
        WorkflowRecordParams(agent=agent, tasks=[task], workflow_mode="auto", limit=20, apply=True)
    )

    assert result == []
