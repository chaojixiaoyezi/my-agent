"""run_command 沙箱(1 层 bwrap)命令构造单测。

重点:① **放行外网**(--share-net 在、--unshare-net 不在)——硬约束,沙箱只隔离文件/进程不断网;
② owner home/已授权 workspace 读写 bind、系统库只读、进程隔离;③ bwrap 不可用时
抛 SandboxUnavailable，owner-scoped 调用方必须 fail-closed。
实际隔离效果(rm -rf 只删沙箱/跨 owner 拦/外网可达)在 testbox 真机极限测,mac 只测命令构造。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.tooling.sandbox import (
    SandboxReadiness,
    SandboxSpec,
    SandboxUnavailable,
    build_bwrap_argv,
    probe_sandbox,
    wrap_shell_command,
)
from agent_py_agent.agent.tooling.shell import _sandbox_exec


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


def test_same_sandbox_policy_can_explicitly_remove_network(tmp_path) -> None:
    spec, _ = _spec(tmp_path)
    argv = build_bwrap_argv(
        SandboxSpec(
            owner_home=spec.owner_home,
            workspace=spec.workspace,
            bwrap_path=spec.bwrap_path,
            network_access=False,
        )
    )

    assert "--unshare-net" in argv
    assert "--share-net" not in argv


def test_process_and_file_isolation(tmp_path) -> None:
    spec, home = _spec(tmp_path)
    argv = build_bwrap_argv(spec)
    assert "--unshare-pid" in argv  # ps 只看自己
    assert "--proc" not in argv  # hardened 容器不允许嵌套 mount proc；空 /proc 防外部进程可见
    assert argv[argv.index("--dir") + 1] == "/proc"
    assert "--die-with-parent" in argv
    # owner home 读写 bind
    i = argv.index("--bind")
    assert argv[i + 1] == str(home) and argv[i + 2] == str(home)


def test_persona_files_are_readonly_inside_owner_shell(tmp_path) -> None:
    spec, home = _spec(tmp_path)
    (home / "SOUL.md").write_text("# SOUL\n", encoding="utf-8")
    (home / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    (home / "USER.md").write_text("# USER\n", encoding="utf-8")
    argv = build_bwrap_argv(spec)
    ro_pairs = {(argv[i + 1], argv[i + 2]) for i, item in enumerate(argv) if item == "--ro-bind"}
    assert (str(home / "SOUL.md"), str(home / "SOUL.md")) in ro_pairs
    assert (str(home / "AGENTS.md"), str(home / "AGENTS.md")) in ro_pairs
    assert (str(home / "USER.md"), str(home / "USER.md")) in ro_pairs


def test_full_access_shell_still_ro_binds_persona_files(tmp_path) -> None:
    home = tmp_path / "owner"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (home / name).write_text(name, encoding="utf-8")
    argv = build_bwrap_argv(
        SandboxSpec(
            owner_home=home,
            workspace=workspace,
            bwrap_path="/fake/bwrap",
            protected_persona_root=home,
            full_access=True,
        )
    )
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
    assert all((str(home / name), str(home / name)) in ro_pairs for name in ("SOUL.md", "USER.md", "AGENTS.md"))


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
    assert argv[-4:] == ["-o", "pipefail", "-c", "curl https://api.example.com"]
    assert argv[-5].endswith("bash")
    assert argv[-6] == "--"


def test_unavailable_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox.find_bwrap", lambda: None)
    home = tmp_path / "A"
    home.mkdir()
    with pytest.raises(SandboxUnavailable):
        build_bwrap_argv(SandboxSpec(owner_home=home, workspace=home))


def test_workspace_outside_home_is_explicit_writable_bind(tmp_path) -> None:
    """工作区在 owner home 外时只额外挂本次 workspace，不暴露父目录。"""
    home = tmp_path / "owners" / "A"
    home.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    argv = build_bwrap_argv(SandboxSpec(owner_home=home, workspace=outside, bwrap_path="/fake/bwrap"))
    i = argv.index("--chdir")
    assert argv[i + 1] == str(outside)
    bind_pairs = [(argv[i + 1], argv[i + 2]) for i, item in enumerate(argv) if item == "--bind"]
    assert (str(home), str(home)) in bind_pairs
    assert (str(outside), str(outside)) in bind_pairs
    assert (str(tmp_path), str(tmp_path)) not in bind_pairs


def test_structured_write_roots_make_owner_readonly_and_reopen_only_task_root(tmp_path) -> None:
    """子任务 shell 以 owner home 为只读底图，只把结构化任务根重新开放为可写。"""
    home = tmp_path / "owners" / "A"
    task_root = home / "tasks" / "2026-07-16" / "demo"
    task_root.mkdir(parents=True)

    argv = build_bwrap_argv(
        SandboxSpec(
            owner_home=home,
            workspace=home,
            write_roots=(task_root,),
            bwrap_path="/fake/bwrap",
        )
    )

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
    assert (str(home), str(home)) in ro_pairs
    assert (str(home), str(home)) not in bind_pairs
    assert (str(task_root), str(task_root)) in bind_pairs


def test_structured_write_roots_keep_external_read_workspace_readonly(tmp_path) -> None:
    home = tmp_path / "owners" / "A"
    task_root = home / "tasks" / "demo"
    external_source = tmp_path / "shared-source"
    task_root.mkdir(parents=True)
    external_source.mkdir()

    argv = build_bwrap_argv(
        SandboxSpec(
            owner_home=home,
            workspace=external_source,
            write_roots=(task_root,),
            bwrap_path="/fake/bwrap",
        )
    )

    ro_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, item in enumerate(argv)
        if item == "--ro-bind"
    }
    bind_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, item in enumerate(argv)
        if item == "--bind"
    }
    assert (str(external_source), str(external_source)) in ro_pairs
    assert (str(external_source), str(external_source)) not in bind_pairs


def test_read_root_is_mounted_before_nested_write_carveout(tmp_path) -> None:
    """共享源码整体只读，只有结构化指定的输出子目录重新开放写入。"""
    home = tmp_path / "owners" / "A"
    shared = tmp_path / "shared-source"
    output = shared / "build-output"
    home.mkdir(parents=True)
    output.mkdir(parents=True)

    argv = build_bwrap_argv(
        SandboxSpec(
            owner_home=home,
            workspace=shared,
            public_ro_roots=(shared,),
            write_roots=(output,),
            bwrap_path="/fake/bwrap",
        )
    )

    read_index = next(
        index
        for index, item in enumerate(argv)
        if item == "--ro-bind" and argv[index + 1] == str(shared)
    )
    write_index = next(
        index
        for index, item in enumerate(argv)
        if item == "--bind" and argv[index + 1] == str(output)
    )
    assert read_index < write_index
    assert argv[argv.index("--chdir") + 1] == str(shared)


def test_owner_scoped_shell_fails_closed_without_bwrap(tmp_path, monkeypatch) -> None:
    """owner-scoped 命令缺 sandbox 时必须拒绝，不能返回 shell=True 宿主执行。"""
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox.find_bwrap", lambda: None)
    with pytest.raises(SandboxUnavailable, match="BWRAP_NOT_FOUND"):
        _sandbox_exec("echo forbidden", tmp_path, tmp_path / "owner")


def test_owner_scoped_shell_is_hidden_when_bwrap_is_unavailable(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    monkeypatch.setattr("agent_py_agent.agent.tooling.shell.find_bwrap", lambda: None)
    tool = ShellTool(
        tmp_path,
        options=ShellToolOptions(owner_scope_root=str(tmp_path)),
    )

    availability = tool.availability()

    assert availability.available is False
    assert availability.error_code == "SANDBOX_UNAVAILABLE"
    assert "bwrap" in availability.reason


def test_unscoped_shell_stays_available_without_bwrap(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.tooling.shell import ShellTool

    monkeypatch.setattr("agent_py_agent.agent.tooling.shell.find_bwrap", lambda: None)

    assert ShellTool(tmp_path).availability().available is True


def test_probe_binary_only_returns_structured_readiness(monkeypatch) -> None:
    """镜像构建使用的 binary-only 探针保留机器 code/version。"""
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.sandbox._probe_binary",
        lambda path, timeout: SandboxReadiness(
            True,
            "SANDBOX_BINARY_READY",
            "ok",
            bwrap_path=path,
            version="bubblewrap 1.0",
            checks=("binary",),
        ),
    )
    report = probe_sandbox(bwrap_path="/fake/bwrap", binary_only=True)
    assert report.ready is True
    assert report.code == "SANDBOX_BINARY_READY"
    assert report.to_dict()["version"] == "bubblewrap 1.0"


def test_sandbox_unavailable_has_fail_closed_error_contract() -> None:
    """模型收到 sandbox 错误后应报告执行节点阻塞，不能请求未隔离重试。"""
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    contract = error_contract("SANDBOX_UNAVAILABLE")
    assert contract.code == "SANDBOX_UNAVAILABLE"
    assert contract.retryable is False
    assert contract.recommended_action == "report_blocker"
