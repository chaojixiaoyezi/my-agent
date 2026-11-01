"""F2：Linux 宿主独占普通文件由 bwrap 在 --chdir 前逐个精确遮罩；遮罩缺失时，真实启动链必须在任何业务进程前拒绝。

只用临时合成来源。就绪探针与进程边界使用替身，配置解析、脚本注册、
ScriptRunner 和 bwrap 参数编译保持真实；这些测试不冒充 Linux 内核挂载验收。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.attempt import sandbox as attempt
from agent_py_agent.agent.path_access_policy import configured_host_source_paths
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.tooling import sandbox as tooling
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.workflows import script_runner

_REJECTION = "SANDBOX_UNAVAILABLE: HOST_EXCLUSIVE_FILE_ISOLATION_UNAVAILABLE"
# 单文件遮罩的前两个参数；第三个参数是被遮罩的 namespace 位置。
_MASK = ["--ro-bind", tooling.HIDDEN_FILE_MASK_SOURCE]


@pytest.fixture
def linux_boundary(monkeypatch):
    # 保证测试的是当前工作树，而不是解释器的 editable 安装所指向的旧树。
    assert Path(attempt.__file__).resolve() == (
        Path(__file__).resolve().parents[1] / "agent" / "attempt" / "sandbox.py"
    )
    monkeypatch.setattr(attempt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(attempt.AttemptExecutionSandbox, "_READINESS_CACHE", {})
    readiness = tooling.SandboxReadiness(True, "FIXTURE_READY", "synthetic readiness")
    probe = Mock(return_value=readiness)
    monkeypatch.setattr(attempt.AttemptExecutionSandbox, "probe", probe)
    monkeypatch.setattr(attempt, "_network_checked", lambda report, *_: report)
    monkeypatch.setattr(tooling, "find_bwrap", lambda: "/fixture/bwrap")
    monkeypatch.setattr(tooling, "_PROC_MOUNT_KIND", "dir")
    monkeypatch.setattr(script_runner, "gateway_bound_ports", lambda: ())
    execute = Mock(return_value=(0, "{}", "", "", {}))
    process = Mock(side_effect=AssertionError("business process must not start"))
    monkeypatch.setattr(script_runner, "_execute", execute)
    monkeypatch.setattr(attempt, "run_sandbox_process", process)
    popen = Mock(side_effect=AssertionError("Popen must not be reached"))
    monkeypatch.setattr(attempt.subprocess, "Popen", popen)
    return probe, execute, process, popen


def _spec(tmp_path, **options):
    owner = tmp_path.resolve() / "owner"
    workspace = owner / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return attempt.AttemptSandboxSpec(
        attempt_view=workspace, staging_root=workspace, shared_workspace=owner,
        owner_home=owner, bwrap_path="/fixture/bwrap", **options,
    )


def _assert_no_start(boundary):
    for call in boundary[1:]:
        call.assert_not_called()


# 函数用途: 只放行同步 Attempt 的进程边界替身并返回成功结果；脚本 runner、Popen 替身仍禁止启动。
def _allow_attempt_start(boundary):
    process = boundary[2]
    process.side_effect = None
    process.return_value = subprocess.CompletedProcess([], 0, "", "")
    return process


# 函数用途: 断言同一启动只到达一次进程边界，并返回交给该边界的完整 argv。
def _single_launch(boundary, started):
    started.assert_called_once()
    for call in boundary[1:]:
        if call is not started:
            call.assert_not_called()
    return started.call_args.args[0]


# 函数用途: 建一个与宿主来源同前缀的普通业务文件，用来证明遮罩按精确路径而不是字符串前缀。
def _sibling_prefix(source: Path) -> Path:
    sibling = source.with_name(source.name + "-sibling")
    sibling.write_text("ordinary business sibling")
    return sibling


# 函数用途: 断言遮罩恰为给定位置、全部紧贴 --chdir 之前（其后无任何挂载），且未遮罩列出的同前缀业务文件。
def _assert_final_masks(argv, locations, *unmasked):
    assert argv.count("--chdir") == 1, argv
    end = argv.index("--chdir")
    masked = []
    while end >= 3 and argv[end - 3:end - 1] == _MASK:
        masked.append(argv[end - 1])
        end -= 3
    # 末尾遮罩块之外不能再有 /dev/null 遮罩：每个遮罩都排在全部其它挂载之后。
    assert sum(argv[index:index + 2] == _MASK for index in range(len(argv) - 1)) == len(masked), argv
    assert sorted(masked) == sorted({str(location) for location in locations}), argv
    for path in unmasked:
        assert str(path) not in masked, argv


@pytest.mark.parametrize("interpreter", ["bash", "sh", "python3"])
def test_registered_script_launcher_masks_configured_file_sources_last(
    tmp_path, monkeypatch, linux_boundary, interpreter,
):
    owner = tmp_path.resolve() / "owner"
    owner.mkdir()
    source = owner / "host-source.data"
    source.write_text("synthetic-not-a-credential", encoding="utf-8")
    sibling = _sibling_prefix(source)
    business = owner / "business.txt"
    business.write_text("business input", encoding="utf-8")
    config_file = owner / "deployment.yaml"
    config_file.write_text(f"feishu_app_secret: {json.dumps('file:' + str(source))}\n")
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "data-home"))
    config = load_config(config_file)
    hidden = configured_host_source_paths(config)
    assert source in hidden and config_file in hidden
    assert owner not in hidden and business not in hidden and sibling not in hidden
    tool = ScriptsTool(owner, runner_options_provider=lambda: {
        "strict_sandbox": True, "host_exclusive_paths": hidden,
    })
    code = "print('{}')" if interpreter == "python3" else "printf '{}\\n'"
    registered = tool.execute({
        "action": "register", "script_id": "isolation", "code": code,
        "interpreter": interpreter,
        "descriptor": {"name": "isolation", "access_targets": [], "data_sources": [],
                       "credential_refs": [], "io_contract": {"inputs": [], "outputs": []}},
    })
    assert registered.ok, registered.output
    public = tool.execute({"action": "run", "script_id": "isolation"})
    assert public.ok, public.output
    assert linux_boundary[0].call_count == 1
    argv = _single_launch(linux_boundary, linux_boundary[1])
    # 部署配置本身与外部 file: 凭据都在 owner bind 里可见，两者都由最终单文件遮罩隐藏，同级业务文件不受影响。
    _assert_final_masks(argv, (source, config_file), sibling)
    assert str(business) not in argv
    assert source.read_text() == "synthetic-not-a-credential"
    assert business.read_text() == "business input"


@pytest.mark.parametrize("interpreter", ["python3", "bash", "sh"])
@pytest.mark.parametrize("full_access", [False, True])
def test_shared_attempt_masks_file_after_successful_readiness(
    tmp_path, linux_boundary, interpreter, full_access,
):
    spec = _spec(tmp_path, full_access=full_access)
    source = spec.owner_home / "host-source.data"
    source.write_text("synthetic source")
    sibling = _sibling_prefix(source)
    sandbox = attempt.AttemptExecutionSandbox(replace(spec, hidden_paths=(source,)))
    assert sandbox.require_ready().ready
    command = [interpreter, "fixture-script"]
    built = sandbox.build_argv(command)
    process = _allow_attempt_start(linux_boundary)
    assert sandbox.run(command).returncode == 0
    launched = _single_launch(linux_boundary, process)
    # build_argv 与同步 run 两个入口编译同一份带遮罩的启动参数。
    assert launched == built and launched[-len(command):] == command
    _assert_final_masks(launched, (source,), sibling)
    assert linux_boundary[0].call_count == 1


@pytest.mark.parametrize("mode", ["owner", "full", "hide_home", "allowlist", "private"])
def test_file_covered_by_effective_hidden_directory_is_allowed(tmp_path, linux_boundary, mode):
    spec = _spec(tmp_path)
    directory = spec.owner_home / "host-only"
    directory.mkdir()
    source = directory / "source.data"
    source.write_text("synthetic source")
    options = {"hidden_paths": (directory, source)}
    if mode == "full":
        options["full_access"] = True
    elif mode in {"hide_home", "allowlist", "private"}:
        options.update(read_only_root=True, implicit_attempt_write_roots=False)
        if mode == "allowlist":
            system = tmp_path.resolve() / "system"
            system.mkdir()
            options.update(read_mode="allowlist", system_read_roots=(system,),
                           public_read_roots=(spec.attempt_view,))
        elif mode == "private":
            options.update(hidden_paths=(source,), private_read_roots=(directory,))
        else:
            options["read_mode"] = "hide_home"
    command = ["sh", "fixture-script"]
    argv = attempt.AttemptExecutionSandbox(replace(spec, **options)).build_argv(command)
    assert argv[-len(command):] == command
    assert ["--tmpfs", str(directory)] in [argv[i:i + 2] for i in range(len(argv) - 1)]
    _assert_no_start(linux_boundary)


@pytest.mark.parametrize("reexpose", ["workspace", "public", "write", "readonly"])
def test_hide_home_directory_reexposed_after_tmpfs_gets_final_file_mask(
    tmp_path, linux_boundary, reexpose,
):
    spec = _spec(tmp_path)
    directory = spec.owner_home / "host-only"
    directory.mkdir()
    source = directory / "source.data"
    source.write_text("synthetic source")
    sibling = _sibling_prefix(source)
    options = {"read_only_root": True, "read_mode": "hide_home",
               "implicit_attempt_write_roots": False, "hidden_paths": (directory, source)}
    if reexpose == "workspace":
        options.update(attempt_view=directory, staging_root=directory)
    elif reexpose == "public":
        options["public_read_roots"] = (directory,)
    elif reexpose == "write":
        options["extra_write_roots"] = (directory,)
    else:
        options["protected_write_paths"] = (source,)
    process = _allow_attempt_start(linux_boundary)
    assert attempt.AttemptExecutionSandbox(replace(spec, **options)).run(["sh", "fixture"]).returncode == 0
    argv = _single_launch(linux_boundary, process)
    # 业务重挂压过了目录 tmpfs；重新暴露的同一位置由排在其后的精确遮罩隐藏，同级业务文件仍可见。
    assert ["--tmpfs", str(directory)] in [argv[i:i + 2] for i in range(len(argv) - 1)]
    _assert_final_masks(argv, (source,), sibling)


@pytest.mark.parametrize("kind", ["sibling-prefix", "file-alias-outside", "file-alias-inside", "directory-alias"])
def test_hidden_file_mask_uses_canonical_source_not_lexical_prefix(tmp_path, linux_boundary, kind):
    spec = _spec(tmp_path)
    directory = spec.owner_home / "host-only"
    directory.mkdir()
    outside = spec.owner_home / "host-only-sibling"
    outside.mkdir()
    source = (directory if kind in {"file-alias-inside", "directory-alias"} else outside) / "source.data"
    source.write_text("synthetic source")
    sibling = _sibling_prefix(source)
    hidden = (directory, source)
    alias = None
    if kind == "file-alias-outside":
        alias = directory / "alias.data"
        alias.symlink_to(source)
        hidden = (directory, alias)
    elif kind == "file-alias-inside":
        alias = outside / "alias.data"
        alias.symlink_to(source)
        hidden = (directory, alias)
    elif kind == "directory-alias":
        alias = spec.owner_home / "directory-alias"
        alias.symlink_to(directory, target_is_directory=True)
        hidden = (alias, source)
    sandbox = attempt.AttemptExecutionSandbox(replace(spec, hidden_paths=hidden))
    process = _allow_attempt_start(linux_boundary)
    assert sandbox.run(["sh", "fixture"]).returncode == 0
    argv = _single_launch(linux_boundary, process)
    assert argv[-2:] == ["sh", "fixture"]
    assert ["--tmpfs", str(directory)] in [argv[i:i + 2] for i in range(len(argv) - 1)]
    # 只有 canonical 来源落在隐藏目录之外（兄弟前缀目录、或别名指向目录外）时才需要单文件遮罩；
    # 目录内的 canonical 来源已由最终 tmpfs 隐藏，别名入口本身既不是来源也不被遮罩。
    exposed = kind in {"sibling-prefix", "file-alias-outside"}
    _assert_final_masks(argv, (source,) if exposed else (), sibling, *((alias,) if alias is not None else ()))


def test_owner_alias_bind_location_of_canonically_hidden_file_is_masked(tmp_path, linux_boundary):
    spec = _spec(tmp_path)
    directory = spec.owner_home / "host-only"
    directory.mkdir()
    source = directory / "source.data"
    source.write_text("synthetic source")
    sibling = _sibling_prefix(source)
    alias = tmp_path.resolve() / "owner-alias"
    alias.symlink_to(spec.owner_home, target_is_directory=True)
    sandbox = attempt.AttemptExecutionSandbox(replace(
        spec, owner_home=alias, shared_workspace=alias,
        attempt_view=alias / "workspace", staging_root=alias / "workspace",
        hidden_paths=(directory, source),
    ))
    process = _allow_attempt_start(linux_boundary)
    assert sandbox.run(["sh", "fixture"]).returncode == 0
    argv = _single_launch(linux_boundary, process)
    # canonical 名称在隐藏目录 tmpfs 之下；owner 别名 bind 把同一宿主文件再次暴露，必须在该别名位置精确遮罩。
    assert ["--tmpfs", str(directory)] in [argv[i:i + 2] for i in range(len(argv) - 1)]
    via_alias = alias / directory.name / source.name
    _assert_final_masks(argv, (via_alias,), sibling, alias / directory.name / sibling.name)


@pytest.mark.parametrize("full_access", [False, True])
@pytest.mark.parametrize("entry", ["build_argv", "run"])
def test_missing_final_file_mask_fails_closed_before_any_process(
    tmp_path, monkeypatch, linux_boundary, full_access, entry,
):
    spec = _spec(tmp_path, full_access=full_access)
    source = spec.owner_home / "host-source.data"
    source.write_text("synthetic source")
    # 编译器若漏发遮罩，启动校验必须独立失败关闭，而不是信任编译器。
    monkeypatch.setattr(tooling, "_hidden_file_masks", lambda _argv, _paths: [])
    sandbox = attempt.AttemptExecutionSandbox(replace(spec, hidden_paths=(source,)))
    assert sandbox.require_ready().ready
    with pytest.raises(attempt.SandboxUnavailableError) as rejected:
        getattr(sandbox, entry)(["sh", "fixture-script"])
    assert str(rejected.value) == _REJECTION
    assert isinstance(rejected.value, tooling.SandboxUnavailable)
    _assert_no_start(linux_boundary)


@pytest.mark.parametrize("full_access", [False, True])
def test_business_files_and_directory_only_masks_keep_existing_argv(tmp_path, linux_boundary, full_access):
    spec = _spec(tmp_path, full_access=full_access, network_access=False)
    business = spec.owner_home / "business.txt"
    business.write_text("ordinary business")
    directory = spec.owner_home / "hidden-store"
    directory.mkdir()
    (directory / "data").write_text("synthetic store")
    command = ["sh", "-c", f"cat {business}"]
    baseline = attempt.AttemptExecutionSandbox(spec).build_argv(command)
    argv = attempt.AttemptExecutionSandbox(replace(spec, hidden_paths=(directory,))).build_argv(command)
    mask = ["--tmpfs", str(directory), "--remount-ro", str(directory)]
    index = argv.index("--tmpfs")
    assert argv[index:index + 4] == mask
    assert argv[:index] + argv[index + 4:] == baseline
    assert "--unshare-net" in argv and "--share-net" not in argv
    assert business.read_text() == "ordinary business"
    _assert_no_start(linux_boundary)
