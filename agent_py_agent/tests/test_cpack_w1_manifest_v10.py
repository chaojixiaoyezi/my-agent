"""v10 只扩展能力包；旧清单固定字节、相对运行树及环境边界均走真实读取器。"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import runpy
import zipfile
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from agent_py_agent.agent import capability_package_manifest as pack_schema
from agent_py_agent.agent.plugin_entry import PluginFile
from agent_py_agent.agent.plugin_files_environment import inspect_plugin_files
from agent_py_agent.agent.plugin_manifest import PluginManifest, PluginPackageError
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.tests.test_plugin_manifest_v8 import _v8, legacy_payload
from agent_py_agent.tests.test_plugin_manifest_v9 import v9_payload


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _v10(*, mode="pinned_on_enable", root="mcp/server", command="server.py", executable=False):
    files = {"CAPABILITY.md": b"# private entry", "方法/分镜 示例.md": b"private method",
             f"{root}/{command}": b"raise RuntimeError('must not execute')\n"}
    tools = [{"name": "check", "description": "校验材料", "requested_effect": "dangerous",
              "input_schema": {"type": "object", "properties": {}}}] if mode == "declared" else []
    payload = {
        "schema_version": "plugin_package.v10", "package_kind": "capability",
        "plugin_id": "w1-manifest", "version": "1.0", "summary": "私有资料与可选工具",
        "capability": {"description": "分镜方法", "keywords": ["分镜"], "entry_document": "CAPABILITY.md"},
        "files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                   "executable": executable and path == f"{root}/{command}"} for path, data in files.items()],
        "mcp_servers": [{"name": "story-tools", "root": root,
                         "entry": {"kind": "executable" if executable else "interpreter", "command": command,
                                   "args": [], **({} if executable else {"interpreter": "python3"})},
                         "platforms": ["any"], "permissions": {"network": False},
                         "tool_catalog": mode, "tools": tools}],
        "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    return payload, files


def _accepted(payload=None):
    payload = _v10()[0] if payload is None else payload
    try:
        manifest = PluginManifest.from_payload(payload)
    except PluginPackageError as exc:
        manifest = None
        detail = str(exc.__cause__)
    else:
        detail = ""
    assert manifest is not None, f"有效 v10 必须被解析，不能由旧版本门一概拒绝：{detail}"
    return manifest


def _archive(payload, files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("plugin.json", _canonical(payload))
        for path, data in files.items():
            archive.writestr(path, data)
    return stream.getvalue()


@pytest.mark.parametrize("mode", ["declared", "pinned_on_enable"])
@pytest.mark.parametrize("executable", [False, True])
def test_v10_roundtrip_projections_and_runtime_only_reader(mode, executable):
    payload, files = _v10(mode=mode, executable=executable)
    manifest = _accepted(payload)
    assert manifest.is_capability_pack and manifest.has_mcp_runtime and not manifest.is_content_only
    assert manifest.entry == manifest.mcp_servers[0].entry
    assert manifest.platforms == manifest.mcp_servers[0].platforms
    assert manifest.permissions.network is False
    assert manifest.tools == manifest.mcp_servers[0].tools
    assert _canonical(manifest.to_payload()) == _canonical(payload)
    assert PluginManifest.from_payload(manifest.to_payload()) == manifest
    assert len(manifest.runtime_files) == 1
    assert manifest.runtime_files[0].path == "server.py"
    assert manifest.runtime_files[0].executable is executable
    assert isinstance(manifest.runtime_files[0], PluginFile)
    package = inspect_plugin_package(_archive(payload, files))
    inspected = inspect_plugin_files(package)
    assert tuple(item.declaration for item in inspected) == manifest.runtime_files
    assert inspected[0].content == files["mcp/server/server.py"]
    assert b"private entry" not in b"".join(item.content for item in inspected)
    assert not {"entry", "platforms", "permissions", "tools"} & manifest.to_payload().keys()
    with pytest.raises(FrozenInstanceError):
        manifest.mcp_servers[0].network = True


@pytest.mark.parametrize("change", [
    lambda p: p.update(package_kind="plugin"),
    lambda p: p.update(mcp_servers=[]),
    lambda p: p["mcp_servers"].append(copy.deepcopy(p["mcp_servers"][0])),
    lambda p: p.update(mcp_servers={}),
    lambda p: p["mcp_servers"][0].update(name="Bad"),
    lambda p: p["mcp_servers"][0].update(name="x" * 33),
    lambda p: p["mcp_servers"][0].update(root=""),
    lambda p: p["mcp_servers"][0].update(root="a/b/c/d/e"),
    lambda p: p["mcp_servers"][0].update(root="../mcp"),
    lambda p: p["mcp_servers"][0].update(root="中文"),
    lambda p: p["mcp_servers"][0].update(root="a//b"),
    lambda p: p["mcp_servers"][0].update(root="mcp\\server"),
    lambda p: p["mcp_servers"][0].update(permissions={}),
    lambda p: p["mcp_servers"][0].update(permissions={"network": 1}),
    lambda p: p["mcp_servers"][0].update(permissions={"network": False, "read": True}),
    lambda p: p["mcp_servers"][0].pop("permissions"),
    lambda p: p["mcp_servers"][0].update(platforms=[]),
    lambda p: p["mcp_servers"][0].update(platforms=["bad"]),
    lambda p: p["mcp_servers"][0].update(tool_catalog="other"),
    lambda p: p["mcp_servers"][0].update(tool_catalog="declared"),
    lambda p: p["mcp_servers"][0].update(tools=[{"name": "x", "description": "工具",
        "requested_effect": "dangerous", "input_schema": {"type": "object"}}]),
    lambda p: p["mcp_servers"][0].update(unknown=True),
    lambda p: p["capability"].update(verification=None),
    lambda p: p["capability"].update(entry_document="mcp/server/server.py"),
    lambda p: p["capability"].update(entry_document="mcp/server"),
    lambda p: p["files"][0].update(executable=True),
    lambda p: p["files"].pop(),
    lambda p: p.update(settings_schema={"type": "array"}),
])
def test_v10_invalid_variants_are_invalid_manifest_not_unknown_version(change):
    _accepted()  # 正对照防止“尚不识别 v10”让所有负例伪绿。
    payload, files = _v10()
    change(payload)
    for read in (PluginManifest.from_payload, lambda p: inspect_plugin_package(_archive(p, files))):
        with pytest.raises(PluginPackageError) as error:
            read(payload)
        assert error.value.reason == "invalid_manifest"


@pytest.mark.parametrize("field,value", [
    ("wheels", []), ("actions", []), ("default_action", ""), ("panels", []),
    ("skills", []), ("host_api", []), ("events", []), ("tool_gates", []),
    ("entry", None), ("permissions", {"network": False}), ("host_api_v9", False),
])
def test_v10_cannot_smuggle_legacy_top_level_fields(field, value):
    _accepted()
    payload, _ = _v10()
    payload[field] = value
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(payload)
    assert error.value.reason == "invalid_manifest"


@pytest.mark.parametrize("relative_depth,valid", [(8, True), (9, False)])
def test_runtime_depth_is_relative_to_four_segment_root(relative_depth, valid):
    _accepted()
    command = "/".join(["segment"] * (relative_depth - 1) + ["server.py"])
    payload, _ = _v10(root="a/b/c/d", command=command)
    if valid:
        manifest = _accepted(payload)
        assert manifest.runtime_files[0].path == command
        assert manifest.files[-1].path == f"a/b/c/d/{command}"
        assert _canonical(manifest.to_payload()) == _canonical(payload)
    else:
        with pytest.raises(PluginPackageError) as error:
            PluginManifest.from_payload(payload)
        assert error.value.reason == "invalid_manifest"


@pytest.mark.parametrize("runtime_count,valid", [(127, True), (128, False)])
def test_runtime_file_count_keeps_original_v6_limit(runtime_count, valid):
    _accepted()
    payload, _ = _v10()
    payload["files"].extend({"path": f"mcp/server/data/{i}.txt", "sha256": "a" * 64, "executable": False}
                            for i in range(runtime_count - 1))
    if valid:
        assert len(_accepted(payload).runtime_files) == runtime_count
    else:
        with pytest.raises(PluginPackageError) as error:
            PluginManifest.from_payload(payload)
        assert error.value.reason == "invalid_manifest"


@pytest.mark.parametrize("path", ["mcp/server/中文.py", "mcp/server/skills/private/SKILL.md",
                                  "mcp/server/../escape.py", "mcp/server/plugin.json"])
def test_runtime_paths_reuse_plugin_file_and_undeclared_skill_rules(path):
    _accepted()
    payload, _ = _v10()
    payload["files"].append({"path": path, "sha256": "a" * 64, "executable": False})
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(payload)
    assert error.value.reason == "invalid_manifest"


@pytest.mark.parametrize("field", ["observation", "observation_ref"])
def test_v10_tools_do_not_accept_even_empty_observation_fields(field):
    payload, _ = _v10(mode="declared")
    _accepted(payload)
    payload["mcp_servers"][0]["tools"][0][field] = None
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(payload)
    assert error.value.reason == "invalid_manifest"


@pytest.mark.parametrize("override", [
    {"entry_module": "foreign"}, {"entry_wheel": "wheels/foreign.whl"},
    {"tools": ()}, {"default_action": "check"}, {"skills": ("private",)},
    {"host_api": ("read",)}, {"host_api_v9": True}, {"platforms": ("linux-x86_64",)},
    {"permissions": None}, {"mcp_servers": []}, {"mcp_servers": ()},
])
def test_direct_construction_cannot_desynchronize_v10_projections(override):
    manifest = _accepted(_v10(mode="declared")[0])
    with pytest.raises(ValueError):
        replace(manifest, **override)


def test_server_payload_is_strict_immutable_and_exports_frozen_wire_values():
    manifest = _accepted()
    server = manifest.mcp_servers[0]
    assert pack_schema.CAPABILITY_PACKAGE_SCHEMA_V10 == "plugin_package.v10"
    assert type(pack_schema.MAX_PACK_MCP_SERVERS) is int and pack_schema.MAX_PACK_MCP_SERVERS == 1
    assert pack_schema.PACK_TOOL_CATALOG_MODES == ("declared", "pinned_on_enable")
    assert pack_schema.PACK_MCP_CONFIRMATION_KIND == "capability_pack_mcp"
    assert type(server).from_payload(server.to_payload()) == server
    assert tuple(PluginManifest.__dataclass_fields__)[-2:] == ("host_api_v9", "mcp_servers")
    assert pack_schema.validate_pack_runtime_files(server, manifest.files) == manifest.runtime_files
    with pytest.raises(ValueError):
        replace(server, root="a/b/c/d/e")
    with pytest.raises(ValueError):
        replace(server, network=1)


@pytest.mark.parametrize("version", range(1, 10))
def test_all_legacy_versions_keep_exact_canonical_payload_and_predicate_meaning(version):
    payload = legacy_payload(version) if version <= 7 else _v8() if version == 8 else v9_payload()
    manifest = PluginManifest.from_payload(payload)
    assert _canonical(manifest.to_payload()) == _canonical(payload)
    assert hasattr(manifest, "is_capability_pack"), "显式包判据必须在旧版本同样可用"
    assert manifest.is_capability_pack is (version == 7)
    assert manifest.is_content_only is (version == 7)
    assert manifest.has_mcp_runtime is False
    assert manifest.runtime_files == ()


def _embedded_manifests(value):
    if isinstance(value, str) and value.startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            return
    if isinstance(value, dict):
        version = value.get("schema_version")
        if isinstance(version, str) and version in {f"plugin_package.v{i}" for i in range(1, 10)}:
            yield value
        else:
            for child in value.values():
                yield from _embedded_manifests(child)
    elif isinstance(value, list):
        for child in value:
            yield from _embedded_manifests(child)


def test_every_shipped_json_sample_manifest_keeps_exact_canonical_bytes():
    fixtures = Path(__file__).parent / "fixtures"
    found = []
    for path in sorted(fixtures.rglob("*.json")):
        for payload in _embedded_manifests(json.loads(path.read_text(encoding="utf-8"))):
            manifest = PluginManifest.from_payload(payload)
            assert _canonical(manifest.to_payload()) == _canonical(payload), str(path.relative_to(fixtures))
            found.append(payload["schema_version"])
    assert {f"plugin_package.v{i}" for i in range(1, 8)} <= set(found)


def test_shipped_v9_portal_sample_recipe_keeps_exact_manifest_bytes(tmp_path):
    recipe = Path(__file__).parent / "fixtures/web_portal_plugin_handoff/build_plugin_package.py"
    namespace = runpy.run_path(str(recipe), run_name="w1_static_fixture")
    output, package = namespace["build_workbench_plugin_zip"](tmp_path / "portal.zip")
    with zipfile.ZipFile(output) as archive:
        payload = json.loads(archive.read("plugin.json"))
    assert payload["schema_version"] == "plugin_package.v9"
    assert _canonical(package.manifest.to_payload()) == _canonical(payload)


def test_original_v8_permission_only_and_host_api_conflict_errors_remain_exact():
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(_v8(events=[], tool_gates=[]))
    assert error.value.reason == "invalid_manifest"
    assert str(error.value.__cause__) == "v8 必须使用文件入口并声明订阅"
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(_v8(host_api=["read"]))
    assert error.value.reason == "events_with_host_api_unsupported"
    assert str(error.value) == "第一期事件插件不能同时声明宿主 API。"


@pytest.mark.parametrize("mode", ["declared", "pinned_on_enable"])
def test_public_runtime_file_values_roundtrip_and_install(tmp_path, mode):
    """公开 PluginFile 与归档内部声明同值，不能让真实安装的完整性复验误拒绝。"""
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home

    payload, contents = _v10(mode=mode)
    package = inspect_plugin_package(_archive(payload, contents))
    plain = replace(package.manifest, files=tuple(
        PluginFile(item.path, item.sha256, item.executable) if isinstance(item, PluginFile) else item
        for item in package.manifest.files
    ))
    assert plain.to_payload() == package.manifest.to_payload()
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    store.install(PluginInstallRequest(replace(package, manifest=plain), "install-direct", 0))
    assert store.snapshot()[0].manifest == plain
    assert PluginManifest.from_payload(plain.to_payload()) == plain == package.manifest
    runtime, public = package.manifest.files[-1], plain.files[-1]
    assert runtime == public and public == runtime
    assert hash(runtime) == hash(public)
    assert {runtime: "same file"}[public] == "same file"


@pytest.mark.parametrize("executable", [False, True])
def test_real_environment_materializes_only_rerooted_runtime_without_process(tmp_path, monkeypatch, executable):
    """真实原操作、配额和安全落盘链路；只验证准备，不冒充进程或 MCP 握手。"""
    import stat
    import subprocess

    from agent_py_agent.agent.plugin_environment import EnvironmentBuildLimits
    from agent_py_agent.agent.plugin_files_environment import prepare_files_environment
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    from agent_py_agent.tests.plugin_environment_fixtures import environment_operation

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("环境准备不得执行程序"))
    payload, contents = _v10(executable=executable)
    package = inspect_plugin_package(_archive(payload, contents))
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    store.install(PluginInstallRequest(package, "install-for-prepare", 0))
    table_before = (store.root / "installations.json").read_bytes()
    operation = environment_operation(owner, package, "prepare-runtime-only")
    prepared = prepare_files_environment(owner, package, operation, limits=EnvironmentBuildLimits())
    candidate = owner.plugins_dir / "environments" / prepared.environment_ref
    runtime = candidate / "files" / "server.py"
    assert prepared.python_relative_path == "files/server.py"
    assert runtime.read_bytes() == contents["mcp/server/server.py"]
    assert stat.S_IMODE(runtime.stat().st_mode) == (0o500 if executable else 0o400)
    assert {str(p.relative_to(candidate / "files")) for p in (candidate / "files").rglob("*") if p.is_file()} == {"server.py"}
    assert not (candidate / "files" / "mcp").exists()
    assert not (candidate / "files" / "CAPABILITY.md").exists()
    assert not (candidate / "files" / "方法").exists()
    assert (store.root / "installations.json").read_bytes() == table_before
    assert store.snapshot()[0].enabled is False
