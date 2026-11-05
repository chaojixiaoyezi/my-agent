"""W1 冻结包投影、文档/runtime 分区与 owner 边界；仅用 tmp_path，不启动进程。"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.capability.package_provider import (
    enabled_capability_packages,
    enabled_public_capability_packages,
    list_installed_capability_packages,
    read_capability_member,
)
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation, plugin_catalog_digest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_installation_state import (
    decode_installation_state,
    encode_installation_state,
)
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_update import PluginUpdateRequest
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


def _build(name="owner-pack", *, runtime=True, version="1.0.0", pinned=False):
    """通过真实构建器生成自包含归档；文档脚本和前缀相近文档都不属于 runtime。"""
    contents = {"CAPABILITY.md": b"# Capability\n", "skills/method/SKILL.md": b"private method\n",
                "scripts/helper.py": b"raise RuntimeError('document only')\n",
                "mcp/checks-not-runtime.md": b"a document next to the runtime root\n"}
    declaration = {"plugin_id": name, "version": version, "summary": "测试能力",
                   "capability": {"description": "文档与检查能力", "keywords": ["检查", "方法"],
                                  "entry_document": "CAPABILITY.md"},
                   "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False}}
    if runtime:
        contents.update({"mcp/checks/server.py": b"raise RuntimeError('must not execute')\n",
                         "mcp/checks/assets/policy.txt": b"runtime data\n",
                         "mcp/checks/scripts/helper.py": b"raise RuntimeError('runtime only')\n"})
        tools = [{"name": name, "description": "检查工具 " + name,
                  "input_schema": {"type": "object", "properties": {}}, "requested_effect": "dangerous"}
                 for name in ("inspect", "verify")]
        declaration["mcp_servers"] = [{"name": "checks", "root": "mcp/checks",
            "entry": {"kind": "interpreter", "command": "server.py", "args": [], "interpreter": "python3"},
            "platforms": ["any"], "permissions": {"network": False},
            "tool_catalog": "pinned_on_enable" if pinned else "declared", "tools": [] if pinned else tools}]
    declaration["files"] = [{"path": path} for path in contents]
    return build_capability_pack(declaration, contents), contents


def _install(store, built, *, scope="private", operation=None):
    package = inspect_plugin_package(built.payload)
    return store.install(PluginInstallRequest(package, operation or f"install-{built.package_id}", 0,
                                            installation_scope=scope)).installation


def _enable(store, row):
    """发布声明目录或内容激活；不准备环境，不调用解释器或 MCP。"""
    operation = f"enable-{row.manifest.plugin_id}"
    if row.manifest.is_content_only:
        activation = PluginContentActivation(operation, row.manifest.plugin_id, row.package_sha256,
                                             row.revision, row.settings_revision)
    else:
        plan = PluginEnvironmentPlan(operation, row.manifest.plugin_id, row.package_sha256,
                                     row.revision, row.settings_revision, "a" * 64)
        activation = PluginActivation(plan, "preparing")
        row = store.change_activation(PluginActivationRequest(operation, row.revision, activation)).installation
        activation = replace(activation, phase="active", catalog_sha256=plugin_catalog_digest(row.manifest))
    return store.change_activation(PluginActivationRequest(operation, row.revision, activation)).installation


def _project(owner, scope):
    packages = enabled_public_capability_packages(owner) if scope == "public" else enabled_capability_packages(owner)
    assert len(packages) == 1
    return packages[0]


def _v7_snapshot(tmp_path):
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    built, _contents = _build(runtime=False)
    _enable(store, _install(store, built))
    return enabled_capability_packages(owner)[0]


@pytest.mark.parametrize("runtime,names", [("none", ()), ("on_demand", ()),
                                           ("on_demand", ("model_tool_a", "model_tool_b")),
                                           ("owner_unsupported", ())])
def test_snapshot_runtime_card_fields_do_not_change_refs_or_tool_requirements(tmp_path, runtime, names):
    base = _v7_snapshot(tmp_path)
    old_card = base.to_card()
    assert base.mcp_runtime == "none" and base.mcp_tool_names == ()
    legacy_card = {
        "package_id": base.package_id, "name": base.name, "version": base.version, "summary": base.summary,
        "installation_scope": "private", "application_scope": base.application_scope,
        "assets": {"skills": ["method"], "scripts": ["helper"], "workflows": []},
        "asset_counts": {"skills": 1, "scripts": 1, "workflows": 0},
        "digest": base.package_sha256, "enabled": True,
    }
    assert json.dumps(old_card, ensure_ascii=False, separators=(",", ":")).encode() == json.dumps(
        legacy_card, ensure_ascii=False, separators=(",", ":")).encode()
    assert set(old_card) == {"package_id", "name", "version", "summary", "installation_scope", "application_scope",
                             "assets", "asset_counts", "digest", "enabled"}
    reference = {"kind": "capability_package", "stable_id": "capability:owner-pack", "name": "owner-pack",
                 "source": "capability_package", "content_sha256": base.package_sha256,
                 "package_id": "owner-pack", "activation_id": base.activation_id}
    assert base.to_ref() == reference
    projected = replace(base, mcp_runtime=runtime, mcp_tool_names=names)
    assert projected.to_ref() == reference
    assert projected.tools_required == ()
    assert projected.read() == b"# Capability\n"
    expected = old_card if runtime == "none" else {**old_card, "mcp_runtime": runtime, "mcp_tool_count": len(names)}
    assert projected.to_card() == expected
    assert projected.to_detail() == {**expected, "description": base.description, "keywords": list(base.keywords),
                                      "entry_document": base.entry_document}


@pytest.mark.parametrize("runtime,names", [("unknown", ()), ("", ()), (None, ()),
                                           ([], ()), ({}, ()),
                                           ("none", ("tool",)), ("owner_unsupported", ("tool",)),
                                           ("on_demand", []), ("on_demand", "tool"), ("on_demand", None),
                                           ("on_demand", ("",)), ("on_demand", (" ",)),
                                           ("on_demand", (None,)), ("on_demand", ([],)),
                                           ("on_demand", ("tool", "tool"))])
def test_snapshot_rejects_runtime_states_or_names_outside_on_demand(tmp_path, runtime, names):
    base = _v7_snapshot(tmp_path)
    with pytest.raises(ValueError):
        replace(base, mcp_runtime=runtime, mcp_tool_names=names)


@pytest.mark.parametrize("scope", ["private", "public"])
def test_built_v10_real_store_projects_local_main_names_and_only_document_members(tmp_path, scope):
    from agent_py_agent.agent.capability_package_manifest import CapabilityFile
    from agent_py_agent.agent.plugin_entry import PluginFile
    from agent_py_agent.agent.plugin_files_environment import inspect_plugin_files
    from agent_py_agent.agent.plugin_runtime import plugin_tool_name

    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    built, contents = _build()
    row = _install(store, built, scope=scope)
    assert _project_disabled(owner, scope) == ()
    row = _enable(store, row)
    assert store.snapshot() == (row,)
    inventory = list_installed_capability_packages(owner.home_dir, include_disabled=False)
    expected_id = "public/owner-pack" if scope == "public" else "owner-pack"
    assert [item["package_id"] for item in inventory] == [expected_id]
    snapshot = _project(owner, scope)
    assert snapshot.package_id == expected_id
    assert snapshot.mcp_runtime == "on_demand"
    assert snapshot.mcp_tool_names == tuple(plugin_tool_name(row.manifest.plugin_id, tool.name) for tool in row.manifest.tools)
    assert snapshot.tools_required == ()
    assert snapshot.to_card()["mcp_tool_count"] == 2
    assert inventory[0]["assets"] == snapshot.to_card()["assets"]
    assert inventory[0]["assets"]["scripts"] == ["helper"], "runtime 脚本不能进入文档资产目录"
    documents = {path for path in contents if not path.startswith("mcp/checks/")}
    assert {item.path for item in snapshot.members} == documents
    assert all(isinstance(item, CapabilityFile) for item in snapshot.members)
    for path in documents:
        assert snapshot.read(path) == contents[path]
        assert read_capability_member(store, row, path) == contents[path]
    runtime_files = inspect_plugin_files(inspect_plugin_package(store.package_bytes(row)))
    assert {item.declaration.path: item.content for item in runtime_files} == {
        "server.py": contents["mcp/checks/server.py"], "assets/policy.txt": contents["mcp/checks/assets/policy.txt"],
        "scripts/helper.py": contents["mcp/checks/scripts/helper.py"]}
    assert all(isinstance(item.declaration, PluginFile) for item in runtime_files)
    assert {item.path for item in row.manifest.runtime_files} == {item.declaration.path for item in runtime_files}
    for path in ("mcp/checks/server.py", "mcp/checks/assets/policy.txt", "mcp/checks/scripts/helper.py", "server.py"):
        assert snapshot.resolve(path) is None
        with pytest.raises(ValueError, match="CAPABILITY_RESOURCE_NOT_AVAILABLE"):
            snapshot.read(path)
        with pytest.raises(ValueError):
            read_capability_member(store, row, path)
    assert not (store.root / "environments").exists(), "构建、发现和只读不能偷偷准备或启动 runtime"


def _project_disabled(owner, scope):
    return enabled_public_capability_packages(owner) if scope == "public" else enabled_capability_packages(owner)


@pytest.mark.parametrize("identity", [OwnerIdentity.local_user("reader"),
                                      OwnerIdentity.provider_user("feishu", "reader"),
                                      OwnerIdentity.provider_group("feishu", "readers")])
def test_public_runtime_pack_is_docs_only_for_each_non_main_owner(tmp_path, identity):
    from agent_py_agent.agent.plugin_runtime import plugin_tool_name

    main = resolve_owner_home(tmp_path)
    store = PluginInstallStore(main)
    built, contents = _build()
    row = _enable(store, _install(store, built, scope="public"))
    private, _ = _build("main-private", runtime=False)
    _enable(store, _install(store, private))
    consumer = resolve_owner_home(tmp_path, identity)
    assert enabled_capability_packages(consumer) == ()
    inventory = list_installed_capability_packages(consumer.home_dir)
    assert [item["package_id"] for item in inventory] == ["public/owner-pack"]
    snapshot = _project(consumer, "public")
    assert snapshot.mcp_runtime == "owner_unsupported"
    assert snapshot.mcp_tool_names == () and snapshot.tools_required == ()
    assert snapshot.to_card()["mcp_runtime"] == "owner_unsupported"
    assert snapshot.to_card()["mcp_tool_count"] == 0
    assert {item.path for item in snapshot.members} == {path for path in contents if not path.startswith("mcp/checks/")}
    assert snapshot.read() == contents["CAPABILITY.md"]
    assert "mcp_tool_names" not in snapshot.to_ref()
    assert all(plugin_tool_name(row.manifest.plugin_id, tool.name) not in repr(snapshot.to_detail())
               for tool in row.manifest.tools)
    with pytest.raises(ValueError, match="CAPABILITY_RESOURCE_NOT_AVAILABLE"):
        snapshot.read("mcp/checks/server.py")
    assert not PluginInstallStore(consumer).snapshot(), "公共文档投影不把 main 的安装复制给消费 owner"


def test_private_non_main_runtime_projection_does_not_gain_main_tool_names(tmp_path):
    owner = resolve_owner_home(tmp_path, OwnerIdentity.local_user("reader"))
    store = PluginInstallStore(owner)
    built, _ = _build()
    _enable(store, _install(store, built))
    snapshot = _project(owner, "private")
    assert snapshot.mcp_runtime == "owner_unsupported"
    assert snapshot.mcp_tool_names == () and snapshot.tools_required == ()
    assert snapshot.read() == b"# Capability\n"


def test_public_v7_projection_keeps_legacy_card_for_non_main_owner(tmp_path):
    """仅 v10 的执行能力被裁剪；共享内容包的旧卡片与文档入口不变。"""
    main = resolve_owner_home(tmp_path)
    store = PluginInstallStore(main)
    built, contents = _build(runtime=False)
    _enable(store, _install(store, built, scope="public"))
    consumer = resolve_owner_home(tmp_path, OwnerIdentity.local_user("reader"))
    public = _project(consumer, "public")
    assert public.mcp_runtime == "none" and public.mcp_tool_names == ()
    assert public.to_card() == _project(main, "public").to_card()
    assert "mcp_runtime" not in public.to_card() and "mcp_tool_count" not in public.to_card()
    assert public.read() == contents["CAPABILITY.md"]


@pytest.mark.parametrize("runtime", [False, True])
def test_v7_and_v10_frozen_document_readers_reject_revocation(tmp_path, runtime):
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    built, _ = _build(runtime=runtime)
    row = _enable(store, _install(store, built))
    snapshot = _project(owner, "private")
    assert snapshot.read() == b"# Capability\n"
    store.change_activation(PluginActivationRequest("disable", row.revision, replace(row.activation, phase="revoked")))
    assert enabled_capability_packages(owner) == ()
    with pytest.raises(PluginInstallationError) as error:
        snapshot.read()
    assert error.value.reason == "activation_unavailable"


def test_pinned_projection_uses_persisted_catalog_not_the_empty_manifest_tools(tmp_path):
    """W1 只验证记录与读侧；显式用真实 DTO 编码持久行，不冒充 W2 的 pin/握手流程。"""
    from agent_py_agent.agent import plugin_activation_record
    from agent_py_agent.agent.plugin_installation import PluginCommitReceipt
    from agent_py_agent.agent.plugin_runtime import plugin_tool_name

    api = tuple(getattr(plugin_activation_record, name, None) for name in (
        "catalog_digest_of", "pinned_catalog_json_from_tools", "pinned_tool_declarations"))
    assert all(callable(function) for function in api), "W1 固定目录 API 尚未实现"
    catalog_digest_of, pinned_catalog_json_from_tools, pinned_tool_declarations = api
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    built, _ = _build(pinned=True)
    installed = _install(store, built)
    assert installed.manifest.tools == ()
    plan = PluginEnvironmentPlan("enable-pin", installed.manifest.plugin_id, installed.package_sha256,
                                 installed.revision, installed.settings_revision, "a" * 64)
    prepared = store.change_activation(PluginActivationRequest("enable-pin", installed.revision,
                                                               PluginActivation(plan, "preparing"))).installation
    infos = [SimpleNamespace(name=name, description="固定工具 " + name,
                             input_schema={"type": "object", "properties": {}}) for name in ("zeta", "alpha")]
    pin = pinned_catalog_json_from_tools(infos)
    tools = pinned_tool_declarations(pin)
    activation = PluginActivation(plan, "active", catalog_digest_of(tools), pinned_catalog_json=pin)
    request = PluginActivationRequest("enable-pin", prepared.revision, activation)
    receipt = PluginCommitReceipt(request.operation_id, request.input_digest, built.package_id, built.sha256,
                                  prepared.revision, prepared.revision + 1, "activate",
                                  activation_sha256=activation.content_sha256)
    active = replace(prepared, revision=receipt.after_revision, last_commit=receipt, activation=activation)
    # 写入限于本测试 tmp_path；序列化由产品 DTO 完成，读取由原 Store 完成，无假 snapshot 返回。
    table = store.root / "installations.json"
    table.write_text(encode_installation_state((active,), owner.identity, "null"), encoding="utf-8")
    assert store.snapshot() == (active,)
    snapshot = _project(owner, "private")
    assert snapshot.mcp_runtime == "on_demand"
    assert snapshot.mcp_tool_names == tuple(plugin_tool_name(built.package_id, tool.name) for tool in tools)
    assert snapshot.to_card()["mcp_tool_count"] == 2
    assert snapshot.tools_required == ()
    consumer = resolve_owner_home(tmp_path, OwnerIdentity.provider_user("feishu", "reader"))
    assert enabled_capability_packages(consumer) == ()
    assert enabled_public_capability_packages(consumer) == ()


@pytest.mark.parametrize("scope", ["private", "public"])
def test_one_hundred_runtime_packs_decode_the_table_once_per_listing(tmp_path, monkeypatch, scope):
    """计数 wrapper 委托原解码器；不以时间阈值、假行或替换 snapshot 断言复杂度。"""
    from agent_py_agent.agent import plugin_install_store

    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    expected = set()
    for index in range(100):
        name = f"runtime-pack-{index:03d}"
        built, _ = _build(name)
        _enable(store, _install(store, built, scope=scope))
        expected.add(("public/" if scope == "public" else "") + name)
    decode = plugin_install_store.decode_installation_state
    calls = []

    def counted_decode(content, identity):
        calls.append(identity)
        return decode(content, identity)

    monkeypatch.setattr(plugin_install_store, "decode_installation_state", counted_decode)
    listed = list_installed_capability_packages(owner.home_dir, include_disabled=False)
    assert {item["package_id"] for item in listed} == expected
    assert len(listed) == 100
    assert calls == [owner.identity]
    calls.clear()
    snapshots = _project_disabled(owner, scope)
    assert {item.package_id for item in snapshots} == expected
    assert len(snapshots) == 100
    assert all(item.mcp_runtime == "on_demand" and len(item.mcp_tool_names) == 2 for item in snapshots)
    assert calls == [owner.identity], "构造 100 份快照不能为每包再解码安装表"
    if scope == "public":
        calls.clear()
        consumer = resolve_owner_home(tmp_path, OwnerIdentity.local_user("reader"))
        snapshots = enabled_public_capability_packages(consumer)
        assert {item.package_id for item in snapshots} == expected
        assert all(item.mcp_runtime == "owner_unsupported" and item.mcp_tool_names == () for item in snapshots)
        assert calls == [owner.identity], "公共投影也只能读一次 main 表，不能逐包重读"


def _assert_table_roundtrip(store, owner, expected):
    raw = (store.root / "installations.json").read_bytes()
    decoded = decode_installation_state(raw, owner.identity)
    assert decoded.entries == expected
    assert store.snapshot() == expected
    assert encode_installation_state(decoded.entries, owner.identity, decoded.migration_json).encode("utf-8") == raw


def test_same_id_v7_to_v10_then_v7_updates_leave_the_whole_table_decodable(tmp_path):
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    old, _ = _build("changing-pack", runtime=False)
    new, _ = _build("changing-pack", runtime=True, version="2.0.0")
    unrelated, _ = _build("unrelated-pack", runtime=False)
    sidecar = _enable(store, _install(store, unrelated))
    initial = _install(store, old)
    _assert_table_roundtrip(store, owner, (sidecar, initial))
    forward = PluginUpdateRequest(old.package_id, inspect_plugin_package(new.payload), "update-to-runtime", initial.revision)
    updated = store.update_package(forward)
    assert updated.outcome == "updated" and updated.commit_state == "committed"
    assert updated.installation.manifest.is_capability_pack
    assert updated.installation.manifest.is_content_only is False
    assert updated.installation.manifest.has_mcp_runtime
    assert updated.installation.revision == initial.revision + 1
    _assert_table_roundtrip(store, owner, (sidecar, updated.installation))
    assert store.update_package(forward).outcome == "replayed"
    backward = PluginUpdateRequest(old.package_id, inspect_plugin_package(old.payload), "update-back-to-content",
                                   updated.installation.revision)
    reverted = store.update_package(backward)
    assert reverted.outcome == "updated" and reverted.commit_state == "committed"
    assert reverted.installation.manifest.is_capability_pack
    assert reverted.installation.manifest.is_content_only
    assert reverted.installation.manifest.has_mcp_runtime is False
    assert reverted.installation.revision == updated.installation.revision + 1
    _assert_table_roundtrip(store, owner, (sidecar, reverted.installation))
    assert store.package_bytes(initial) == old.payload
    assert store.package_bytes(updated.installation) == new.payload
    assert store.package_bytes(reverted.installation) == old.payload
    assert [item.package_id for item in enabled_capability_packages(owner)] == ["unrelated-pack"]
