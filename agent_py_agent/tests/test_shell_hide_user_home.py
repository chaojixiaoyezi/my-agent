"""macOS Shell 沙箱读边界第二步（开关 shell_sandbox_hide_user_home，默认关闭）。

开关打开时，非本机管理员的 owner 拒读用户家目录（本 owner 的可见范围除外），其 Shell 的 HOME 改指到 owner home，
否则 git 读 ~/.gitconfig 会因 EPERM 直接退出。本机管理员、开关关闭、以及本来就隐藏宿主路径的平台（Linux bwrap）
都只拒读 my-agent 根，行为不变。
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    _private_read_rules,
)
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.settings.user_config_capability import BOUNDARY_KEYS
from agent_py_agent.agent.tooling.shell import (
    ShellTool,
    ShellToolOptions,
    _redirected_home,
    _sandbox_scope_notice,
    _subprocess_text_env,
)
from agent_py_agent.agent.user_space.owner_access import owner_hidden_host_roots

needs_macos = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt 沙箱")


# 函数用途: 造一个假用户家目录：里面有 my-agent 根、一个飞书 owner、~/.ssh 和另一个项目，返回这些路径。
def _fake_home(tmp_path: Path) -> SimpleNamespace:
    home = (tmp_path / "home").resolve()
    root = home / ".my-agent"
    owner = root / "owners" / "providers" / "feishu" / "users" / "u1"
    (owner / "workspace").mkdir(parents=True)
    (owner / "note.md").write_text("mine", encoding="utf-8")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_test").write_text("key", encoding="utf-8")
    (home / "project").mkdir()
    (home / "project" / "notes.txt").write_text("other project", encoding="utf-8")
    return SimpleNamespace(home=home, root=root, owner=owner)


def test_switch_defaults_off_and_the_model_cannot_change_it():
    shipped = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    normalized, warnings = normalize_agent_config({"shell_sandbox_hide_user_home": "false"})

    assert AgentConfig().shell_sandbox_hide_user_home is False and shipped.shell_sandbox_hide_user_home is False
    assert normalized["shell_sandbox_hide_user_home"] is False and warnings == []
    assert "shell_sandbox_hide_user_home" in BOUNDARY_KEYS


def test_home_is_hidden_only_for_non_admin_owners_when_the_switch_is_on(tmp_path, monkeypatch):
    paths = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(paths.home))
    on = SimpleNamespace(shell_sandbox_hide_user_home=True)
    off = SimpleNamespace(shell_sandbox_hide_user_home=False)
    feishu = SimpleNamespace(root=paths.root, owner_provider="feishu", owner_kind="users")
    admin = SimpleNamespace(root=paths.root, owner_provider="local", owner_kind="main")

    assert owner_hidden_host_roots(feishu, on, platform_hides_host_paths=False) == (str(paths.root), str(paths.home))
    # 开关关、本机管理员、平台本来就隐藏宿主路径（Linux）时都只拒读 my-agent 根。
    assert owner_hidden_host_roots(feishu, off, platform_hides_host_paths=False) == (str(paths.root),)
    assert owner_hidden_host_roots(admin, on, platform_hides_host_paths=False) == (str(paths.root),)
    assert owner_hidden_host_roots(feishu, on, platform_hides_host_paths=True) == (str(paths.root),)


def test_rules_deny_every_hidden_root_before_allowing_the_owner_view(tmp_path):
    paths = _fake_home(tmp_path)
    view = paths.owner / "workspace"
    spec = AttemptSandboxSpec(attempt_view=view, staging_root=view, shared_workspace=paths.owner,
                              owner_home=paths.owner, extra_write_roots=(view,), implicit_attempt_write_roots=False,
                              private_read_roots=(paths.root, paths.home))
    rules = _private_read_rules(spec)

    # Seatbelt 后写覆盖先写：全部拒读根在前，本 owner 的放行在后，上层目录的元数据放行在最后。
    assert set(rules[:2]) == {f'(deny file-read* (subpath "{paths.home}"))',
                              f'(deny file-read* (subpath "{paths.root}"))'}
    allowed, metadata = rules[2:-1], rules[-1]
    assert all(rule.startswith("(allow file-read* (subpath ") for rule in allowed)
    assert f'(allow file-read* (subpath "{paths.owner}"))' in allowed
    # 家目录到 owner 之间的各级目录只放行元数据（git 逐级 lstat），家目录里的其它内容不放行。
    for path in (paths.home, paths.root, paths.root / "owners"):
        assert f'(literal "{path}")' in metadata
    assert ".ssh" not in metadata and "project" not in metadata


def test_subprocess_home_moves_to_the_owner_home_only_when_home_is_hidden(tmp_path, monkeypatch):
    paths = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(paths.home))

    assert _subprocess_text_env(paths.owner, hidden_roots=(str(paths.root), str(paths.home)))["HOME"] == str(paths.owner)
    # 只拒读 my-agent 根时家目录仍可读，HOME 不动；本机管理员（没有 owner 墙）也不动。
    assert _subprocess_text_env(paths.owner, hidden_roots=(str(paths.root),))["HOME"] == str(paths.home)
    assert _subprocess_text_env("", hidden_roots=(str(paths.home),))["HOME"] == str(paths.home)
    assert _redirected_home(paths.home / "sub", paths.owner, (str(paths.home),)) == str(paths.owner)


def test_owner_scoped_receipt_names_the_hidden_home(tmp_path, monkeypatch):
    paths = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(paths.home))
    monkeypatch.setattr("agent_py_agent.agent.tooling.shell.sandbox_hides_host_paths", lambda: False)

    # 函数用途: 用给定拒读根跑一次（命令执行被替换）并返回回执正文。
    def receipt(roots: tuple[str, ...]) -> str:
        tool = ShellTool(paths.owner, options=ShellToolOptions(owner_scope_root=str(paths.owner),
                                                                host_private_roots=roots))
        monkeypatch.setattr(tool, "_run_command", lambda *_a, **_k: subprocess.CompletedProcess(
            args=["bash"], returncode=0, stdout="ok\n", stderr=""))
        return tool.execute({"command": "ls", "working_dir": str(paths.owner)}).output

    assert "用户家目录" in receipt((str(paths.root), str(paths.home)))
    assert "用户家目录" not in receipt((str(paths.root),))
    assert "用户家目录" not in _sandbox_scope_notice(False, private_hidden=True)


@needs_macos
def test_registry_built_shell_hides_the_home_but_git_still_works(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    paths = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(paths.home))
    workspace = paths.owner / "workspace"
    probe = AttemptExecutionSandbox(AttemptSandboxSpec(attempt_view=workspace, staging_root=workspace,
                                                       shared_workspace=paths.owner, owner_home=paths.owner))
    if not probe.probe().ready:
        pytest.skip("sandbox-exec 不可用")

    # 函数用途: 用给定拒读根装配一个 owner 隔离的 run_command，返回在工作区里执行命令的函数。
    def shell(roots: tuple[str, ...]):
        registry = ToolRegistry(ToolRegistryParams(
            workspace_root=workspace, max_chars=6000, max_entries=200, max_matches=50, web_max_chars=12000,
            http_timeout=30, catalog_limit=20, retrieval_limit=3, vector_search_enabled=False, shell_tool_timeout=60,
            owner_scope_root=str(paths.owner), host_private_roots=roots,
        ))
        tool = registry.tools["run_command"]
        return lambda command: tool.execute({"command": command, "working_dir": str(workspace)})

    hidden = shell((str(paths.root), str(paths.home)))
    assert hidden(f"cat {paths.owner / 'note.md'}").ok
    assert not hidden(f"cat {paths.home / '.ssh' / 'id_test'}").ok
    assert not hidden(f"cat {paths.home / 'project' / 'notes.txt'}").ok
    home = hidden('printf "HOME=%s" "$HOME"')
    assert home.ok and f"HOME={paths.owner}" in home.output
    # HOME 已改指到 owner home：git 读 ~/.gitconfig 不会因 EPERM 退出。
    git = hidden("git init -q repo && git -C repo status --short && echo git-ok")
    assert git.ok and "git-ok" in git.output, git.output
    # 只拒读 my-agent 根（开关关闭）时，家目录里的其它项目仍读得到。
    assert shell((str(paths.root),))(f"cat {paths.home / 'project' / 'notes.txt'}").ok


def test_agent_wires_the_home_into_hidden_roots_for_a_non_admin_owner(tmp_path, monkeypatch):
    from agent_py_agent.agent import core
    from agent_py_agent.agent.core import SimpleAgent

    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(core, "sandbox_hides_host_paths", lambda: False)
    agent = SimpleAgent(AgentConfig(model_backend="echo", shell_sandbox_hide_user_home=True,
                                    my_agent_owner_provider="feishu", my_agent_owner_kind="users",
                                    my_agent_owner_id="u1"), tmp_path / "workspace")

    # 装配链：全局配置 + owner 身份 + 平台事实 → 注册参数 → Shell 工具。
    assert agent.tools.tools["run_command"].host_private_roots == (str(agent.home_paths.root), str(home))


def test_background_and_terminal_commands_also_get_the_owner_home(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import pty_sessions
    from agent_py_agent.agent.tooling import shell as shell_module

    paths = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(paths.home))
    roots = (str(paths.root), str(paths.home))
    captured: dict[str, dict[str, str]] = {}

    # 函数用途: 截下后台启动请求里的子进程环境，并按启动失败返回。
    def fake_start(request):
        captured["background"] = dict(request.env)
        raise ValueError("captured")

    monkeypatch.setattr(shell_module, "_background_command_argv", lambda *_a, **_k: ["/usr/bin/true"])
    monkeypatch.setattr(shell_module, "start_background_process", fake_start)
    tool = ShellTool(paths.owner, options=ShellToolOptions(owner_scope_root=str(paths.owner), host_private_roots=roots))
    tool.execute({"command": "sleep 5", "run_in_background": True, "working_dir": str(paths.owner)})

    # 函数用途: 截下终端会话 Popen 的子进程环境，并按启动失败抛出。
    def fake_popen(*_args, **kwargs):
        captured["terminal"] = dict(kwargs["env"])
        raise OSError("captured")

    monkeypatch.setattr(pty_sessions, "_sandbox_exec", lambda *_a, **_k: (["/usr/bin/true"], False))
    monkeypatch.setattr(pty_sessions.subprocess, "Popen", fake_popen)
    with pytest.raises(OSError, match="captured"):
        pty_sessions.pty_session_registry.start("true", paths.owner, paths.owner, private_roots=roots)

    assert captured["background"]["HOME"] == str(paths.owner)
    assert captured["terminal"]["HOME"] == str(paths.owner)
