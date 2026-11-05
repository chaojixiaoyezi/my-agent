"""W1 构建合同：v7 字节不变，v10 单服务器、声明执行位及真实安装/读取往返。

夹具完全在本文件内；不依赖其它 W1 测试，不替换 builder、reader 或 Store。
临时激活只验证安装表和文档准入，不代表运行时握手、环境准备或服务健康。
本文件不启动包进程；baseline 应在调用/断言处变红，而不是缺新符号的 collection error。
"""
from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.package_build import (
    KIND_CAPABILITY_PACK,
    PackageBuildError,
    build_capability_pack,
    capability_pack_paths,
    read_declared_files,
)
from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.plugin_package import inspect_plugin_package


@pytest.fixture(autouse=True)
def _isolated_home_and_no_process(tmp_path, monkeypatch):
    """只隔离宿主 HOME 并拦截误启动；真实协议两端和持久 Store 不做替换。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_AGENT_HOME", str(home))

    def unexpected_process(*args, **kwargs):
        pytest.fail("构建/安装/读取合同不得启动进程")

    monkeypatch.setattr("subprocess.Popen", unexpected_process)


def _tool():
    return {"name": "review", "description": "核对分场",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "requested_effect": "read_only"}


def _source(*, catalog="declared", executable_entry=False):
    """文档含 Unicode、私有 Skill 及 root 相似前缀，运行树使用 v6 ASCII 规则。"""
    contents = {
        "CAPABILITY.md": "# 分场方法\r\n先列人物，再列冲突。\n".encode(),
        "方法/复核.md": "核对转折和钩子。\n".encode(),
        "skills/story/SKILL.md": b"# private document, not a runtime skill\n",
        "mcp/runtime-not/说明.md": "相似前缀仍是文档。\n".encode(),
        "mcp/runtime/server.py": b"raise RuntimeError('package source must not execute')\n",
        "mcp/runtime/bin/helper": b"#!/bin/sh\nexit 99\n",
        "mcp/runtime/data.json": b'{"version":1}\n',
    }
    entry = ({"kind": "executable", "command": "bin/helper", "args": []} if executable_entry else
             {"kind": "interpreter", "command": "server.py", "args": [], "interpreter": "python3"})
    declaration = {
        "plugin_id": "w1-build-roundtrip", "version": "0.2.0", "summary": "分场方法与检查服务",
        "capability": {"description": "分场、节奏与对白复核", "keywords": ["短剧", "复核"],
                       "entry_document": "CAPABILITY.md"},
        "files": [{"path": path, **({"executable": True} if path.endswith("/bin/helper") else {})}
                  for path in contents],
        "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "mcp_servers": [{"name": "review-tools", "root": "mcp/runtime", "entry": entry,
                         "platforms": ["any"], "permissions": {"network": False},
                         "tool_catalog": catalog, "tools": [_tool()] if catalog == "declared" else []}],
    }
    return declaration, contents


def _valid_source(*, catalog="declared", executable_entry=False):
    """负例前必须先让未修改正样本走通 v10，避免未知字段的提前拒绝被误算成通过。"""
    declaration, contents = _source(catalog=catalog, executable_entry=executable_entry)
    built = build_capability_pack(declaration, contents)
    manifest = inspect_plugin_package(built.payload).manifest
    assert manifest.to_payload()["schema_version"] == "plugin_package.v10"
    assert manifest.to_payload()["mcp_servers"] == declaration["mcp_servers"]
    return declaration, contents


def _write_source(root, contents):
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _assert_mcp_error(code, function, *args):
    with pytest.raises(PackageBuildError) as caught:
        function(*args)
    assert caught.value.code == code


def _anchor_v7_bytes(declaration, contents):
    """冻结旧 v7 ZIP 编码为独立字节 oracle，不调用生产私有 _zip/_finish。"""
    manifest = {**declaration, "schema_version": "plugin_package.v7", "package_kind": "capability",
                "files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "executable": False}
                          for path, data in sorted(contents.items())]}
    members = {"plugin.json": json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(), **contents}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, members[name])
    return buffer.getvalue()


def test_absent_mcp_servers_keeps_anchor_v7_bytes_and_manifest():
    declaration, contents = _source()
    declaration.pop("mcp_servers")
    declaration["files"] = [{"path": path} for path in contents]
    expected = _anchor_v7_bytes(declaration, contents)
    built = build_capability_pack(declaration, contents)
    assert built.payload == expected
    assert built.sha256 == hashlib.sha256(expected).hexdigest()
    manifest = inspect_plugin_package(built.payload).manifest
    assert manifest.to_payload()["schema_version"] == "plugin_package.v7"
    assert "mcp_servers" not in manifest.to_payload()
    assert manifest.is_content_only


@pytest.mark.parametrize("name,digest", [
    ("drama-text-a", "51ca17734b865464ad18ecf1de5c254f97cb3d16afc66f1595a776f081619a36"),
    ("drama-workflow-b", "90c058e1ae79e442967fe0f3b5f4f917a95618725b08977b386e02a44ba63209"),
])
def test_v7_existing_fixed_digests_remain_locked(name, digest):
    # 摘要原样引用只读 test_package_build.py 的历史值，不重录或自动更新。
    root = Path(__file__).resolve().parents[2] / "examples" / "capability-packages" / name
    declaration = json.loads((root / "declaration.json").read_text(encoding="utf-8"))
    assert "mcp_servers" not in declaration
    contents = read_declared_files(root, capability_pack_paths(declaration))
    built = build_capability_pack(declaration, contents)
    assert built.sha256 == digest
    assert built.payload == _anchor_v7_bytes(declaration, contents)


@pytest.mark.parametrize("catalog", ["declared", "pinned_on_enable"])
@pytest.mark.parametrize("executable_entry", [False, True])
def test_one_server_builds_v10_and_preserves_input(catalog, executable_entry):
    declaration, contents = _source(catalog=catalog, executable_entry=executable_entry)
    before = deepcopy(declaration), dict(contents)
    assert capability_pack_paths(declaration) == list(contents)
    built = build_capability_pack(declaration, contents)
    assert (declaration, contents) == before
    assert built == build_capability_pack(declaration, contents)
    assert built.kind == KIND_CAPABILITY_PACK
    assert built.sha256 == hashlib.sha256(built.payload).hexdigest()
    assert built.files == tuple(sorted(contents))
    manifest = inspect_plugin_package(built.payload).manifest
    payload = manifest.to_payload()
    assert set(payload) == {"schema_version", "package_kind", "plugin_id", "version", "summary",
                            "capability", "files", "mcp_servers", "settings_schema"}
    assert payload["schema_version"] == "plugin_package.v10" and payload["package_kind"] == "capability"
    assert payload["mcp_servers"] == declaration["mcp_servers"]
    assert manifest.is_capability_pack and manifest.has_mcp_runtime and not manifest.is_content_only
    assert len(manifest.mcp_servers) == 1
    assert manifest.entry.to_payload() == declaration["mcp_servers"][0]["entry"]
    assert manifest.platforms == ("any",) and manifest.permissions.network is False
    assert tuple(tool.name for tool in manifest.tools) == (("review",) if catalog == "declared" else ())
    assert {item.path for item in manifest.runtime_files} == {"server.py", "bin/helper", "data.json"}
    for item in payload["files"]:
        assert item["sha256"] == hashlib.sha256(contents[item["path"]]).hexdigest()
        assert item["executable"] is item["path"].endswith("/bin/helper")
    with zipfile.ZipFile(io.BytesIO(built.payload)) as archive:
        assert archive.namelist() == sorted(["plugin.json", *contents])
        assert json.loads(archive.read("plugin.json")) == payload


@pytest.mark.parametrize("servers", [None, {}, "server", [], [None]])
def test_present_but_non_single_server_is_invalid_not_a_v7_fallback(servers):
    declaration, contents = _valid_source()
    declaration["mcp_servers"] = servers
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", capability_pack_paths, declaration)
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


def test_more_than_one_server_has_its_own_limit_code():
    declaration, contents = _valid_source()
    second = deepcopy(declaration["mcp_servers"][0])
    second.update(name="other", root="mcp/other")
    declaration["mcp_servers"].append(second)
    _assert_mcp_error("PACKAGE_BUILD_MCP_SERVER_LIMIT", capability_pack_paths, declaration)
    _assert_mcp_error("PACKAGE_BUILD_MCP_SERVER_LIMIT", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("field", ["plugin_id", "version", "summary", "capability", "files", "settings_schema"])
def test_mcp_extension_does_not_make_original_pack_fields_optional(field):
    declaration, contents = _valid_source()
    declaration.pop(field)
    _assert_mcp_error("PACKAGE_BUILD_DECLARATION_INVALID", capability_pack_paths, declaration)
    _assert_mcp_error("PACKAGE_BUILD_DECLARATION_INVALID", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("field", ["schema_version", "package_kind", "entry", "permissions", "unknown"])
def test_mcp_is_the_only_extra_top_level_declaration_key(field):
    declaration, contents = _valid_source()
    declaration[field] = None
    _assert_mcp_error("PACKAGE_BUILD_DECLARATION_INVALID", capability_pack_paths, declaration)
    _assert_mcp_error("PACKAGE_BUILD_DECLARATION_INVALID", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("verification", [None, {}])
def test_mcp_pack_rejects_even_empty_verification_declaration(verification):
    declaration, contents = _valid_source()
    declaration["capability"]["verification"] = verification
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", capability_pack_paths, declaration)
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("field", ["name", "root", "entry", "platforms", "permissions", "tool_catalog", "tools"])
def test_missing_server_fields_are_mcp_invalid(field):
    declaration, contents = _valid_source()
    declaration["mcp_servers"][0].pop(field)
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", capability_pack_paths, declaration)
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("change", [
    {"extra": True}, {"name": "Bad Name"}, {"permissions": {}}, {"permissions": {"network": 1}},
    {"permissions": {"network": False, "write": True}}, {"platforms": []},
    {"tool_catalog": "unknown"}, {"tool_catalog": "declared", "tools": []},
    {"tool_catalog": "pinned_on_enable", "tools": [_tool()]},
    {"tools": [{**_tool(), "observation": {}}]},
    {"entry": {"kind": "interpreter", "command": "server.py", "args": [], "interpreter": "python3", "extra": True}},
])
def test_server_keys_network_and_catalog_mismatch_are_mcp_invalid(change):
    declaration, contents = _valid_source()
    declaration["mcp_servers"][0].update(deepcopy(change))
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", capability_pack_paths, declaration)
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("root", ["", "/absolute", "../runtime", "mcp/../runtime", "mcp\\runtime",
                                   "mcp:runtime", "mcp//runtime", "mcp/中文", "a/b/c/d/e", "CAPABILITY.md"])
def test_invalid_server_roots_are_classified_before_file_reads(root):
    declaration, contents = _valid_source()
    declaration["mcp_servers"][0]["root"] = root
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", capability_pack_paths, declaration)
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("document", ["CAPABILITY.md", "mcp/runtime-not/说明.md"])
def test_document_declarations_stay_path_only_even_for_false_executable(document, flag):
    declaration, contents = _valid_source()
    next(item for item in declaration["files"] if item["path"] == document)["executable"] = flag
    # 文档属于原 files 外形错误；MCP 结构错误和运行树错误另有 MCP_INVALID 用例。
    with pytest.raises(PackageBuildError) as caught:
        capability_pack_paths(declaration)
    assert caught.value.code == "PACKAGE_BUILD_DECLARATION_INVALID"
    with pytest.raises(PackageBuildError) as caught:
        build_capability_pack(declaration, contents)
    assert caught.value.code == "PACKAGE_BUILD_DECLARATION_INVALID"


@pytest.mark.parametrize("flag", [False, True])
def test_runtime_items_accept_explicit_boolean_executable(flag):
    declaration, contents = _source()
    next(item for item in declaration["files"] if item["path"].endswith("/server.py"))["executable"] = flag
    assert capability_pack_paths(declaration) == list(contents)
    built = build_capability_pack(declaration, contents)
    files = inspect_plugin_package(built.payload).manifest.runtime_files
    assert next(item for item in files if item.path == "server.py").executable is flag


@pytest.mark.parametrize("flag", [0, 1, "true", None])
def test_runtime_executable_is_not_coerced_from_non_boolean(flag):
    declaration, contents = _valid_source()
    next(item for item in declaration["files"] if item["path"].endswith("/server.py"))["executable"] = flag
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("command", ["missing.py", "mcp/runtime/server.py", "../CAPABILITY.md", "/server.py"])
def test_entry_must_be_root_relative_and_declared(command):
    declaration, contents = _valid_source()
    declaration["mcp_servers"][0]["entry"]["command"] = command
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


def test_executable_entry_needs_declared_execution_bit():
    declaration, contents = _valid_source(executable_entry=True)
    next(item for item in declaration["files"] if item["path"].endswith("/bin/helper"))["executable"] = False
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


@pytest.mark.parametrize("path", ["中文.py", ".hidden", "bad name.py", "a" * 65,
                                   "a/b/c/d/e/f/g/h/server.py", "skills/hidden/SKILL.md"])
def test_runtime_tree_reuses_entry_path_depth_and_undeclared_skill_rules(path):
    declaration, contents = _valid_source()
    full = "mcp/runtime/" + path
    declaration["files"].append({"path": full})
    contents[full] = b"runtime member\n"
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


def test_entry_document_cannot_be_inside_runtime_root():
    declaration, contents = _valid_source()
    declaration["capability"]["entry_document"] = "mcp/runtime/server.py"
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


def test_runtime_file_limit_is_independent_of_document_budget():
    declaration, contents = _valid_source()
    # 基础运行文件三个，再加125个，跨过 v6 的127项；总包仍远低于能力包预算。
    for number in range(125):
        path = f"mcp/runtime/data/item-{number}.txt"
        declaration["files"].append({"path": path})
        contents[path] = b"x"
    _assert_mcp_error("PACKAGE_BUILD_MCP_INVALID", build_capability_pack, declaration, contents)


def test_zip_modes_follow_declaration_never_host_file_mode(tmp_path):
    declaration, contents = _source(executable_entry=True)
    root = tmp_path / "source"
    _write_source(root, contents)
    # 声明可执行的入口在宿主不可执行，其余文件全部在宿主可执行；两边都不能提升/降低声明。
    for path in contents:
        (root / path).chmod(0o600 if path.endswith("/bin/helper") else 0o755)
    first = build_capability_pack(declaration, read_declared_files(root, capability_pack_paths(declaration)))
    with zipfile.ZipFile(io.BytesIO(first.payload)) as archive:
        modes = {info.filename: stat.S_IMODE(info.external_attr >> 16) for info in archive.infolist()}
        assert all(stat.S_ISREG(info.external_attr >> 16) for info in archive.infolist())
    assert modes == {"plugin.json": 0o644, **{path: 0o755 if path.endswith("/bin/helper") else 0o644
                                            for path in contents}}
    for path in contents:
        (root / path).chmod(0o755 if path.endswith("/bin/helper") else 0o600)
    second = build_capability_pack(declaration, read_declared_files(root, capability_pack_paths(declaration)))
    assert first.payload == second.payload and first.sha256 == second.sha256


def test_build_uses_read_snapshot_not_changed_source_files(tmp_path):
    """读源后改动原文件，构建仍须只消费已读快照，不能在复验时重读来源。"""
    declaration, contents = _source()
    root = tmp_path / "source"
    _write_source(root, contents)
    read_contents = read_declared_files(root, capability_pack_paths(declaration))
    (root / "mcp/runtime/server.py").write_bytes(b"changed after read\n")
    (root / "CAPABILITY.md").write_bytes(b"changed document after read\n")
    built = build_capability_pack(declaration, read_contents)
    with zipfile.ZipFile(io.BytesIO(built.payload)) as archive:
        assert archive.read("mcp/runtime/server.py") == contents["mcp/runtime/server.py"]
        assert archive.read("CAPABILITY.md") == contents["CAPABILITY.md"]
    assert read_contents == contents


def test_four_segment_root_does_not_consume_root_relative_file_depth():
    declaration, contents = _source()
    old, new = "mcp/runtime/", "mcp/a/b/c/"
    contents = {path.replace(old, new, 1) if path.startswith(old) else path: data
                for path, data in contents.items()}
    for item in declaration["files"]:
        if item["path"].startswith(old):
            item["path"] = item["path"].replace(old, new, 1)
    declaration["mcp_servers"][0]["root"] = new.rstrip("/")
    relative = "a/b/c/d/e/f/g/data.txt"
    declaration["files"].append({"path": new + relative})
    contents[new + relative] = b"valid eight-segment runtime path\n"
    built = build_capability_pack(declaration, contents)
    assert relative in {item.path for item in inspect_plugin_package(built.payload).manifest.runtime_files}


@pytest.mark.parametrize("network", [False, True])
def test_network_is_a_required_boolean_not_an_implicit_default(network):
    declaration, contents = _source()
    declaration["mcp_servers"][0]["permissions"]["network"] = network
    built = build_capability_pack(declaration, contents)
    manifest = inspect_plugin_package(built.payload).manifest
    assert manifest.permissions.network is network
    assert manifest.to_payload()["mcp_servers"][0]["permissions"] == {"network": network}


def test_runtime_source_symlink_is_refused_by_real_no_follow_reader(tmp_path):
    declaration, contents = _valid_source()
    root = tmp_path / "source"
    _write_source(root, {path: data for path, data in contents.items() if path != "mcp/runtime/server.py"})
    target = tmp_path / "other.py"
    target.write_bytes(contents["mcp/runtime/server.py"])
    (root / "mcp/runtime/server.py").symlink_to(target)
    with pytest.raises(PackageBuildError) as caught:
        read_declared_files(root, capability_pack_paths(declaration))
    assert caught.value.code == "PACKAGE_BUILD_FILE_INVALID"


@pytest.mark.parametrize("extra", [False, True])
def test_build_contents_must_match_declared_paths(extra):
    declaration, contents = _valid_source()
    if extra:
        contents["mcp/runtime/undeclared.py"] = b"not declared\n"
    else:
        contents.pop("mcp/runtime/server.py")
    with pytest.raises(PackageBuildError) as caught:
        build_capability_pack(declaration, contents)
    assert caught.value.code == "PACKAGE_BUILD_DECLARATION_INVALID"


@pytest.mark.parametrize("extra", [False, True])
def test_real_reader_rejects_undeclared_or_missing_archive_runtime_members(extra):
    declaration, contents = _valid_source()
    built = build_capability_pack(declaration, contents)
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(built.payload)) as original, zipfile.ZipFile(buffer, "w") as changed:
        for info in original.infolist():
            if not extra and info.filename == "mcp/runtime/server.py":
                continue
            changed.writestr(info, original.read(info.filename))
        if extra:
            info = zipfile.ZipInfo("mcp/runtime/undeclared.py", date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            changed.writestr(info, b"not declared\n")
    with pytest.raises(PluginPackageError) as caught:
        inspect_plugin_package(buffer.getvalue())
    assert caught.value.reason == "invalid_archive"


def _publish_declared_state_without_process(store, installed):
    """使用真实两阶段安装状态合同；合成指纹仅是状态夹具，不是进程验收证明。"""
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_activation_record import (
        PluginActivation,
        plugin_catalog_digest,
    )
    from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan

    plan = PluginEnvironmentPlan("enable-w1-build", installed.manifest.plugin_id, installed.package_sha256,
                                 installed.revision, installed.settings_revision, "a" * 64)
    preparing = store.change_activation(PluginActivationRequest(
        plan.operation_id, installed.revision, PluginActivation(plan, "preparing"))).installation
    return store.change_activation(PluginActivationRequest(
        plan.operation_id, preparing.revision,
        replace(preparing.activation, phase="active", catalog_sha256=plugin_catalog_digest(installed.manifest)),
    )).installation


def test_real_build_inspect_install_document_read_and_rerooted_runtime_roundtrip(tmp_path):
    from agent_py_agent.agent.capability.package_provider import (
        enabled_capability_packages,
        list_installed_capability_packages,
        read_capability_member,
    )
    from agent_py_agent.agent.plugin_files_environment import inspect_plugin_files
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    declaration, contents = _source()
    source = tmp_path / "source"
    _write_source(source, contents)
    built = build_capability_pack(declaration, read_declared_files(source, capability_pack_paths(declaration)))
    candidate = inspect_plugin_package(built.payload)
    owner = resolve_owner_home(tmp_path / "home", OwnerIdentity.local_main())
    assert owner.home_dir.is_relative_to(tmp_path)
    store = PluginInstallStore(owner)
    result = store.install(PluginInstallRequest(candidate, "install-w1-build", 0))
    assert result.outcome == "installed" and result.commit_state == "committed"
    installed = result.installation
    assert not installed.enabled and installed.activation is None
    assert store.snapshot() == (installed,)
    assert store.package_bytes(installed) == built.payload
    listing = list_installed_capability_packages(owner.home_dir)
    assert [row["package_id"] for row in listing] == [built.package_id]
    assert listing[0]["digest"] == built.sha256 and listing[0]["enabled"] is False
    assert list_installed_capability_packages(owner.home_dir, include_disabled=False) == []

    # 全部状态由真实 Store 提交和重新解码；没有写表捷径，也没有创建/启动环境。
    active = _publish_declared_state_without_process(store, installed)
    assert store.snapshot() == (active,) and active.enabled
    packages = enabled_capability_packages(owner)
    assert len(packages) == 1
    package = packages[0]
    documents = {path: data for path, data in contents.items() if not path.startswith("mcp/runtime/")}
    assert {item.path for item in package.members} == set(documents)
    assert package.read() == contents["CAPABILITY.md"]
    for path, data in documents.items():
        assert package.read(path) == read_capability_member(store, active, path) == data
    for path in set(contents) - set(documents):
        assert package.resolve(path) is None
        with pytest.raises(ValueError, match="CAPABILITY_RESOURCE_NOT_AVAILABLE"):
            package.read(path)
        with pytest.raises(ValueError):
            read_capability_member(store, active, path)

    persisted = inspect_plugin_package(store.package_bytes(store.snapshot()[0]))
    runtime = inspect_plugin_files(persisted)
    expected_runtime = {path.removeprefix("mcp/runtime/"): data for path, data in contents.items()
                        if path.startswith("mcp/runtime/")}
    assert {item.declaration.path: item.content for item in runtime} == expected_runtime
    assert {item.declaration.path: item.declaration.executable for item in runtime} == {
        "server.py": False, "bin/helper": True, "data.json": False,
    }
    for item in runtime:
        assert item.declaration.sha256 == hashlib.sha256(item.content).hexdigest()
    assert persisted.manifest.entry.command == "server.py"
    assert not (owner.plugins_dir / "environments").exists()


def test_disabled_v7_to_v10_to_v7_updates_keep_real_table_decodable(tmp_path):
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.plugin_update import PluginUpdateRequest
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home

    v10, runtime_contents = _source()
    v7 = deepcopy(v10)
    v7.pop("mcp_servers")
    v7["version"] = "0.1.0"
    docs = {path: data for path, data in runtime_contents.items() if not path.startswith("mcp/runtime/")}
    v7["files"] = [{"path": path} for path in docs]
    old = build_capability_pack(v7, docs)
    new = build_capability_pack(v10, runtime_contents)
    store = PluginInstallStore(resolve_owner_home(tmp_path / "home"))
    current = store.install(PluginInstallRequest(inspect_plugin_package(old.payload), "install-v7", 0)).installation
    assert store.snapshot() == (current,) and current.manifest.is_content_only
    for operation, built, schema in [("upgrade-v10", new, "plugin_package.v10"),
                                     ("revert-v7", old, "plugin_package.v7")]:
        result = store.update_package(PluginUpdateRequest(
            built.package_id, inspect_plugin_package(built.payload), operation, current.revision))
        assert result.outcome == "updated" and result.commit_state == "committed"
        assert result.installation.revision == current.revision + 1
        current = result.installation
        assert store.snapshot() == (current,)
        assert current.manifest.to_payload()["schema_version"] == schema
        assert current.activation is None and not current.enabled
        assert store.package_bytes(current) == built.payload


@pytest.mark.parametrize("code,retryable", [("PACKAGE_BUILD_MCP_INVALID", True),
                                           ("PACKAGE_BUILD_MCP_SERVER_LIMIT", False)])
def test_mcp_build_error_contract_classification_is_registered(code, retryable):
    from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
    from agent_py_agent.agent.contracts.recovery import RecoveryAction

    assert code in ERROR_CONTRACTS
    contract = ERROR_CONTRACTS[code]
    assert contract.code == code and contract.category == "tool"
    assert contract.retryable is retryable
    assert contract.recommended_action == RecoveryAction.CHANGE_STRATEGY.value
    assert any("\u4e00" <= character <= "\u9fff" for character in contract.recovery_hint)
