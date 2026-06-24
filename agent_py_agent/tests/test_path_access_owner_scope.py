"""多用户隔离 0 层:PathAccessPolicy owner 白名单——堵住 owner A 读 B 的家。

根因:OwnerScopedAgentPool 给每个 owner 的 scoped agent 传共享 base root,只 home_paths 按 owner 分,
文件工具此前对 ~/.my-agent 整个豁免 → A 能读 ~/.my-agent/owners/B/。本测试验证:设 owner_scope_root 后,
自己家放行、别人家拦(PATH_CROSS_OWNER_BLOCKED)、公共区放行、系统危险目录仍拦;不设则向后兼容(原豁免)。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.path_access_policy import PathAccessPolicy


def _home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / ".my-agent"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    return home


def test_owner_scope_blocks_cross_owner(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    owner_a = home / "owners" / "feishu" / "A"
    owner_b = home / "owners" / "feishu" / "B"
    policy = PathAccessPolicy.from_values(owner_scope_root=str(owner_a))
    # 自己家:随便读写
    assert policy.check(owner_a / "SOUL.md").allowed
    assert policy.check(owner_a / "memory" / "long_term" / "memory.jsonl").allowed
    # 别人家:拦
    d = policy.check(owner_b / "SOUL.md")
    assert d.allowed is False and d.code == "PATH_CROSS_OWNER_BLOCKED"
    # 路径穿越也拦(resolve 已展开 ..)
    assert policy.check(owner_a / ".." / "B" / "USER.md").allowed is False


def test_owner_scope_allows_public_top_level(tmp_path, monkeypatch) -> None:
    """.my-agent 顶层公共区(非 owners/,如全局 SOUL/全局 skills)放行——公共可用。"""
    home = _home(tmp_path, monkeypatch)
    policy = PathAccessPolicy.from_values(owner_scope_root=str(home / "owners" / "feishu" / "A"))
    assert policy.check(home / "SOUL.md").allowed  # 全局默认人格
    assert policy.check(home / "skills" / "shared" / "x" / "SKILL.md").allowed  # 公共 skills


def test_owner_scope_system_dangerous_still_blocked(tmp_path, monkeypatch) -> None:
    """开了 owner 隔离,系统高危目录仍照拦(两道墙不冲突)。"""
    home = _home(tmp_path, monkeypatch)
    policy = PathAccessPolicy.from_values(owner_scope_root=str(home / "owners" / "x" / "A"))
    assert policy.check("/etc/passwd").allowed is False
    assert policy.check("/root/secret").allowed is False


def test_no_owner_scope_backward_compat(tmp_path, monkeypatch) -> None:
    """不设 owner_scope_root = 原行为:整个 .my-agent 豁免(单租户/主代理,无隔离)。"""
    home = _home(tmp_path, monkeypatch)
    policy = PathAccessPolicy.from_values()
    assert policy.check(home / "owners" / "feishu" / "B" / "SOUL.md").allowed  # 单租户:不拦
    assert policy.check(home / "anything.txt").allowed


def test_full_mode_allows_all(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    policy = PathAccessPolicy.from_values(mode="full", owner_scope_root=str(home / "owners" / "x" / "A"))
    assert policy.check(home / "owners" / "x" / "B" / "SOUL.md").allowed  # full:全放
    assert policy.check("/etc/passwd").allowed


def test_from_config_has_no_owner_scope(tmp_path, monkeypatch) -> None:
    """from_config 默认不带 owner_scope(主代理/单租户路径不受影响)。"""
    _home(tmp_path, monkeypatch)

    class _Cfg:
        path_access_mode = "normal"
        path_dangerous_roots = ("/etc",)

    policy = PathAccessPolicy.from_config(_Cfg())
    assert policy.owner_scope_root is None


def test_filesystem_tool_owner_scope_wired(tmp_path, monkeypatch) -> None:
    """接线验证:owner_scope_root 经 access_options 一路传到文件工具的 policy,真拦跨 owner。"""
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool, filesystem_access_options

    home = _home(tmp_path, monkeypatch)
    owner_a = home / "owners" / "providers" / "feishu" / "users" / "A"
    opt = filesystem_access_options(path_access_mode="normal", owner_scope_root=str(owner_a))
    tool = ReadFileTool(tmp_path, 1000, [tmp_path], opt)
    assert tool.path_access_policy.owner_scope_root is not None
    blocked = tool.path_access_policy.check(home / "owners" / "providers" / "feishu" / "users" / "B" / "SOUL.md")
    assert blocked.allowed is False and blocked.code == "PATH_CROSS_OWNER_BLOCKED"
    assert tool.path_access_policy.check(owner_a / "SOUL.md").allowed


def test_shell_tool_owner_scope_wired(tmp_path, monkeypatch) -> None:
    """接线验证:owner_scope_root 经 ShellToolOptions 传到 run_command 工具的 policy。"""
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    home = _home(tmp_path, monkeypatch)
    owner_a = home / "owners" / "providers" / "feishu" / "users" / "A"
    tool = ShellTool(tmp_path, options=ShellToolOptions(workspace_roots=[tmp_path], owner_scope_root=str(owner_a)))
    blocked = tool.path_access_policy.check(home / "owners" / "providers" / "feishu" / "users" / "B" / "SOUL.md")
    assert blocked.allowed is False and blocked.code == "PATH_CROSS_OWNER_BLOCKED"
