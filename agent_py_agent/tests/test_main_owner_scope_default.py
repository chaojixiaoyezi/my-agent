"""Local/main Full Access and default WorkspaceOnly owner-wall contracts."""

from __future__ import annotations

import types
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_access import resolve_owner_scope_and_access


# LLM: 测试夹具保留既有调用形状，只把三个真实依赖传给共享权限裁决。
# 函数用途: 复用原 owner 边界用例核对职责迁移，不创建产品 Agent。
def _resolve_owner_scope_and_access(agent, config):
    return resolve_owner_scope_and_access(agent.home_paths, config, agent.owner_policy)


def _home(tmp_path: Path, monkeypatch):
    home = tmp_path / ".my-agent"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    return ensure_my_agent_home(home)


def _agent(home, *, shell_access_mode: str = ""):
    owner_policy = (
        types.SimpleNamespace(shell_access_mode=shell_access_mode)
        if shell_access_mode
        else None
    )
    return types.SimpleNamespace(home_paths=home, owner_policy=owner_policy)


def _config(access_mode: str = "workspace-write"):
    return types.SimpleNamespace(access_mode=access_mode)


def test_local_main_defaults_to_owner_home_workspace_only(tmp_path, monkeypatch) -> None:
    """Local TUI admin stays inside its owner home unless Full Access is explicit."""
    home = _home(tmp_path, monkeypatch)

    scope, access = _resolve_owner_scope_and_access(_agent(home), _config())

    assert scope == str(home.owner_home_dir)
    assert access == "workspace-write"


def test_local_main_explicit_full_access_lifts_owner_wall(tmp_path, monkeypatch) -> None:
    """Only the structured local/main identity may turn full-access into host-wide access."""
    home = _home(tmp_path, monkeypatch)

    scope, access = _resolve_owner_scope_and_access(
        _agent(home),
        _config("full-access"),
    )

    assert scope == ""
    assert access == "full-access"


def test_remote_owner_cannot_self_enable_full_access(tmp_path, monkeypatch) -> None:
    """A remote user's config text cannot remove the owner wall."""
    home = replace(
        _home(tmp_path, monkeypatch),
        owner_provider="feishu",
        owner_kind="user",
        owner_id="ou_remote",
    )

    scope, access = _resolve_owner_scope_and_access(
        _agent(home),
        _config("full-access"),
    )

    assert scope == str(home.owner_home_dir)
    assert access == "workspace-write"


def test_owner_policy_can_only_narrow_non_full_access(tmp_path, monkeypatch) -> None:
    home = replace(
        _home(tmp_path, monkeypatch),
        owner_provider="feishu",
        owner_kind="user",
        owner_id="ou_restricted",
    )

    scope, access = _resolve_owner_scope_and_access(
        _agent(home, shell_access_mode="restricted"),
        _config("full-access"),
    )

    assert scope == str(home.owner_home_dir)
    assert access == "restricted"


def test_owner_wall_blocks_external_and_cross_owner_paths(tmp_path, monkeypatch) -> None:
    """WorkspaceOnly means exactly owner home plus the canonical shared capability tree."""
    home = _home(tmp_path, monkeypatch)
    scope, _ = _resolve_owner_scope_and_access(_agent(home), _config())
    policy = PathAccessPolicy.from_values(owner_scope_root=scope)

    assert policy.check(home.owner_home_dir / "projects" / "demo" / "app.py").allowed
    assert policy.check(home.owner_home_dir / "projects" / "demo" / ".env").allowed
    assert policy.check(tmp_path / "repo" / "src" / "a.py").code == "PATH_OWNER_SCOPE_BLOCKED"
    other = home.root / "owners" / "providers" / "feishu" / "users" / "B" / "USER.md"
    assert policy.check(other).code == "PATH_CROSS_OWNER_BLOCKED"


def test_explicit_full_access_can_see_external_and_other_owner_paths(
    tmp_path,
    monkeypatch,
) -> None:
    """Full Access lifts path heuristics; soft prompt guidance still asks for user intent."""
    home = _home(tmp_path, monkeypatch)
    scope, access = _resolve_owner_scope_and_access(
        _agent(home),
        _config("full-access"),
    )
    policy = PathAccessPolicy.from_values(mode="full", owner_scope_root=scope)

    assert access == "full-access"
    assert policy.check(tmp_path / "external" / "system.log").allowed
    other = home.root / "owners" / "providers" / "feishu" / "users" / "B" / "USER.md"
    assert policy.check(other).allowed
    assert policy.check(home.admin_grants_dir / "grant.json").allowed


@pytest.mark.parametrize("full,inherited", [(False, False), (True, False), (True, True)])
def test_real_agent_model_scope_uses_shared_owner_policy_and_restores_tools(tmp_path, full, inherited):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.settings.model_scope import selected_model_scope
    from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation

    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    original = agent.tools
    if full:
        execute_approval_mode_operation(agent.home_paths, "set", "full-access")
    with selected_model_scope(agent, inherited=inherited):
        unrestricted = full and not inherited
        assert agent.tools.owner_scope_root == ("" if unrestricted else str(agent.home_paths.owner_home_dir))
        assert agent.tools.path_access_mode == ("full" if unrestricted else "normal")
        assert agent.tools._mcp_clients is original._mcp_clients
    assert agent.tools is original
