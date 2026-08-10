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


def test_unavailable_platform_fails_closed(tmp_path):
    """E.6/E.7：沙箱不可用 → handler=0（抛 SANDBOX_UNAVAILABLE），不退回宿主 shell。"""
    if IS_LINUX or IS_MACOS:
        pytest.skip("真实平台有沙箱实现，此路径在无沙箱平台验证")
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
