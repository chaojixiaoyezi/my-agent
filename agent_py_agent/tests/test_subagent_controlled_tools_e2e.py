from __future__ import annotations

"""LLM: local E2E for capability grant -> shell gateway -> trash -> recovery report.

给人看的解释：
这个测试把受控工具链路串起来，确保每一步都只写 refs 和受控文件。
"""

import sys
from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.recovery_report import (
    RecoveryReportRequest,
    write_recovery_report,
)
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
)
from agent_py_agent.agent.subagents.shell_gateway import ShellGatewayRequest
from agent_py_agent.agent.subagents.shell_gateway_execution import execute_shell_command
from agent_py_agent.agent.subagents.task_trash import TaskTrashMoveRequest, move_to_task_trash


@dataclass(frozen=True)
class ControlledToolsFixture:
    task: object
    task_dir: Path
    request: object
    grant: object


def _prepare_controlled_shell_grant(tmp_path: Path) -> ControlledToolsFixture:
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(goal="controlled tools", thought="exercise primitives", plan=["run"])
    task_dir = Path(task.task_dir)
    request = manager.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="需要运行一个受控 Python 命令验证输出。",
            needed_capability="shell",
            capability_type="shell",
            requested_commands=[sys.executable],
            path_scope=[str(task_dir)],
            output_budget={"stdout_bytes": 64, "stderr_bytes": 64},
        ),
    )
    grant = manager.record_capability_grant(
        task.id,
        RecordCapabilityGrantParams(
            request_id=request.id,
            grant_type="shell",
            command_allowlist=[sys.executable],
            path_scope=[str(task_dir)],
            output_budget={"stdout_bytes": 64, "stderr_bytes": 64},
            reason="local controlled tools e2e",
        ),
    )
    return ControlledToolsFixture(task, task_dir, request, grant)


def _run_controlled_shell(tmp_path: Path, fixture: ControlledToolsFixture):
    shell_result = execute_shell_command(
        ShellGatewayRequest(
            command=[sys.executable, "-c", "print('controlled-ok')"],
            workspace_root=tmp_path,
            cwd=fixture.task_dir,
            allowed_roots=[fixture.task_dir],
            command_allowlist=fixture.grant.command_allowlist,
            output_budget=fixture.grant.output_budget,
            artifact_dir=fixture.task_dir / "shell_outputs",
            run_id=fixture.task.id,
            request_id=fixture.request.id,
        )
    )
    return shell_result


def _archive_stdout_and_report(fixture: ControlledToolsFixture, shell_result):
    trash_result = move_to_task_trash(
        TaskTrashMoveRequest(
            task_dir=fixture.task_dir,
            source_path=shell_result.stdout_ref,
            allowed_roots=[fixture.task_dir],
            reason="archive shell stdout",
            actor_run_id=fixture.task.id,
        )
    )
    recovery = write_recovery_report(
        RecoveryReportRequest(
            task_dir=fixture.task_dir,
            run_id=fixture.task.id,
            title="Controlled Tools E2E",
            summary=shell_result.stdout_preview.strip(),
            details="Shell stdout was moved to task trash after verification.",
            artifact_refs=[trash_result.destination],
            evidence_refs=[shell_result.audit_ref],
        )
    )
    return trash_result, recovery


def test_controlled_tools_local_e2e(tmp_path: Path) -> None:
    fixture = _prepare_controlled_shell_grant(tmp_path)
    shell_result = _run_controlled_shell(tmp_path, fixture)
    trash_result, recovery = _archive_stdout_and_report(fixture, shell_result)

    assert shell_result.executed is True
    assert shell_result.stdout_preview.strip() == "controlled-ok"
    assert trash_result.moved is True
    assert recovery.written is True
    assert Path(recovery.markdown_ref).read_text(encoding="utf-8").startswith("# Controlled Tools E2E")
