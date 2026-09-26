"""AttemptExecutionSandbox 平台测试（3.txt E.4-E.8，§5 测试 8）。

平台自检通过时验证真实拦截：
- 相对路径写 attempt view 成功；
- 绝对路径 / cd 到共享 / Python open / cp / mv / symlink 写共享失败；
- 后台孙进程与主动 setsid 进程仍不能写共享；
- sandbox 不可用时 handler=0（require_ready 抛 SANDBOX_UNAVAILABLE）；
- staging 与共享 inode 不同（视图层保证）。

平台能力缺失（Linux 无 bwrap / macOS 无 sandbox-exec）→ skip 而非 fail：
readiness 的 fail-closed 行为单独用非 POSIX 平台/显式不可用路径验证。
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    SandboxUnavailableError,
)
from agent_py_agent.agent.attempt.view import build_attempt_view

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"

needs_linux = pytest.mark.skipif(not IS_LINUX, reason="Linux bwrap 沙箱")
needs_macos = pytest.mark.skipif(not IS_MACOS, reason="macOS Seatbelt 沙箱")


@pytest.fixture
def env(tmp_path):
    """共享 workspace + 视图 + staging 的沙箱环境。"""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "keep.txt").write_text("keep", encoding="utf-8")
    view = build_attempt_view(
        shared_workspace=shared,
        views_root=tmp_path / "views",
        view_name="att-1",
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    spec = AttemptSandboxSpec(
        attempt_view=view.view_path,
        staging_root=staging,
        shared_workspace=shared,
        owner_home=tmp_path / "home" / "owner",
    )
    (tmp_path / "home" / "owner").mkdir(parents=True)
    sandbox = AttemptExecutionSandbox(spec)
    return _Env(sandbox, view.view_path, shared, staging)


class _Env:
    def __init__(self, sandbox, view, shared, staging):
        self.sandbox = sandbox
        self.view = view
        self.shared = shared
        self.staging = staging


def _ready(sandbox) -> bool:
    return sandbox.probe().ready


@needs_linux
def test_linux_readiness_and_view_write(env):
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    env.sandbox.require_ready()
    result = env.sandbox.run(["sh", "-c", "printf ok > view.txt"], timeout=30)
    assert result.returncode == 0
    assert (env.view / "view.txt").read_text(encoding="utf-8") == "ok"


@needs_linux
def test_linux_absolute_write_to_shared_blocked(env):
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    result = env.sandbox.run(
        ["sh", "-c", f"echo pwn > {env.shared / 'pwn.txt'}"], timeout=30
    )
    assert result.returncode != 0
    assert not (env.shared / "pwn.txt").exists()
    assert (env.shared / "keep.txt").read_text(encoding="utf-8") == "keep"


@needs_linux
def test_linux_cd_shared_write_blocked(env):
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    result = env.sandbox.run(
        ["sh", "-c", f"cd {env.shared} && echo pwn > pwn.txt"], timeout=30
    )
    assert result.returncode != 0
    assert not (env.shared / "pwn.txt").exists()


@needs_linux
def test_linux_python_open_shared_blocked(env):
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    code = f"open({str(env.shared / 'pwn.txt')!r}, 'w').write('pwn')"
    result = env.sandbox.run(["python3", "-c", code], timeout=30)
    assert result.returncode != 0
    assert not (env.shared / "pwn.txt").exists()


@needs_linux
def test_linux_cp_mv_shared_blocked(env):
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    (env.view / "src.txt").write_text("x", encoding="utf-8")
    cp = env.sandbox.run(
        ["sh", "-c", f"cp {env.view / 'src.txt'} {env.shared / 'cp.txt'}"], timeout=30
    )
    assert cp.returncode != 0
    assert not (env.shared / "cp.txt").exists()
    mv = env.sandbox.run(
        ["sh", "-c", f"mv {env.view / 'src.txt'} {env.shared / 'mv.txt'}"], timeout=30
    )
    assert mv.returncode != 0
    assert not (env.shared / "mv.txt").exists()


@needs_linux
def test_linux_symlink_write_shared_blocked(env):
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    result = env.sandbox.run(
        ["sh", "-c", f"ln -s {env.shared / 'keep.txt'} {env.view / 'lnk'} && echo x > {env.view / 'lnk'}"],
        timeout=30,
    )
    # 写共享文件的 symlink → 失败（共享不可写）。symlink 本身可能建在 view 内，
    # 关键断言：共享内容未被改。
    assert (env.shared / "keep.txt").read_text(encoding="utf-8") == "keep"


@needs_linux
def test_linux_background_grandchild_cannot_write_shared(env):
    """E.11：后台孙进程（脱组）仍只能写 staging，不能写共享。"""
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    script = (
        f"(sleep 0.3; echo pwn > {env.shared / 'grandchild.txt'}) & "
        f"wait"
    )
    result = env.sandbox.run(["sh", "-c", script], timeout=30)
    assert result.returncode == 0  # 后台 shell 自身不报错
    assert not (env.shared / "grandchild.txt").exists()


@needs_linux
def test_linux_setsid_process_cannot_write_shared(env):
    """E.11：主动 setsid 脱离进程组的进程仍不能写共享。"""
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    result = env.sandbox.run(
        ["sh", "-c", f"setsid sh -c 'echo pwn > {env.shared / 'setsid.txt'}' ; wait"],
        timeout=30,
    )
    assert not (env.shared / "setsid.txt").exists()


@needs_linux
def test_linux_staging_inode_differs_from_shared(env):
    """测试 8：staging 与共享文件 inode 不同（视图构建保证，非 hardlink）。"""
    if not _ready(env.sandbox):
        pytest.skip("bwrap 不可用")
    (env.view / "f.txt").write_text("x", encoding="utf-8")
    assert env.view.stat().st_ino != env.shared.stat().st_ino
    assert (env.view / "keep.txt").stat().st_ino != (env.shared / "keep.txt").stat().st_ino


@needs_macos
def test_macos_readiness(env):
    if not _ready(env.sandbox):
        pytest.skip("sandbox-exec 不可用")
    report = env.sandbox.require_ready()
    assert report.ready


@needs_macos
def test_macos_absolute_write_to_shared_blocked(env):
    if not _ready(env.sandbox):
        pytest.skip("sandbox-exec 不可用")
    result = env.sandbox.run(
        ["/bin/sh", "-c", f"echo pwn > {env.shared / 'pwn.txt'}"], timeout=30
    )
    assert result.returncode != 0
    assert not (env.shared / "pwn.txt").exists()


@needs_macos
def test_macos_view_write_allowed(env):
    if not _ready(env.sandbox):
        pytest.skip("sandbox-exec 不可用")
    result = env.sandbox.run(
        ["/bin/sh", "-c", f"printf ok > {env.view / 'view.txt'}"], timeout=30
    )
    assert result.returncode == 0
    assert (env.view / "view.txt").read_text(encoding="utf-8") == "ok"


@needs_macos
def test_macos_tmp_write_blocked(env):
    if not _ready(env.sandbox):
        pytest.skip("sandbox-exec 不可用")
    result = env.sandbox.run(
        ["/bin/sh", "-c", "echo pwn > /tmp/seatbelt-pwn.txt"], timeout=30
    )
    assert result.returncode != 0
    assert not Path("/tmp/seatbelt-pwn.txt").exists()


def test_unavailable_platform_fails_closed(tmp_path, monkeypatch):
    """E.6/E.7：沙箱不可用 → handler=0（抛 SANDBOX_UNAVAILABLE），不退回宿主 shell。

    G6 起真实平台（macOS/Linux）有沙箱实现，强制无平台分支验证 fail-closed。
    """
    monkeypatch.setattr(
        "agent_py_agent.agent.attempt.sandbox.platform.system", lambda: "FreeBSD"
    )
    spec = AttemptSandboxSpec(
        attempt_view=tmp_path / "view",
        staging_root=tmp_path / "staging",
        shared_workspace=tmp_path / "shared",
        owner_home=tmp_path / "owner",
    )
    sandbox = AttemptExecutionSandbox(spec)
    with pytest.raises(SandboxUnavailableError, match="SANDBOX_UNAVAILABLE"):
        sandbox.require_ready()
    with pytest.raises(SandboxUnavailableError, match="SANDBOX_UNAVAILABLE"):
        sandbox.run(["echo", "pwn"], timeout=5)


# ---------------------------------------------------------------- G6 生产接线


def _sandbox_exec_argv(command, target, owner_home, **kwargs):
    """真实走 tooling.shell._sandbox_exec（G6 生产 spawn 唯一门）。"""
    from agent_py_agent.agent.tooling.shell import _sandbox_exec

    argv, use_shell = _sandbox_exec(command, target, owner_home, **kwargs)
    assert use_shell is False  # 只能返回沙箱 argv，绝不宿主 shell=True
    return argv


def test_sandbox_exec_owner_scoped_returns_platform_sandbox_argv(tmp_path, monkeypatch):
    """G6：owner-scoped spawn 经 Attempt 网关（Linux bwrap / macOS sandbox-exec）。"""
    if not (IS_LINUX or IS_MACOS):
        pytest.skip("Attempt 网关只支持 macOS/Linux")
    monkeypatch.setattr(
        "agent_py_agent.agent.attempt.sandbox.AttemptExecutionSandbox._READINESS_CACHE",
        {},
    )
    target = tmp_path / "task"
    target.mkdir()
    argv = _sandbox_exec_argv("echo hi", target, tmp_path / "owner")
    if IS_MACOS:
        assert Path(argv[0]).name == "sandbox-exec"
        assert "-p" in argv
    else:
        assert Path(argv[0]).name.startswith("bwrap")  # 系统 bwrap 或随包 bwrap.linux-x86_64
    assert argv[-1] == "echo hi"


# LLM: task work 是 host-authored 临时根；即使命令 cwd 位于更深项目目录，Linux /tmp
# 也不能回写 project/.sandbox-tmp，否则普通复制交付会携带构建缓存。
# 函数用途: 验证 attempt 沙箱优先用结构化任务 work 根承载持久 /tmp。
def test_linux_attempt_tmp_stays_outside_project_cwd(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.sandbox import SandboxReadiness

    owner = tmp_path / "owner"
    task_work = owner / "tasks" / "t1" / "work"
    task_output = owner / "tasks" / "t1" / "output"
    project = task_work / "project"
    for path in (owner, task_work, task_output, project):
        path.mkdir(parents=True, exist_ok=True)
    spec = AttemptSandboxSpec(
        attempt_view=project,
        staging_root=project,
        shared_workspace=owner,
        owner_home=owner,
        extra_write_roots=(task_work, task_output),
        bwrap_path="/fake/bwrap",
    )
    sandbox = AttemptExecutionSandbox(spec)
    sandbox._platform = "Linux"
    sandbox._ready = SandboxReadiness(True, "SANDBOX_READY", "ok")

    argv = sandbox.build_argv(["true"])

    bind_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, item in enumerate(argv)
        if item == "--bind"
    }
    assert (str(task_work / ".sandbox-tmp"), "/tmp") in bind_pairs
    assert (str(project / ".sandbox-tmp"), "/tmp") not in bind_pairs


def test_sandbox_exec_unscoped_uses_full_access_but_still_sandboxed(tmp_path):
    """G6/E.8：单租户（owner_home 空）也进沙箱——full_access 档（文件语义不变，
    网关统一+进程隔离）。macOS profile 不加 deny；Linux 整根 bind。"""
    if IS_MACOS:
        from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec

        target = tmp_path / "task"
        target.mkdir()
        spec = AttemptSandboxSpec(
            attempt_view=target,
            staging_root=target,
            shared_workspace=target,
            owner_home=target,
            full_access=True,
        )
        profile = AttemptExecutionSandbox._macos_profile(
            attempt_view=target,
            staging=target,
            shared=target,
            full_access=True,
        )
        assert "(deny file-write*)" not in profile  # 文件语义与宿主一致
        assert "(allow default)" in profile
    elif IS_LINUX:
        argv = _sandbox_exec_argv("echo hi", tmp_path / "task", None)
        assert ("/", "/") in {
            (argv[index + 1], argv[index + 2])
            for index, item in enumerate(argv)
            if item == "--bind"
        }


def test_full_access_keeps_persona_readonly_without_downgrading_host_access(tmp_path):
    """Persona is a precise read-only overlay, not a reason to downgrade Full Access."""
    if not IS_MACOS:
        pytest.skip("macOS Seatbelt profile 检查")
    from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox

    target = tmp_path / "task"
    target.mkdir()
    persona = tmp_path / "persona"
    persona.mkdir()
    (persona / "SOUL.md").write_text("# SOUL\n", encoding="utf-8")
    profile = AttemptExecutionSandbox._macos_profile(
        attempt_view=target,
        staging=target,
        shared=persona,
        protected_persona_root=persona,
        full_access=True,
    )
    assert "(deny file-write*)" not in profile
    soul = persona / "SOUL.md"
    assert f'(deny file-write* (literal "{soul}"))' in profile


def test_linux_full_access_with_persona_binds_host_and_ro_overlays_persona(tmp_path):
    from agent_py_agent.agent.tooling.sandbox import SandboxReadiness

    target = tmp_path / "external"
    persona = tmp_path / "owner"
    target.mkdir()
    persona.mkdir()
    (persona / "SOUL.md").write_text("# SOUL\n", encoding="utf-8")
    sandbox = AttemptExecutionSandbox(
        AttemptSandboxSpec(
            attempt_view=target,
            staging_root=target,
            shared_workspace=persona,
            owner_home=persona,
            protected_persona_root=persona,
            full_access=True,
            bwrap_path="/fake/bwrap",
        )
    )
    sandbox._platform = "Linux"
    sandbox._ready = SandboxReadiness(True, "SANDBOX_READY", "ok")

    argv = sandbox.build_argv(["true"])

    bind_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, item in enumerate(argv)
        if item == "--bind"
    }
    ro_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, item in enumerate(argv)
        if item == "--ro-bind"
    }
    assert ("/", "/") in bind_pairs
    assert (str(persona / "SOUL.md"), str(persona / "SOUL.md")) in ro_pairs


def test_linux_explicit_write_roots_do_not_make_readonly_cwd_writable(tmp_path):
    """A source cwd may be readable and executable without becoming an implicit write root."""
    from agent_py_agent.agent.tooling.sandbox import SandboxReadiness

    owner = tmp_path / "owner"
    source = tmp_path / "source"
    output = owner / "tasks" / "t1" / "work"
    for path in (owner, source, output):
        path.mkdir(parents=True)
    sandbox = AttemptExecutionSandbox(
        AttemptSandboxSpec(
            attempt_view=source,
            staging_root=source,
            shared_workspace=owner,
            owner_home=owner,
            extra_write_roots=(output,),
            public_read_roots=(source,),
            implicit_attempt_write_roots=False,
            bwrap_path="/fake/bwrap",
        )
    )
    sandbox._platform = "Linux"
    sandbox._ready = SandboxReadiness(True, "SANDBOX_READY", "ok")

    argv = sandbox.build_argv(["true"])

    bind_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, item in enumerate(argv)
        if item == "--bind"
    }
    ro_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, item in enumerate(argv)
        if item == "--ro-bind"
    }
    assert (str(source), str(source)) in ro_pairs
    assert (str(source), str(source)) not in bind_pairs
    assert (str(output), str(output)) in bind_pairs


def test_sandbox_exec_rejects_shared_workspace_write(tmp_path):
    """G6 真实拦截：owner-scoped spawn 经网关后写共享区（owner home）被拦。"""
    if not (IS_LINUX or IS_MACOS):
        pytest.skip("Attempt 网关只支持 macOS/Linux")
    from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "keep.txt").write_text("keep", encoding="utf-8")
    target = tmp_path / "task"
    target.mkdir()
    spec = AttemptSandboxSpec(
        attempt_view=target,
        staging_root=target,
        shared_workspace=shared,
        owner_home=shared,
    )
    sandbox = AttemptExecutionSandbox(spec)
    if not sandbox.probe().ready:
        pytest.skip("平台沙箱不可用")
    # argv 已是完整沙箱包装（sandbox-exec/bwrap 前缀），直接真实 spawn。
    command = f"echo pwn > {shared / 'pwn.txt'}"
    argv = _sandbox_exec_argv(command, target, shared)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0  # 写共享被沙箱拒绝，shell 报错
    assert not (shared / "pwn.txt").exists()  # 共享区无文件泄漏
    assert (shared / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_build_argv_requires_ready(tmp_path):
    spec = AttemptSandboxSpec(
        attempt_view=tmp_path / "view",
        staging_root=tmp_path / "staging",
        shared_workspace=tmp_path / "shared",
        owner_home=tmp_path / "owner",
    )
    sandbox = AttemptExecutionSandbox(spec)
    if sandbox.probe().ready:
        argv = sandbox.build_argv(["echo", "hi"])
        assert argv  # 平台就绪时能构造包装 argv
    else:
        with pytest.raises(SandboxUnavailableError):
            sandbox.build_argv(["echo", "hi"])


# 函数用途: 造一个带 my-agent 根、两个 owner、配置和共享只读包的真实目录，返回 (根, 本 owner 的 spec)。
def _private_root_spec(tmp_path: Path, **changes) -> tuple[Path, AttemptSandboxSpec]:
    root = (tmp_path / "my-agent-home").resolve()
    owner = root / "owners" / "local" / "main"
    view = owner / "workspace" / "task"
    view.mkdir(parents=True)
    (owner / "note.md").write_text("mine", encoding="utf-8")
    other = root / "owners" / "providers" / "feishu" / "users" / "u-other"
    other.mkdir(parents=True)
    (other / "memory.md").write_text("other", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "desktop.yaml").write_text("secret: x", encoding="utf-8")
    package = root / "shared" / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("pkg", encoding="utf-8")
    values = dict(attempt_view=view, staging_root=view, shared_workspace=owner, owner_home=owner,
                  extra_write_roots=(view,), public_read_roots=(package,), implicit_attempt_write_roots=False,
                  private_read_roots=(root,), macos_sandbox_exec="/usr/bin/sandbox-exec")
    values.update(changes)
    return root, AttemptSandboxSpec(**values)


def test_private_read_rules_deny_the_root_first_then_allow_the_owner_view(tmp_path):
    from agent_py_agent.agent.attempt.sandbox import _private_read_rules

    root, spec = _private_root_spec(tmp_path)
    rules = _private_read_rules(spec)

    # Seatbelt 后写覆盖先写：拒绝根必须在最前，放行在后，上层目录的元数据放行在最后。
    assert rules[0] == f'(deny file-read* (subpath "{root}"))'
    allowed, metadata = rules[1:-1], rules[-1]
    assert all(rule.startswith("(allow file-read* (subpath ") for rule in allowed)
    for path in (spec.owner_home, spec.attempt_view, *spec.public_read_roots):
        assert f'(allow file-read* (subpath "{path}"))' in allowed
    assert not any("config" in rule or "u-other" in rule for rule in allowed)
    # 根到放行目录之间的各级目录只放行元数据（literal），同级目录不放行。
    assert metadata.startswith("(allow file-read-metadata ")
    for path in (root, root / "owners", root / "owners" / "local", root / "shared"):
        assert f'(literal "{path}")' in metadata
    assert "providers" not in metadata and "config" not in metadata and "subpath" not in metadata


def test_macos_profile_adds_private_read_rules_only_for_owner_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    _root, spec = _private_root_spec(tmp_path)
    scoped = AttemptExecutionSandbox(spec)._macos_argv(["/bin/true"])[2]
    full = AttemptExecutionSandbox(AttemptSandboxSpec(**{**spec.__dict__, "full_access": True}))._macos_argv(["/bin/true"])[2]
    unset = AttemptExecutionSandbox(AttemptSandboxSpec(**{**spec.__dict__, "private_read_roots": ()}))._macos_argv(["/bin/true"])[2]

    assert "(deny file-read*" in scoped
    # Full Access 没有 owner 墙，也就没有私有目录读拒绝；未给根时保持原规则。
    assert "file-read*" not in full
    assert "file-read*" not in unset


@needs_macos
def test_macos_private_read_root_hides_other_owners_and_config(tmp_path):
    root, spec = _private_root_spec(tmp_path)
    (tmp_path / "outside.txt").write_text("host", encoding="utf-8")
    sandbox = AttemptExecutionSandbox(spec)
    if not _ready(sandbox):
        pytest.skip("sandbox-exec 不可用")

    def can_read(path: Path) -> bool:
        return sandbox.run(["/bin/cat", str(path)], timeout=30).returncode == 0

    assert can_read(spec.owner_home / "note.md")
    assert can_read(root / "shared" / "pkg" / "SKILL.md")
    assert not can_read(root / "owners" / "providers" / "feishu" / "users" / "u-other" / "memory.md")
    assert not can_read(root / "config" / "desktop.yaml")
    # 一般宿主路径仍可读（macOS 回执如实说明），写入仍只落写根。
    assert can_read(tmp_path / "outside.txt")
    write = sandbox.run(["/bin/sh", "-c", f"printf ok > {spec.attempt_view / 'out.txt'}"], timeout=30)
    assert write.returncode == 0 and (spec.attempt_view / "out.txt").read_text(encoding="utf-8") == "ok"

    def can_stat(path: Path) -> bool:
        return sandbox.run(["/usr/bin/stat", "-f", "%N", str(path)], timeout=30).returncode == 0

    # 上层目录能 stat（git 等规范化路径要逐级 lstat），但列不出内容；不在放行路径上的同级目录仍 stat 不到。
    assert can_stat(root) and can_stat(root / "owners")
    assert sandbox.run(["/bin/ls", str(root)], timeout=30).returncode != 0
    assert not can_stat(root / "owners" / "providers") and not can_stat(root / "config")
    # 回归（2026-09-26 生产）：owner 工作区在拒读根之下时，git 曾报 Invalid path … Operation not permitted。
    git = sandbox.run(["/bin/sh", "-c", f"cd {spec.attempt_view} && git init -q repo && touch repo/a "
                       "&& git -C repo add -A && echo git-ok"], timeout=60)
    assert git.returncode == 0 and "git-ok" in git.stdout, git.stderr


def test_shell_passes_the_private_root_only_for_owner_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.attempt import sandbox as attempt_sandbox
    from agent_py_agent.agent.tooling.shell import _sandbox_exec

    captured = []

    class _Capture:
        def __init__(self, spec):
            captured.append(spec)

        def build_argv(self, argv):
            return list(argv)

    monkeypatch.setattr(attempt_sandbox, "AttemptExecutionSandbox", _Capture)
    root = (tmp_path / "home").resolve()
    owner = root / "owners" / "local" / "main"
    owner.mkdir(parents=True)
    _sandbox_exec("true", owner, owner, private_roots=(root,))
    _sandbox_exec("true", owner, "", private_roots=(root,))

    assert captured[0].private_read_roots == (root,)
    assert captured[1].full_access is True and captured[1].private_read_roots == ()


@needs_macos
def test_registry_built_shell_cannot_read_other_owners_or_config_on_macos(tmp_path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    root, spec = _private_root_spec(tmp_path)
    owner = spec.owner_home
    if not _ready(AttemptExecutionSandbox(spec)):
        pytest.skip("sandbox-exec 不可用")
    registry = ToolRegistry(ToolRegistryParams(
        workspace_root=owner, max_chars=6000, max_entries=200, max_matches=50, web_max_chars=12000, http_timeout=30,
        catalog_limit=20, retrieval_limit=3, vector_search_enabled=False, shell_tool_timeout=30,
        owner_scope_root=str(owner), host_private_roots=(str(root),),
    ))
    shell = registry.tools["run_command"]

    def run(command: str):
        return shell.execute({"command": command, "working_dir": str(owner)})

    # 装配链：注册参数 → Shell 工具 → 沙箱；真实 sandbox-exec 下只剩本 owner 的范围可读。
    assert run(f"cat {owner / 'note.md'}").ok
    assert not run(f"cat {root / 'config' / 'desktop.yaml'}").ok
    assert not run(f"cat {root / 'owners' / 'providers' / 'feishu' / 'users' / 'u-other' / 'memory.md'}").ok


def test_background_command_carries_the_private_root(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import shell as shell_module
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    owner = (tmp_path / "home" / "owners" / "local" / "main").resolve()
    owner.mkdir(parents=True)
    captured = {}

    def fake_argv(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("captured")

    monkeypatch.setattr(shell_module, "_background_command_argv", fake_argv)
    tool = ShellTool(owner, options=ShellToolOptions(owner_scope_root=str(owner),
                                                     host_private_roots=(str(tmp_path / "home"),)))
    tool.execute({"command": "sleep 5", "run_in_background": True, "working_dir": str(owner)})

    # 后台命令与前台、终端会话一样把拒读根交给沙箱。
    assert captured["private_roots"] == (str(tmp_path / "home"),)


def test_agent_wires_its_home_root_into_the_shell_tool(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    shell = agent.tools.tools["run_command"]

    # 开关默认关闭：只拒读 my-agent 根，不含用户家目录。
    assert shell.host_private_roots == (str(agent.home_paths.root),)
