"""LLM: dispatch runner record child-ref regression tests.

模块用途: 验证 runner dispatch record 持久化 child refs、角色和部分成功信息。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.runner.dispatch import (
    RunnerDispatchRecordParams,
    _runner_dispatch_record,
)
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult


def _runner_result() -> SubAgentRunnerResult:
    return SubAgentRunnerResult(
        run_id="root",
        dry_run=False,
        ok=True,
        status="DONE",
        verification_status="VERIFIED",
        message="runner done",
        structured_summary="创建两个直接孩子。",
        execution_context_json="/tmp/execution_context.json",
        result_json="/tmp/runner_result.json",
        output_json="/tmp/output.json",
    )


def test_runner_dispatch_record_carries_created_child_summary():
    before = SimpleNamespace(status="PLANNING", verification_status="UNVERIFIED")
    after = SimpleNamespace(
        status="DONE",
        verification_status="VERIFIED",
        child_ids=["child-a", "child-b"],
    )
    agent = MagicMock()
    agent.subagents.load.side_effect = [
        SimpleNamespace(role="researcher"),
        SimpleNamespace(role="worker"),
    ]
    agent.subagents.make_dispatch_record.side_effect = lambda *, params: params

    record = _runner_dispatch_record(
        RunnerDispatchRecordParams(
            agent=agent,
            run_id="root",
            before=before,
            after=after,
            result=_runner_result(),
            retry_reason="",
            start_runner=True,
        )
    )

    assert record.runner_summary == "创建两个直接孩子。"
    assert record.runner_created_child_count == 2
    assert record.runner_created_child_ids == ["child-a", "child-b"]
    assert record.runner_created_roles == ["researcher", "worker"]


def test_runner_dispatch_record_marks_partial_success_children():
    before = SimpleNamespace(status="PLANNING", verification_status="UNVERIFIED")
    after = SimpleNamespace(status="TIMEOUT", verification_status="UNVERIFIED", child_ids=["child-a", "child-b"])
    result = _runner_result()
    result.ok = False
    result.status = "TIMEOUT"
    agent = MagicMock()
    agent.subagents.load.side_effect = [
        SimpleNamespace(role="worker", status="DONE"),
        SimpleNamespace(role="tester", status="PLANNING"),
    ]
    agent.subagents.make_dispatch_record.side_effect = lambda *, params: params

    record = _runner_dispatch_record(
        RunnerDispatchRecordParams(
            agent=agent,
            run_id="root",
            before=before,
            after=after,
            result=result,
            retry_reason="",
            start_runner=True,
        )
    )

    assert record.runner_partial_success is True
    assert record.runner_child_status_counts == {"DONE": 1, "PLANNING": 1}
    assert record.runner_unfinished_child_ids == ["child-b"]


def test_runner_dispatch_record_surfaces_child_load_error():
    before = SimpleNamespace(status="PLANNING", verification_status="UNVERIFIED")
    after = SimpleNamespace(status="DONE", verification_status="VERIFIED", child_ids=["child-broken"])
    agent = MagicMock()
    agent.subagents.load.side_effect = ValueError("child ledger broken")
    agent.subagents.make_dispatch_record.side_effect = lambda *, params: params

    record = _runner_dispatch_record(
        RunnerDispatchRecordParams(
            agent=agent,
            run_id="root",
            before=before,
            after=after,
            result=_runner_result(),
            retry_reason="",
            start_runner=True,
        )
    )

    assert record.runner_child_status_counts == {"UNKNOWN": 1}
    assert record.runner_unfinished_child_ids == ["child-broken"]
    assert record.runner_child_load_errors[0]["run_id"] == "child-broken"
    assert record.runner_child_load_errors[0]["context"] == "subagents.load"
    assert "不要把它当成子代理没产物" in record.runner_child_load_errors[0]["model_message"]
