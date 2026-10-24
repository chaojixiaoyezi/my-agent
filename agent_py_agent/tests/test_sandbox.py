"""run_command 沙箱(1 层 bwrap)命令构造单测。

重点:① **放行外网**(--share-net 在、--unshare-net 不在)——硬约束,沙箱只隔离文件/进程不断网;
② owner home/已授权 workspace 读写 bind、系统库只读、进程隔离;③ bwrap 不可用时
抛 SandboxUnavailable，owner-scoped 调用方必须 fail-closed。
实际隔离效果(rm -rf 只删沙箱/跨 owner 拦/外网可达)在 测试机 真机极限测,mac 只测命令构造。
"""

from __future__ import annotations

import subprocess

import pytest

from agent_py_agent.agent.tooling.sandbox import (
    SandboxReadiness,
    SandboxSpec,
    SandboxUnavailable,
    build_bwrap_argv,
    probe_sandbox,
    wrap_shell_command,
)
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions, _sandbox_exec


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


# LLM: 在实际bwrap构造入口观察启动器，不以未接线的helper存在冒充生产生效。
# 函数用途: 暴露工具命令尚未安装子进程资源守卫。
def test_bwrap_command_installs_child_resource_launcher(tmp_path):
    spec, _ = _spec(tmp_path)
    argv = build_bwrap_argv(spec)
    assert "--resource-launcher" in argv
    assert argv.index("--resource-launcher") < argv.index("/fake/bwrap")
    assert "--new-session" in argv and "--die-with-parent" in argv


# LLM: AS/NPROC默认不设，避免破坏V8/JVM虚拟内存保留；正nice与OOM偏置是软防护不是绝对存活保证。
# 函数用途: 锁定安全默认值及结构化资源策略。
def test_child_resource_limits_defaults():
    from agent_py_agent.agent.tooling import sandbox
    limits_type = getattr(sandbox, "ResourceLimits", None)
    assert limits_type is not None
    limits = limits_type()
    assert (limits.nice_increment, limits.max_memory_mb, limits.max_processes, limits.oom_score_adj) == (5, 0, 0, 300)


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
    # /proc 隔离:真机挂只读真实 procfs,嵌套容器回退空目录——两种形态都不暴露宿主进程
    if "--proc" in argv:
        assert "--remount-ro" in argv  # procfs 必须锁只读,防改内核全局参数
        assert argv[argv.index("--proc") + 1] == "/proc"
    else:
        assert argv[argv.index("--dir") + 1] == "/proc"
    assert "--die-with-parent" in argv
    # owner home 读写 bind（第一个 --bind 是 .sandbox-tmp 持久临时目录）
    binds = {
        (argv[i + 1], argv[i + 2]) for i, flag in enumerate(argv) if flag == "--bind"
    }
    assert (str(home), str(home)) in binds
    assert (str(home / ".sandbox-tmp"), "/tmp") in binds


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


def test_full_access_rebinds_device_tree_after_root_mount(tmp_path) -> None:
    """Full Access 的根挂载不能把 /dev/null 等基础设备变成 nodev。"""
    home = tmp_path / "owner"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()

    argv = build_bwrap_argv(
        SandboxSpec(
            owner_home=home,
            workspace=workspace,
            bwrap_path="/fake/bwrap",
            full_access=True,
        )
    )

    root_bind = argv.index("--bind")
    device_bind = argv.index("--dev-bind")
    assert argv[root_bind + 1 : root_bind + 3] == ["/", "/"]
    assert argv[device_bind + 1 : device_bind + 3] == ["/dev", "/dev"]
    assert root_bind < device_bind


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


# LLM: 资源包装改变前缀但不能改写原bwrap/严格shell尾部，仍不使用shell=True或preexec。
# 函数用途: 核对资源启动器与原沙箱命令的组合形状。
def test_wrap_shell_command_shape(tmp_path) -> None:
    spec, _ = _spec(tmp_path)
    argv = wrap_shell_command("curl https://api.example.com", spec)
    assert argv[argv.index("--resource-launcher") + 2] == "/fake/bwrap"
    assert argv[-4:] == ["-o", "pipefail", "-c", "curl https://api.example.com"]
    assert argv[-5].endswith("bash")
    assert argv[-6] == "--"


# LLM: 父进程只冻结env，模型后来更改环境不能解除本次策略；用真实SandboxSpec验证生产接线。
# 函数用途: 验证资源上限的配置冻结与显式策略覆盖。
def test_resource_environment_is_frozen_in_spec(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.tooling import sandbox
    monkeypatch.setenv("MY_AGENT_TOOL_MAX_MEMORY_MB", "256")
    monkeypatch.setenv("MY_AGENT_TOOL_MAX_PIDS", "64")
    monkeypatch.setenv("MY_AGENT_TOOL_NICE", "7")
    spec, _ = _spec(tmp_path)
    monkeypatch.setenv("MY_AGENT_TOOL_MAX_MEMORY_MB", "0")
    argv = build_bwrap_argv(spec)
    policy = json.loads(argv[argv.index("--resource-launcher") + 1])
    assert policy == {"max_memory_mb": 256, "max_processes": 64, "nice_increment": 7, "oom_score_adj": 300}
    explicit = sandbox.ResourceLimits(nice_increment=0, oom_score_adj=500)
    assert sandbox.SandboxSpec(spec.owner_home, spec.workspace, resource_limits=explicit).resource_limits is explicit


# LLM: 私有POSIX视图隔离宿主os.name，兼容没有nice的Windows；全部资源施加由替身记录，不mock守卫本身。
# 函数用途: 提供资源施加顺序、继承限制及失败测试所用的系统观察器。
def _resource_os(monkeypatch, *, inherited=(-1, -1), oom="0"):
    import sys
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import sandbox
    seen = []
    resource = SimpleNamespace(RLIMIT_AS=9, RLIMIT_NPROC=6, RLIM_INFINITY=-1,
        getrlimit=lambda _which: inherited,
        setrlimit=lambda which, values: seen.append((which, values)))
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    os_view = SimpleNamespace(**vars(sandbox.os))
    os_view.name = "posix"
    monkeypatch.setattr(sandbox, "os", os_view)
    monkeypatch.setattr(sandbox.os, "nice", lambda value: seen.append(("nice", value)), raising=False)
    monkeypatch.setattr(sandbox, "Path", lambda _path: SimpleNamespace(
        read_text=lambda: oom, write_text=lambda value: seen.append(("oom", value))))
    return sandbox, resource, seen


# LLM: 仅替换sandbox持有的OS命名空间，不改宿主os.name；Linux夹具即使在缺nice的Windows入口也须记录POSIX分支。
# 函数用途: 复现nice初始不存在时夹具失败，并验证平台模拟不依赖宿主。
@pytest.mark.parametrize("host_name", ["nt", "posix"])
def test_resource_os_fixture_accepts_namespace_without_nice(monkeypatch, host_name):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import sandbox

    original_os = sandbox.os
    namespace = SimpleNamespace(name=host_name)
    assert not hasattr(namespace, "nice")
    failure = None
    with monkeypatch.context() as patch:
        patch.setattr(sandbox, "os", namespace)
        try:
            module, _resource, seen = _resource_os(patch)
        except AttributeError as error:
            failure = error
        assert failure is None, f"missing os.nice must not break the fixture: {failure}"
        assert module._apply_child_resources(module.ResourceLimits()) == []
        assert seen == [("nice", 5), ("oom", "300")]
        assert namespace.name == host_name
    assert sandbox.os is original_os


# LLM: 子进程软硬都只收紧原上限，原OOM偏置较高时不能调低；正配置才添加RLIMIT。
# 函数用途: 验证默认soft防护与显式内存/进程数上限。
@pytest.mark.parametrize("inherited,oom,expected", [((-1, -1), "0", 128), ((80, 90), "500", 80)])
def test_child_resource_guards_apply_and_preserve_stricter_limits(monkeypatch, inherited, oom, expected):
    sandbox, _resource, seen = _resource_os(monkeypatch, inherited=inherited, oom=oom)
    limits = sandbox.ResourceLimits(max_memory_mb=128, max_processes=32)
    assert sandbox._apply_child_resources(limits) == []
    memory = 128 * 1024**2 if expected == 128 else expected
    assert seen == [("nice", 5), ("oom", str(max(300, int(oom)))), (9, (memory, memory)), (6, (32, 32))]


# LLM: 默认不调用RLIMIT；OS缺nice/proc/resource能力时记录unavailable而不把其它保护或工具执行一起取消。
# 函数用途: 核对跨平台不支持和权限拒绝的soft降级。
@pytest.mark.parametrize("missing", ["nice", "proc", "resource", "constant", "not_implemented", "unsupported_errno"])
def test_child_guards_report_unsupported_capabilities(monkeypatch, missing):
    import errno
    import sys
    sandbox, resource, seen = _resource_os(monkeypatch)

    # LLM: 抛出精确的系统失败类型，不替换守卫或直接伪造unavailable结果。
    # 函数用途: 模拟当前OS不支持指定资源操作。
    def unavailable(*_args):
        raise OSError(errno.ENOSYS, "fixture unsupported")

    if missing == "nice":
        monkeypatch.setattr(sandbox.os, "nice", unavailable, raising=False)
    elif missing == "proc":
        monkeypatch.setattr(sandbox, "Path", unavailable)
    elif missing == "resource":
        monkeypatch.setitem(sys.modules, "resource", None)
    elif missing == "constant":
        monkeypatch.delattr(resource, "RLIMIT_AS")
    elif missing == "not_implemented":
        monkeypatch.setattr(resource, "getrlimit", lambda _which: (_ for _ in ()).throw(NotImplementedError()))
    else:
        monkeypatch.setattr(resource, "getrlimit", unavailable)
    reports = sandbox._apply_child_resources(sandbox.ResourceLimits(max_memory_mb=128))
    assert reports and all("resource" in report and "error_type" in report for report in reports)
    assert ("nice", 5) in seen or missing == "nice"


# LLM: 硬上限无法施加不是成功；不能吞EPERM后执行无限额命令。所有限制系统调用均替身隔离。
# 函数用途: 验证施加失败时结构化拒绝exec而非悄悄放开。
def test_explicit_limit_failure_refuses_exec(monkeypatch, capsys):
    import errno
    import json
    sandbox, resource, _seen = _resource_os(monkeypatch)
    executed = []

    # LLM: 模拟确有能力但本次不能施加，而非平台缺失。
    # 函数用途: 返回硬上限权限错误。
    def denied(*_args):
        raise PermissionError(errno.EPERM, "fixture limit denied")

    monkeypatch.setattr(resource, "setrlimit", denied)
    monkeypatch.setattr(sandbox.os, "execvp", lambda *_: executed.append(True))
    result = sandbox._resource_launcher([json.dumps({"max_memory_mb": 128}), "/bin/true"])
    assert result == 98 and not executed
    assert json.loads(capsys.readouterr().err)["error_code"] == "SANDBOX_RESOURCE_GUARD_FAILED"


# LLM: 标准Popen新session由原调用方建立，资源启动器不可破坏它；真实限制仅在新子进程运行。
# 函数用途: 读回子进程nice/session，确认exec保留PID且父进程值不变。
def test_real_posix_resource_launcher_preserves_session_and_parent(tmp_path):
    import json
    import os
    import sys

    from agent_py_agent.agent.tooling.sandbox import ResourceLimits, wrap_resource_command
    if os.name != "posix":
        pytest.skip("requires POSIX nice/session")
    before, sid = os.getpriority(os.PRIO_PROCESS, 0), os.getsid(0)
    code = "import os,json; print(json.dumps([os.getpid(),os.getsid(0),os.getpgrp(),os.getpriority(os.PRIO_PROCESS,0)]))"
    argv = wrap_resource_command([sys.executable, "-I", "-B", "-c", code], ResourceLimits())
    with subprocess.Popen(argv, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, stdin=subprocess.DEVNULL, start_new_session=True) as child:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert json.loads(stdout) == [child.pid, child.pid, child.pid, min(19, before + 5)]
    assert os.getpriority(os.PRIO_PROCESS, 0) == before and os.getsid(0) == sid


# LLM: 不制造主机OOM或fork炸弹；Linux真子进程只试受控分配，验证RLIMIT与OOM偏置的真实读回。
# 函数用途: 在Linux验证超过显式地址空间上限时子进程得到MemoryError而父进程继续存活。
def test_real_linux_memory_ceiling_and_oom_bias(tmp_path):
    import os
    import sys

    from agent_py_agent.agent.tooling.sandbox import ResourceLimits, wrap_resource_command
    if sys.platform != "linux":
        pytest.skip("requires real Linux RLIMIT_AS and /proc")
    from pathlib import Path
    before = Path("/proc/self/oom_score_adj").read_text()
    code = ("import resource; from pathlib import Path; "
            "assert resource.getrlimit(resource.RLIMIT_AS) == (134217728,134217728); "
            "assert int(Path('/proc/self/oom_score_adj').read_text()) >= 300; "
            "exec('try:\\n bytearray(268435456)\\nexcept MemoryError:\\n print(\"MEMORY_GUARDED\")')")
    result = subprocess.run(wrap_resource_command([sys.executable, "-I", "-B", "-c", code],
        ResourceLimits(max_memory_mb=128)), cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == "MEMORY_GUARDED", result.stderr
    assert Path("/proc/self/oom_score_adj").read_text() == before and os.getpid() > 0


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


def test_owner_control_metadata_overrides_broad_owner_write_mount(tmp_path) -> None:
    """A writable owner home still mounts its host-authoritative policy file read-only last."""
    home = tmp_path / "owners" / "local" / "main"
    policy = home / "permissions.json"
    home.mkdir(parents=True)
    policy.write_text("{}", encoding="utf-8")

    argv = build_bwrap_argv(
        SandboxSpec(
            owner_home=home,
            workspace=home,
            write_roots=(home,),
            read_only_paths=(policy,),
            bwrap_path="/fake/bwrap",
        )
    )

    owner_write_index = next(
        index
        for index, item in enumerate(argv)
        if item == "--bind" and argv[index + 1] == str(home)
    )
    policy_read_index = next(
        index
        for index, item in enumerate(argv)
        if item == "--ro-bind" and argv[index + 1] == str(policy)
    )
    assert owner_write_index < policy_read_index


def test_owner_scoped_shell_fails_closed_without_bwrap(tmp_path, monkeypatch) -> None:
    """owner-scoped 命令缺平台沙箱时必须拒绝，不能返回 shell=True 宿主执行。

    G6：macOS 用 Seatbelt（sandbox-exec）兜底，故强制 Linux+bwrap 缺失分支验证
    fail-closed；macOS 真实 Seatbelt 拦截在 test_attempt_sandbox.py 真机覆盖。
    """
    monkeypatch.setattr(
        "agent_py_agent.agent.attempt.sandbox.AttemptExecutionSandbox._READINESS_CACHE",
        {},
    )
    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox.find_bwrap", lambda: None)
    with pytest.raises(SandboxUnavailable, match="BWRAP_NOT_FOUND"):
        _sandbox_exec("echo forbidden", tmp_path, tmp_path / "owner")


def test_owner_scoped_shell_is_hidden_when_bwrap_is_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_py_agent.agent.attempt.sandbox.AttemptExecutionSandbox._READINESS_CACHE",
        {},
    )
    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox.find_bwrap", lambda: None)
    tool = ShellTool(
        tmp_path,
        options=ShellToolOptions(owner_scope_root=str(tmp_path)),
    )

    availability = tool.availability()

    assert availability.available is False
    assert availability.error_code == "SANDBOX_UNAVAILABLE"
    assert "attempt 沙箱" in availability.reason


def test_owner_scoped_shell_failure_explains_hidden_host_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agent_py_agent.agent.tooling.shell.sandbox_hides_host_paths", lambda: True)
    owner = tmp_path / "owner"
    owner.mkdir()
    tool = ShellTool(
        owner,
        options=ShellToolOptions(owner_scope_root=str(owner)),
    )
    monkeypatch.setattr(
        tool,
        "_run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["bash"],
            returncode=1,
            stdout="",
            stderr="bash: /hidden/other-owner: No such file or directory\n",
        ),
    )

    result = tool.execute({"command": "test -e /hidden/other-owner", "working_dir": str(owner)})

    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert "未挂载路径的不存在、拒绝或沙箱内成功都不能证明宿主路径状态" in result.output
    # 第 16 条：owner 隔离时另附本次允许的目录；在沙箱里非零退出再附"可能越界"的提示与建议码。
    roots = {"read_write": [str(owner.resolve())], "read_only": [str(owner.resolve())]}
    assert result.result_envelope["sandbox"] == {
        "file_scope": "owner_workspace_only",
        "external_host_paths_hidden": True,
        "host_path_absence_proven": False,
        "sandbox_active": True,
        "allowed_roots": roots,
        "boundary_hint": {"may_be_sandbox_boundary": True,
                          "suggested_actions": ["limit_to_allowed_roots", "request_capability", "ask_user"]},
    }


def test_owner_scoped_shell_stderr_keeps_scope_fact_when_command_masks_failure(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("agent_py_agent.agent.tooling.shell.sandbox_hides_host_paths", lambda: True)
    owner = tmp_path / "owner"
    owner.mkdir()
    tool = ShellTool(
        owner,
        options=ShellToolOptions(owner_scope_root=str(owner)),
    )
    monkeypatch.setattr(
        tool,
        "_run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["bash"],
            returncode=0,
            stdout="exit=1\n",
            stderr="bash: /hidden/other-owner: No such file or directory\n",
        ),
    )

    result = tool.execute(
        {
            "command": "false; echo exit=$?",
            "working_dir": str(owner),
        }
    )

    assert result.ok is True
    assert "host_path_absence_proven=false" in result.output
    assert result.result_envelope["sandbox"]["external_host_paths_hidden"] is True


def test_owner_scoped_shell_success_explains_isolated_absolute_paths(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("agent_py_agent.agent.tooling.shell.sandbox_hides_host_paths", lambda: True)
    owner = tmp_path / "owner"
    owner.mkdir()
    tool = ShellTool(
        owner,
        options=ShellToolOptions(owner_scope_root=str(owner)),
    )
    monkeypatch.setattr(
        tool,
        "_run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["bash"],
            returncode=0,
            stdout="uid=0(root)\nEXIT_CODE:0\n",
            stderr="",
        ),
    )

    result = tool.execute(
        {
            "command": "whoami",
            "working_dir": str(owner),
        }
    )

    assert result.ok is True
    assert "uid=0、/root 或其它绝对路径都不代表宿主权限" in result.output
    assert "只有结构化授权写根内的结果会持久化到宿主" in result.output
    roots = {"read_write": [str(owner.resolve())], "read_only": [str(owner.resolve())]}
    assert result.result_envelope["sandbox"] == {
        "file_scope": "owner_workspace_only",
        "external_host_paths_hidden": True,
        "host_path_absence_proven": False,
        "sandbox_active": True,
        "allowed_roots": roots,
    }


def test_sandbox_read_isolation_fact_follows_the_platform() -> None:
    from agent_py_agent.agent.attempt.sandbox import sandbox_hides_host_paths

    # Linux bwrap 只挂载授权根；macOS Seatbelt 只拦写、读取不受限。
    assert sandbox_hides_host_paths("Linux") is True
    assert sandbox_hides_host_paths("Darwin") is False


def test_owner_scoped_shell_on_macos_says_host_paths_are_readable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agent_py_agent.agent.tooling.shell.sandbox_hides_host_paths", lambda: False)
    owner = tmp_path / "owner"
    owner.mkdir()
    tool = ShellTool(owner, options=ShellToolOptions(owner_scope_root=str(owner)))
    monkeypatch.setattr(
        tool,
        "_run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(args=["bash"], returncode=0, stdout="ok\n", stderr=""),
    )

    result = tool.execute({"command": "cat /etc/hosts", "working_dir": str(owner)})

    # 回执文本与结构化事实同源：macOS 不能再声称宿主路径已隐藏，也不给工作区外的读取背书。
    assert "external_host_paths_hidden=false" in result.output
    assert "当前平台的沙箱只限制写入，不隐藏宿主上的一般路径" in result.output
    # 这个工具没有配置 my-agent 根，就不能声称私有目录读不到。
    assert "读不到" not in result.output
    assert "不要读取或依赖它们" in result.output
    assert "隔离视图" not in result.output
    assert result.result_envelope["sandbox"] == {
        "file_scope": "owner_workspace_only",
        "external_host_paths_hidden": False,
        "host_path_absence_proven": False,
        "sandbox_active": True,
        "allowed_roots": {"read_write": [str(owner.resolve())], "read_only": [str(owner.resolve())]},
    }


def test_owner_scoped_shell_on_macos_names_the_private_dirs_when_denied(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agent_py_agent.agent.tooling.shell.sandbox_hides_host_paths", lambda: False)
    owner = tmp_path / "owner"
    owner.mkdir()
    tool = ShellTool(owner, options=ShellToolOptions(owner_scope_root=str(owner), host_private_roots=(str(tmp_path),)))
    monkeypatch.setattr(
        tool,
        "_run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(args=["bash"], returncode=0, stdout="ok\n", stderr=""),
    )

    result = tool.execute({"command": "ls", "working_dir": str(owner)})

    assert "其它 owner、配置、发布记录）读不到" in result.output
    assert result.result_envelope["sandbox"]["external_host_paths_hidden"] is False


def test_unscoped_shell_availability_tracks_attempt_sandbox(tmp_path, monkeypatch) -> None:
    """单租户也经 attempt 沙箱（E.8）：平台沙箱可用→ready；不可用→unavailable。"""
    from agent_py_agent.agent.attempt.sandbox import SandboxReadiness
    from agent_py_agent.agent.tooling.shell import ShellTool

    ready = SandboxReadiness(True, "SANDBOX_READY", "ok")
    monkeypatch.setattr(
        "agent_py_agent.agent.attempt.sandbox.AttemptExecutionSandbox.probe",
        lambda self, binary_only=False: ready,
    )
    assert ShellTool(tmp_path).availability().available is True

    monkeypatch.setattr(
        "agent_py_agent.agent.attempt.sandbox.AttemptExecutionSandbox.probe",
        lambda self, binary_only=False: SandboxReadiness(
            False, "BWRAP_NOT_FOUND", "当前执行环境不支持沙箱"
        ),
    )
    availability = ShellTool(tmp_path).availability()
    assert availability.available is False
    assert availability.error_code == "SANDBOX_UNAVAILABLE"


def test_windows_unscoped_shell_stays_available_without_sandbox(tmp_path, monkeypatch) -> None:
    """Windows 单租户保留宿主 powershell（Attempt 网关不支持该平台）。"""
    from agent_py_agent.agent.tooling import shell as shell_module
    from agent_py_agent.agent.tooling.shell import ShellTool

    monkeypatch.setattr(shell_module, "os", _FakeOs("nt"))
    assert ShellTool(tmp_path).availability().available is True


class _FakeOs:
    def __init__(self, name: str) -> None:
        self.name = name


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


def test_proc_mount_uses_real_procfs_when_probe_succeeds(tmp_path, monkeypatch) -> None:
    """真机可挂真实 procfs 时用 --proc:go 等 trimmed 发行版二进制靠 /proc/self/exe
    推导自身根目录(实测 测试机 空 /proc 里 go 报 "binary is trimmed and GOROOT is not set")。"""
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox._PROC_MOUNT_KIND", None)
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox.find_bwrap", lambda: "/fake/bwrap")
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.sandbox.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0),
    )

    spec, _ = _spec(tmp_path)
    argv = build_bwrap_argv(spec)

    assert "--proc" in argv
    assert argv[argv.index("--proc") + 1] == "/proc"
    assert "--remount-ro" in argv  # procfs 必须锁只读(Docker 同款)
    assert argv[argv.index("--remount-ro") + 1] == "/proc"
    assert "--dir" not in argv


def test_proc_mount_falls_back_to_empty_dir_when_procfs_probe_fails(tmp_path, monkeypatch) -> None:
    """嵌套容器(Docker Desktop/K8s hardened runtime)内核拒 mount("proc") 时
    回退旧行为:空 /proc 目录,进程隔离仍在(--unshare-pid)。"""
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox._PROC_MOUNT_KIND", None)
    monkeypatch.setattr("agent_py_agent.agent.tooling.sandbox.find_bwrap", lambda: "/fake/bwrap")
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.sandbox.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=1),
    )

    spec, _ = _spec(tmp_path)
    argv = build_bwrap_argv(spec)

    assert "--proc" not in argv
    assert argv[argv.index("--dir") + 1] == "/proc"
    assert "--unshare-pid" in argv


# LLM: SANDBOX-01(2026-08-15 真机): 沙箱 /tmp 映射根必须落在任务 write_roots[0]
# (任务 work 目录)而非 workspace(项目目录)——否则项目目录被污染且任务收口无从发布。
# 函数用途: 验证有 write_roots 时 tmp 根在 write_roots[0]/.sandbox-tmp。
def test_sandbox_tmp_root_uses_write_roots(tmp_path):
    from agent_py_agent.agent.tooling.sandbox import SandboxSpec, build_bwrap_argv

    workspace = tmp_path / "project"
    work_dir = tmp_path / "tasks" / "t1" / "work"
    workspace.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    spec = SandboxSpec(
        workspace=workspace,
        write_roots=[work_dir],
        owner_home=tmp_path / "owner",
        network_access=False,
        bwrap_path="/usr/bin/bwrap",  # 本机无 bwrap，测试只验证 argv 构造
    )
    argv = build_bwrap_argv(spec)
    joined = " ".join(argv)
    assert str(work_dir / ".sandbox-tmp") in joined
    assert "/tmp" in joined
    # 项目目录不应被当作 tmp 根
    assert str(workspace / ".sandbox-tmp") not in joined


def test_allowlisted_directory_skips_missing_roots_without_raising(tmp_path) -> None:
    """3a 终审补：某个根在启动前被删掉时只当它不覆盖 cwd，结构化判定照常进行，不抛未处理异常。"""
    from agent_py_agent.agent.tooling.sandbox import allowlisted_directory

    cwd = tmp_path / "work"
    cwd.mkdir()
    missing = tmp_path / "gone"
    assert allowlisted_directory(cwd, (missing, tmp_path)) is True
    assert allowlisted_directory(cwd, (missing,)) is False
    assert allowlisted_directory(tmp_path / "nope", (tmp_path,)) is False


# LLM: 在合成规范Owner树运行真实构参，缺persona时不得请求bwrap在宿主可写父目录建立tmpfs挂载点。
# 函数用途: 验证缺失保护目录只try-bind，并且构参两次不创建宿主路径或改变权限。
def test_missing_persona_uses_try_bind_without_host_mountpoint_creation(tmp_path):
    root = tmp_path.resolve() / ".my-agent"
    home = root / "owners/local/main"
    other = root / "owners/providers/feishu/users/other"
    home.mkdir(parents=True, mode=0o700)
    other.mkdir(parents=True, mode=0o700)
    spec = SandboxSpec(owner_home=home, workspace=home, protected_persona_root=home,
                       full_access=True, bwrap_path="/fake/bwrap")
    before = {path.relative_to(root): path.lstat().st_mode for path in root.rglob("*")}
    first = build_bwrap_argv(spec)
    second = build_bwrap_argv(spec)
    assert first == second
    assert {path.relative_to(root): path.lstat().st_mode for path in root.rglob("*")} == before
    for owner in (home, other):
        path = str(owner / "persona")
        assert ["--tmpfs", path] not in [first[index:index + 2] for index in range(len(first))]
        assert ["--ro-bind-try", path, path] in [first[index:index + 3] for index in range(len(first))]
        assert not (owner / "persona").exists()


# LLM: 真Linux/bwrap执行只运行/bin/true，观察宿主树的路径和mode；平台或readiness缺失明确跳过。
# 函数用途: 防止bwrap创建挂载点这类argv构参阶段看不出的宿主副作用。
def test_linux_missing_persona_command_keeps_host_paths_and_modes(tmp_path):
    import sys

    if not sys.platform.startswith("linux"):
        pytest.skip("需要Linux bwrap观察点")
    readiness = probe_sandbox()
    if not readiness.ready:
        pytest.skip(f"bwrap未就绪：{readiness.code}")
    home = tmp_path.resolve() / ".my-agent/owners/local/main"
    home.mkdir(parents=True, mode=0o700)
    before = {path.relative_to(tmp_path): path.lstat().st_mode for path in tmp_path.rglob("*")}
    spec = SandboxSpec(owner_home=home, workspace=home, protected_persona_root=home,
                       full_access=True, bwrap_path=readiness.bwrap_path)
    result = subprocess.run([*build_bwrap_argv(spec), "--", "/bin/true"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert {path.relative_to(tmp_path): path.lstat().st_mode for path in tmp_path.rglob("*")} == before
