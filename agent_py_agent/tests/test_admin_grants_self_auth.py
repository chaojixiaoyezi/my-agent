"""F11④ 自授权漏洞验收:owner-scoped agent 无法自写一张 bypass 授权。

端到端走 ToolRegistry.execute_call:owner-scoped(降权)场景下用 write_file 写
`<my_agent_home>/admin_grants/grant_x.json`(想给自己发 owner.full_access)→ 断言实际**没写到**
admin_grants 目录:有任务上下文时被 F11① 归一重定向进 agent 自己的 output(agent 无感"成功"了),
没任务上下文时被 owner 墙(PATH_ADMIN_GRANTS_BLOCKED)硬拦。两条路都自授权不成。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


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


def test_self_authorize_redirected_by_relocation(tmp_path, monkeypatch) -> None:
    """有任务上下文:write_file 写 admin_grants → ①归一重定向进 task_output,admin_grants 没被写。"""
    home, owner_home, workspace, admin_grant_path = _setup(tmp_path, monkeypatch)
    task_output = owner_home / "tasks" / "t1" / "output"
    registry = _registry(workspace, str(owner_home))
    result = registry.execute_call(
        {"tool": "write_file", "path": str(admin_grant_path), "content": _EVIL},
        write_boundary={"task_output_dir": str(task_output)},
    )
    # 真 admin_grants 目录没被写入这张恶意授权
    assert not admin_grant_path.exists()
    # 内容被重定向落进 agent 自己的 output(归一目的地)
    relocated = list(task_output.rglob("grant_evil.json"))
    assert relocated, f"expected relocated file under {task_output}; result={result.output}"


def test_self_authorize_blocked_without_task_ctx(tmp_path, monkeypatch) -> None:
    """无任务上下文(①归一不触发):write_file 写 admin_grants → 被 owner 墙硬拦,没写成。"""
    home, owner_home, workspace, admin_grant_path = _setup(tmp_path, monkeypatch)
    registry = _registry(workspace, str(owner_home))
    result = registry.execute_call(
        {"tool": "write_file", "path": str(admin_grant_path), "content": _EVIL},
        write_boundary=None,
    )
    assert not admin_grant_path.exists()
    assert result.ok is False
