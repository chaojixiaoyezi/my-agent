"""W1 pin 子记录合同：真实 DTO/安装表 codec，不启动服务、不访问真实 home。

新 API 只在测试体/辅助函数内取用，anchor 上应产生行为失败而非 collection 导入失败。
样本与摘要 oracle 自包含，不依赖其他 W1 新测试或新增 conftest。
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace

import pytest

from agent_py_agent.agent import plugin_activation_record as record
from agent_py_agent.agent import plugin_installation as installation
from agent_py_agent.agent import plugin_installation_state as codec
from agent_py_agent.agent import plugin_manifest as manifest_module
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


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
