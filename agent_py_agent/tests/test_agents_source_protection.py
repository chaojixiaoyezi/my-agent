"""任意文件名来源的业务写拒绝：临时home、原字节核对、正常业务写入不受伤。"""
import json
import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from agent_py_agent.agent.tooling._persona_write_guard import _persona_runtime_redirect_error
from agent_py_agent.agent.tooling.sandbox import SandboxSpec, build_bwrap_argv
from agent_py_agent.agent.workspace_write_context import WorkspaceWriteContext


@pytest.fixture
def protected(tmp_path, monkeypatch):
    root = (tmp_path / "data-root").resolve()
    home = root / "owners/local/main"
    source = home / "docs/rules.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\xef\xbb\xbf# rules\r\noriginal\r\n")
    control = home / "persona/agents_source.json"
    control.parent.mkdir()
    # 夹具只采集自己刚创建的临时路径身份；不调用被测来源加载器，避免混淆fixture与业务拒写断言。
    chain = [(path.stat().st_dev, path.stat().st_ino) for path in (*reversed(source.parents), source)]
    identity = sha256(json.dumps(chain, separators=(",", ":")).encode()).hexdigest()
    record = dict(schema_version=1, owner="local/main", kind="local_path", value=str(source),
                  revision=1, updated_by="local/main", updated_at=1000.0, identity=identity)
    control.write_text(json.dumps(record))
    for name in ("AGENTS.md", "SOUL.md", "USER.md"):
        (home / name).write_text(name)
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    return SimpleNamespace(root=root, home=home, source=source, control=control, record=record,
                           before=source.read_bytes(), config_before=control.read_bytes())


@pytest.mark.parametrize("channel", ["write", "append", "edit", "patch", "delete", "move"])
def test_file_tools_cannot_write_configured_arbitrary_filename(protected, channel):
    p = protected
    access = FileSystemAccessOptions(path_access_mode="full", protected_persona_root=str(p.home))
    if channel in {"write", "append"}:
        tool = WriteFileTool(p.home, [p.home], WriteFileToolOptions(access_options=access))
        result = tool.execute({"path": "docs/rules.bin", "content": "tamper", "mode": "append" if channel == "append" else "overwrite"})
    elif channel == "edit":
        result = EditFileTool(p.home, [p.home], access_options=access).execute(
            {"path": "docs/rules.bin", "old_string": "original", "new_string": "tamper"})
    else:
        body = {"patch": "*** Update File: docs/rules.bin\n@@\n-original\n+tamper\n",
                "delete": "*** Delete File: docs/rules.bin\n",
                "move": "*** Update File: docs/rules.bin\n*** Move to: docs/moved.bin\n@@\n-original\n+tamper\n"}[channel]
        result = ApplyPatchTool(p.home, [p.home], access_options=access).execute(
            {"patch": "*** Begin Patch\n" + body + "*** End Patch"})
    assert not result.ok, (channel, result)
    assert p.source.read_bytes() == p.before
    assert p.control.read_bytes() == p.config_before
    assert not (p.home / "docs/moved.bin").exists()


def test_full_policy_and_sdk_transport_deny_source_and_control_but_allow_business(protected):
    p = protected
    policy = PathAccessPolicy.from_values(mode="full", agent_home_root=p.root)
    context = WorkspaceWriteContext(p.home, (p.home,), (), (), None, (), policy)
    copied = WorkspaceWriteContext.from_payload(context.to_payload())
    for target in (p.source, p.control, p.control.parent, p.home / "persona/new.json", p.source.parent, p.home):
        assert not policy.check_write(target).allowed, target
        assert not copied.check(target).allowed, target
    target = p.home / "docs/output.txt"
    assert policy.check_write(target).allowed
    assert copied.check(target).allowed
    tool = WriteFileTool(p.home, [p.home], WriteFileToolOptions(access_options=FileSystemAccessOptions(
        path_access_mode="full", protected_persona_root=str(p.home))))
    assert tool.execute({"path": "docs/output.txt", "content": "normal"}).ok
    assert target.read_bytes() == b"normal"
    assert p.source.read_bytes() == p.before


def test_source_symlink_alias_cannot_bypass_policy(protected):
    p = protected
    alias = p.home / "friendly-name.txt"
    alias.symlink_to(p.source)
    policy = PathAccessPolicy.from_values(mode="full", agent_home_root=p.root)
    assert not policy.check_write(alias).allowed
    assert p.source.read_bytes() == p.before


def test_runtime_redirect_includes_source_and_persona_control(protected):
    p = protected
    agent = SimpleNamespace(home_paths=SimpleNamespace(
        owner_soul_md=p.home / "SOUL.md", owner_user_md=p.home / "USER.md", owner_agents_md=p.home / "AGENTS.md"))
    for target in ("docs/rules.bin", "persona/agents_source.json"):
        assert _persona_runtime_redirect_error(agent, {"tool": "write_file", "path": target}, workspace_root=p.home)
    assert not _persona_runtime_redirect_error(agent, {"tool": "write_file", "path": "docs/output.txt"}, workspace_root=p.home)


def test_linux_overlay_covers_source_and_control_after_writable_root(protected):
    p = protected
    for extra in ({"full_access": True}, {"read_only_root": True}, {}):
        argv = build_bwrap_argv(SandboxSpec(owner_home=p.home, workspace=p.home, write_roots=(p.home,),
                                           bwrap_path="/fake/bwrap", protected_persona_root=p.home, **extra))
        for target in (p.source, p.control.parent):
            index = next((i for i, flag in enumerate(argv) if flag == "--ro-bind" and argv[i + 1] == str(target)), None)
            assert index is not None, (extra, target, argv)
            assert index > max(i for i, flag in enumerate(argv) if flag == "--bind")


# LLM: 只生成profile，不执行Seatbelt；按原SBPL Unicode字面编码核对路径，不用JSON默认ASCII转义冒充路径。
# 函数用途: 核对来源、控制目录及祖先重命名保护规则。
def test_macos_profile_covers_source_control_and_directory_rename(protected):
    p = protected
    spec = AttemptSandboxSpec(p.home, p.home, p.home, p.home, full_access=True,
                              protected_persona_root=p.home, macos_sandbox_exec="/fake/sandbox-exec")
    argv = AttemptExecutionSandbox(spec)._macos_argv(["/bin/true"])
    profile = argv[argv.index("-p") + 1]
    for target in (p.source, p.control.parent, p.source.parent, p.home):
        assert f"(literal {json.dumps(str(target), ensure_ascii=False)})" in profile, (target, profile)
    assert f"(subpath {json.dumps(str(p.control.parent), ensure_ascii=False)})" in profile


@pytest.mark.parametrize("bad", ["json", "owner", "bool_revision", "bool_schema", "time_int", "dotdot", "symlink", "oversize", "identity"])
def test_bad_source_configuration_cannot_fail_open(protected, bad):
    p = protected
    _damage_source_configuration(p, bad)
    before = p.control.read_bytes()
    policy = PathAccessPolicy.from_values(mode="full", agent_home_root=p.root)
    assert not policy.check_write(p.source).allowed, bad
    assert not policy.check_write(p.control).allowed, bad
    assert p.source.read_bytes() == p.before
    assert p.control.read_bytes() == before


@pytest.mark.parametrize("replacement", ["leaf", "parent", "hardlink"])
def test_identity_change_and_hardlink_alias_do_not_release_protection(protected, replacement):
    p = protected
    if replacement == "parent":
        p.source.parent.rename(p.home / "saved-docs")
        p.source.parent.mkdir()
        p.source.write_bytes(p.before)
        alias = p.home / "saved-docs/rules.bin"
    elif replacement == "leaf":
        alias = p.source.with_name("saved.bin")
        p.source.rename(alias)
        p.source.write_bytes(p.before)
    else:
        alias = p.home / "hard-alias"
        os.link(p.source, alias)
    policy = PathAccessPolicy.from_values(mode="full", agent_home_root=p.root)
    assert not policy.check_write(p.source).allowed
    assert not policy.check_write(alias).allowed
    assert p.source.read_bytes() == p.before
    assert alias.read_bytes() == p.before


# LLM: 这是唯一源码静态守卫；pyc包不读源，其SDK实际独立加载与保护行为由相邻运行测试继续验收。
# 函数用途: 检查路径政策源码只依赖标准库，不把AST扫描当作包内执行结果。
def test_projection_remains_stdlib_only():
    import ast
    import sys

    from agent_py_agent.agent import path_access_policy
    source_file = Path(path_access_policy.__file__)
    tree = ast.parse(source_file.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module.split(".")[0] in sys.stdlib_module_names
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)


# LLM: 原模块是py或pyc就按同后缀复制并核字节；真实loader单文件加载必须守原source拒写和business允许，不补开发源码。
# 函数用途: 在隔离子进程验证当前发行路径政策可独立执行，复制仅写本次tmp_path。
def test_single_file_sdk_can_enforce(protected, tmp_path):
    import hashlib
    import shutil
    import subprocess
    import sys

    from agent_py_agent.agent import path_access_policy
    source_file = Path(path_access_policy.__file__)
    copied = tmp_path / ("sdk_guard" + source_file.suffix)
    shutil.copyfile(source_file, copied)
    assert copied.suffix == source_file.suffix
    assert hashlib.sha256(copied.read_bytes()).digest() == hashlib.sha256(source_file.read_bytes()).digest()
    script = """import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('sdk_guard',sys.argv[1])
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
policy=module.PathAccessPolicy.from_values(mode='full',agent_home_root=Path(sys.argv[2]))
assert not policy.check_write(sys.argv[3]).allowed
assert policy.check_write(sys.argv[4]).allowed
print('SDK_SINGLE_FILE_PROTECTION_OK')
"""
    p = protected
    result = subprocess.run([sys.executable, "-I", "-c", script, str(copied), str(p.root), str(p.source),
                             str(p.home / "docs/business.txt")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "SDK_SINGLE_FILE_PROTECTION_OK" in result.stdout


@pytest.mark.parametrize("field,value", [("kind", []), ("updated_at", float("nan")), ("revision", 0),
                                         ("schema_version", 2), ("updated_by", False), ("identity", False)])
def test_projection_rejects_non_exact_types_and_nonfinite_time(protected, field, value):
    from agent_py_agent.agent.path_access_policy import (
        AgentsSourceProtectionError,
        persona_protected_paths,
    )
    p = protected
    record = dict(p.record, **{field: value})
    p.control.write_text(json.dumps(record))
    before = p.control.read_bytes()
    with pytest.raises(AgentsSourceProtectionError):
        persona_protected_paths(p.home)
    assert p.source.read_bytes() == p.before and p.control.read_bytes() == before


def test_projection_missing_strict_fd_support_fails_closed_without_plain_open(protected, monkeypatch):
    from agent_py_agent.agent import path_access_policy
    p = protected
    monkeypatch.setattr(path_access_policy.os, "supports_dir_fd", set())
    decision = PathAccessPolicy.from_values(mode="full", agent_home_root=p.root).check_write(p.source)
    assert not decision.allowed and decision.code == "PATH_AGENTS_SOURCE_PROTECTION_INVALID"
    assert p.source.read_bytes() == p.before


def test_duplicate_keys_and_symlink_control_directory_are_not_missing_configuration(protected):
    from agent_py_agent.agent.path_access_policy import (
        AgentsSourceProtectionError,
        persona_protected_paths,
    )
    p = protected
    p.control.write_text(json.dumps(p.record)[:-1] + ',"kind":"url"}')
    with pytest.raises(AgentsSourceProtectionError):
        persona_protected_paths(p.home)
    p.control.parent.rename(p.home / "old-persona")
    p.control.parent.symlink_to(p.home / "old-persona", target_is_directory=True)
    with pytest.raises(AgentsSourceProtectionError):
        persona_protected_paths(p.home)
    assert p.source.read_bytes() == p.before


# LLM: 插件spec投影仍走原工厂；路径按原SBPL Unicode字面编码核对，占位平台二进制不替代真实OS测试。
# 函数用途: 在各平台核对插件的来源保护路径投影。
def test_restricted_plugin_factory_does_not_lose_owner_source_protection(protected):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_sandbox import PluginRestrictedSandbox, plugin_sandbox_spec
    p = protected
    cwd = p.home / "data/plugins/environments/fixture"
    data = p.home / "data/plugins/data/fixture"
    cwd.mkdir(parents=True)
    data.mkdir(parents=True)
    policy = PluginRestrictedSandbox((p.home,), (p.home / "docs",), (), True, None, "allowlist")
    spec = plugin_sandbox_spec(cwd=cwd, data_dir=data, owner_home=p.home, sandbox_policy=policy)
    assert spec.owner_home == cwd  # 现有插件合同；保护层须从规范插件库反推owner，不修改工厂。
    mac = AttemptExecutionSandbox(replace(spec, macos_sandbox_exec="/fake/sandbox-exec"))._macos_argv(["/bin/true"])
    assert f"(literal {json.dumps(str(p.source), ensure_ascii=False)})" in mac[mac.index("-p") + 1]
    assert "(deny file-link)" in mac[mac.index("-p") + 1]
    linux = AttemptExecutionSandbox(replace(spec, bwrap_path="/fake/bwrap"))._linux_argv(["/bin/true"])
    assert any(flag == "--ro-bind" and linux[index + 1] == str(p.source) for index, flag in enumerate(linux))
    assert p.source.read_bytes() == p.before




def test_canonical_provider_owner_and_external_local_source_keep_same_guard(protected, tmp_path):
    from agent_py_agent.agent.path_access_policy import persona_protected_paths
    p = protected
    home = p.root / "owners/providers/feishu/users/alice"
    home.mkdir(parents=True)
    source = (tmp_path / "external/documents/rules.data").resolve()
    source.parent.mkdir(parents=True)
    source.write_bytes(p.before)
    chain = [(path.stat().st_dev, path.stat().st_ino) for path in (*reversed(source.parents), source)]
    identity = sha256(json.dumps(chain, separators=(",", ":")).encode()).hexdigest()
    config = home / "persona/agents_source.json"
    config.parent.mkdir()
    config.write_text(json.dumps(dict(p.record, owner="providers/feishu/users/alice", value=str(source), identity=identity)))
    before = config.read_bytes()
    assert source in persona_protected_paths(home)
    policy = PathAccessPolicy.from_values(mode="full", agent_home_root=p.root)
    assert not policy.check_write(source).allowed
    assert not policy.check_write(source.parent).allowed
    assert policy.check_write(source.with_name("normal-output.txt")).allowed
    assert source.read_bytes() == p.before and config.read_bytes() == before


# LLM: 九种坏配置数据与原夹具等价；不修改判定/字节保留断言，不模拟被测策略。
# 函数用途: 制造坏JSON、目录符号链接、超限或严格字段类型反例。
def _damage_source_configuration(p, bad):
    if bad == "json":
        p.control.write_bytes(b"broken JSON")
        return
    if bad == "symlink":
        saved = p.home / "saved-config"
        p.control.rename(saved)
        p.control.symlink_to(saved)
        return
    if bad == "oversize":
        p.control.write_bytes(b" " * (512 * 1024 + 1))
        return
    record = dict(p.record)
    key, value = {"owner": ("owner", "providers/feishu/users/alice"), "bool_revision": ("revision", True),
                  "bool_schema": ("schema_version", True), "time_int": ("updated_at", 1000),
                  "dotdot": ("value", str(p.home / "docs/../docs/rules.bin")), "identity": ("identity", "bad")}[bad]
    record[key] = value
    p.control.write_text(json.dumps(record))
