"""R1 / OPUS-R4-P0-01：宿主独占来源集合（config_path 与外部 file: 来源）在 Linux 以精确单文件遮罩隐藏，命令链路接线同一集合。

真实 load_config、SimpleAgent 装配、脚本/工作流注册与 ScriptRunner、Shell 装配、插件规格工厂和 bwrap 参数编译；
Linux 编译边界只替换平台判定、就绪探针与进程启动点，bwrap 从不执行。真实 Seatbelt / bwrap 车道按平台与就绪跳过。
来源全是临时合成文件，不含真实凭据。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent import plugin_sandbox
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.attempt import sandbox as attempt
from agent_py_agent.agent.common.config_paths import DEFAULT_CONFIG
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.tooling import sandbox as tooling
from agent_py_agent.agent.tooling import shell as shell_module
from agent_py_agent.agent.workflows import script_runner

# 单文件遮罩的前两个参数；第三个参数是被遮罩的 namespace 位置。
_MASK = ["--ro-bind", tooling.HIDDEN_FILE_MASK_SOURCE]
_SECRET = "synthetic-host-only-value"
_SIBLING = "ordinary business sibling"
_INTERPRETERS = ("python3", "bash", "sh")


@pytest.fixture
def linux_boundary(monkeypatch):
    # 保证测试的是当前工作树，而不是解释器的 editable 安装所指向的旧树。
    tree = Path(__file__).resolve().parents[1]
    for module in (attempt, tooling, shell_module, script_runner, plugin_sandbox):
        assert Path(module.__file__).resolve().is_relative_to(tree)
    monkeypatch.setattr(attempt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(attempt.AttemptExecutionSandbox, "_READINESS_CACHE", {})
    probe = Mock(return_value=tooling.SandboxReadiness(True, "FIXTURE_READY", "synthetic readiness"))
    monkeypatch.setattr(attempt.AttemptExecutionSandbox, "probe", probe)
    monkeypatch.setattr(attempt, "_network_checked", lambda report, *_: report)
    monkeypatch.setattr(tooling, "find_bwrap", lambda: "/fixture/bwrap")
    monkeypatch.setattr(tooling, "_PROC_MOUNT_KIND", "dir")
    monkeypatch.setattr(attempt, "gateway_bound_ports", lambda: ())
    monkeypatch.setattr(script_runner, "gateway_bound_ports", lambda: ())
    # 模拟的 Linux 宿主没有外层 macOS Seatbelt，脚本必须经 bwrap 包装后才到进程边界。
    monkeypatch.setattr(script_runner, "_outer_seatbelt_active", lambda: False)
    execute = Mock(return_value=(0, "{}", "", "", {}))
    monkeypatch.setattr(script_runner, "_execute", execute)
    return SimpleNamespace(probe=probe, execute=execute)


# 函数用途: 模拟系统级安装或 /etc 部署目录：把装有配置的目录作为宿主只读系统根挂进沙箱，使其中的配置文件对命令可见。
def _expose_read_only_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(tooling, "_SYSTEM_RO_ROOTS", (*tooling._SYSTEM_RO_ROOTS, str(root)))


# 函数用途: 返回紧贴 --chdir 之前的遮罩块目标，并断言块外没有任何遮罩（每个遮罩都排在全部其它挂载之后）。
def _final_masks(argv: list[str]) -> list[str]:
    assert argv.count("--chdir") == 1, argv
    end = argv.index("--chdir")
    masked = []
    while end >= 3 and argv[end - 3:end - 1] == _MASK:
        masked.append(argv[end - 1])
        end -= 3
    assert sum(argv[index:index + 2] == _MASK for index in range(len(argv) - 1)) == len(masked), argv
    return masked


# 函数用途: 断言恰好一次业务进程启动并返回交给进程边界的完整 argv。
def _single_business_start(boundary) -> list[str]:
    boundary.execute.assert_called_once()
    return boundary.execute.call_args.args[0]


# 函数用途: 断言配置文件遮罩是 --chdir 之前的最后一条挂载。
def _assert_config_masked_last(argv: list[str], config_path: Path) -> None:
    assert str(config_path) in _final_masks(argv)
    chdir = argv.index("--chdir")
    assert argv[chdir - 3:chdir] == [*_MASK, str(config_path)], argv


# 函数用途: 经原 scripts 工具注册并运行一个只打印空对象的脚本。
def _run_registered_script(agent: SimpleAgent, script_id: str, interpreter: str):
    tool = agent.tools.tools["scripts"]
    code = "print('{}')" if interpreter == "python3" else "printf '{}\\n'"
    registered = tool.execute({
        "action": "register", "script_id": script_id, "code": code, "interpreter": interpreter,
        "descriptor": {"name": script_id, "access_targets": [], "data_sources": [],
                       "credential_refs": [], "io_contract": {"inputs": [], "outputs": []}},
    })
    assert registered.ok, registered.output
    return tool.execute({"action": "run", "script_id": script_id})


# 函数用途: 按用例选择打包默认配置或 MY_AGENT_HOME/config 之外的部署配置，并让其所在目录对命令可见。
def _config_for(placement: str, tmp_path: Path, monkeypatch):
    if placement == "packaged-default":
        config = load_config(DEFAULT_CONFIG)
        assert Path(config.config_path) == DEFAULT_CONFIG.resolve()
        # 系统级安装（如 dist-packages 下的包）：已安装的包目录是宿主只读根。
        _expose_read_only_root(monkeypatch, DEFAULT_CONFIG.resolve().parents[1])
        return config
    etc = tmp_path.resolve() / "etc"
    etc.mkdir()
    config_file = etc / "agent.yaml"
    config_file.write_bytes(DEFAULT_CONFIG.read_bytes())
    _expose_read_only_root(monkeypatch, etc)
    config = load_config(config_file)
    assert Path(config.config_path) == config_file
    return config


@pytest.mark.parametrize("interpreter", _INTERPRETERS)
@pytest.mark.parametrize("placement", ["packaged-default", "outside-home-config"])
def test_script_launch_succeeds_with_config_path_masked_last(tmp_path, monkeypatch, linux_boundary, placement, interpreter):
    config = _config_for(placement, tmp_path, monkeypatch)
    config_path = Path(config.config_path).resolve()
    project = tmp_path / "project"
    project.mkdir()
    agent = SimpleAgent(config, project)
    try:
        assert not config_path.is_relative_to(Path(agent.home_paths.root) / "config")
        result = _run_registered_script(agent, f"mask-{interpreter}", interpreter)
    finally:
        agent.tools.close_mcp_clients()
    assert result.ok, result.output
    _assert_config_masked_last(_single_business_start(linux_boundary), config_path)


@pytest.mark.parametrize("interpreter", _INTERPRETERS)
def test_packaged_default_config_outside_every_script_mount_does_not_block_launch(tmp_path, linux_boundary, interpreter):
    config = load_config(DEFAULT_CONFIG)
    project = tmp_path / "project"
    project.mkdir()
    agent = SimpleAgent(config, project)
    try:
        result = _run_registered_script(agent, f"plain-{interpreter}", interpreter)
    finally:
        agent.tools.close_mcp_clients()
    # 源码检出里的打包配置通常不在任何脚本挂载之下：不可见位置无需遮罩，启动不再失败关闭；若检出恰被挂载则只遮罩它。
    assert result.ok, result.output
    assert set(_final_masks(_single_business_start(linux_boundary))) <= {str(DEFAULT_CONFIG.resolve())}


def test_workflow_script_step_launches_with_config_path_masked(tmp_path, monkeypatch, linux_boundary):
    config = _config_for("packaged-default", tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    agent = SimpleAgent(config, project)
    try:
        scripts = agent.tools.tools["scripts"]
        registered = scripts.execute({
            "action": "register", "script_id": "mask-step", "code": "print('{}')", "interpreter": "python3",
            "descriptor": {"name": "mask-step", "access_targets": [], "data_sources": [],
                           "credential_refs": [], "io_contract": {"inputs": [], "outputs": []}},
        })
        assert registered.ok, registered.output
        workflows = agent.tools.tools["workflows"]
        definition = {"descriptor": AssetDescriptor("mask-flow", "workflow", "mask-flow").to_dict(),
                      "steps": [{"step_id": "collect", "name": "collect", "mode": "deterministic_script",
                                 "script_id": "mask-step", "on_error": "fail"}]}
        saved = workflows.execute({"action": "save", "workflow_id": "mask-flow", "definition": definition})
        assert saved.ok, saved.output
        outcome = workflows.execute({"action": "run", "workflow_id": "mask-flow"})
    finally:
        agent.tools.close_mcp_clients()
    assert outcome.ok, outcome.output
    assert json.loads(outcome.output)["status"] == "succeeded", outcome.output
    _assert_config_masked_last(_single_business_start(linux_boundary), Path(config.config_path).resolve())


# 函数用途: 建一个 SimpleAgent，其部署配置的 feishu_app_secret 指向合成 file: 来源；命令经结构化读根看得到该部署目录。
def _shell_case(tmp_path: Path) -> SimpleNamespace:
    deploy = tmp_path.resolve() / "deploy"
    deploy.mkdir()
    secret = deploy / "secret.txt"
    secret.write_text(_SECRET)
    sibling = deploy / "secret.txt-sibling"
    sibling.write_text(_SIBLING)
    config_file = deploy / "agent.yaml"
    config_file.write_text(f"feishu_app_secret: {json.dumps('file:' + str(secret))}\n")
    project = tmp_path / "project"
    project.mkdir()
    agent = SimpleAgent(load_config(config_file), project)
    shell = agent.tools.tools["run_command"]
    # 原 registry 装配把同一来源集合交给 Shell；后台任务与 PTY 会话沿用这个 ShellTool 实例。
    assert {secret, config_file} <= set(shell.host_exclusive_paths)
    owner = Path(agent.home_paths.owner_home_dir).resolve()
    # 工作目录留在 owner 自己的范围；部署目录作为宿主下发的结构化读根进入本条命令的视图。
    params = {"working_dir": str(owner), "__sandbox_read_roots": [str(deploy)]}
    return SimpleNamespace(agent=agent, shell=shell, params=params, secret=secret, sibling=sibling,
                           config_file=config_file)


def test_shell_command_argv_masks_configured_source_and_config_path(tmp_path, monkeypatch, linux_boundary):
    case = _shell_case(tmp_path)
    launches = []

    # 进程边界替身：记录交给前台启动点的完整 argv，只起一个空 Python 进程代替 bwrap。
    def start(exec_arg, use_shell, target, tool):
        launches.append(list(exec_arg))
        return subprocess.Popen([sys.executable, "-c", ""], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)

    monkeypatch.setattr(shell_module, "_start_attempt_shell_process", start)
    try:
        result = case.shell.execute({"command": f"cat {case.secret}", **case.params})
    finally:
        case.agent.tools.close_mcp_clients()
    assert result.ok, result.output
    (argv,) = launches
    masks = _final_masks(argv)
    assert {str(case.secret), str(case.config_file)} <= set(masks), argv
    assert str(case.sibling) not in masks


def test_darwin_shell_command_cannot_read_configured_host_source(tmp_path):
    if sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
        pytest.skip("需要真实 macOS Seatbelt")
    workspace = tmp_path.resolve() / "probe"
    workspace.mkdir()
    readiness = attempt.AttemptExecutionSandbox(attempt.AttemptSandboxSpec(
        attempt_view=workspace, staging_root=workspace, shared_workspace=workspace, owner_home=workspace)).probe()
    if not readiness.ready:
        pytest.skip(f"Seatbelt 未就绪：{readiness.code}")
    case = _shell_case(tmp_path)
    try:
        control = case.shell.execute({"command": f"cat {case.sibling}", **case.params})
        result = case.shell.execute({"command": f"cat {case.secret}", **case.params})
    finally:
        case.agent.tools.close_mcp_clients()
    assert control.ok and _SIBLING in control.output, control.output
    assert not result.ok, result.output
    assert _SECRET not in result.output


def test_plugin_preflight_accepts_and_launch_masks_external_file_source(tmp_path, linux_boundary):
    owner_home = tmp_path.resolve() / "home" / "owners" / "local" / "main"
    (owner_home / "config").mkdir(parents=True)
    source = tmp_path.resolve() / "external" / "secret.txt"
    source.parent.mkdir()
    source.write_text(_SECRET)
    sibling = source.with_name("secret.txt-sibling")
    sibling.write_text(_SIBLING)
    assert plugin_sandbox.plugin_sandbox_problem(True, owner_home, host_exclusive_roots=(source,)) == ""
    spec = plugin_sandbox.plugin_sandbox_spec(cwd=owner_home, data_dir=owner_home, owner_home=owner_home,
                                              host_exclusive_roots=(source,))
    command = plugin_sandbox.sandboxed_plugin_argv(["/usr/bin/true"], spec)
    chdir = command.index("--chdir")
    overlay = ["--tmpfs", str(owner_home / "config"), "--remount-ro", str(owner_home / "config")]
    # 单文件遮罩之后才是插件包装补的目录覆盖，最后是 --chdir。
    assert command[chdir - 4:chdir] == overlay, command
    assert command[chdir - 7:chdir - 4] == [*_MASK, str(source)], command
    assert str(sibling) not in command
    assert command[command.index("--") + 1:] == ["/usr/bin/true"]


def test_real_bwrap_hidden_file_under_bound_directory_reads_back_empty(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("需要 Linux bwrap")
    readiness = tooling.probe_sandbox()
    if not readiness.ready:
        pytest.skip(f"bwrap 未就绪：{readiness.code}")
    owner = tmp_path.resolve() / "owner"
    bound, workspace = owner / "bound", owner / "workspace"
    bound.mkdir(parents=True)
    workspace.mkdir()
    secret = bound / "secret.txt"
    secret.write_text(_SECRET)
    sibling = bound / "secret.txt-sibling"
    sibling.write_text(_SIBLING)
    sandbox = attempt.AttemptExecutionSandbox(attempt.AttemptSandboxSpec(
        attempt_view=workspace, staging_root=workspace, shared_workspace=owner, owner_home=owner,
        bwrap_path=readiness.bwrap_path, hidden_paths=(secret,)))
    # 遮罩位置无论读成空还是拒绝打开都不能交出任何字节；同级业务文件照常可读。
    script = 'printf "[%s]" "$(cat "$1" 2>/dev/null)"; printf "[%s]" "$(cat "$2")"'
    argv = sandbox.build_argv(["/bin/sh", "-c", script, "sh", str(secret), str(sibling)])
    assert _final_masks(argv) == [str(secret)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"[][{_SIBLING}]"
    assert _SECRET not in result.stdout + result.stderr
    assert secret.read_text() == _SECRET and sibling.read_text() == _SIBLING
