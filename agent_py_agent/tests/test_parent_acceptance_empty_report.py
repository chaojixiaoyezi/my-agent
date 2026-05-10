"""Tests for empty parent acceptance reports with traceable artifact evidence."""

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.models import EvidencePacket


# LLM: _agent_and_task creates a minimal awaiting-acceptance run for empty-report decisions.
# 函数用途: 搭建独立临时 workspace 和子代理任务，避免复用大测试文件里的 fixture。
def _agent_and_task():
    root_ctx = tempfile.TemporaryDirectory()
    root = Path(root_ctx.name)
    cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
    agent = SimpleAgent(cfg, root)
    task = agent.subagents.create_run(
        goal="parent acceptance empty report",
        thought="worker wrote an artifact-only result",
        plan=["write artifact", "wait for parent acceptance"],
        acceptance_checks=["artifact ref must be traceable"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    agent.subagents.save(task)
    return root_ctx, agent, task


# LLM: _write_output writes artifact-only structured output without executable tests.
# 函数用途: 生成 output.json 和 runner_result.json，让父级验收读取真实文件事实源。
def _write_output(task) -> None:
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "status": "AWAITING_ACCEPTANCE",
                "summary": "artifact-only output",
                "tests": [],
                "artifacts": [],
                "patches": [],
                "blockers": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "structured_output_found": True,
                "structured_output_ok": True,
                "structured_parse_error": "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# LLM: This regression locks artifact-only empty reports to inspect_only instead of rescue.
# 函数用途: 验证空测试报告配合 artifact evidence refs 时，父级验收继续检查而不是误判失败。
def test_parent_acceptance_plan_inspects_empty_report_with_traceable_artifact_evidence():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(task)
        task.evidence_packets = [
            EvidencePacket(
                id="packet-artifact",
                claim="artifact proves requested output",
                artifact_refs=["/tmp/proof.txt"],
            )
        ]
        agent.subagents.save(task)
        write_test_execution_report(
            task.reports_dir,
            [],
            options=TestExecutionReportOptions(executed_at="2026-05-10T12:00:00Z"),
        )

        decision = agent.subagents.plan_parent_acceptance(task.id)

        assert decision.decision == "inspect_only"
        assert decision.risk_level == "low"
        assert "no executable tests" in decision.reason
