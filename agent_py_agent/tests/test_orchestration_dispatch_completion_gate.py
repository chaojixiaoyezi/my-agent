"""Dispatch completion gate tests split out from the broad execute suite."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


# LLM: dispatch gate tests cover root-facing not-complete signals and refs.
# 函数用途: 验证 dispatch_subagents 顶层和 markdown 都暴露阻塞状态，防止父级误报完成。
def test_dispatch_payload_exposes_blocking_gate_without_deliverable_refs():
    """dispatch 有阻塞时不能把未验收产物暴露成可交付 refs。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

    record = SimpleNamespace(
        step="acceptance",
        action="reject",
        run_id="child-bad",
        ok=False,
        dry_run=True,
        applied=False,
        message="验收失败",
        before_status="AWAITING_ACCEPTANCE",
        after_status="AWAITING_ACCEPTANCE",
        parent_acceptance_auto_execution_test_ref="/tmp/child-bad/reports/test_execution.json",
        parent_acceptance_followup_ref="/tmp/child-bad/reports/parent_acceptance_auto_followup.json",
        parent_acceptance_test_failure_summary="存在未完成 child",
        parent_acceptance_test_failure_details=["direct children acceptance failed"],
    )
    mock_report = SimpleNamespace(dry_run=False, summary={"failed": 1}, records=[record])

    mock_agent = MagicMock()
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.load.return_value = SimpleNamespace(
        artifact_refs=["/tmp/site/final_report.md"],
        evidence_refs=["/tmp/site/evidence.json"],
    )

    result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "execute_runners": True})
    payload = json.loads(result.output)

    assert payload["completion_status"]["status"] == "not_complete"
    assert payload["must_not_report_done"] is True
    assert payload["blocking_run_ids"] == ["child-bad"]
    assert "deliverable_artifact_refs" not in payload
    assert payload["pending_artifact_refs"] == ["/tmp/site/final_report.md"]
    assert payload["parent_acceptance_repair_advice"]["failed_run_ids"] == ["child-bad"]


# LLM: dispatch gate must treat unexecuted remembered runs as not complete, not just failed records.
# 函数用途: 覆盖 Task17 真实 E2E：只跑完 1 个 run 但还有 2 个 PLANNING 时，root 不能汇报完成。
def test_dispatch_payload_blocks_when_remembered_runs_remain_unfinished():
    """还有 remembered run 未 DONE/VERIFIED 时，dispatch 顶层必须标记未完成。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

    record = SimpleNamespace(
        step="runner",
        action="execute_runner",
        run_id="strategy",
        ok=True,
        dry_run=False,
        applied=True,
        message="runner done",
        before_status="PLANNING",
        after_status="DONE",
    )
    mock_report = SimpleNamespace(dry_run=False, summary={"ok": 1}, records=[record])

    mock_agent = MagicMock()
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    states = {
        "market": SimpleNamespace(status="PLANNING", verification_status="UNVERIFIED", artifact_refs=[]),
        "competition": SimpleNamespace(status="PLANNING", verification_status="UNVERIFIED", artifact_refs=[]),
        "strategy": SimpleNamespace(
            status="DONE",
            verification_status="VERIFIED",
            artifact_refs=["/tmp/site/entry_strategy.md"],
            evidence_refs=["/tmp/site/evidence.json"],
        ),
    }
    mock_agent.subagents.load.side_effect = lambda run_id: states[run_id]

    result = DispatchSubagentsTool(mock_agent).execute({
        "apply": True,
        "execute_runners": True,
        "run_ids": ["market", "competition", "strategy"],
    })
    payload = json.loads(result.output)

    assert payload["completion_status"]["status"] == "not_complete"
    assert payload["must_not_report_done"] is True
    assert payload["unfinished_run_ids"] == ["competition", "market"]
    assert "deliverable_artifact_refs" not in payload
    assert payload["pending_artifact_refs"] == ["/tmp/site/entry_strategy.md"]


# LLM: markdown gate coverage keeps artifact-only readers from bypassing machine status.
# 函数用途: 验证 SUBAGENT_DISPATCH.md 写出 completion gate，便于模型或人工读 markdown 时继续修复。
def test_dispatch_markdown_exposes_completion_gate():
    """SUBAGENT_DISPATCH.md 也要写清楚未全验收，避免模型读 markdown 后误报完成。"""
    from agent_py_agent.agent.subagents.rendering_dispatch import render_dispatch_markdown

    record = SimpleNamespace(
        step="acceptance",
        action="reject",
        run_id="child-bad",
        ok=False,
        dry_run=True,
        applied=False,
        message="验收失败",
        runner_summary="",
        runner_created_child_count=0,
        parent_acceptance_policy_ref="",
        parent_acceptance_auto_execution_ref="",
        parent_acceptance_followup_ref="",
    )
    report = SimpleNamespace(dry_run=False, generated_at=1.0, summary={"failed": 1}, records=[record])

    rendered = render_dispatch_markdown(report)

    assert "## Completion Gate" in rendered
    assert "status: not_complete" in rendered
    assert "must_not_report_done: true" in rendered
    assert "blocking_run_ids: child-bad" in rendered


# LLM: closeout should trust explicit coverage_records only when the covering run is verified.
# 函数用途: 覆盖真实 E2E：坏 leaf 输出损坏，但父级用机器字段声明已由 verified sibling 覆盖时，不再无限卡收口。
def test_closeout_resolves_explicit_verified_coverage_record():
    from agent_py_agent.agent.agent_core.subagent_dispatch_closeout_resolution import (
        blocking_task_ids,
        task_resolved_for_closeout,
    )

    broken = SimpleNamespace(
        id="bad-leaf",
        status="BLOCKED",
        verification_status="UNVERIFIED",
        takeover_by="",
        attributes={},
    )
    coverer = SimpleNamespace(
        id="good-leaf",
        status="DONE",
        verification_status="VERIFIED",
        takeover_by="",
        output_json="",
        result="",
        attributes={},
    )
    parent = SimpleNamespace(
        id="parent-coordinator",
        status="AWAITING_ACCEPTANCE",
        verification_status="NEEDS_ACCEPTANCE",
        takeover_by="",
        attributes={
            "coverage_records": [{
                "covered_run_id": "bad-leaf",
                "covered_by_run_id": "good-leaf",
                "reason": "verified sibling completed the same scope",
            }]
        },
    )

    tasks = [broken, coverer, parent]

    assert task_resolved_for_closeout(broken, tasks) is True
    assert "bad-leaf" not in blocking_task_ids(tasks)


# LLM: sibling target coverage must not hide unstarted PLANNING tasks.
# 函数用途: 覆盖真实自然语言 E2E：一个旧 worker 仍在 PLANNING，另一个 verified sibling 写了同一文件时，最终收口不能把旧 run 算完成。
def test_closeout_does_not_resolve_planning_run_by_verified_sibling_target():
    from agent_py_agent.agent.agent_core.subagent_dispatch_closeout_resolution import (
        blocking_task_ids,
        done_verified_count,
        task_resolved_for_closeout,
    )

    planned = SimpleNamespace(
        id="old-worker",
        status="PLANNING",
        verification_status="UNVERIFIED",
        takeover_by="",
        goal="写入 lab_outputs/site-output/index.html",
        output_json="",
        result="",
        attributes={},
    )
    verified = SimpleNamespace(
        id="new-worker",
        status="DONE",
        verification_status="VERIFIED",
        takeover_by="",
        goal="写入 lab_outputs/site-output/index.html",
        output_json="",
        result="",
        attributes={},
    )
    tasks = [planned, verified]

    assert task_resolved_for_closeout(planned, tasks) is False
    assert blocking_task_ids(tasks) == ["old-worker"]
    assert done_verified_count(tasks) == 1
