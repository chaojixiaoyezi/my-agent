"""所有 WorkspaceOnly owner 的工具工作区必须收缩到 owner home。

逃逸形态:owner 池把网关 root(部署机=源码树)传给飞书用户的 scoped agent → 源码树成了该用户
workspace_root,F11 owner 墙对工作区内路径有意豁免 → 子代理把建站文件直接写进源码树。
本测试钉死:local/remote WorkspaceOnly 都回 owner home；仅 local/main Full Access 保留显式项目。
"""

from __future__ import annotations

from pathlib import Path

from agent.core import SimpleAgent
from agent.settings.config import AgentConfig


def _build_agent(tmp_path, monkeypatch, **owner_fields):
    home = tmp_path / "ma_home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    config = AgentConfig(memory_path="memory.jsonl", **owner_fields)
    return SimpleAgent(config, project), project


def test_feishu_scoped_agent_workspace_locked_to_owner_home(tmp_path, monkeypatch):
    agent, project = _build_agent(
        tmp_path,
        monkeypatch,
        my_agent_owner_provider="feishu",
        my_agent_owner_kind="user",
        my_agent_owner_id="ou_ws_test_1",
    )
    owner_home = Path(str(agent.home_paths.owner_home_dir)).resolve()
    shell = agent.tools.tools["run_command"]
    assert shell.workspace_root == owner_home
    assert shell.artifact_backup_root == agent.home_paths.owner_artifact_backups_dir
    # 源码树(项目根)绝不能再是远程用户的合法工作区。
    assert project.resolve() not in [Path(root).resolve() for root in shell.workspace_roots]
    # 子代理管理器同规(子代理工具与主代理同一工作区边界)。
    assert Path(str(agent.subagents.workspace_root)).resolve() == owner_home


def test_local_main_workspace_only_uses_owner_home(tmp_path, monkeypatch):
    agent, project = _build_agent(tmp_path, monkeypatch)
    shell = agent.tools.tools["run_command"]

    assert shell.workspace_root == Path(agent.home_paths.owner_home_dir).resolve()
    assert shell.artifact_backup_root == agent.home_paths.owner_artifact_backups_dir
    assert project.resolve() not in shell.workspace_roots


def test_local_main_full_access_keeps_explicit_project(tmp_path, monkeypatch):
    agent, project = _build_agent(tmp_path, monkeypatch, access_mode="full-access")
    shell = agent.tools.tools["run_command"]

    assert shell.workspace_root == project.resolve()
    assert shell.path_access_policy.owner_scope_root is None
    assert shell.artifact_backup_root == agent.home_paths.owner_artifact_backups_dir
    assert not shell.artifact_backup_root.is_relative_to(project.resolve())
