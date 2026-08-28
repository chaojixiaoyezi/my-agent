"""宿主控制目录隔离验收:owner-scoped agent 无法写入 admin_grants。

端到端走 canonical Tool Gateway:owner-scoped(降权)场景下用 write_file 写
`<my_agent_home>/admin_grants/grant_x.json`(尝试伪造 owner.full_access)必须明确拒绝。
该目录不再是 Full Access 的运行时来源，但仍是宿主保留路径，不能让 owner 写入。
显式绝对路径不能静默改写到其他位置后谎报成功。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


def _registry(workspace: Path, owner_scope_root: str) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            shell_tool_output_max_chars=2000,
            owner_scope_root=owner_scope_root,
            path_access_mode="normal",
            operation_store_required=False,
        )
    )


def _setup(tmp_path: Path, monkeypatch):
    home = tmp_path / ".my-agent"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    owner_home = home / "owners" / "local" / "main"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    admin_grant_path = home / "admin_grants" / "grant_evil.json"
    return home, owner_home, workspace, admin_grant_path


_EVIL = '{"capability":"owner.full_access","status":"active","expires_at":"2099-01-01T00:00:00+00:00"}'


def test_self_authorize_is_rejected_without_path_relocation(tmp_path, monkeypatch) -> None:
    """有任务上下文:绝对路径保持原目标语义，并由写边界明确拒绝。"""
    home, owner_home, workspace, admin_grant_path = _setup(tmp_path, monkeypatch)
    task_output = owner_home / "tasks" / "t1" / "output"
    registry = _registry(workspace, str(owner_home))
    result = execute_registry_test_call(
        registry,
        "write_file",
        {"path": str(admin_grant_path), "content": _EVIL},
        write_boundary={"task_output_dir": str(task_output)},
    )
    assert not admin_grant_path.exists()
    assert not list(task_output.rglob("grant_evil.json"))
    assert result.ok is False
    assert result.error_code == "PATH_ADMIN_GRANTS_BLOCKED"
    assert result.failure_stage == "authorization"
    assert result.handler_executed is False
    finding = result.metadata["action_decision"]["evidence"]["gate"]["findings"][0]
    assert finding["evidence"]["resolved_path"] == str(admin_grant_path.resolve())


def test_self_authorize_blocked_without_task_ctx(tmp_path, monkeypatch) -> None:
    """无任务上下文(①归一不触发):write_file 写 admin_grants → 被 owner 墙硬拦,没写成。"""
    home, owner_home, workspace, admin_grant_path = _setup(tmp_path, monkeypatch)
    registry = _registry(workspace, str(owner_home))
    result = execute_registry_test_call(
        registry,
        "write_file",
        {"path": str(admin_grant_path), "content": _EVIL},
        write_boundary=None,
    )
    assert not admin_grant_path.exists()
    assert result.ok is False
