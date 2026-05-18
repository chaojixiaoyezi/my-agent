"""Focused tests for structured-only acceptance evidence contracts."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.subagent import VerificationEvidence
from agent_py_agent.agent.subagents.acceptance_helpers.evidence_acceptance_findings import (
    build_evidence_findings,
)


# LLM: Legacy helper must not turn prose acceptance_checks into machine tool requirements.
# 函数用途: 确认旧验收 helper 也只认 attributes 里的结构化工具证据要求。
def test_legacy_acceptance_helper_does_not_read_acceptance_check_text_as_tool_requirement(tmp_path: Path):
    task = SimpleNamespace(
        acceptance_checks=["必须有 read_file 证据；必须有 write_file 证据"],
        acceptance_file=str(tmp_path / "acceptance.md"),
        evidence=[
            VerificationEvidence(
                kind="read_file",
                summary="read ok",
                command="read_file",
                ok=True,
                created_at=time.time(),
            )
        ],
        used_tools=["read_file"],
        attributes={},
        evidence_packets=[],
        output_json=str(tmp_path / "output.json"),
        task_dir=str(tmp_path / "run"),
        reports_dir=str(tmp_path / "reports"),
        child_ids=[],
        runner_last_attempt_at=0.0,
    )

    findings = build_evidence_findings(task, time.time())

    assert all(item.name != "acceptance_requires_write_file" for item in findings)
