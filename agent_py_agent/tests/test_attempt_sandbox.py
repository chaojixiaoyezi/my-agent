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
        assert Path(argv[0]).name == "bwrap"
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
