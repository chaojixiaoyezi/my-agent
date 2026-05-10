"""LLM: focused tests for runner-created child refs in dispatch payloads.

模块用途: 验证父级 dispatch 输出能看到 runner 实际创建的下级数量、id 和角色，避免把调度记录数误当成孩子数。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.dispatch_params import DispatchContext
from agent_py_agent.agent.agent_core.dispatch_runner_selection import scoped_runner_tasks
from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool
from agent_py_agent.agent.agent_core.runner_dispatch import (
    RunnerDispatchRecordParams,
    _runner_dispatch_record,
)
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult


# LLM: _dispatch_payload_for_record keeps payload tests focused and below size guard limits.
# 函数用途: 用单条 dispatch record 构造工具返回 payload，避免每个测试重复 mock report/agent。
def _dispatch_payload_for_record(record: SimpleNamespace) -> dict:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {record.step: 1}
    mock_report.records = [record]
    mock_agent = MagicMock()
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = []
    return DispatchSubagentsTool(mock_agent)._report_payload(mock_report)


# LLM: _dispatch_payload_with_direct_children builds a runner-context dispatch payload fixture.
# 函数用途: 构造带当前 parent runner 和 direct child 状态的 payload，验证继续调度指令。
def _dispatch_payload_with_direct_children(children: list[SimpleNamespace]) -> dict:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {"total": 0}
    mock_report.records = []
    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "root"
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = children
    return DispatchSubagentsTool(mock_agent)._report_payload(mock_report)


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


# LLM: test_dispatch_payload_exposes_runner_created_children protects parent progress handoff.
# 函数用途: 顶层主代理推进 root 后，要能直接看到 root runner 创建的孩子事实。
def test_dispatch_payload_exposes_runner_created_children():
    record = SimpleNamespace(
        step="runner",
        action="execute_runner",
        run_id="root",
        ok=True,
        dry_run=False,
        applied=True,
        message="runner 已完成模型调用，等待独立验收。",
        before_status="PLANNING",
        after_status="AWAITING_ACCEPTANCE",
        runner_summary="root 创建了 2 个直接孩子。",
        runner_created_child_count=2,
        runner_created_child_ids=["child-a", "child-b"],
        runner_created_roles=["researcher", "worker"],
        runner_child_status_counts={"DONE": 1, "PLANNING": 1},
        runner_unfinished_child_ids=["child-b"],
        runner_partial_success=True,
    )
    payload = _dispatch_payload_for_record(record)

    assert payload["records"][0]["runner_created_child_count"] == 2
    assert payload["records"][0]["runner_created_child_ids"] == ["child-a", "child-b"]
    assert payload["records"][0]["runner_created_roles"] == ["researcher", "worker"]
    assert payload["records"][0]["runner_child_status_counts"] == {"DONE": 1, "PLANNING": 1}
    assert payload["records"][0]["runner_unfinished_child_ids"] == ["child-b"]
    assert payload["records"][0]["runner_partial_success"] is True
    assert "root 创建了 2 个直接孩子" in payload["records"][0]["runner_summary"]


# LLM: test_dispatch_payload_includes_acceptance_followup keeps rescue hints visible to runner context.
# 函数用途: runner 内部要能看到 child 测试失败和 follow-up 动作，才可能继续救援。
def test_dispatch_payload_includes_acceptance_followup():
    record = SimpleNamespace(
        step="acceptance",
        action="reject",
        run_id="leaf-1",
        ok=False,
        dry_run=True,
        applied=False,
        message="验收失败。",
        before_status="AWAITING_ACCEPTANCE",
        after_status="AWAITING_ACCEPTANCE",
        parent_acceptance_auto_execution_test_failed=5,
        parent_acceptance_followup_action="plan_rescue",
        parent_acceptance_followup_command="subagents-acceptance-plan leaf-1 --take-over-by <agent>",
    )
    payload = _dispatch_payload_for_record(record)

    assert payload["records"][0]["test_failed"] == 5
    assert payload["records"][0]["followup_action"] == "plan_rescue"
    assert payload["records"][0]["followup_command"].endswith("--take-over-by <agent>")


# LLM: test_dispatch_payload_exposes_recovery_valid_run_ids covers model retry ergonomics.
# 函数用途: 模型传错 run_id 时，顶层恢复 payload 要直接给机器可读的 valid_run_ids。
def test_dispatch_payload_exposes_recovery_valid_run_ids():
    record = SimpleNamespace(
        step="runner_selection",
        action="invalid_run_ids",
        run_id="",
        ok=False,
        dry_run=False,
        applied=False,
        message="blocked",
        before_status="",
        after_status="",
        evidence_paths=["/tmp/subagents/child-a", "/tmp/subagents/child-b"],
    )
    payload = _dispatch_payload_for_record(record)

    recovery = payload["runner_selection_recovery"]
    assert recovery["action"] == "retry_dispatch_with_valid_run_id"
    assert recovery["valid_run_ids"] == ["child-a", "child-b"]
    assert recovery["valid_task_refs"] == ["/tmp/subagents/child-a", "/tmp/subagents/child-b"]


# LLM: test_dispatch_payload_tells_runner_to_continue_unfinished_children covers partial runner waves.
# 函数用途: runner 内还有 PLANNING/RUNNING 直接孩子时，payload 必须给出机器可读的继续调度动作。
def test_dispatch_payload_tells_runner_to_continue_unfinished_children():
    payload = _dispatch_payload_with_direct_children([
        SimpleNamespace(id="child-a", parent_id="root", status="PLANNING"),
        SimpleNamespace(id="child-b", parent_id="root", status="RUNNING"),
        SimpleNamespace(id="child-c", parent_id="root", status="DONE"),
    ])

    direct = payload["direct_children"]
    assert direct["needs_more_dispatch"] is True
    assert direct["unfinished_run_ids"] == ["child-a", "child-b"]
    assert direct["next_action"] == "continue_dispatch_direct_children"
    assert direct["suggested_tool_call"]["tool"] == "dispatch_subagents"
    assert direct["suggested_tool_call"]["execute_runners"] is True
    assert direct["suggested_tool_call"]["run_ids"] == ["child-a", "child-b"]


# LLM: test_scoped_runner_tasks_honors_include_run_ids protects exact child dispatch waves.
# 函数用途: 父 runner 指定 run_ids 时，调度候选只能包含这些直接孩子，并按指定顺序执行。
def test_scoped_runner_tasks_honors_include_run_ids_order():
    tasks = [
        SimpleNamespace(id="child-a", parent_id="root", root_id="root"),
        SimpleNamespace(id="child-b", parent_id="root", root_id="root"),
        SimpleNamespace(id="child-c", parent_id="root", root_id="root"),
        SimpleNamespace(id="other", parent_id="other-parent", root_id="root"),
    ]
    ctx = DispatchContext(
        cfg=MagicMock(),
        normalized_workflow_mode="off",
        apply=True,
        planner=False,
        runner_instruction="",
        max_runners=10,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=MagicMock(),
        parent_run_id="root",
        root_id="root",
        include_run_ids=["child-b", "child-a"],
    )

    scoped = scoped_runner_tasks(tasks, ctx)

    assert [task.id for task in scoped] == ["child-b", "child-a"]


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
    after = SimpleNamespace(
        status="TIMEOUT",
        verification_status="UNVERIFIED",
        child_ids=["child-a", "child-b"],
    )
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
