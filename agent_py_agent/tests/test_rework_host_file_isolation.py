"""F2：Linux 不支持精确文件遮蔽时，真实启动链必须在任何业务进程前拒绝。

只用临时合成来源。就绪探针与进程边界使用替身，配置解析、脚本注册、
ScriptRunner 和 bwrap 参数编译保持真实；这些测试不冒充 Linux 内核挂载验收。
"""

from __future__ import annotations

import json
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


@pytest.mark.parametrize("interpreter", ["bash", "sh", "python3"])
def test_configured_file_source_rejected_by_registered_script_launcher(
    tmp_path, monkeypatch, linux_boundary, interpreter,
):
    owner = tmp_path.resolve() / "owner"
    owner.mkdir()
    source = owner / "host-source.data"
    source.write_text("synthetic-not-a-credential", encoding="utf-8")
    business = owner / "business.txt"
    business.write_text("business input", encoding="utf-8")
    config_file = owner / "deployment.yaml"
    config_file.write_text(f"feishu_app_secret: {json.dumps('file:' + str(source))}\n")
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "data-home"))
    config = load_config(config_file)
    hidden = configured_host_source_paths(config)
    assert source in hidden and owner not in hidden and business not in hidden
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
    # 原注册入口的领域异常必须保留具体子类，不能被当成普通脚本失败吞掉。
    with pytest.raises(attempt.SandboxUnavailableError) as rejected:
        tool._dispatch({"action": "run", "script_id": "isolation"})
    assert str(rejected.value) == _REJECTION
    public = tool.execute({"action": "run", "script_id": "isolation"})
    assert not public.ok and _REJECTION in public.output
    assert linux_boundary[0].call_count == 1  # 第二次启动复用成功 readiness，仍须拒绝。
    _assert_no_start(linux_boundary)
    assert source.read_text() == "synthetic-not-a-credential"
    assert business.read_text() == "business input"


@pytest.mark.parametrize("interpreter", ["python3", "bash", "sh"])
@pytest.mark.parametrize("full_access", [False, True])
@pytest.mark.parametrize("entry", ["build_argv", "run"])
def test_shared_attempt_rejects_file_after_successful_readiness(
    tmp_path, linux_boundary, interpreter, full_access, entry,
):
    spec = _spec(tmp_path, full_access=full_access)
    source = spec.owner_home / "host-source.data"
    source.write_text("synthetic source")
    sandbox = attempt.AttemptExecutionSandbox(replace(spec, hidden_paths=(source,)))
    assert sandbox.require_ready().ready
    with pytest.raises(attempt.SandboxUnavailableError) as rejected:
        getattr(sandbox, entry)([interpreter, "fixture-script"])
    assert str(rejected.value) == _REJECTION
    assert isinstance(rejected.value, tooling.SandboxUnavailable)
    assert linux_boundary[0].call_count == 1
    _assert_no_start(linux_boundary)


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
def test_hide_home_directory_reexposed_after_mask_does_not_protect_file(
    tmp_path, linux_boundary, reexpose,
):
    spec = _spec(tmp_path)
    directory = spec.owner_home / "host-only"
    directory.mkdir()
    source = directory / "source.data"
    source.write_text("synthetic source")
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
    with pytest.raises(attempt.SandboxUnavailableError) as rejected:
        attempt.AttemptExecutionSandbox(replace(spec, **options)).build_argv(["sh", "fixture"])
    assert str(rejected.value) == _REJECTION
    _assert_no_start(linux_boundary)


@pytest.mark.parametrize("kind", ["sibling-prefix", "file-alias-outside", "file-alias-inside", "directory-alias"])
def test_hidden_file_uses_canonical_source_not_lexical_prefix(tmp_path, linux_boundary, kind):
    spec = _spec(tmp_path)
    directory = spec.owner_home / "host-only"
    directory.mkdir()
    outside = spec.owner_home / "host-only-sibling"
    outside.mkdir()
    source = (directory if kind in {"file-alias-inside", "directory-alias"} else outside) / "source.data"
    source.write_text("synthetic source")
    hidden = (directory, source)
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
    if kind in {"sibling-prefix", "file-alias-outside"}:
        with pytest.raises(attempt.SandboxUnavailableError) as rejected:
            sandbox.build_argv(["sh", "fixture"])
        assert str(rejected.value) == _REJECTION
    else:
        assert sandbox.build_argv(["sh", "fixture"])[-2:] == ["sh", "fixture"]
    _assert_no_start(linux_boundary)


def test_owner_alias_bind_cannot_reexpose_a_canonically_hidden_file(tmp_path, linux_boundary):
    spec = _spec(tmp_path)
    directory = spec.owner_home / "host-only"
    directory.mkdir()
    source = directory / "source.data"
    source.write_text("synthetic source")
    alias = tmp_path.resolve() / "owner-alias"
    alias.symlink_to(spec.owner_home, target_is_directory=True)
    sandbox = attempt.AttemptExecutionSandbox(replace(
        spec, owner_home=alias, shared_workspace=alias,
        attempt_view=alias / "workspace", staging_root=alias / "workspace",
        hidden_paths=(directory, source),
    ))
    with pytest.raises(attempt.SandboxUnavailableError) as rejected:
        sandbox.build_argv(["sh", "fixture"])
    assert str(rejected.value) == _REJECTION
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
