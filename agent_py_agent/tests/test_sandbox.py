"""run_command 沙箱(1 层 bwrap)命令构造单测。

重点:① **放行外网**(--share-net 在、--unshare-net 不在)——硬约束,沙箱只隔离文件/进程不断网;
② owner home 读写 bind、系统库只读、进程隔离;③ bwrap 不可用时抛 SandboxUnavailable(由调用方降级)。
实际隔离效果(rm -rf 只删沙箱/跨 owner 拦/外网可达)在 testbox 真机极限测,mac 只测命令构造。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.tooling.sandbox import (
    SandboxSpec,
    SandboxUnavailable,
    build_bwrap_argv,
    wrap_shell_command,
)


def _spec(tmp_path):
    home = tmp_path / "owners" / "providers" / "feishu" / "users" / "A"
    home.mkdir(parents=True)
    return SandboxSpec(owner_home=home, workspace=home, bwrap_path="/fake/bwrap"), home


def test_shares_net_not_unshare(tmp_path) -> None:
    """★ 外网放行:--share-net 在、--unshare-net 绝不在(否则断网,违反硬约束)。"""
    spec, _ = _spec(tmp_path)
    argv = build_bwrap_argv(spec)
    assert "--share-net" in argv
    assert "--unshare-net" not in argv


def test_process_and_file_isolation(tmp_path) -> None:
    spec, home = _spec(tmp_path)
    argv = build_bwrap_argv(spec)
    assert "--unshare-pid" in argv  # ps 只看自己
    assert "--die-with-parent" in argv
    # owner home 读写 bind
    i = argv.index("--bind")
    assert argv[i + 1] == str(home) and argv[i + 2] == str(home)


def test_system_dirs_readonly(tmp_path) -> None:
    """系统库/工具只读(命令能跑但改不动系统);rm -rf / 删不掉这些。"""
    spec, _ = _spec(tmp_path)
    argv = build_bwrap_argv(spec)
    # /usr 之类存在的系统根以 --ro-bind 挂载
    assert "--ro-bind" in argv
    pairs = [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--ro-bind"]
    src = {p[0] for p in pairs}
    assert "/usr" in src  # mac/linux 都有 /usr


def test_dns_tls_for_outbound_https(tmp_path) -> None:
    """放行外网要带 DNS + TLS 证书,否则 https API 连不上/校验失败。"""
    spec, _ = _spec(tmp_path)
    argv = build_bwrap_argv(spec)
    ro_src = {argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"}
    # /etc/resolv.conf(DNS)在大多数系统存在;若存在必须挂
    import os
    if os.path.exists("/etc/resolv.conf"):
        assert "/etc/resolv.conf" in ro_src


def test_wrap_shell_command_shape(tmp_path) -> None:
    spec, _ = _spec(tmp_path)
    argv = wrap_shell_command("curl https://api.example.com", spec)
    assert argv[0] == "/fake/bwrap"
    assert argv[-3:] == ["/bin/sh", "-c", "curl https://api.example.com"]
    assert argv[-4] == "--"


def test_unavailable_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox.find_bwrap", lambda: None)
    home = tmp_path / "A"
    home.mkdir()
    with pytest.raises(SandboxUnavailable):
        build_bwrap_argv(SandboxSpec(owner_home=home, workspace=home))


def test_workspace_outside_home_falls_back_to_home(tmp_path) -> None:
    """workspace 不在 owner home 下时 chdir 回退到 home(防越界 chdir)。"""
    home = tmp_path / "owners" / "A"
    home.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    argv = build_bwrap_argv(SandboxSpec(owner_home=home, workspace=outside, bwrap_path="/fake/bwrap"))
    i = argv.index("--chdir")
    assert argv[i + 1] == str(home)
