"""W1 包判据的行为横扫；真实 ZIP、原安装表和原投影，不执行包内文件。"""
from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.learnpack_store import BuildProvenance, LearnpackStore
from agent_py_agent.agent.capability.package_build import (
    KIND_CAPABILITY_PACK,
    KIND_PLUGIN,
    BuiltPackage,
)
from agent_py_agent.agent.capability.package_provider import (
    enabled_capability_packages,
    list_installed_capability_packages,
    read_capability_member,
)
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation, plugin_catalog_digest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.plugin_package import PackageReadLimits, inspect_plugin_package
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home


def _archive(version, name, *, documents=1, plugin_files=1):
    """独立写协议样本，避免构建器错误把全部判据回归挡在同一入口。"""
    contents = {"CAPABILITY.md": b"# Entry\n"}
    contents.update({f"docs/method-{index}.md": b"method\n" for index in range(1, documents)})
    tool = {"name": "check", "description": "检查", "input_schema": {"type": "object", "properties": {}},
            "requested_effect": "dangerous"}
    entry = {"kind": "interpreter", "command": "server.py", "args": [], "interpreter": "python3"}
    payload = {"schema_version": f"plugin_package.v{version}", "plugin_id": name,
               "version": "1.0.0", "summary": "判据测试", "settings_schema": {"type": "object", "properties": {}}}
    if version == 6:
        contents = {"server.py": b"raise RuntimeError('must not execute')\n"}
        contents.update({f"data/file-{index}.txt": b"data\n" for index in range(1, plugin_files)})
        payload.update(entry=entry, platforms=["any"], tools=[tool], actions=[], default_action="",
                       panels=[], skills=[], host_api=[])
    else:
        payload.update(package_kind="capability", capability={"description": "内容方法", "keywords": ["方法"],
                                                            "entry_document": "CAPABILITY.md"})
        if version == 10:
            contents["mcp/server.py"] = b"raise RuntimeError('must not execute')\n"
            payload["mcp_servers"] = [{"name": "checks", "root": "mcp", "entry": entry,
                                       "platforms": ["any"], "permissions": {"network": False},
                                       "tool_catalog": "declared", "tools": [tool]}]
    payload["files"] = [{"path": path, "sha256": hashlib.sha256(body).hexdigest(), "executable": False}
                        for path, body in sorted(contents.items())]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, body in {"plugin.json": json.dumps(payload, ensure_ascii=False).encode(), **contents}.items():
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    return output.getvalue()


def _install(store, version, name):
    package = inspect_plugin_package(_archive(version, name))
    return store.install(PluginInstallRequest(package, f"install-{name}", 0)).installation


def _enable(store, row):
    """仅发布真实领域状态；合成解释器指纹不代表启动或环境验收。"""
    operation = f"enable-{row.manifest.plugin_id}"
    if row.manifest.is_content_only:
        activation = PluginContentActivation(operation, row.manifest.plugin_id, row.package_sha256,
                                             row.revision, row.settings_revision)
    else:
        plan = PluginEnvironmentPlan(operation, row.manifest.plugin_id, row.package_sha256,
                                     row.revision, row.settings_revision, "a" * 64)
        preparing = PluginActivation(plan, "preparing")
        row = store.change_activation(PluginActivationRequest(operation, row.revision, preparing)).installation
        activation = replace(preparing, phase="active", catalog_sha256=plugin_catalog_digest(row.manifest))
    return store.change_activation(PluginActivationRequest(operation, row.revision, activation)).installation


@pytest.mark.parametrize("version,pack,content,runtime", [(7, True, True, False), (10, True, False, True),
                                                         (6, False, False, False)])
def test_real_manifests_separate_pack_identity_from_zero_process(version, pack, content, runtime, tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    row = _install(store, version, f"sample-v{version}")
    manifest = row.manifest
    assert manifest.is_capability_pack is pack
    assert manifest.is_content_only is content
    assert manifest.has_mcp_runtime is runtime
    active = _enable(store, row)
    assert active.enabled
    assert isinstance(active.activation, PluginContentActivation if content else PluginActivation)
    assert store.snapshot() == (active,)
    if runtime:
        assert [item.path for item in manifest.runtime_files] == ["server.py"]
    else:
        assert manifest.runtime_files == ()


def test_inventory_and_private_provider_keep_v10_pack_out_of_plugin_inventory(tmp_path):
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    rows = {version: _install(store, version, f"site-v{version}") for version in (7, 10, 6)}
    assert {item["package_id"] for item in list_installed_capability_packages(owner.home_dir)} == {"site-v7", "site-v10"}
    assert list_installed_capability_packages(owner.home_dir, include_disabled=False) == []
    assert enabled_capability_packages(owner) == ()
    for version, row in rows.items():
        rows[version] = _enable(store, row)
    listed = list_installed_capability_packages(owner.home_dir, include_disabled=False)
    assert {item["package_id"] for item in listed} == {"site-v7", "site-v10"}
    assert all(item["enabled"] for item in listed)
    provided = {package.package_id: package for package in enabled_capability_packages(owner)}
    assert set(provided) == {"site-v7", "site-v10"}
    for version in (7, 10):
        assert provided[f"site-v{version}"].read() == b"# Entry\n"
        assert read_capability_member(store, rows[version], "CAPABILITY.md") == b"# Entry\n"
    with pytest.raises(PluginInstallationError) as error:
        read_capability_member(store, rows[6], "server.py")
    assert error.value.reason == "activation_unavailable"
    row = rows[10]
    store.change_activation(PluginActivationRequest("disable-v10", row.revision, replace(row.activation, phase="revoked")))
    assert {item["package_id"] for item in list_installed_capability_packages(owner.home_dir)} == {"site-v7", "site-v10"}
    assert [item["package_id"] for item in list_installed_capability_packages(owner.home_dir, include_disabled=False)] == ["site-v7"]
    assert [item.package_id for item in enabled_capability_packages(owner)] == ["site-v7"]


def _self_map_context(owner):
    """只装配冷读上下文；安装事实仍由真实 Store 读取，不实例化重型 Agent。"""
    from agent_py_agent.agent.tooling.models import ToolInvocationContext, ToolRuntimeSnapshot
    from agent_py_agent.agent.tooling.self_map_registry import SelfMapReadContext

    home = SimpleNamespace(root=owner.root, owner_provider=owner.identity.provider,
                           owner_kind=owner.identity.owner_kind, owner_id=owner.owner_id,
                           owner_home_dir=owner.home_dir, owner_plugins_dir=owner.plugins_dir)
    tools = SimpleNamespace(path_access_mode="workspace-write", path_dangerous_roots=(),
                            owner_scope_root=str(owner.home_dir))
    agent = SimpleNamespace(home_paths=home, tools=tools, _current_skill_snapshot=None)
    runtime = ToolRuntimeSnapshot("predicate-sites", (), frozenset(), (), None)
    return SelfMapReadContext(agent, ToolInvocationContext(runtime), frozenset())


def test_self_map_sections_partition_actual_v7_v10_and_v6_rows(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_install_store, plugin_self_map

    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    for version in (7, 10, 6):
        _install(store, version, f"map-v{version}")
    decode = plugin_install_store.decode_installation_state
    calls = []

    def counted_decode(content, identity):
        calls.append(identity)
        return decode(content, identity)

    monkeypatch.setattr(plugin_install_store, "decode_installation_state", counted_decode)
    context = _self_map_context(owner)
    packages = plugin_self_map.package_summary(context)
    plugins = plugin_self_map.plugin_projection(context)
    assert packages.counts["installed"] == 2
    assert packages.counts["enabled"] == 0
    assert {item["id"] for item in plugins.items} == {"map-v6"}
    assert plugins.counts == {"installed": 1, "enabled": 0}
    assert calls == [owner.identity], "两个分区共用同次安装观察，不逐行或逐区重读表"


@pytest.mark.parametrize("version,kind", [(7, KIND_CAPABILITY_PACK), (10, KIND_CAPABILITY_PACK), (6, KIND_PLUGIN)])
def test_learnpack_kind_match_uses_pack_identity_not_content_only(version, kind, tmp_path):
    payload = _archive(version, f"build-v{version}")
    package = inspect_plugin_package(payload)
    built = BuiltPackage(kind, package.manifest.plugin_id, package.manifest.version, payload, package.sha256,
                         tuple(item.path for item in package.manifest.files))
    store = LearnpackStore(resolve_owner_home(tmp_path).home_dir)
    provenance = BuildProvenance("self-contained fixture", "MIT", "predicate-sites")
    record = store.save_build(built, provenance)
    assert store.build(record.sha256) == record
    other_kind = KIND_PLUGIN if kind == KIND_CAPABILITY_PACK else KIND_CAPABILITY_PACK
    store.save_build(replace(built, kind=other_kind), provenance)
    assert store.build(record.sha256) is None


@pytest.mark.parametrize("version", [7, 10])
def test_pack_member_budget_is_not_legacy_process_budget(version):
    payload = _archive(version, f"large-v{version}", documents=130)
    package = inspect_plugin_package(payload)
    assert package.manifest.is_capability_pack
    assert len(package.manifest.files) > PackageReadLimits().legacy_members
    # 同一真实归档超过宿主明确预算仍须拒绝；不靠放宽全局预算通过。
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_package(payload, limits=replace(PackageReadLimits(), members=len(package.manifest.files)))
    assert error.value.reason == "package_limit"


@pytest.mark.parametrize("version", [7, 10, 6])
def test_member_limit_boundary_selects_the_pack_branch(version):
    limits = replace(PackageReadLimits(), members=8, legacy_members=3)
    payload = _archive(version, f"budget-v{version}", documents=3, plugin_files=3)
    if version == 6:
        # v6 的 3 个文件加 plugin.json 超出 legacy_members=3，仍是旧规则。
        with pytest.raises(PluginPackageError) as error:
            inspect_plugin_package(payload, limits=limits)
        assert error.value.reason == "package_limit"
        accepted = _archive(6, "budget-at-limit", plugin_files=2)
        assert inspect_plugin_package(accepted, limits=limits).manifest.is_capability_pack is False
    else:
        assert inspect_plugin_package(payload, limits=limits).manifest.is_capability_pack
        with pytest.raises(PluginPackageError) as error:
            inspect_plugin_package(payload, limits=replace(limits, members=3))
        assert error.value.reason == "package_limit"
