from __future__ import annotations

"""LLM: tests parent-grant compilation for controlled subagent exec requests.

模块用途: 验证受控 exec 框架只相信父级 grant 的范围，不让子代理自填命令授权。
"""

from pathlib import Path

from agent_py_agent.agent.subagents.controlled_exec_gateway import (
    ControlledExecRequest,
    controlled_exec_grant_refs,
    plan_controlled_exec,
)
from agent_py_agent.agent.subagents.models import CapabilityGrant


def _grant(tmp_path: Path, **overrides) -> CapabilityGrant:
    data = {
        "id": "capgrant-1",
        "request_id": "capreq-1",
        "grant_to_run_id": "run-1",
        "grant_type": "shell",
        "command_allowlist": ["pwd"],
        "path_scope": [str(tmp_path)],
        "network_scope": [],
        "output_budget": {"stdout_bytes": 32, "stderr_bytes": 16},
    }
    data.update(overrides)
    return CapabilityGrant(**data)


def test_controlled_exec_uses_parent_grant_scope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    grant = _grant(tmp_path, path_scope=[str(project)])

    plan = plan_controlled_exec(
        ControlledExecRequest(command="pwd", workspace_root=tmp_path, cwd=project, grant=grant)
    )

    assert plan.allowed is True
    assert plan.action == "execute_shell"
    assert plan.shell_decision is not None
    assert plan.shell_decision.cwd == str(project.resolve())
    assert plan.shell_decision.output_budget["stdout_bytes"] == 32
    assert plan.shell_decision.audit["run_id"] == "run-1"
    assert plan.shell_decision.audit["request_id"] == "capreq-1"


def test_controlled_exec_refs_include_controlled_exec_tool_grants(tmp_path: Path) -> None:
    grant = _grant(
        tmp_path,
        grant_type="tool",
        tools=["controlled_exec", "read_artifact"],
        command_allowlist=["pwd"],
        path_scope=[str(tmp_path / "deliverables")],
    )

    refs = controlled_exec_grant_refs([grant])

    assert refs == [
        {
            "grant_id": "capgrant-1",
            "request_id": "capreq-1",
            "run_id": "run-1",
            "command_allowlist": ["pwd"],
            "path_scope": [str(tmp_path / "deliverables")],
            "network_scope": [],
            "output_budget": {"stdout_bytes": 32, "stderr_bytes": 16},
            "risk_level": "",
            "constraints": {},
            "delete_policy": {
                "mode": "task_trash",
                "commands": ["rm", "rmdir", "unlink"],
                "requires_apply": True,
                "command_allowlist_required": False,
                "completion_requires": ["moved=true", "trash_manifest_ref"],
            },
        }
    ]


def test_controlled_exec_rejects_command_not_in_parent_grant(tmp_path: Path) -> None:
    plan = plan_controlled_exec(
        ControlledExecRequest(command="python --version", workspace_root=tmp_path, grant=_grant(tmp_path))
    )

    assert plan.allowed is False
    assert plan.action == "blocked"
    assert plan.blockers == ["command_not_granted:python"]


def test_controlled_exec_requires_parent_path_scope(tmp_path: Path) -> None:
    grant = _grant(tmp_path, path_scope=[])

    plan = plan_controlled_exec(ControlledExecRequest(command="pwd", workspace_root=tmp_path, grant=grant))

    assert plan.allowed is False
    assert plan.action == "blocked"
    assert plan.blockers == ["missing_parent_path_scope"]


def test_controlled_exec_routes_delete_to_task_trash(tmp_path: Path) -> None:
    grant = _grant(tmp_path, command_allowlist=["rm"])

    plan = plan_controlled_exec(
        ControlledExecRequest(command="rm stale.txt", workspace_root=tmp_path, grant=grant, task_dir=tmp_path)
    )

    assert plan.allowed is False
    assert plan.action == "use_task_trash"
    assert plan.blockers == ["delete_requires_task_trash:rm"]
    assert plan.trash_hint["task_dir"] == str(tmp_path.resolve())
    assert plan.trash_hint["source_path"] == str((tmp_path / "stale.txt").resolve())


def test_controlled_exec_uses_parent_network_scope(tmp_path: Path) -> None:
    grant = _grant(
        tmp_path,
        command_allowlist=["curl"],
        network_scope=["https://api.example.test"],
    )

    allowed = plan_controlled_exec(
        ControlledExecRequest(
            command="curl https://api.example.test/status",
            workspace_root=tmp_path,
            grant=grant,
        )
    )
    blocked = plan_controlled_exec(
        ControlledExecRequest(
            command="curl https://other.example.test/status",
            workspace_root=tmp_path,
            grant=grant,
        )
    )

    assert allowed.allowed is True
    assert blocked.allowed is False
    assert blocked.blockers == ["network_scope_denied:https://other.example.test/status"]
