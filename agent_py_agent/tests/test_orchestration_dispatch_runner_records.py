"""LLM: dispatch runner record child-ref regression tests.

模块用途: 验证 runner dispatch record 持久化 child refs、角色和部分成功信息。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.runner_dispatch import (
    RunnerDispatchRecordParams,
    _runner_dispatch_record,
)
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult


# LLM: _runner_result returns the minimal successful runner result used by child-ref dispatch tests.
# 函数用途: 构造带结构化摘要的 runner 结果，供 dispatch record 测试复用。
def _runner_result() -> SubAgentRunnerResult:
    return SubAgentRunnerResult(
        run_id="root",
        dry_run=False,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        verification_status="NEEDS_ACCEPTANCE",
        message="runner done",
        structured_summary="创建两个直接孩子。",
        execution_context_json="/tmp/execution_context.json",
        result_json="/tmp/runner_result.json",
        output_json="/tmp/output.json",
    )


# LLM: test_runner_dispatch_record_carries_created_child_summary validates persisted dispatch evidence.
# 函数用途: runner 内创建 children 后，父级 dispatch record 要保留 child refs 和角色摘要。
def test_runner_dispatch_record_carries_created_child_summary():
    before = SimpleNamespace(status="PLANNING", verification_status="UNVERIFIED")
    after = SimpleNamespace(
        status="AWAITING_ACCEPTANCE",
        verification_status="NEEDS_ACCEPTANCE",
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
            execute_runners=True,
        )
    )

    assert record.runner_summary == "创建两个直接孩子。"
    assert record.runner_created_child_count == 2
    assert record.runner_created_child_ids == ["child-a", "child-b"]
    assert record.runner_created_roles == ["researcher", "worker"]


# LLM: test_runner_dispatch_record_marks_partial_success_children covers timeout-after-schedule E2E facts.
# 函数用途: root runner 超时但已创建孩子时，dispatch record 要保留部分成功和未完成 child ids。
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
            execute_runners=True,
        )
    )

    assert record.runner_partial_success is True
    assert record.runner_child_status_counts == {"DONE": 1, "PLANNING": 1}
    assert record.runner_unfinished_child_ids == ["child-b"]
