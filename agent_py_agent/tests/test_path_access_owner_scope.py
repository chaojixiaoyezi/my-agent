"""多用户隔离 0 层:PathAccessPolicy owner 白名单——堵住 owner A 读 B 的家。

根因:OwnerScopedAgentPool 给每个 owner 的 scoped agent 传共享 base root,只 home_paths 按 owner 分,
文件工具此前对 ~/.my-agent 整个豁免 → A 能读 ~/.my-agent/owners/B/。本测试验证:设 owner_scope_root 后,
只放行自己家和 shared/，其它 owner/identity/system/根级数据都拦；不设则保留本地管理员语义。
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


def test_owner_scope_allows_only_shared_public_root(tmp_path, monkeypatch) -> None:
    """公共能力只有 shared/ 一个权威位置；根级模板和旧 skills 目录不对 owner 暴露。"""
    home = _home(tmp_path, monkeypatch)
    policy = PathAccessPolicy.from_values(owner_scope_root=str(home / "owners" / "feishu" / "A"))
    assert policy.check(home / "shared" / "skills" / "x" / "SKILL.md").allowed
    assert policy.check(home / "shared" / "tools" / "read_file.json").allowed
    assert policy.check(home / "shared" / "scripts" / "review.py").allowed


def test_owner_scope_derives_shared_root_without_process_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MY_AGENT_HOME", raising=False)
    home = tmp_path / "custom-home"
    owner = home / "owners" / "providers" / "feishu" / "groups" / "g1"
    policy = PathAccessPolicy.from_values(owner_scope_root=str(owner))

    assert policy.check(home / "shared" / "skills" / "review" / "SKILL.md").allowed
    assert not policy.check(home / "owners" / "providers" / "feishu" / "users" / "u1" / "USER.md").allowed
    assert policy.check(home / "SOUL.md").code == "PATH_OWNER_SCOPE_BLOCKED"
    assert policy.check(home / "skills" / "shared" / "x" / "SKILL.md").allowed is False
    assert policy.check(home / "identity" / "linked_identities.jsonl").allowed is False
    assert policy.check(home / "system" / "config" / "runtime.json").allowed is False


def test_owner_scope_system_dangerous_still_blocked(tmp_path, monkeypatch) -> None:
    """开了 owner 隔离,系统高危目录仍照拦(两道墙不冲突)。"""
    from agent_py_agent.agent import path_access_policy

    home = _home(tmp_path, monkeypatch)
    # 固定模拟生产的 root systemd 进程，避免该回归只在 Linux root 节点才会暴露。
    monkeypatch.setattr(path_access_policy, "_current_user_home", lambda: Path("/root"))
    policy = PathAccessPolicy.from_values(owner_scope_root=str(home / "owners" / "x" / "A"))
    assert policy.check("/etc/passwd").allowed is False
    assert policy.check("/root/secret").allowed is False


def test_local_admin_may_use_root_home_without_owner_scope(tmp_path, monkeypatch) -> None:
    """无 owner scope 的本地管理员仍可把 root home 当项目工作区，不受远程 owner 收紧影响。"""
    from agent_py_agent.agent import path_access_policy

    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(path_access_policy, "_current_user_home", lambda: Path("/root"))
    policy = PathAccessPolicy.from_values()
    assert policy.check("/root/my-agent-src/README.md").allowed is True


def test_no_owner_scope_backward_compat(tmp_path, monkeypatch) -> None:
    """不设 owner_scope_root = 原行为:整个 .my-agent 豁免(单租户/主代理,无隔离)。"""
    home = _home(tmp_path, monkeypatch)
    policy = PathAccessPolicy.from_values()
    assert policy.check(home / "owners" / "feishu" / "B" / "SOUL.md").allowed  # 单租户:不拦
    assert policy.check(home / "anything.txt").allowed


def test_full_mode_cannot_cross_owner_boundary(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    policy = PathAccessPolicy.from_values(mode="full", owner_scope_root=str(home / "owners" / "x" / "A"))
    blocked = policy.check(home / "owners" / "x" / "B" / "SOUL.md")
    assert blocked.allowed is False and blocked.code == "PATH_CROSS_OWNER_BLOCKED"
    assert policy.check(home / "shared" / "skills" / "x" / "SKILL.md").allowed
    host_path = policy.check("/etc/passwd")
    assert host_path.allowed is False and host_path.code == "PATH_OWNER_SCOPE_BLOCKED"


def test_owner_scope_denies_unrelated_host_workspace(tmp_path, monkeypatch) -> None:
    """远程 owner 不得把宿主 service-cwd 或临时目录当成额外可读区。"""
    home = _home(tmp_path, monkeypatch)
    owner = home / "owners" / "providers" / "feishu" / "users" / "A"
    policy = PathAccessPolicy.from_values(owner_scope_root=str(owner))

    decision = policy.check(tmp_path / "service-cwd" / "private.txt")

    assert decision.allowed is False
    assert decision.code == "PATH_OWNER_SCOPE_BLOCKED"


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
    from agent_py_agent.agent.tooling._filesystem_read import (
        ReadFileTool,
        filesystem_access_options,
    )

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


def test_owner_scoped_write_cannot_use_public_my_agent_directory(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.tooling._filesystem_read import filesystem_access_options
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    home = _home(tmp_path, monkeypatch)
    owner = home / "owners" / "providers" / "feishu" / "users" / "A"
    public = home / "service-cwd"
    owner.mkdir(parents=True)
    public.mkdir(parents=True)
    tool = WriteFileTool(
        owner,
        [owner],
        WriteFileToolOptions(access_options=filesystem_access_options(owner_scope_root=str(owner))),
    )

    blocked = tool.execute({"path": str(public / "escaped.txt"), "content": "no"})
    allowed = tool.execute({"path": "tasks/ok.txt", "content": "yes"})

    assert blocked.ok is False
    assert blocked.error_code == "WRITE_FORBIDDEN"
    assert not (public / "escaped.txt").exists()
    assert allowed.ok is True


def test_owner_scoped_write_allows_structured_temporary_root(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.tooling._filesystem_read import filesystem_access_options
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    home = _home(tmp_path, monkeypatch)
    owner = home / "owners" / "providers" / "feishu" / "users" / "A"
    external = tmp_path / "explicit-output"
    owner.mkdir(parents=True)
    external.mkdir()
    tool = WriteFileTool(
        owner,
        [owner],
        WriteFileToolOptions(access_options=filesystem_access_options(owner_scope_root=str(owner))),
    )
    tool.workspace_roots = [owner.resolve(), external.resolve()]

    result = tool.execute({"path": str(external / "report.md"), "content": "ok"})

    assert result.ok is True
    assert (external / "report.md").read_text(encoding="utf-8") == "ok"
