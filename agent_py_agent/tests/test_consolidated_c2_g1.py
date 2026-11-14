"""Wave 3 consolidated tests (test_consolidated_c2_g1).

- test_cpack_w1_activation_pin_record.py: W1 pin 子记录合同：真实 DTO/安装表 codec，不启动服务、不访问真实 home。
- test_cpack_w1_pack_projection_owner.py: W1 冻结包投影、文档/runtime 分区与 owner 边界；仅用 tmp_path，不启动进程。
- test_cpack_w1_predicate_sites.py: W1 包判据的行为横扫；真实 ZIP、原安装表和原投影，不执行包内文件。
- test_cpack_w2_confirmation_preview.py: 运行确认只绑定结构化事实；老插件预览字节不变，导入摘要不回显外部正文。
- test_cpack_w2_linked_grant.py: 文档授权联动工具，但父级快照、只读 preset 与精确工具表始终是上界。
- test_cpack_w4_import_receipt.py: W4 回执：顺序、路径、摘要及组件覆盖不变量。
- test_cpack_w4_import_walk_safety.py: W4 来源树安全与预算；Windows 无 FD 时必须明确拒绝而非路径降级。
- test_v15_w9_outbound_index.py: 外发索引：分片、期限、精确身份和重放的回归。
"""
from __future__ import annotations

import builtins
import copy
import gc
import hashlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import (
    asdict,
    dataclass,
    fields,
    replace,
)
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import (
    plugin_activation_record as record,
)
from agent_py_agent.agent import (
    plugin_installation as installation,
)
from agent_py_agent.agent import (
    plugin_installation_state as codec,
)
from agent_py_agent.agent import (
    plugin_manifest as manifest_module,
)
from agent_py_agent.agent import (
    plugin_runtime_facts as facts,
)
from agent_py_agent.agent.agent_core.orchestration.create_policy import _role_policy
from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
    ResolveCapabilityRequestsTool,
    _ResolveContext,
)
from agent_py_agent.agent.capability import external_plugin_import as imp
from agent_py_agent.agent.capability.external_plugin_import import (
    ExternalImportRequest,
    import_claude_plugin,
)
from agent_py_agent.agent.capability.learnpack_store import (
    BuildProvenance,
    LearnpackStore,
)
from agent_py_agent.agent.capability.package_build import (
    KIND_CAPABILITY_PACK,
    KIND_PLUGIN,
    BuiltPackage,
    build_capability_pack,
)
from agent_py_agent.agent.capability.package_provider import (
    enabled_capability_packages,
    enabled_public_capability_packages,
    list_installed_capability_packages,
    read_capability_member,
)
from agent_py_agent.agent.capability.package_snapshot import CapabilityPackageSnapshot
from agent_py_agent.agent.capability.skill_snapshot import (
    SkillSnapshot,
    SkillSnapshotError,
)
from agent_py_agent.agent.capability_package_manifest import CapabilityFile
from agent_py_agent.agent.conversation.outbound_message_index import OutboundMessageIndex
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import (
    PluginActivation,
    plugin_catalog_digest,
)
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_files_environment import inspect_plugin_files
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import (
    PluginInstallationError,
    PluginInstallRequest,
)
from agent_py_agent.agent.plugin_installation_state import (
    decode_installation_state,
    encode_installation_state,
)
from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.plugin_package import (
    PackageReadLimits,
    inspect_plugin_package,
)
from agent_py_agent.agent.plugin_runtime_facts import (
    PluginRuntimeFacts,
    confirmation_code,
    confirmation_details,
    confirmation_message,
)
from agent_py_agent.agent.plugin_update import PluginUpdateRequest
from agent_py_agent.agent.subagents.capability_scope import (
    DIRECT_PARENT_TOOL_AUTHORITY_ATTR,
    bind_creation_tool_authority,
    current_creation_tool_authority,
)
from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    resolve_owner_home,
)
from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import (
    _build,
    _enable,
    _service,
)
from agent_py_agent.tests.test_cpack_w4_import_synthesis import (
    bundled,
    fixture_tree,
    members,
    put,
    requires_fd,
)
from agent_py_agent.tests.test_plugin_management import manager

# === Source: agent_py_agent/tests/test_cpack_w1_activation_pin_record.py ===

def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _legacy_bytes(value):
    # 旧 content_sha256 刻意使用默认 ensure_ascii=True；不能换成 pin DTO 的 UTF-8 公式。
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _info(name="scan", description="检查目录", schema=None):
    return MCPToolInfo(name, description, {} if schema is None else deepcopy(schema))


def _row(name="scan", description="检查目录", schema=None, effect="dangerous"):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": {}} if schema is None else schema,
            "requested_effect": effect}


def _pin():
    return record.pinned_catalog_json_from_tools([_info()])


def _legacy_catalog_digest(tools):
    # anchor 四字段的独立公式；不使用生产 to_payload/asdict 自动吸收新字段。
    rows = [{"name": tool.name, "description": tool.description,
             "input_schema_json": tool.input_schema_json, "requested_effect": tool.requested_effect}
            for tool in sorted(tools, key=lambda item: item.name)]
    return hashlib.sha256(_legacy_bytes(rows)).hexdigest()


def _plan():
    return PluginEnvironmentPlan("enable-pin", "pin-pack", "a" * 64, 1, 0, "b" * 64)


def _manifest(mode="pinned_on_enable", *, version="plugin_package.v10", declared_effect="read_only"):
    """本文件自己的 v10/v6 样本，只有静态文件摘要，不物化或运行入口。"""
    tool = _row("declared", "静态工具", effect=declared_effect)
    entry = {"kind": "interpreter", "command": "server.py", "args": [], "interpreter": "python3"}
    common = {"plugin_id": "pin-pack", "version": "1.0.0", "summary": "目录锁定测试",
              "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False}}
    if version == "plugin_package.v6":
        value = {**common, "schema_version": version, "entry": entry,
                 "files": [{"path": "server.py", "sha256": "c" * 64, "executable": False}],
                 "platforms": ["any"], "actions": [], "default_action": "", "tools": [tool],
                 "panels": [], "skills": [], "host_api": []}
    else:
        value = {**common, "schema_version": version, "package_kind": "capability",
                 "capability": {"description": "目录锁定样本", "keywords": ["pin"],
                                "entry_document": "CAPABILITY.md"},
                 "files": [{"path": "CAPABILITY.md", "sha256": "d" * 64, "executable": False},
                           {"path": "mcp/server.py", "sha256": "c" * 64, "executable": False}]}
        if version == "plugin_package.v10":
            value["mcp_servers"] = [{"name": "pin-tools", "root": "mcp", "entry": entry,
                                     "platforms": ["any"], "permissions": {"network": False},
                                     "tool_catalog": mode, "tools": [tool] if mode == "declared" else []}]
    return manifest_module.PluginManifest.from_payload(value)


def _activation(pin, *, phase="active", digest=None):
    if digest is None:
        digest = record.catalog_digest_of(record.pinned_tool_declarations(pin))
    return record.PluginActivation(_plan(), phase, digest, pinned_catalog_json=pin)


def _installation(manifest, activation=None):
    """构造匹配原计划版本偏移、输入摘要及 activation_sha256 的真实安装记录。"""
    if activation is None:
        action, revision, operation_id, activation_sha = "install", 1, "install-pin", ""
    else:
        action = {"preparing": "prepare", "active": "activate", "revoked": "revoke"}[activation.phase]
        offset = {"preparing": 1, "active": 2, "revoked": 3 if activation.catalog_sha256 else 2}[activation.phase]
        revision = activation.plan.installation_revision + offset
        operation_id = "revoke-pin" if action == "revoke" else activation.plan.operation_id
        activation_sha = activation.content_sha256
    digest = installation.plugin_input_digest(action, manifest.plugin_id, "a" * 64, revision - 1,
                                              activation_sha256=activation_sha)
    receipt = installation.PluginCommitReceipt(operation_id, digest, manifest.plugin_id, "a" * 64,
                                                revision - 1, revision, action=action,
                                                activation_sha256=activation_sha)
    return installation.PluginInstallation(manifest, "a" * 64, revision, receipt, activation=activation)


def _table_bytes(entry):
    return codec.encode_installation_state((entry,), OwnerIdentity.local_main(), "null").encode("utf-8")


def _permission(activation, *, host=False):
    """合法旧授权作为阳性对照，避免用无效 {} 得到无意义的拒绝。"""
    from agent_py_agent.agent.plugin_permissions import host_grants

    value = {"kind": "plugin_legacy_permissions", "policy_version": 1, "mode": "restricted",
             "plugin_id": activation.plan.plugin_id, "package_version": "1.0.0",
             "installation_ref": "e" * 64, "package_sha256": activation.plan.package_sha256,
             "activation_id": activation.activation_id, "plan": asdict(activation.plan),
             "permissions": {"read_roots": [], "write_roots": [], "network": False, "program_roots": []},
             "entry_module": "", "entry": {"kind": "interpreter", "command": "server.py", "args": [],
                                             "interpreter": "python3"},
             "host_api": [], "sandbox_status": "platform_sandbox"}
    if not host:
        return _canonical(value), None
    capability = sorted(host_grants.host_capabilities())[0]
    grant = {**host_grants.default_selection((capability,)), "schema": "plugin_host_grant.v1",
             "grant_revision": 2, "installation_ref": value["installation_ref"],
             "package_sha256": value["package_sha256"], "activation_id": value["activation_id"]}
    return _canonical(value), _canonical(grant)


def test_normalizer_sorts_canonicalizes_and_never_trusts_readonly_annotations():
    raw_schema = {"properties": {"path": {"type": "string"}}, "additionalProperties": False}
    first = _info("z.last", "  读\x00取\n\t 目录\x7f ", raw_schema)
    # MCP 自报注解和任意 effect 属性不具有降级效力。
    first.annotations = {"readOnlyHint": True, "destructiveHint": False}
    first.requested_effect = "read_only"
    second = _info("A_first", "\x01\n\t \x7f")
    before = deepcopy(raw_schema)
    expected_schema = {**before, "type": "object"}
    expected = [_row("A_first", "工具 A_first"), _row("z.last", "读 取 目录", expected_schema)]
    text = record.pinned_catalog_json_from_tools([first, second])
    assert text == _canonical(expected)
    assert record.pinned_catalog_json_from_tools([second, first]) == text
    assert raw_schema == before
    assert "\\u76ee" not in text
    tools = record.pinned_tool_declarations(text)
    assert isinstance(tools, tuple)
    assert all(isinstance(tool, manifest_module.PluginToolDeclaration) for tool in tools)
    assert [(tool.name, tool.description, tool.requested_effect) for tool in tools] == [
        ("A_first", "工具 A_first", "dangerous"), ("z.last", "读 取 目录", "dangerous")]
    assert tools[1].input_schema == expected_schema
    assert all(tool.observation is None and tool.observation_ref is None for tool in tools)
    assert record.pinned_catalog_json_from_tools([
        MCPToolInfo(tool.name, tool.description, tool.input_schema) for tool in tools]) == text
    raw_schema["properties"]["path"]["type"] = "integer"
    exposed = tools[1].input_schema
    exposed["properties"]["path"]["type"] = "boolean"
    assert tools[1].input_schema == expected_schema


@pytest.mark.parametrize("code", [*range(32), 127])
def test_every_ascii_control_in_description_becomes_a_space(code):
    text = record.pinned_catalog_json_from_tools([_info(description=f" 左{chr(code)}  右 ")])
    assert json.loads(text) == [_row(description="左 右")]


@pytest.mark.parametrize("name", ["A", "_", "a.b-c_9", "A" + "x" * 63])
def test_tool_names_accept_the_manifest_boundary(name):
    text = record.pinned_catalog_json_from_tools([_info(name)])
    assert record.pinned_tool_declarations(text)[0].name == name


@pytest.mark.parametrize("name", ["", "1tool", "a/b", "a b", "中文", "a\n", "a\x00", "a" * 65, None, 7])
def test_tool_names_reject_invalid_values_with_stable_code(name):
    with pytest.raises(ValueError, match="^MCP_PACK_CATALOG_INVALID$"):
        record.pinned_catalog_json_from_tools([_info(name)])


@pytest.mark.parametrize("schema", [None, [], "object", {"type": "array"}, {"properties": []},
                                    {"properties": {"x": {"type": "unknown"}}},
                                    {"$ref": "#/missing"}, {"required": "path"},
                                    {"properties": {"x": {"maximum": float("nan")}}}])
def test_bad_schemas_are_catalog_errors_not_silently_empty_objects(schema):
    info = _info()
    info.input_schema = schema
    with pytest.raises(ValueError, match="^MCP_PACK_CATALOG_INVALID$"):
        record.pinned_catalog_json_from_tools([info])


def test_catalog_has_inclusive_row_limit_and_rejects_empty_or_duplicate_names():
    infos = [_info(f"t{index:02}") for index in range(64)]
    tools = record.pinned_tool_declarations(record.pinned_catalog_json_from_tools(infos[::-1]))
    assert tuple(tool.name for tool in tools) == tuple(info.name for info in infos)
    assert len(tools) == 64
    for invalid in ([], [_info("same"), _info("same", "不同描述")], [*infos, _info("t64")]):
        with pytest.raises(ValueError, match="^MCP_PACK_CATALOG_INVALID$"):
            record.pinned_catalog_json_from_tools(invalid)


@pytest.mark.parametrize("char", ["x", "界"])
def test_catalog_byte_budget_is_utf8_and_inclusive_at_262144(char):
    # 独立协议 oracle 填充到精确字节边界，不先调用生产 normalizer 倒推阈值。
    overhead = len(_canonical([_row("x", "")]).encode("utf-8"))
    available = 262144 - overhead
    count, remainder = divmod(available, len(char.encode("utf-8")))
    description = char * count + "x" * remainder
    expected = _canonical([_row("x", description)])
    assert len(expected.encode("utf-8")) == 262144
    text = record.pinned_catalog_json_from_tools([_info("x", description)])
    assert text == expected
    assert record.pinned_tool_declarations(text)[0].description == description
    with pytest.raises(ValueError, match="^MCP_PACK_CATALOG_INVALID$"):
        record.pinned_catalog_json_from_tools([_info("x", description + "x")])


def test_pin_is_last_optional_hidden_field_and_old_positional_constructor_keeps_meaning():
    declarations = fields(record.PluginActivation)
    assert tuple(item.name for item in declarations) == (
        "plan", "phase", "catalog_sha256", "permission_json", "host_api_grant_json", "pinned_catalog_json")
    assert declarations[-1].default is None
    assert declarations[-1].repr is False
    old = record.PluginActivation(_plan(), "active", "f" * 64, None, None)
    assert old.permission_json is None and old.host_api_grant_json is None
    assert old.pinned_catalog_json is None
    assert "pinned_catalog" not in old.to_payload()
    pinned = _activation(_pin())
    assert "pinned_catalog_json=" not in repr(pinned)
    assert "检查目录" not in repr(pinned)


@pytest.mark.parametrize("phase", ["active", "revoked"])
def test_canonical_pin_survives_activation_and_real_table_roundtrip(phase):
    pin = _pin()
    activation = _activation(pin, phase=phase)
    expected = {"plan": asdict(_plan()), "phase": phase, "catalog_sha256": activation.catalog_sha256,
                "pinned_catalog": json.loads(pin)}
    assert activation.to_payload() == expected
    restored = record.PluginActivation.from_payload(json.loads(_canonical(expected)))
    assert restored == activation
    assert restored.pinned_catalog_json.encode("utf-8") == pin.encode("utf-8")
    assert _canonical(restored.to_payload()).encode("utf-8") == _canonical(expected).encode("utf-8")
    assert restored.content_sha256 == hashlib.sha256(_legacy_bytes(expected)).hexdigest()
    assert restored.activation_id == activation.activation_id

    entry = _installation(_manifest(), activation)
    raw = _table_bytes(entry)
    decoded = codec.decode_installation_state(raw, OwnerIdentity.local_main())
    assert decoded.entries == (entry,)
    assert codec.encode_installation_state(decoded.entries, OwnerIdentity.local_main(),
                                          decoded.migration_json).encode("utf-8") == raw
    assert decoded.entries[0].enabled is (phase == "active")
    assert record.installation_tool_declarations(decoded.entries[0]) == record.pinned_tool_declarations(pin)


def test_payload_decoder_canonicalizes_mapping_key_order_without_changing_digest():
    pin = _pin()
    activation = _activation(pin)
    payload = activation.to_payload()
    payload["pinned_catalog"] = [dict(reversed(list(row.items()))) for row in json.loads(pin)]
    restored = record.PluginActivation.from_payload(payload)
    assert restored.pinned_catalog_json == pin
    assert restored.content_sha256 == activation.content_sha256


@pytest.mark.parametrize("phase,digest", [("preparing", ""), ("active", ""), ("revoked", ""),
                                         ("preparing", "f" * 64), ("pending", "f" * 64)])
def test_pin_requires_published_or_revoked_phase_and_nonempty_digest(phase, digest):
    control = _activation(_pin())
    assert control.phase == "active" and control.catalog_sha256
    with pytest.raises(ValueError):
        replace(control, phase=phase, catalog_sha256=digest)


@pytest.mark.parametrize("host,carried", [(False, False), (True, False), (True, True)])
def test_pin_never_coexists_with_valid_permission_or_host_grant(host, carried):
    unpinned = record.PluginActivation(_plan(), "active", "f" * 64)
    permission, grant = _permission(unpinned, host=host)
    if carried:
        permission = _canonical({**json.loads(permission), "host_api_grant": json.loads(grant)})
        grant = None
    # 没有 pin 的相同授权先须被真实 DTO 接受，排除授权本身无效的假阳性。
    control = replace(unpinned, permission_json=permission, host_api_grant_json=grant)
    assert control.permission_json is not None
    assert (control.host_api_grant_json is not None) is host
    with pytest.raises(ValueError):
        replace(control, pinned_catalog_json=_pin())
    payload = control.to_payload()
    payload["pinned_catalog"] = json.loads(_pin())
    with pytest.raises(ValueError):
        record.PluginActivation.from_payload(payload)


@pytest.mark.parametrize("variant", ["spaces", "ascii_escapes", "unsorted", "empty", "duplicate",
                                      "effect", "controls", "schema", "extra", "missing", "object"])
def test_direct_pin_must_be_a_byte_canonical_valid_catalog(variant):
    control = _activation(_canonical([_row()]))
    assert control.pinned_catalog_json == _canonical([_row()])
    rows = [_row()]
    if variant == "spaces":
        text = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    elif variant == "ascii_escapes":
        text = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    elif variant == "unsorted":
        text = _canonical([_row("z"), _row("a")])
    else:
        if variant == "empty":
            rows = []
        elif variant == "duplicate":
            rows = [_row(), _row()]
        elif variant == "effect":
            rows[0]["requested_effect"] = "read_only"
        elif variant == "controls":
            rows[0]["description"] = "目录\n说明"
        elif variant == "schema":
            rows[0]["input_schema"] = {}
        elif variant == "extra":
            rows[0]["observation"] = None
        elif variant == "missing":
            del rows[0]["description"]
        elif variant == "object":
            rows = {"tools": rows}
        text = _canonical(rows)
    with pytest.raises(ValueError):
        record.PluginActivation(_plan(), "active", "f" * 64, pinned_catalog_json=text)


@pytest.mark.parametrize("mode,version", [("declared", "plugin_package.v10"),
                                         ("declared", "plugin_package.v6"),
                                         ("declared", "plugin_package.v7")])
def test_pin_is_not_allowed_on_declared_or_non_runtime_manifests(mode, version):
    manifest = _manifest(mode, version=version, declared_effect="dangerous")
    if version != "plugin_package.v7":
        # 先证明该 manifest 的无 pin active 记录有效，排除旧 digest 拒绝掩盖模式检查。
        plain = record.PluginActivation(_plan(), "active", record.plugin_catalog_digest(manifest))
        assert _installation(manifest, plain).enabled
        pin = record.pinned_catalog_json_from_tools([
            MCPToolInfo(tool.name, tool.description, tool.input_schema) for tool in manifest.tools])
        assert record.catalog_digest_of(record.pinned_tool_declarations(pin)) == record.plugin_catalog_digest(manifest)
    else:
        pin = _pin()
    # 相同 pin 在 pinned 模式的 v10 阳性表可接受；不得因未知版本或拒绝所有 pin 而伪绿。
    accepted = _installation(_manifest(), _activation(pin))
    assert codec.decode_installation_state(_table_bytes(accepted), OwnerIdentity.local_main()).entries == (accepted,)
    # 两条目录摘要也相同，不能靠错误模式下的 digest mismatch 代替模式检查。
    activation = _activation(pin, digest=record.plugin_catalog_digest(manifest))
    with pytest.raises(ValueError):
        _installation(manifest, activation)


@pytest.mark.parametrize("phase", ["active", "revoked"])
def test_one_byte_pin_change_fails_decode_even_with_resealed_receipt(tmp_path, phase):
    pin = record.pinned_catalog_json_from_tools([_info(description="catalog A")])
    original = _table_bytes(_installation(_manifest(), _activation(pin, phase=phase)))
    changed = original.replace(b"catalog A", b"catalog B")
    assert len(changed) == len(original)
    assert sum(left != right for left, right in zip(original, changed)) == 1
    owner = OwnerIdentity.local_main()
    with pytest.raises(ValueError):
        codec.decode_installation_state(changed, owner)

    payload = json.loads(changed)
    row = payload["installations"][0]
    receipt = row["last_commit"]
    # 只留下旧 catalog_sha256，不让外层 activation/receipt 内容摘要先拒绝而掩盖 F2。
    receipt["activation_sha256"] = hashlib.sha256(_legacy_bytes(row["activation"])).hexdigest()
    receipt["input_digest"] = installation.plugin_input_digest(
        receipt["action"], receipt["plugin_id"], receipt["package_sha256"], receipt["before_revision"],
        activation_sha256=receipt["activation_sha256"])
    path = tmp_path / "installations.json"
    path.write_bytes((_canonical(payload) + "\n").encode("utf-8"))
    before = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises(ValueError):
        codec.decode_installation_state(path.read_bytes(), owner)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("phase", ["preparing", "active", "revoked"])
@pytest.mark.parametrize("grant_kind", ["none", "permission", "host"])
def test_anchor_activation_bytes_and_content_sha_are_unchanged(phase, grant_kind):
    old_payload = {"plan": asdict(_plan()), "phase": phase,
                   "catalog_sha256": "" if phase == "preparing" else "f" * 64}
    base = record.PluginActivation.from_payload(deepcopy(old_payload))
    if grant_kind != "none":
        permission, host = _permission(base, host=grant_kind == "host")
        old_payload["permission_grant"] = json.loads(permission)
        if host is not None:
            old_payload["host_api_grant"] = json.loads(host)
    expected = _legacy_bytes(old_payload)
    restored = record.PluginActivation.from_payload(json.loads(expected))
    assert _legacy_bytes(restored.to_payload()) == expected
    assert restored.content_sha256 == hashlib.sha256(expected).hexdigest()
    assert restored.pinned_catalog_json is None
    assert "pinned_catalog" not in restored.to_payload()
    assert record.PluginActivation.from_payload(restored.to_payload()) == restored


def test_anchor_unpinned_installation_table_keeps_bytes_and_activation_digest(tmp_path):
    manifest = _manifest(version="plugin_package.v6")
    tools_digest = _legacy_catalog_digest(manifest.tools)
    old_activation_payload = {"plan": asdict(_plan()), "phase": "active", "catalog_sha256": tools_digest}
    old_content_digest = hashlib.sha256(_legacy_bytes(old_activation_payload)).hexdigest()
    old_activation = record.PluginActivation.from_payload(deepcopy(old_activation_payload))
    entry = _installation(manifest, old_activation)
    # 表格式是 anchor v5；显式写旧字段，而非从增长中的 activation dataclass 生成期望。
    row = {"manifest": manifest.to_payload(), "package_sha256": "a" * 64, "revision": 3,
           "last_commit": asdict(entry.last_commit), "settings_json": None, "settings_revision": 0,
           "activation": old_activation_payload, "legacy_permission_grant": None,
           "installation_scope": "private"}
    expected = (_canonical({"schema_version": "plugin_installations.v5", "owner": asdict(OwnerIdentity.local_main()),
                            "installations": [row], "migration": None}) + "\n").encode("utf-8")
    path = tmp_path / "anchor-installations.json"
    path.write_bytes(expected)
    before = path.read_bytes(), path.stat().st_mtime_ns
    state = codec.decode_installation_state(path.read_bytes(), OwnerIdentity.local_main())
    assert state.entries == (entry,)
    assert state.entries[0].activation.content_sha256 == old_content_digest
    assert codec.encode_installation_state(state.entries, OwnerIdentity.local_main(),
                                          state.migration_json).encode("utf-8") == expected
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_catalog_digest_excludes_none_fields_but_keeps_every_non_none_field():
    tools = record.pinned_tool_declarations(_pin())
    assert record.catalog_digest_of(tools) == _legacy_catalog_digest(tools)
    assert record.catalog_digest_of(()) == hashlib.sha256(b"[]").hexdigest()

    @dataclass(frozen=True)
    class FutureTool(manifest_module.PluginToolDeclaration):
        future_optional: str | None = None

    base = tools[0]
    future = FutureTool(base.name, base.description, base.input_schema_json, base.requested_effect)
    assert record.catalog_digest_of((future,)) == record.catalog_digest_of(tools)
    valued = replace(future, future_optional="declared")
    rows = [{"name": valued.name, "description": valued.description,
             "input_schema_json": valued.input_schema_json, "requested_effect": valued.requested_effect,
             "future_optional": "declared"}]
    assert record.catalog_digest_of((valued,)) == hashlib.sha256(_legacy_bytes(rows)).hexdigest()
    assert record.catalog_digest_of((valued,)) != record.catalog_digest_of(tools)

    plain = replace(base, requested_effect="read_only")
    observed = replace(plain, observation=manifest_module.PluginToolObservation("page", 50))
    assert record.catalog_digest_of((observed,)) != record.catalog_digest_of((plain,))
    expected_rows = [{"name": observed.name, "description": observed.description,
                      "input_schema_json": observed.input_schema_json, "requested_effect": "read_only",
                      "observation": asdict(observed.observation)}]
    assert record.catalog_digest_of((observed,)) == hashlib.sha256(_legacy_bytes(expected_rows)).hexdigest()

    another = replace(base, name="another")
    assert record.catalog_digest_of((base, another)) == record.catalog_digest_of((another, base))
    manifest = _manifest(version="plugin_package.v6")
    assert record.plugin_catalog_digest(manifest) == record.catalog_digest_of(manifest.tools)
    assert record.plugin_catalog_digest(manifest) == _legacy_catalog_digest(manifest.tools)


@pytest.mark.parametrize("version", ["plugin_package.v6", "plugin_package.v10"])
def test_declared_catalog_helpers_use_manifest_tools_before_and_after_activation(version):
    manifest = _manifest("declared", version=version)
    disabled = _installation(manifest)
    expected_digest = _legacy_catalog_digest(manifest.tools)
    assert record.installation_tool_declarations(disabled) == manifest.tools
    assert record.activation_catalog_digest(manifest, None) == expected_digest
    active = record.PluginActivation(_plan(), "active", expected_digest)
    enabled = _installation(manifest, active)
    assert record.installation_tool_declarations(enabled) == manifest.tools
    assert record.activation_catalog_digest(manifest, active) == expected_digest
    decoded = codec.decode_installation_state(_table_bytes(enabled), OwnerIdentity.local_main())
    assert record.installation_tool_declarations(decoded.entries[0]) == manifest.tools


def test_pinned_catalog_helpers_have_no_tools_before_pin_and_use_persisted_pin_afterward():
    manifest = _manifest()
    assert manifest.tools == ()
    disabled = _installation(manifest)
    preparing = record.PluginActivation(_plan(), "preparing")
    pending = _installation(manifest, preparing)
    for entry in (disabled, pending):
        assert record.installation_tool_declarations(entry) == ()
        assert record.activation_catalog_digest(manifest, entry.activation) == hashlib.sha256(b"[]").hexdigest()
    pin = _pin()
    expected_tools = record.pinned_tool_declarations(pin)
    expected_digest = _legacy_catalog_digest(expected_tools)
    active = _activation(pin)
    assert record.activation_catalog_digest(manifest, active) == expected_digest
    assert expected_digest != record.plugin_catalog_digest(manifest)
    enabled = _installation(manifest, active)
    assert record.installation_tool_declarations(enabled) == expected_tools
    revoked = _installation(manifest, replace(active, phase="revoked"))
    assert record.installation_tool_declarations(revoked) == expected_tools
    assert record.activation_catalog_digest(manifest, revoked.activation) == expected_digest


@pytest.mark.parametrize("phase", ["preparing", "active"])
def test_real_table_decoder_rejects_pin_on_preparing_or_declared_catalog(phase):
    # 阳性表先解码，再只增加 pin 并重算外层回执；其余绑定仍与原记录一致。
    manifest = _manifest("declared", declared_effect="dangerous") if phase == "active" else _manifest()
    digest = record.plugin_catalog_digest(manifest) if phase == "active" else ""
    plain = record.PluginActivation(_plan(), phase, digest)
    raw = _table_bytes(_installation(manifest, plain))
    assert codec.decode_installation_state(raw, OwnerIdentity.local_main()).entries[0].activation == plain
    payload = json.loads(raw)
    row = payload["installations"][0]
    pin = (record.pinned_catalog_json_from_tools([
        MCPToolInfo(tool.name, tool.description, tool.input_schema) for tool in manifest.tools])
        if phase == "active" else _pin())
    if phase == "active":
        assert record.catalog_digest_of(record.pinned_tool_declarations(pin)) == digest
    row["activation"]["pinned_catalog"] = json.loads(pin)
    receipt = row["last_commit"]
    receipt["activation_sha256"] = hashlib.sha256(_legacy_bytes(row["activation"])).hexdigest()
    receipt["input_digest"] = installation.plugin_input_digest(
        receipt["action"], receipt["plugin_id"], receipt["package_sha256"], receipt["before_revision"],
        activation_sha256=receipt["activation_sha256"])
    with pytest.raises(ValueError):
        codec.decode_installation_state((_canonical(payload) + "\n").encode("utf-8"), OwnerIdentity.local_main())


@pytest.mark.parametrize("infos", [None, "scan", [object()], [{"name": "scan"}]])
def test_malformed_discovery_objects_use_catalog_error(infos):
    with pytest.raises(ValueError, match="^MCP_PACK_CATALOG_INVALID$"):
        record.pinned_catalog_json_from_tools(infos)


@pytest.mark.parametrize("description", [None, 1, [], "\ud800"])
def test_invalid_description_or_utf8_is_a_catalog_error(description):
    info = _info()
    info.description = description
    with pytest.raises(ValueError, match="^MCP_PACK_CATALOG_INVALID$"):
        record.pinned_catalog_json_from_tools([info])


@pytest.mark.parametrize("variant", ["duplicate_key", "whitespace", "schema", "effect", "extra", "utf8"])
def test_pinned_declaration_reader_itself_rejects_noncanonical_catalog(variant):
    pin = _pin()
    assert record.pinned_tool_declarations(pin)[0].name == "scan"
    rows = json.loads(pin)
    if variant == "duplicate_key":
        invalid = pin.replace('"name":"scan"', '"name":"scan","name":"scan"')
    elif variant == "whitespace":
        invalid = pin + "\n"
    elif variant == "utf8":
        invalid = pin.replace("检查目录", "\ud800")
    else:
        if variant == "schema":
            rows[0]["input_schema"] = {}
        elif variant == "effect":
            rows[0]["requested_effect"] = "read_only"
        else:
            rows[0]["observation_ref"] = None
        invalid = _canonical(rows)
    with pytest.raises(ValueError, match="^MCP_PACK_CATALOG_INVALID$"):
        record.pinned_tool_declarations(invalid)


@pytest.mark.parametrize("code,category,retryable,action", [
    ("PACK_MCP_DISABLED", "configuration", False, "report_blocker"),
    ("PACK_MCP_OWNER_UNSUPPORTED", "configuration", False, "report_blocker"),
    ("MCP_PACK_CATALOG_INVALID", "tool", False, "change_strategy"),
    ("MCP_PACK_CATALOG_DRIFT", "tool", False, "request_user_input"),
    ("MCP_PACK_SERVER_UNAVAILABLE", "tool", False, "change_strategy"),
    ("PACKAGE_BUILD_MCP_INVALID", "tool", True, "change_strategy"),
    ("PACKAGE_BUILD_MCP_SERVER_LIMIT", "tool", False, "change_strategy"),
    ("PLUGIN_IMPORT_SOURCE_INVALID", "tool", False, "change_strategy"),
    ("PLUGIN_IMPORT_PATH_UNSAFE", "tool", False, "change_strategy"),
    ("PLUGIN_IMPORT_MANIFEST_INVALID", "tool", False, "change_strategy"),
    ("PLUGIN_IMPORT_ID_INVALID", "tool", False, "change_strategy"),
    ("PLUGIN_IMPORT_ID_CONFLICT", "state", False, "change_strategy"),
    ("PLUGIN_IMPORT_LIMIT_EXCEEDED", "tool", False, "change_strategy"),
    ("PLUGIN_IMPORT_NO_CONTENT", "tool", False, "change_strategy"),
])
def test_w1_registered_errors_keep_exact_recovery_contract(code, category, retryable, action):
    from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS, error_contract

    contract = ERROR_CONTRACTS[code]
    assert error_contract(code) is contract
    assert (contract.code, contract.category, contract.retryable, contract.recommended_action) == (
        code, category, retryable, action)
    assert any("\u4e00" <= char <= "\u9fff" for char in contract.recovery_hint)


# === Source: agent_py_agent/tests/test_cpack_w1_pack_projection_owner.py ===

def _build_w1_proj(name="owner-pack", *, runtime=True, version="1.0.0", pinned=False):
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


def _install_w1_proj(store, built, *, scope="private", operation=None):
    package = inspect_plugin_package(built.payload)
    return store.install(PluginInstallRequest(package, operation or f"install-{built.package_id}", 0,
                                            installation_scope=scope)).installation


def _enable_w1_proj(store, row):
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
    built, _contents = _build_w1_proj(runtime=False)
    _enable_w1_proj(store, _install_w1_proj(store, built))
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
    built, contents = _build_w1_proj()
    row = _install_w1_proj(store, built, scope=scope)
    assert _project_disabled(owner, scope) == ()
    row = _enable_w1_proj(store, row)
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
    built, contents = _build_w1_proj()
    row = _enable_w1_proj(store, _install_w1_proj(store, built, scope="public"))
    private, _ = _build_w1_proj("main-private", runtime=False)
    _enable_w1_proj(store, _install_w1_proj(store, private))
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
    built, _ = _build_w1_proj()
    _enable_w1_proj(store, _install_w1_proj(store, built))
    snapshot = _project(owner, "private")
    assert snapshot.mcp_runtime == "owner_unsupported"
    assert snapshot.mcp_tool_names == () and snapshot.tools_required == ()
    assert snapshot.read() == b"# Capability\n"


def test_public_v7_projection_keeps_legacy_card_for_non_main_owner(tmp_path):
    """仅 v10 的执行能力被裁剪；共享内容包的旧卡片与文档入口不变。"""
    main = resolve_owner_home(tmp_path)
    store = PluginInstallStore(main)
    built, contents = _build_w1_proj(runtime=False)
    _enable_w1_proj(store, _install_w1_proj(store, built, scope="public"))
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
    built, _ = _build_w1_proj(runtime=runtime)
    row = _enable_w1_proj(store, _install_w1_proj(store, built))
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
    built, _ = _build_w1_proj(pinned=True)
    installed = _install_w1_proj(store, built)
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
        built, _ = _build_w1_proj(name)
        _enable_w1_proj(store, _install_w1_proj(store, built, scope=scope))
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
    old, _ = _build_w1_proj("changing-pack", runtime=False)
    new, _ = _build_w1_proj("changing-pack", runtime=True, version="2.0.0")
    unrelated, _ = _build_w1_proj("unrelated-pack", runtime=False)
    sidecar = _enable_w1_proj(store, _install_w1_proj(store, unrelated))
    initial = _install_w1_proj(store, old)
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


# === Source: agent_py_agent/tests/test_cpack_w1_predicate_sites.py ===

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


def _install_w1_pred(store, version, name):
    package = inspect_plugin_package(_archive(version, name))
    return store.install(PluginInstallRequest(package, f"install-{name}", 0)).installation


def _enable_w1_pred(store, row):
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
    row = _install_w1_pred(store, version, f"sample-v{version}")
    manifest = row.manifest
    assert manifest.is_capability_pack is pack
    assert manifest.is_content_only is content
    assert manifest.has_mcp_runtime is runtime
    active = _enable_w1_pred(store, row)
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
    rows = {version: _install_w1_pred(store, version, f"site-v{version}") for version in (7, 10, 6)}
    assert {item["package_id"] for item in list_installed_capability_packages(owner.home_dir)} == {"site-v7", "site-v10"}
    assert list_installed_capability_packages(owner.home_dir, include_disabled=False) == []
    assert enabled_capability_packages(owner) == ()
    for version, row in rows.items():
        rows[version] = _enable_w1_pred(store, row)
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
        _install_w1_pred(store, version, f"map-v{version}")
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


# === Source: agent_py_agent/tests/test_cpack_w2_confirmation_preview.py ===

@pytest.mark.parametrize("mode", ["declared", "pinned_on_enable"])
def test_pack_facts_are_pure_runtime_only_and_have_frozen_dto(mode, monkeypatch):
    manifest = _build(mode).manifest
    builder = getattr(facts, "pack_mcp_confirmation_facts", None)
    assert callable(builder), "缺少运行包确认事实构造器"
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", lambda *a, **kw: calls.append("open"))
        patch.setattr(os, "stat", lambda *a, **kw: calls.append("stat"))
        result = builder(manifest, idle_stop_seconds=0)
    assert calls == []
    assert set(result) == {"kind", "package_id", "server", "entry", "files", "network", "workspace_access",
                           "data_dir", "tool_catalog", "per_call_approval", "idle_stop_seconds"}
    assert result["kind"] == "capability_pack_mcp" and result["package_id"] == "w2-pack"
    assert result["server"] == "checks" and result["entry"] == manifest.entry.to_payload()
    assert [item["path"] for item in result["files"]] == ["server.py"]
    assert result["network"] is False and result["workspace_access"] == "none"
    assert result["data_dir"] == "plugins/data/w2-pack"
    assert result["per_call_approval"] == "dangerous" and result["idle_stop_seconds"] == 0
    assert result["tool_catalog"] == {"mode": mode, "tools": ["inspect"] if mode == "declared" else []}


@pytest.mark.parametrize("mode,idle", [("declared", 600), ("pinned_on_enable", 0)])
def test_real_preview_binds_runtime_file_sizes_and_plain_chinese_message(tmp_path, mode, idle):
    from agent_py_agent.agent.plugin_management import PluginManagement

    service, _ = _service(tmp_path, mode=mode)
    service = PluginManagement(replace(service.context, capability_pack_mcp_idle_stop_seconds=idle))
    reply = _enable(service)
    details = reply["details"]["confirmation"]
    assert details["files"][0]["size"] == len(inspect_plugin_files(_build(mode))[0].content)
    assert details["confirm_code"] == confirmation_code({k: v for k, v in details.items() if k != "confirm_code"})
    assert details["interpreter"]["path"] and len(details["interpreter"]["sha256"]) == 64
    message = confirmation_message(details)
    assert "MCP" in message and "server.py" in message and details["interpreter"]["path"] in message
    assert "禁止联网" in message and "工作区" in message and "不能读写" in message
    assert "plugins/data/w2-pack" in message and "强制" in message and "沙箱" in message
    assert "危险" in message and "审批" in message and "订阅事件：无" not in message
    assert ("永不" in message) if idle == 0 else ("600" in message)
    assert ("inspect" in message) if mode == "declared" else ("固定" in message and "变化" in message)
    assert message.splitlines()[-1] == f"确认无误后输入：/plugins#w2-pack 启用 {details['confirm_code']}"


@pytest.mark.parametrize("field", ["interpreter_path", "interpreter_sha256", "platform", "file_digest", "network",
                                   "catalog_mode", "catalog_names", "idle_stop_seconds", "entry_args"])
def test_each_runtime_fact_changes_the_confirmation_code(tmp_path, field):
    service, _ = _service(tmp_path, mode="declared")
    details = _enable(service)["details"]["confirmation"]
    changed = copy.deepcopy({k: v for k, v in details.items() if k != "confirm_code"})
    if field.startswith("interpreter_"):
        changed["interpreter"][field.removeprefix("interpreter_")] += "changed"
    elif field == "file_digest":
        changed["files"][0]["sha256"] = "f" * 64
    elif field == "catalog_mode":
        changed["tool_catalog"]["mode"] = "pinned_on_enable"
    elif field == "catalog_names":
        changed["tool_catalog"]["tools"].append("other")
    elif field == "entry_args":
        changed["entry"]["args"].append("--other")
    elif field == "network":
        changed[field] = not changed[field]
    elif field == "idle_stop_seconds":
        changed[field] += 1
    else:
        changed[field] += "changed"
    assert confirmation_code(changed) != details["confirm_code"]


@pytest.mark.parametrize("network", [True, False])
def test_old_confirmation_bytes_remain_identical_to_anchor(network):
    # 固定完整旧输入与原文 oracle，不让新实现自生成期望值。
    confirmation = {"plugin_id": "old", "version": "1", "package_sha256": "a" * 64,
        "entry": {"kind": "executable", "command": "bin/run", "args": []}, "platform": "fixture",
        "declared_platforms": ["any"], "files": [], "confirm_code": "abcdef012345"}
    expected = ("启用前需要你确认：这个插件会以你本人的权限在本机运行下面的程序。\n"
        "插件：old 1（包摘要 aaaaaaaaaaaaaaaa…）\n入口：随包可执行文件 bin/run\n"
        "本机平台：fixture（包声明：any）\n随包文件（0 个）：\n"
        "确认无误后输入：/plugins enable old --confirm abcdef012345")
    if network:
        confirmation.update(events=[], tool_gates=[], network=False, sandbox="required", read_scope="原读范围")
        expected = expected.replace("确认无误后输入", "订阅事件：无\n网络权限：禁止联网（含本机回环）\n"
            "沙箱要求：强制使用插件进程沙箱；不可用时不得启用。\n原读范围\n确认无误后输入")
    assert confirmation_message(confirmation).encode() == expected.encode()


@pytest.mark.parametrize("tool", ["plugin_install", "plugin_update"])
def test_import_receipt_reaches_details_and_summary_uses_status_counts_only(tmp_path, tool):
    service, _ = manager(tmp_path)
    receipt = {"schema": "external_plugin_import_receipt.v1", "plugin_id": "w2-pack",
        "components": [{"status": status, "name": "untrusted body", "source_path": "untrusted path"}
            for status in ("imported", "imported", "mapped", "unsupported", "ignored")]}
    payload = {"ok": True, "state": "succeeded", "tool_name": tool,
               "result": {tool: {"plugin_id": "w2-pack", "enabled": False, "import_receipt": receipt}}}
    result = service._reply(payload)
    assert result["details"]["import_receipt"] == receipt
    assert "已导入为能力包" in result["message"]
    assert "2 项已导入" in result["message"] and "1 项已映射" in result["message"]
    assert "1 项不支持且未运行" in result["message"] and "/plugins#w2-pack 查看" in result["message"]
    assert "untrusted" not in result["message"]
    plain = service._reply({**payload, "result": {tool: {"plugin_id": "w2-pack", "enabled": False}}})
    assert plain["message"] == "插件管理请求已完成。"


def test_confirmation_details_legacy_digest_is_schema_stable():
    package = _build("declared")
    manifest = replace(package.manifest, capability=None, mcp_servers=(), permissions=None,
                       files=package.manifest.runtime_files)
    runtime = PluginRuntimeFacts("interpreter", "fixture", "python3", "/fixture/python3", "a" * 64)
    files = inspect_plugin_files(package)
    actual = confirmation_details(manifest, "b" * 64, runtime, files)
    assert "kind" not in actual and "tool_catalog" not in actual
    oracle = hashlib.sha256(json.dumps(actual, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    assert confirmation_code(actual) == oracle


@pytest.mark.parametrize("mode,idle", [("declared", 600), ("pinned_on_enable", 0)])
def test_component_confirmation_uses_real_package_bytes_without_os_launch(tmp_path, mode, idle):
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_enable_tool import PluginEnablePolicy, PluginEnableTool

    service, row = _service(tmp_path, mode=mode)
    tool = PluginEnableTool(service.context.owner, None,
        SimpleNamespace(request=SimpleNamespace(operation_id="component-preview")), row, "fixed",
        policy=PluginEnablePolicy(pack_mcp_enabled=True, pack_mcp_idle_stop_seconds=idle))
    details = tool._confirmation()
    assert details["files"][0]["size"] == len(inspect_plugin_files(_build(mode))[0].content)
    assert details["workspace_access"] == "none" and details["tool_catalog"]["mode"] == mode
    assert details["confirm_code"] == confirmation_code({key: value for key, value in details.items() if key != "confirm_code"})
    message = confirmation_message(details)
    assert "禁止联网" in message and "强制" in message and "不能读写工作区" in message
    assert "危险动作" in message and "审批" in message and "订阅事件：无" not in message
    assert message.endswith(f"确认无误后输入：/plugins#w2-pack 启用 {details['confirm_code']}")


def test_component_changed_idle_fact_invalidates_old_code_before_any_candidate(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_management import PluginManagement
    from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import _component_transport

    observed = _component_transport(monkeypatch)
    service, row = _service(tmp_path)
    original = _enable(service)["details"]["confirmation"]
    changed = PluginManagement(replace(service.context, capability_pack_mcp_idle_stop_seconds=0))
    result = _enable(changed, "changed-fact", original["confirm_code"])
    assert result["details"]["reason"] == "confirmation_required"
    assert result["details"]["confirmation"]["confirm_code"] != original["confirm_code"]
    assert changed.installations.snapshot() == (row,)
    assert observed == {"starts": 0, "lists": 0, "stops": 0}


# === Source: agent_py_agent/tests/test_cpack_w2_linked_grant.py ===

def _package(name="sample", runtime="on_demand", names=("plugin__sample__one", "plugin__sample__two")):
    return CapabilityPackageSnapshot(name, "1", "样本", "方法", ("检查",), "CAPABILITY.md", "a" * 64, "b" * 64,
        (CapabilityFile("CAPABILITY.md", "c" * 64),), lambda _: pytest.fail("联动不允许读文档"),
        mcp_runtime=runtime, mcp_tool_names=names if runtime == "on_demand" else ())


def _snapshot(*packages):
    return SkillSnapshot((), (), "fixture", "local_main", "/fixture", packages)


def _authority(names):
    class Tool(BaseTool):
        def __init__(self, name):
            self.model_spec = ToolModelSpec(name, "测试父权限", {"type": "object", "properties": {}})
            self.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("dangerous"))

        def execute(self, params):
            pytest.fail("授权测试不应调用工具")

    tools = [Tool(name) for name in names]
    runtimes = tuple(ToolRuntime(tool.model_spec, tool.runtime_policy, tool) for tool in tools)
    return ToolRuntimeSnapshot("parent", runtimes, frozenset(names), (), frozenset(names))


def _helper():
    module = "agent_py_agent.agent.capability.pack_tool_grant"
    assert importlib.util.find_spec(module) is not None, "缺少联动授权纯函数"
    return importlib.import_module(module).pack_linked_tool_names


def test_linked_names_preserve_reference_order_deduplicate_and_never_read_files(monkeypatch):
    helper = _helper()
    snapshot = _snapshot(_package(), _package("other", names=("plugin__sample__two", "plugin__other__three")),
        _package("docs", "none"), _package("foreign", "owner_unsupported"))
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", lambda *a, **kw: calls.append("open"))
        patch.setattr(os, "stat", lambda *a, **kw: calls.append("stat"))
        names = helper(snapshot, ["builtin:method", "capability:other", "capability:sample", "capability:other",
            "capability:missing", "capability:docs", "capability:foreign", None])
    assert names == ("plugin__sample__two", "plugin__other__three", "plugin__sample__one")
    assert calls == []


@pytest.mark.parametrize("raw,expected", [({}, ["plugin__sample__one"]),
    ({"tool_preset": "read_only"}, []), ({"_exact_allowed_tools": True}, [])])
def test_creation_only_links_parent_held_names_and_never_expands_readonly_or_exact(raw, expected):
    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()))
    with bind_creation_tool_authority(_authority(["plugin__sample__one"])):
        answer = _role_policy(agent, {"role": "worker", "allowed_skills": ["capability:sample"], **raw}, "检查", [])
    assert answer.allowed_tools == expected


@pytest.mark.parametrize("runtime", ["none", "owner_unsupported"])
def test_creation_does_not_link_docs_or_owner_unsupported(runtime):
    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package(runtime=runtime)))
    with bind_creation_tool_authority(_authority(["plugin__sample__one"])):
        assert _role_policy(agent, {"allowed_skills": ["capability:sample"]}, "检查", []).allowed_tools == []


@pytest.mark.parametrize("missing", [True, False])
def test_creation_snapshot_unavailable_fails_closed(missing):
    def unavailable():
        raise SkillSnapshotError("SKILL_READ_FAILED")
    agent = SimpleNamespace() if missing else SimpleNamespace(current_skill_snapshot=unavailable)
    with bind_creation_tool_authority(_authority(["plugin__sample__one"])):
        assert _role_policy(agent, {"allowed_skills": ["capability:sample"]}, "检查", []).allowed_tools == []


def _grant(agent, names, *, explicit=(), skills=("capability:sample",), raw=None):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes

    if not hasattr(agent, "subagents"):
        agent.subagents = SimpleNamespace()
    with bind_creation_tool_authority(_authority(names)):
        attrs = create_task_attributes(raw or {})
    task = SimpleNamespace(id="child", parent_id="parent", attributes=attrs)
    request = SimpleNamespace(id="request", requested_tools=list(explicit), requested_skills=list(skills))
    return ResolveCapabilityRequestsTool(agent)._mark_grant(_ResolveContext(task, "grant", {}, "授权"), request)


@pytest.mark.parametrize("raw", [{"tool_preset": "read_only"}, {"_exact_allowed_tools": True}])
def test_resolution_never_implicitly_expands_readonly_or_exact_child(raw):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes

    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()))
    attrs = create_task_attributes({**raw, "attributes": {"pack_linked_tools_allowed": True}})
    assert attrs.get("pack_linked_tools_allowed") is False
    result = _grant(agent, ["plugin__sample__one"], raw=raw)
    assert result["ok"] is True and result["skills"] == ["capability:sample"]
    assert result["tools"] == []
    explicit = _grant(agent, ["plugin__sample__one"], raw=raw, explicit=["plugin__sample__one"])
    assert explicit["ok"] is True and explicit["tools"] == ["plugin__sample__one"]


def test_resolution_links_only_grantable_names_and_keeps_the_original_record_schema():
    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()))
    result = _grant(agent, ["plugin__sample__one"])
    assert result["ok"] is True and result["tools"] == ["plugin__sample__one"]
    assert result["mcp_tools"] == [] and result["skills"] == ["capability:sample"]
    assert result["capability_cards"][0]["activation_id"] == "b" * 64
    assert result["parent_tool_authority"]["grantable_tools"] == ["plugin__sample__one"]
    assert set(result) == {"ok", "request_id", "status", "path_scope", "tools", "mcp_tools", "skills",
                           "capability_cards", "parent_tool_authority"}


def test_legacy_child_without_creation_proof_gets_docs_but_no_implicit_tools():
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes

    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()), subagents=SimpleNamespace())
    with bind_creation_tool_authority(_authority(["plugin__sample__one"])):
        attrs = create_task_attributes({})
    attrs.pop("pack_linked_tools_allowed")
    task = SimpleNamespace(id="child", parent_id="parent", attributes=attrs)
    request = SimpleNamespace(id="request", requested_tools=[], requested_skills=["capability:sample"])
    result = ResolveCapabilityRequestsTool(agent)._mark_grant(_ResolveContext(task, "grant", {}, "授权"), request)
    assert result["ok"] is True and result["skills"] == ["capability:sample"]
    assert result["tools"] == []
    request.requested_tools = ["plugin__sample__one"]
    explicit = ResolveCapabilityRequestsTool(agent)._mark_grant(_ResolveContext(task, "grant", {}, "显式授权"), request)
    assert explicit["ok"] is True and explicit["tools"] == ["plugin__sample__one"]


def test_unavailable_explicit_tool_is_refused_but_unavailable_linked_tool_is_not():
    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()))
    assert _grant(agent, [])["ok"] is True
    refused = _grant(agent, [], explicit=["plugin__sample__one"])
    assert refused["ok"] is False and refused["error_code"] == "MISSING_CAPABILITY"
    assert refused["unavailable_tools"] == ["plugin__sample__one"]


def _headless_sequence(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.package_provider import enabled_capability_packages
    from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import _confirmed, _service
    from agent_py_agent.tests.test_resolve_capability_requests_tool import _agent_and_blocked_task

    service, _ = _service(tmp_path)
    enabled, _, _ = _confirmed(service)
    assert enabled["state"] == "succeeded", enabled
    packages = enabled_capability_packages(service.context.owner)
    assert packages and packages[0].mcp_tool_names
    parent_names = packages[0].mcp_tool_names
    snapshot = _snapshot(*packages)
    parent = SimpleNamespace(current_skill_snapshot=lambda: snapshot)
    with bind_creation_tool_authority(_authority(parent_names)):
        created = _role_policy(parent, {"allowed_skills": ["capability:w2-pack"]}, "检查", [])
    assert tuple(created.allowed_tools) == parent_names
    # 使用真实 lifecycle 保存申请与授权；W3 代理未在此分支，父工具由已有创建快照承运。
    agent, task, _ = _agent_and_blocked_task(str(tmp_path / "child"))
    monkeypatch.setattr(agent, "current_skill_snapshot", lambda: snapshot)
    with bind_creation_tool_authority(_authority(parent_names)):
        current = agent.subagents.load(task.id)
        current.parent_id = "parent"
        current.attributes[DIRECT_PARENT_TOOL_AUTHORITY_ATTR] = current_creation_tool_authority()
        from agent_py_agent.agent.agent_core.orchestration.create_policy import (
            create_task_attributes,
        )

        current.attributes.update(create_task_attributes({}))
        agent.subagents.save(current)
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams

    request = agent.subagents.lifecycle.record_capability_request(task.id,
        RecordCapabilityRequestParams(problem="需要包工具", needed_capability="pack", requested_skills=["capability:w2-pack"]))
    granted = ResolveCapabilityRequestsTool(agent).execute({"run_id": task.id, "request_id": request.id,
        "decision": "grant", "reason": "同包联动"})
    assert granted.ok, granted.output
    saved = agent.subagents.load(task.id)
    assert set(parent_names).issubset(set(saved.capability_grants[-1].tools))
    disabled = service.command("/plugins disable w2-pack", revision=service.catalog().revision, request_id="disable")
    assert disabled["state"] == "succeeded", disabled
    snapshot = _snapshot(*enabled_capability_packages(service.context.owner))
    assert snapshot.packages == ()
    with bind_creation_tool_authority(_authority([])):
        assert _role_policy(parent, {"allowed_skills": ["capability:w2-pack"]}, "检查", []).allowed_tools == []
    assert _grant(parent, [], skills=["capability:w2-pack"])["ok"] is False


def test_headless_enable_create_disable_and_capability_request_sequence(tmp_path, monkeypatch):
    _headless_sequence(tmp_path, monkeypatch)


def test_component_headless_sequence_with_canonical_skill_grant(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import _component_transport

    _component_transport(monkeypatch)
    _headless_sequence(tmp_path, monkeypatch)


# === Source: agent_py_agent/tests/test_cpack_w4_import_receipt.py ===

@requires_fd
def test_parent_path_and_creation_order_do_not_change_package_or_receipt(tmp_path):
    one = fixture_tree(tmp_path / "one/source", servers=bundled())
    two = fixture_tree(tmp_path / "other/source", servers=bundled(), reverse=True)
    a, b = [import_claude_plugin(ExternalImportRequest(path)) for path in (one, two)]
    assert a.built.sha256 == b.built.sha256
    assert members(a)["import/RECEIPT.json"] == members(b)["import/RECEIPT.json"]
    assert a.receipt == b.receipt
    assert set(a.receipt) == {"schema", "source_kind", "source_tree_sha256", "plugin_id", "version",
                              "package_schema", "docs_only", "network", "components", "ignored_fields"}
    assert a.receipt["schema"] == "external_plugin_import_receipt.v1"
    assert a.receipt["package_schema"] == "plugin_package.v10"
    canonical = (json.dumps(a.receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    assert members(a)["import/RECEIPT.json"] == canonical
    assert str(tmp_path) not in canonical.decode()
    rows = a.receipt["components"]
    assert rows == sorted(rows, key=lambda row: (row["kind"], row["source_path"], row["name"]))
    assert len(rows) == len({(r["kind"], r["source_path"], r["name"]) for r in rows})
    assert all(set(r) == {"kind", "name", "source_path", "status", "code", "target_path"} for r in rows)
    assert all(r["status"] in {"imported", "mapped", "unsupported", "ignored"} for r in rows)
    assert sum(r["code"] == "skill_imported" for r in rows) == 1
    paths = {r["source_path"] for r in rows}
    assert {"skills/story", "commands/draft.md", "agents/reviewer.md", "hooks", ".git"} <= paths
    lines = []
    # 输入路径只由夹具明确列出，不以 importer 输出构造期望摘要。
    for path in sorted([".claude-plugin/plugin.json", ".mcp.json", "agents/reviewer.md", "commands/draft.md",
                        "hooks/hooks.json", "notes.data", "server/config.txt", "server/main.py",
                        "skills/story/SKILL.md", "skills/story/data.bin"]):
        data = (one / path).read_bytes()
        lines.append(f"{path}\0{hashlib.sha256(data).hexdigest()}\0{len(data)}\n")
    assert a.receipt["source_tree_sha256"] == hashlib.sha256("".join(lines).encode()).hexdigest()


@requires_fd
def test_real_directory_install_and_update_keep_receipt_inside_original_envelope(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_cpack_w4_import_synthesis import put
    from agent_py_agent.tests.test_pack_commands import _agent, _manager, _tui

    agent = _agent(tmp_path, monkeypatch)
    root = fixture_tree(agent.home_paths.owner_home_dir / "fixture-source", servers=bundled())
    result = _tui(agent, f"/plugins install {root}")
    assert result["ok"], result
    receipt = result["details"]["import_receipt"]
    assert receipt["plugin_id"] == "example-pack" and result["details"]["enabled"] is False
    row = _manager(agent).installations.snapshot()[0]
    assert row.activation is None and row.manifest.has_mcp_runtime
    assert "example-pack" in _tui(agent, "/plugins#")["message"]
    view = _tui(agent, "/plugins#example-pack 查看")
    assert view["ok"] and "带 MCP 工具：是" in view["message"]
    assert "外部导入：" in view["message"]
    # 当前 W1 冻结读口拒绝停用包；不能把读取失败写成 0 项，更不能为了详情伪造 active。
    assert "统计未验证" in view["message"]
    again = _tui(agent, f"/plugins install {root}")
    assert again["ok"] and _manager(agent).installations.snapshot()[0].package_sha256 == row.package_sha256
    put(root, ".claude-plugin/plugin.json", json.dumps({"name": "Example-Pack", "version": "1.3.0", "description": "新版分镜"}))
    changed = _tui(agent, f"/plugins update example-pack {root}")
    assert changed["ok"] and changed["details"]["import_receipt"]["version"] == "1.3.0"
    assert changed["details"]["package_version"] == "1.3.0" and "receipt" in changed["details"]
    before = _manager(agent).installations.snapshot()[0]
    put(root, ".claude-plugin/plugin.json", '{"name":"another-id"}')
    refused = _tui(agent, f"/plugins update example-pack {root}")
    assert refused["error_code"] == "PLUGIN_IMPORT_ID_CONFLICT"
    assert _manager(agent).installations.snapshot()[0] == before


@requires_fd
def test_docs_only_real_sequence_enables_reads_receipt_disables_and_removes(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_pack_commands import _agent, _tui

    agent = _agent(tmp_path, monkeypatch)
    root = fixture_tree(agent.home_paths.owner_home_dir / "fixture-source")
    installed = _tui(agent, f"/plugins install {root}")
    assert installed["ok"], installed
    receipt = installed["details"]["import_receipt"]
    assert _tui(agent, "/plugins#example-pack 启用")["ok"]
    view = _tui(agent, "/plugins#example-pack 查看")
    counts = {status: sum(row["status"] == status for row in receipt["components"]) for status in ("imported", "mapped", "unsupported")}
    expected = f"外部导入：导入 {counts['imported']} 项、映射 {counts['mapped']} 项、未支持 {counts['unsupported']} 项"
    assert expected in view["message"] and "hooks_unsupported" in view["message"]
    assert _tui(agent, "/plugins#example-pack 停用")["ok"]
    assert _tui(agent, "/plugins#example-pack 删除")["ok"]
    assert "example-pack" not in _tui(agent, "/plugins#")["message"]


@requires_fd
def test_directory_pack_never_overwrites_legacy_plugin(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_cpack_w4_import_synthesis import put
    from agent_py_agent.tests.test_pack_commands import _agent, _manager, _tui
    from agent_py_agent.tests.test_plugin_update import _v2_bundle

    agent = _agent(tmp_path, monkeypatch)
    home = agent.home_paths.owner_home_dir
    legacy = _v2_bundle(home)
    assert _tui(agent, f"/plugins install {legacy}")["ok"]
    before = _manager(agent).installations.snapshot()[0]
    assert not before.manifest.is_capability_pack
    root = fixture_tree(home / "fixture-source", manifest={"name": "sample-peek"})
    put(root, "CAPABILITY.md", "# 不能覆盖插件\n")
    for text in (f"/plugins install {root}", f"/plugins update sample-peek {root}"):
        refused = _tui(agent, text)
        assert refused["error_code"] == "PLUGIN_IMPORT_ID_CONFLICT", refused
        assert _manager(agent).installations.snapshot()[0] == before


@requires_fd
def test_directory_install_and_update_refuse_policy_denied_descendant(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_install_tool import PluginInstallTool
    from agent_py_agent.agent.plugin_update_tool import PluginUpdateTool
    from agent_py_agent.tests.test_pack_commands import _agent, _manager, _tui

    outcomes = []
    for tool_type in (PluginInstallTool, PluginUpdateTool):
        original_execute = tool_type.execute

        def record_outcome(self, params, execute=original_execute):
            outcome = execute(self, params)
            outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(tool_type, "execute", record_outcome)

    agent = _agent(tmp_path, monkeypatch)
    manager = _manager(agent)
    policy = manager.context.path_policy
    store = manager.installations
    root = fixture_tree(agent.home_paths.owner_home_dir / "workspace" / "runtime")
    denied = put(root, "services/queue/item.md", "marker\n")
    assert policy.check(root).allowed
    assert not policy.check(denied).allowed

    def store_bytes():
        return {path.relative_to(store.root): path.read_bytes()
                for path in store.root.rglob("*") if path.is_file()}

    empty = store_bytes()
    refused = _tui(agent, f"/plugins install {root}")
    assert refused["error_code"] == "PLUGIN_IMPORT_PATH_UNSAFE", refused
    assert refused["ok"] is False
    assert len(outcomes) == 1
    assert outcomes[-1].effect_outcome == "not_started"
    assert _manager(agent).installations.snapshot() == ()
    assert store_bytes() == empty

    clean = fixture_tree(agent.home_paths.owner_home_dir / "fixture-source")
    installed = _tui(agent, f"/plugins install {clean}")
    assert installed["ok"], installed
    before = _manager(agent).installations.snapshot()[0]
    before_bytes = store_bytes()
    package_bytes = store.package_bytes(before)
    outcomes.clear()
    refused = _tui(agent, f"/plugins update example-pack {root}")
    assert refused["error_code"] == "PLUGIN_IMPORT_PATH_UNSAFE", refused
    assert refused["ok"] is False
    assert len(outcomes) == 1
    assert outcomes[-1].effect_outcome == "not_started"
    assert _manager(agent).installations.snapshot() == (before,)
    assert store.package_bytes(before) == package_bytes
    assert store_bytes() == before_bytes


# === Source: agent_py_agent/tests/test_cpack_w4_import_walk_safety.py ===

def refused(root, code, *, limits=None):
    with pytest.raises(imp.ExternalImportError) as error:
        imp.import_claude_plugin(imp.ExternalImportRequest(root, limits=limits or PackageReadLimits()))
    assert error.value.code == code
    return error.value


def test_platform_without_fd_is_an_executed_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_fd", set())
    assert refused(tmp_path, "PLUGIN_IMPORT_SOURCE_INVALID").detail == "platform_unsupported"


@requires_fd
@pytest.mark.parametrize("name", ["a:b", "a\\b", "line\nbreak", "bad\x7f"])
def test_control_or_separator_name_refuses_whole_tree(tmp_path, name):
    put(tmp_path, "CAPABILITY.md", "# 正文\n")
    put(tmp_path, name, "data")
    refused(tmp_path, "PLUGIN_IMPORT_PATH_UNSAFE")


# LLM: 只在 pytest 临时目录创建特殊文件；socket 用短相对地址绑定，失败也关闭，cwd 由 monkeypatch 恢复。
# 函数用途: 确认隐藏特殊文件仍拒绝整树导入，不让 macOS 地址长度遮住实际断言。
@requires_fd
@pytest.mark.parametrize("kind", ["file_link", "dir_link", "fifo", "socket"])
def test_special_types_refuse_even_before_hidden_filter(tmp_path, kind, monkeypatch):
    put(tmp_path, "CAPABILITY.md", "# 方法\n")
    target = tmp_path / ".hidden-unsafe"
    channel = None
    try:
        if kind == "file_link":
            target.symlink_to(tmp_path / "CAPABILITY.md")
        elif kind == "dir_link":
            target.symlink_to(tmp_path, target_is_directory=True)
        elif kind == "fifo":
            os.mkfifo(target)
        else:
            monkeypatch.chdir(tmp_path)
            channel = socket.socket(socket.AF_UNIX)
            channel.bind(target.name)
        refused(tmp_path, "PLUGIN_IMPORT_PATH_UNSAFE")
    finally:
        if channel is not None:
            channel.close()


@requires_fd
def test_root_link_and_parent_escape_refuse(tmp_path):
    root = tmp_path / "source"
    put(root, "CAPABILITY.md", "# 方法\n")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    refused(alias, "PLUGIN_IMPORT_PATH_UNSAFE")
    refused(root / ".." / "source", "PLUGIN_IMPORT_PATH_UNSAFE")


@requires_fd
def test_regular_file_is_invalid_source_not_an_unsafe_tree(tmp_path):
    source = put(tmp_path, "plain-source", "data")
    refused(source, "PLUGIN_IMPORT_SOURCE_INVALID")


@requires_fd
def test_member_budget_stops_before_reading_over_limit(tmp_path, monkeypatch):
    for name in ("a", "b", "c", "d"):
        put(tmp_path, name, "data")
    reads = []
    real = imp.read_bytes_beneath

    def read(root, parts, **kwargs):
        reads.append("/".join(parts))
        return real(root, parts, **kwargs)

    monkeypatch.setattr(imp, "read_bytes_beneath", read)
    refused(tmp_path, "PLUGIN_IMPORT_LIMIT_EXCEEDED", limits=replace(PackageReadLimits(), members=2))
    assert reads == ["a", "b"]


@requires_fd
@pytest.mark.parametrize("limit", ["member_bytes", "archive_bytes", "expanded_bytes"])
def test_byte_budgets_count_unimported_files_too(tmp_path, limit):
    put(tmp_path, "hooks/hooks.json", "x" * 20)
    refused(tmp_path, "PLUGIN_IMPORT_LIMIT_EXCEEDED", limits=replace(PackageReadLimits(), **{limit: 10}))


@requires_fd
def test_deep_tree_and_long_path_are_bounded(tmp_path):
    deep = "/".join(["d"] * 33)
    put(tmp_path, deep + "/x", "x")
    refused(tmp_path, "PLUGIN_IMPORT_LIMIT_EXCEEDED")


@requires_fd
def test_walk_reads_each_regular_file_once_and_never_reads_hidden(tmp_path, monkeypatch):
    put(tmp_path, "CAPABILITY.md", "# 方法\n")
    put(tmp_path, "sub/a", "a")
    put(tmp_path, ".cache/hidden", "hidden")
    reads, listings = [], []
    real_read, real_list = imp.read_bytes_beneath, os.listdir

    def read(root, parts, **kwargs):
        reads.append("/".join(parts))
        return real_read(root, parts, **kwargs)

    def listing(fd):
        assert isinstance(fd, int)
        st = os.fstat(fd)
        listings.append((st.st_dev, st.st_ino))
        return real_list(fd)

    monkeypatch.setattr(imp, "read_bytes_beneath", read)
    monkeypatch.setattr(os, "listdir", listing)
    monkeypatch.setattr(os, "supports_fd", os.supports_fd | {listing})
    imp.import_claude_plugin(imp.ExternalImportRequest(tmp_path))
    assert reads == ["CAPABILITY.md", "sub/a"]
    assert len(listings) == len(set(listings)) == 2


@requires_fd
def test_policy_denied_member_refuses_before_read(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_cpack_w4_import_synthesis import fixture_tree
    from agent_py_agent.tests.test_pack_commands import _agent, _manager

    agent = _agent(tmp_path, monkeypatch)
    policy = _manager(agent).context.path_policy
    root = fixture_tree(agent.home_paths.owner_home_dir / "workspace" / "runtime")
    denied = put(root, "services/queue/item.md", "marker\n")
    assert policy.check(root).allowed
    assert not policy.check(denied).allowed
    reads = []
    original_read = imp.read_bytes_beneath

    def recorder(source_root, parts, **kwargs):
        reads.append(tuple(parts))
        return original_read(source_root, parts, **kwargs)

    monkeypatch.setattr(imp, "read_bytes_beneath", recorder)
    fd_root = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
    gc.collect()
    before = len(os.listdir(fd_root))
    request = imp.ExternalImportRequest(root, path_check=policy.check)
    try:
        with pytest.raises(imp.ExternalImportError) as error:
            imp.import_claude_plugin(request)
        assert error.value.code == "PLUGIN_IMPORT_PATH_UNSAFE"
        assert error.value.detail == "member_unauthorized"
    finally:
        gc.collect()
        fd_delta = len(os.listdir(fd_root)) - before
        assert fd_delta == 0
        assert all("services" not in parts for parts in reads), reads

    shutil.rmtree(root / "services")
    checked = imp.import_claude_plugin(imp.ExternalImportRequest(root, path_check=policy.check))
    unchecked = imp.import_claude_plugin(imp.ExternalImportRequest(root))
    assert checked.built.sha256 == unchecked.built.sha256
    assert checked.built.payload == unchecked.built.payload
    assert checked.receipt == unchecked.receipt


# === Source: agent_py_agent/tests/test_v15_w9_outbound_index.py ===

def _entry(message_id="om_outbound", *, sent=1_800_000_000, **changes):
    return {"schema_version": "outbound_message.v1", "channel": "feishu",
            "channel_message_id": message_id, "chat_id": "chat-a", "thread_id": "thread-a",
            "conversation_message_id": "msg-a", "task_id": "task-a", "workflow_run_id": "workflow-run-a",
            "evidence_refs": ["artifact://report-a"], "receipt_id": message_id,
            "sent_at": datetime.fromtimestamp(sent, timezone.utc).isoformat(), **changes}


def test_receipt_survives_reopen_and_is_scoped_by_channel_and_chat(tmp_path):
    index = OutboundMessageIndex(tmp_path)
    row = _entry()
    assert index.record(row, now=1_800_000_001) == row
    reopened = OutboundMessageIndex(tmp_path)
    assert reopened.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_000_002) == row
    assert reopened.lookup("om_outbound", channel="other", chat_id="chat-a", now=1_800_000_002) is None
    assert reopened.lookup("om_outbound", channel="feishu", chat_id="chat-b", now=1_800_000_002) is None
    assert OutboundMessageIndex(tmp_path / "other-owner").lookup("om_outbound", channel="feishu", chat_id="chat-a") is None
    path = tmp_path / "data/outbound_messages" / (hashlib.sha256(b"om_outbound").hexdigest()[:2] + ".json")
    assert path.is_file()


def test_lookup_is_readonly_and_expired_entries_are_not_reintroduced(tmp_path):
    index = OutboundMessageIndex(tmp_path)
    assert index.lookup("absent", channel="feishu", chat_id="chat-a", now=1_800_000_000) is None
    assert list(tmp_path.iterdir()) == []
    index.record(_entry(), now=1_800_000_000)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert index.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_000_000 + 30 * 86400) is None
    assert {p: p.read_bytes() for p in before} == before
    assert index.record(_entry(), now=1_800_000_000 + 31 * 86400) == _entry()
    assert index.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_000_000 + 31 * 86400) is None


def test_duplicate_is_idempotent_but_identity_rebinding_is_rejected(tmp_path):
    index = OutboundMessageIndex(tmp_path)
    row = _entry()
    index.record(row, now=1_800_000_000)
    assert index.record(dict(row), now=1_800_000_000) == row
    with pytest.raises(ValueError):
        index.record(_entry(conversation_message_id="different-message"), now=1_800_000_000)
    assert index.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_000_000) == row


def test_one_shard_is_bounded_and_drops_oldest_without_global_scan(tmp_path, monkeypatch):
    index = OutboundMessageIndex(tmp_path)
    row = _entry()
    prefix = hashlib.sha256(b"om_outbound").hexdigest()[:2]
    ids = []
    i = 0
    while len(ids) < 2000:
        value = f"om_shard_{i}"
        if hashlib.sha256(value.encode()).hexdigest().startswith(prefix):
            ids.append(value)
        i += 1
    path = tmp_path / "data/outbound_messages" / f"{prefix}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "outbound_message_index.v1", "entries": [
        _entry(value, sent=1_800_000_000 + n) for n, value in enumerate(ids)]}))
    monkeypatch.setattr(type(tmp_path), "rglob", lambda *a, **k: pytest.fail("全历史枚举"))
    index.record(_entry(sent=1_800_002_001), now=1_800_002_001)
    persisted = json.loads(path.read_text())["entries"]
    assert len(persisted) == 2000
    assert index.lookup(ids[0], channel="feishu", chat_id="chat-a", now=1_800_002_001) is None
    assert index.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_002_001) is not None
    assert row["conversation_message_id"] == "msg-a"


def test_concurrent_same_shard_updates_do_not_erase_neighbors(tmp_path):
    ids = [f"om_parallel_{i}" for i in range(40)]
    def record(value):
        return OutboundMessageIndex(tmp_path).record(_entry(value), now=1_800_000_001)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(list(pool.map(record, ids))) == len(ids)
    index = OutboundMessageIndex(tmp_path)
    for value in ids:
        assert index.lookup(value, channel="feishu", chat_id="chat-a", now=1_800_000_002)["channel_message_id"] == value


@pytest.mark.parametrize("change", [{"channel_message_id": ""}, {"chat_id": ""}, {"thread_id": []},
    {"sent_at": "bad-time"}, {"schema_version": "unknown"}, {"evidence_refs": "not-a-list"}])
def test_invalid_records_never_publish(tmp_path, change):
    with pytest.raises(ValueError):
        OutboundMessageIndex(tmp_path).record(_entry(**change), now=1_800_000_001)
    assert list(tmp_path.iterdir()) == []
