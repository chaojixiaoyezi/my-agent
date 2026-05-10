from __future__ import annotations

"""LLM: tests controlled shell gateway dry-run policy before execution exists.

给人看的解释：
这些测试只验证 shell 网关是否正确允许/拒绝命令，不启动真实 shell。
"""

from agent_py_agent.agent.subagents.shell_gateway import (
    ShellGatewayRequest,
    decision_to_dict,
    plan_shell_command,
)


def test_shell_gateway_dry_run_allows_scoped_command(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    decision = plan_shell_command(
        ShellGatewayRequest(
            command="pwd",
            workspace_root=tmp_path,
            cwd="project",
            allowed_roots=[project],
            command_allowlist=["pwd"],
            output_budget={"stdout_bytes": 1024},
            run_id="run-1",
            request_id="capreq-1",
        )
    )

    assert decision.allowed is True
    assert decision.dry_run is True
    assert decision.would_execute is True
    assert decision.executable == "pwd"
    assert decision.cwd == str(project.resolve())
    assert decision.output_budget["stdout_bytes"] == 1024
    assert decision_to_dict(decision)["audit"]["run_id"] == "run-1"


def test_shell_gateway_dry_run_blocks_dangerous_rm_even_if_granted(tmp_path) -> None:
    decision = plan_shell_command(
        ShellGatewayRequest(
            command="rm file.txt",
            workspace_root=tmp_path,
            command_allowlist=["rm"],
        )
    )

    assert decision.allowed is False
    assert decision.blockers == ["blocked_dangerous_command:rm"]


def test_shell_gateway_dry_run_blocks_shell_metacharacters(tmp_path) -> None:
    decision = plan_shell_command(
        ShellGatewayRequest(
            command="python -m pytest; rm -rf /",
            workspace_root=tmp_path,
            command_allowlist=["python"],
        )
    )

    assert decision.allowed is False
    assert "blocked_shell_metacharacter" in decision.blockers


def test_shell_gateway_dry_run_blocks_cwd_escape(tmp_path) -> None:
    outside = tmp_path.parent

    decision = plan_shell_command(
        ShellGatewayRequest(
            command="pwd",
            workspace_root=tmp_path,
            cwd=outside,
            command_allowlist=["pwd"],
        )
    )

    assert decision.allowed is False
    assert decision.blockers == ["cwd_outside_workspace"]


def test_shell_gateway_dry_run_requires_network_scope_for_curl(tmp_path) -> None:
    blocked = plan_shell_command(
        ShellGatewayRequest(
            command="curl https://api.example.test/status",
            workspace_root=tmp_path,
            command_allowlist=["curl"],
        )
    )
    allowed = plan_shell_command(
        ShellGatewayRequest(
            command="curl https://api.example.test/status",
            workspace_root=tmp_path,
            command_allowlist=["curl"],
            network_allowlist=["https://api.example.test"],
        )
    )

    assert blocked.allowed is False
    assert blocked.blockers == ["missing_network_allowlist"]
    assert allowed.allowed is True


def test_shell_gateway_dry_run_uses_safe_budget_defaults(tmp_path) -> None:
    decision = plan_shell_command(
        ShellGatewayRequest(
            command=["pwd"],
            workspace_root=tmp_path,
            command_allowlist=["pwd"],
            output_budget={"stdout_bytes": -1, "timeout_seconds": "bad"},
        )
    )

    assert decision.allowed is True
    assert decision.output_budget["stdout_bytes"] == 65536
    assert decision.output_budget["timeout_seconds"] == 120
