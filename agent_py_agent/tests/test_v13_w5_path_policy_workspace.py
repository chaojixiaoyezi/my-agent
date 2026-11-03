"""显式工作区只豁免危险根，不扩大 owner、凭据或宿主保护边界；全用任务临时树。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """分离模拟系统危险根与宿主数据根，避免旧数据根豁免掩盖回归。"""
    home = tmp_path / "home"
    data = tmp_path / "agent-data"
    root = tmp_path / "root"
    workspace = root / ".local/share/x/projects/ws"
    workspace.mkdir(parents=True)
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_AGENT_HOME", str(data))
    monkeypatch.setattr(PathAccessPolicy, "_workspace_warning_emitted", False, raising=False)
    return SimpleNamespace(home=home, data=data, root=root, workspace=workspace, base=tmp_path)


def _config(layout, workspace_root=None, **kwargs):
    """通过生产 from_config 入口传结构化配置，不替换真实路径裁决。"""
    return SimpleNamespace(
        path_access_mode="normal",
        path_dangerous_roots=[str(layout.root)],
        workspace_root=str(layout.workspace) if workspace_root is None else workspace_root,
        **kwargs,
    )


@pytest.mark.parametrize("as_list", [False, True])
def test_config_workspace_str_or_list_allows_strict_descendant(layout, as_list):
    value = [str(layout.workspace)] if as_list else str(layout.workspace)
    policy = PathAccessPolicy.from_config(_config(layout, value), agent_home_root=layout.data)
    decision = policy.check_write(layout.workspace / "output/report.txt")
    assert decision.allowed, decision
    assert layout.workspace.resolve() in policy.workspace_roots
    assert not policy.check(layout.workspace).allowed
    assert policy.check(layout.workspace.parent / "sibling/report.txt").code == "PATH_DANGEROUS_ROOT_BLOCKED"
    assert policy.check(layout.workspace.with_name("ws-prefix") / "report.txt").code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_from_values_accepts_frozen_keyword_workspace_roots(layout):
    values = [str(layout.workspace), str(layout.workspace), "", "bad\x00root"]
    policy = PathAccessPolicy.from_values(
        dangerous_roots=[layout.root], agent_home_root=layout.data, workspace_roots=values,
    )
    values.clear()
    assert policy.workspace_roots == (layout.workspace.resolve(),)
    assert policy.check(layout.workspace / "output.txt").allowed


def test_config_list_freezes_normalized_roots_and_ignores_empty_invalid(layout, monkeypatch):
    monkeypatch.setenv("W5_WORKSPACE", str(layout.workspace))
    values = ["$W5_WORKSPACE", "", "bad\x00root", str(layout.workspace / ".." / "ws")]
    cfg = _config(layout, values)
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    values.clear()
    cfg.workspace_root = str(layout.root)
    assert policy.check(layout.workspace / "report.txt").allowed
    assert policy.workspace_roots.count(layout.workspace.resolve()) == 1


@pytest.mark.parametrize("kind", ["equal", "ancestor", "contains_nested"])
def test_workspace_equal_to_or_containing_dangerous_root_never_exempts(layout, kind):
    workspace = layout.root if kind == "equal" else layout.base
    cfg = _config(layout, str(workspace))
    if kind == "contains_nested":
        workspace = layout.workspace
        cfg.workspace_root = str(workspace)
        cfg.path_dangerous_roots.append(str(workspace / "nested-sensitive"))
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    target = layout.workspace / "ordinary.txt"
    assert policy.check(target).code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_workspace_symlink_and_target_escape_use_realpath(layout):
    etc = layout.base / "etc"
    etc.mkdir()
    alias = layout.base / "workspace-alias"
    alias.symlink_to(layout.workspace, target_is_directory=True)
    (layout.workspace / "escape").symlink_to(etc, target_is_directory=True)
    (layout.workspace / "sibling").symlink_to(layout.root / "private", target_is_directory=True)
    cfg = _config(layout, str(alias))
    cfg.path_dangerous_roots.append(str(etc))
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    assert policy.check(alias / "ok.txt").allowed
    assert policy.check(alias / "escape/shadow").code == "PATH_DANGEROUS_ROOT_BLOCKED"
    assert policy.check(alias / "sibling/key").code == "PATH_DANGEROUS_ROOT_BLOCKED"
    assert policy.check(alias / ".." / "outside.txt").code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_workspace_alias_of_dangerous_root_does_not_exempt(layout):
    alias = layout.base / "root-alias"
    alias.symlink_to(layout.root, target_is_directory=True)
    policy = PathAccessPolicy.from_config(_config(layout, str(alias)), agent_home_root=layout.data)
    assert policy.check(alias / "secret").code == "PATH_DANGEROUS_ROOT_BLOCKED"


@pytest.mark.parametrize("workspace_root", ["", [], ["", "bad\x00root"]])
def test_no_explicit_workspace_preserves_legacy_denial(layout, workspace_root):
    policy = PathAccessPolicy.from_config(_config(layout, workspace_root), agent_home_root=layout.data)
    assert policy.check(layout.workspace / "report.txt").code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_owner_workspace_derived_from_trusted_owner_or_local_main(layout):
    owner = layout.data / "owners/providers/test/users/alice"
    scoped = PathAccessPolicy.from_config(_config(layout, ""), owner_scope_root=owner, agent_home_root=layout.data)
    local = PathAccessPolicy.from_config(_config(layout, ""), agent_home_root=layout.data)
    assert owner / "workspace" in scoped.workspace_roots
    assert layout.data / "owners/local/main/workspace" in local.workspace_roots


def test_workspace_does_not_bypass_credential_filenames_or_host_exclusive(layout):
    source = layout.workspace / "host-config"
    cfg = _config(layout, host_exclusive_path_roots=[str(source)])
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    assert policy.check(layout.workspace / ".env").code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert policy.check(layout.workspace / "auth.json").code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert policy.check(layout.workspace / ".env.example").allowed
    assert policy.check(source / "config.txt").code == "PATH_HOST_CREDENTIAL_BLOCKED"


def test_workspace_does_not_bypass_host_credential_or_managed_and_write_guards(layout):
    owner = layout.data / "owners/local/main"
    cfg = _config(layout, [str(layout.data / "config"), str(owner)])
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    assert policy.check(layout.data / "config/shared-model-profiles.json").code == "PATH_HOST_CREDENTIAL_BLOCKED"
    assert policy.check(owner / "data/plugins/payload.txt").code == "PATH_HOST_MANAGED_STORE_BLOCKED"
    assert policy.check_write(owner / "config/options.json").code == "PATH_HOST_CONFIG_WRITE_BLOCKED"
    assert policy.check_write(owner / "workspace/runtime/state.json").code == "PATH_HOST_STATE_WRITE_BLOCKED"


@pytest.mark.parametrize("mode", ["normal", "full"])
def test_explicit_workspace_never_bypasses_owner_wall(layout, mode):
    owner = layout.data / "owners/providers/test/users/alice"
    other = layout.data / "owners/providers/test/users/bob"
    cfg = _config(layout, [str(layout.workspace), str(other)])
    cfg.path_access_mode = mode
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data, owner_scope_root=owner)
    assert policy.check(layout.workspace / "report.txt").code == "PATH_OWNER_SCOPE_BLOCKED"
    assert policy.check(other / "workspace/report.txt").code == "PATH_CROSS_OWNER_BLOCKED"
    assert policy.check(owner / "workspace/report.txt").allowed


def test_startup_warning_once_per_process_across_policy_instances(layout, caplog):
    caplog.set_level(logging.WARNING)
    cfg = _config(layout)
    PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    records = [r for r in caplog.records if "PATH_WORKSPACE_UNDER_DANGEROUS_ROOT" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert str(layout.workspace) in records[0].getMessage()
    assert str(layout.root) in records[0].getMessage()


def test_safe_config_does_not_consume_warning_and_concurrent_builds_warn_once(layout, caplog):
    caplog.set_level(logging.WARNING)
    PathAccessPolicy.from_config(_config(layout, str(layout.base / "safe")), agent_home_root=layout.data)
    assert not [r for r in caplog.records if "PATH_WORKSPACE_UNDER_DANGEROUS_ROOT" in r.getMessage()]
    cfg = _config(layout)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: PathAccessPolicy.from_config(cfg, agent_home_root=layout.data), range(12)))
    records = [r for r in caplog.records if "PATH_WORKSPACE_UNDER_DANGEROUS_ROOT" in r.getMessage()]
    assert len(records) == 1


def test_file_tool_external_grant_keeps_workspace_and_hard_guards(layout):
    from agent_py_agent.agent.tooling._filesystem_read import (
        ReadFileTool,
        filesystem_access_options,
    )

    owner = layout.data / "owners/providers/test/users/alice"
    protected = layout.workspace / "host-config"
    policy = PathAccessPolicy.from_config(
        _config(layout, host_exclusive_path_roots=[str(protected)]),
        agent_home_root=layout.data, owner_scope_root=owner,
    )
    target = layout.workspace / "report.txt"
    target.write_text("workspace report", encoding="utf-8")
    tool = ReadFileTool(owner, 1000, [owner], filesystem_access_options(owner_scope_root=str(owner)))
    tool.path_access_policy = policy
    assert tool.check_path_access(target).code == "PATH_OWNER_SCOPE_BLOCKED"
    tool.granted_external_roots = (layout.workspace.resolve(),)
    assert tool.resolve_path(target) == target.resolve()
    assert tool.execute({"path": str(target)}).ok is True
    assert tool.check_path_access(layout.workspace / ".env").code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert tool.check_path_access(protected / "options.txt").code == "PATH_HOST_CREDENTIAL_BLOCKED"
