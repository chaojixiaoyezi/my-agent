"""冻结业务目录的纯合同回归；不把登记、DTO或模拟结果当作领域接线。"""
from __future__ import annotations

import hashlib
import importlib
from collections import Counter
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_host.action_specs import (
    action_spec,
    action_specs,
    required_capabilities,
)
from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts, decode_call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import (
    canonical_json,
    closed,
    validate_object,
)
from agent_py_agent.agent.plugin_manifest_host_v9 import (
    host_capabilities,
    validate_capabilities,
)
from agent_py_agent.agent.plugin_permissions.host_grants import (
    default_selection,
    validate_selection,
)


# 在实现前从冻结 JSON 和原143动作计算；测试不读取部署目录外的设计文件。
def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def catalog():
    return importlib.import_module("agent_py_agent.agent.plugin_host.business_action_specs")


def wire(action, payload, *, key=None):
    value = {"schema": "plugin_host_call.v2", "action": action, "payload": payload}
    if key is not None:
        value["idempotency_key"] = key
    return canonical_json(value).encode()


def test_exact_vocabulary_and_no_grant_upgrade():
    business = {"business.read", "business.configure", "business.execute", "business.control",
                "business.observe", "business.govern", "business.approve"}
    old = {"read", "write", "chat.history", "chat", "threads.manage", "requests.cancel", "execute",
           "users.read", "users.manage", "act_as_user", "interactions.read", "interactions.respond",
           "tasks.read", "tasks.control", "goals.read", "goals.control", "memory.read", "artifacts.read",
           "assets.read", "assets.write", "datasources.read", "datasources.write", "recorder.control",
           "audit_group.read", "audit_group.invoke", "plugins.manage", "workflows.debug"}
    assert host_capabilities() == old | business
    for cap in business:
        assert validate_capabilities((cap,)) == (cap,)
        selection = default_selection((cap,))
        assert validate_selection(selection) == selection
    original = default_selection(("read", "write", "execute", "interactions.respond"))
    assert validate_selection(original) == original
    assert not business & set(original["approved_capabilities"])
    for bad in (("business.admin",), ("business.read", "business.read")):
        with pytest.raises(ValueError):
            validate_capabilities(bad)


def test_frozen_full_catalog_and_schema_bundle_hashes():
    module = catalog()
    api = module.business_api()
    schemas = module.business_schema_bundle()
    normalized_api = deepcopy(api)
    normalized_schemas = deepcopy(schemas)
    normalized_defs = deepcopy(module.business_defs())
    tasks_get = next(row for row in normalized_api["actions"] if row["action"] == "tasks.get")
    # 已批准的合同变化：普通任务可没有工作流；其它字段仍受原四个摘要约束。
    for result in (normalized_api["$defs"]["Result_tasks_get"],
                   normalized_schemas["$defs"]["Result_tasks_get"],
                   normalized_defs["Result_tasks_get"], tasks_get["result_schema"]):
        assert "workflow_ref" in result["required"]
        assert result["properties"]["workflow_ref"] == {
            "anyOf": [{"$ref": "#/$defs/Id"}, {"type": "null"}]}
        result["properties"]["workflow_ref"] = {"$ref": "#/$defs/Id"}
    # #49-1仅可选追加画布拓扑字段；先证明不进入required，再规范化比较原冻结目录。
    canvas = {"WorkflowDefinition": ("datasource_ids", "edges", "schedule_ids"),
              "WorkflowStepDefinition": ("description", "next_step_ids", "ui_position")}
    for definitions in (normalized_api["$defs"], normalized_schemas["$defs"], normalized_defs):
        for name, added in canvas.items():
            assert set(added) <= set(definitions[name]["properties"])
            assert not set(added) & set(definitions[name].get("required", []))
            for key in added:
                del definitions[name]["properties"][key]
    # W11 允许无归属后台 Agent，并追加只读工作状态与配额；先锁住新合同，再比较旧摘要。
    quota_fields = ("limit", "used", "available", "available_agents", "unassigned")
    quota_schema = {
        "type": "object",
        "properties": {**{key: {"type": ["integer", "null"], "minimum": 0} for key in quota_fields},
                       "over_limit": {"type": ["boolean", "null"]},
                       "unlimited": {"type": ["boolean", "null"]}, "coverage": {"$ref": "#/$defs/Coverage"}},
        "required": [*quota_fields, "over_limit", "unlimited", "coverage"], "additionalProperties": False,
    }
    for definitions in (normalized_api["$defs"], normalized_schemas["$defs"], normalized_defs):
        agent_view = definitions["AgentView"]
        assert agent_view["properties"]["space_id"] == {"$ref": "#/$defs/NullableId"}
        agent_view["properties"]["space_id"] = {"$ref": "#/$defs/Id"}
        assert "work_state" not in agent_view["required"]
        assert agent_view["properties"].pop("work_state") == {
            "type": "string", "enum": ["idle", "running", "waiting", "failed", "unknown"]}
        assert definitions.pop("BackgroundAgentQuotaView") == quota_schema
        result = definitions["Result_background_agents_list"]
        assert "quota" not in result["required"]
        assert result["properties"].pop("quota") == {"$ref": "#/$defs/BackgroundAgentQuotaView"}
    nullable_space_actions = {"background_agents.create", "background_agents.list", "tasks.assign", "tasks.submit"}
    for index, row in enumerate(normalized_api["actions"]):
        if row["action"] in nullable_space_actions:
            request_params = normalized_schemas["request_schema"]["oneOf"][index]["properties"]["params"]
            for params in (row["params_schema"], row["plugin_payload_schema"], request_params):
                assert params["properties"]["space_id"] == {"$ref": "#/$defs/NullableId"}
                params["properties"]["space_id"] = deepcopy(normalized_defs["Id"])
        if row["action"] == "background_agents.list":
            result = row["result_schema"]
            assert "quota" not in result["required"]
            assert result["properties"].pop("quota") == {"$ref": "#/$defs/BackgroundAgentQuotaView"}
    assert digest(normalized_api) == "d7f7859fded58ec5ab3e82a5dc8b1d363c7b45bb9a772deea17dcf54113df737"
    assert digest(normalized_schemas) == "d7883032c859fa4d2c7d6365d50e77b2d3f2cb6fa77904f355925bbf3dca2d4b"
    assert digest(normalized_defs) == "c2441120aa06032bd0aba9a5b83075f72d60d34f7cc69cd376b80779a5b9554a"
    assert digest(normalized_api["actions"]) == "2650124904f6384a5ccfc348bae539b8ed71b39cd6084e94f45b2405886ba021"
    assert module.BUSINESS_LIST_DEFAULT_ROWS == 50
    assert module.BUSINESS_LIST_MAX_ROWS == 100
    assert api["$defs"] == schemas["$defs"] == module.business_defs()
    api["$defs"]["Id"]["x-max-bytes"] = 999
    schemas["$defs"].clear()
    assert module.business_defs()["Id"]["x-max-bytes"] == 128


def test_task_workflow_ref_is_required_nullable_and_still_validates_ids():
    spec = catalog().business_action_spec('tasks.get')
    schema = spec.result_schema
    assert 'workflow_ref' in schema['required']
    assert schema['properties']['workflow_ref'] == {
        'anyOf': [{'$ref': '#/$defs/Id'}, {'type': 'null'}]}
    sample = schema_example(schema, schema)
    for value in (None, 'real-workflow-id'):
        assert spec.validate_result({**sample, 'workflow_ref': value})['workflow_ref'] == value
    for value in ('', False, 1, [], {}, '中' * 43):
        with pytest.raises(HostAPIError):
            spec.validate_result({**sample, 'workflow_ref': value})
    with pytest.raises(HostAPIError):
        spec.validate_result({key: value for key, value in sample.items() if key != 'workflow_ref'})


def test_legacy_143_actions_remain_byte_equivalent():
    specs = action_specs()
    # GAP158-W7 新增三个工作区写动作；它们不属于原 143 项冻结合同。
    added_actions = {"workspace.files.mkdir", "workspace.files.write", "workspace.files.delete"}
    assert added_actions <= specs.keys()
    old = {name: {"name": spec.name, "mode": spec.mode, "capabilities": sorted(spec.capabilities),
                  "schema": spec.payload_schema, "scope_requirements": list(spec.scope_requirements),
                  "any_capabilities": sorted(spec.any_capabilities)}
           for name, spec in specs.items() if not name.startswith("business.") and name not in added_actions}
    assert len(old) == 143
    assert digest(old) == "d3160bf300b61ec2142131bd4bf7822934e44feda0f43217119865249bdab924"
    assert action_spec("tasks.list").validate_payload({"thread_id": "thread-one"}) == {
        "thread_id": "thread-one", "limit": 50}
    assert action_spec("tasks.cancel").mode == "async"


def schema_nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from schema_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from schema_nodes(item)


def assert_local_closure(schema):
    for node in schema_nodes(schema):
        if "$ref" not in node:
            continue
        ref = node["$ref"]
        assert ref.startswith("#/")
        target = schema
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        assert isinstance(target, dict)


def test_all_102_native_plugin_bijections_modes_and_schema_closures():
    module = catalog()
    native = module.business_action_specs()
    rows = module.business_api()["actions"]
    assert len(native) == len(rows) == 102
    assert Counter(spec.host_mode for spec in native.values()) == {"read": 51, "write": 38, "async": 13}
    assert len({row["plugin_action"] for row in rows}) == 102
    assert set(action_specs()) == {n for n in action_specs() if not n.startswith("business.")} | {
        row["plugin_action"] for row in rows} | {"business.events.stream"}
    for row in rows:
        spec = native[row["action"]]
        assert module.business_action_spec(row["action"]) is spec
        assert module.business_plugin_action(row["action"]) == row["plugin_action"]
        assert module.business_native_action(row["plugin_action"]) == row["action"]
        assert spec.metadata == row
        host = action_spec(row["plugin_action"])
        assert host.mode == row["host_mode"] == spec.host_mode
        assert host.requires_idempotency == row["idempotency_required"] == spec.requires_idempotency
        assert host.capabilities == frozenset(row["capabilities"]) == spec.capabilities
        assert host.scope_requirements == tuple(row["scope_requirements"])
        assert not host.any_capabilities
        for field in ("params_schema", "plugin_payload_schema", "result_schema", "accepted_result_schema"):
            actual = getattr(spec, field)
            if field not in row:
                assert actual is None
                continue
            assert {key: item for key, item in actual.items() if key != "$defs"} == row[field]
            assert actual.get("additionalProperties") is False
            assert_local_closure(actual)
        assert host.payload_schema == spec.plugin_payload_schema
        for key, definition in host.payload_schema.get("$defs", {}).items():
            assert definition == module.business_defs()[key]
        assert_local_closure(spec.native_request_schema)
    with pytest.raises(TypeError):
        native["fake"] = next(iter(native.values()))
    for invalid in ("business.tasks.list", "TASKS.LIST", " tasks.list", "events.stream"):
        with pytest.raises(HostAPIError):
            module.business_action_spec(invalid)
    for invalid in ("tasks.list", "business.business.tasks.list", "business.events.stream"):
        with pytest.raises(HostAPIError):
            module.business_native_action(invalid)


def test_execution_and_human_ticket_capabilities_are_not_replaced():
    for action in ("background_agents.messages.send", "tasks.submit", "tasks.assign", "tasks.follow_up",
                   "subagents.messages.send", "workflows.run", "workflow_runs.resume"):
        assert {"business.execute", "execute"} <= required_capabilities("business." + action, {})
    assert {"business.control", "business.execute", "execute"} <= required_capabilities(
        "business.workflow_runs.resume", {})
    approve = action_spec("business.approvals.decide")
    assert approve.capabilities == {"business.approve", "interactions.respond"}
    assert "human_ticket" in approve.scope_requirements
    assert "response_ticket" in approve.payload_schema["required"]
    assert "decision" in approve.payload_schema["required"]
    assert action_spec("business.background_agents.retire").mode == "async"
    assert action_spec("business.subagents.messages.send").mode == "async"


def test_transport_only_stream_is_not_a_domain_action():
    module = catalog()
    stream = action_spec("business.events.stream")
    assert stream.mode == "stream" and not stream.requires_idempotency
    assert stream.capabilities == {"business.observe"}
    row = module.business_api()["transport_only_action_specs"][0]
    assert stream.scope_requirements == tuple(row["scope_requirements"])
    assert "business.events.stream" not in module.business_action_specs()
    assert_local_closure(stream.payload_schema)
    raw = wire("business.events.stream", {"filter": {"space_id": "space"}, "request_id": "request"})
    assert decode_call(raw).action == "business.events.stream"
    with pytest.raises(HostAPIError):
        decode_call(wire("business.events.stream", {"filter": {}, "request_id": "request"},
                         key="operation_key_0001"))


@pytest.mark.parametrize("action,payload,mode", [
    ("system.capabilities", {}, "read"),
    ("spaces.list", {}, "read"),
    ("tasks.list", {"filter": {"space_id": "space"}}, "read"),
    ("spaces.create", {"name": "中文空间", "mounts": [{"package_ref": "pack"}]}, "write"),
    ("spaces.update", {"space_id": "space", "name": "新名称", "expected_revision": 0}, "write"),
    ("interfaces.bind", {"surface_ref": "surface", "name": "入口", "space_ids": ["space"],
                         "default_space_id": "space", "controller": {"mode": "agent", "agent_id": "agent"}}, "write"),
    ("tasks.cancel", {"task_id": "task", "include_descendants": False, "expected_revision": 1}, "async"),
    ("tasks.submit", {"space_id": "space", "instruction": "检查"}, "async"),
    ("subagents.messages.send", {"run_id": "run", "text": "指导"}, "async"),
    ("workflow_runs.resume", {"workflow_run_id": "wrun", "expected_revision": 4}, "async"),
    ("approvals.decide", {"approval_id": "approval", "decision": "denied", "response_ticket": "opaque-ticket",
                          "expected_revision": 1}, "write"),
])
def test_real_decode_entrypoint_closed_payload_and_idempotency(action, payload, mode):
    key = "operation_key_0001" if mode in {"write", "async"} else None
    name = "business." + action
    call = decode_call(wire(name, payload, key=key))
    assert call.action == name
    assert action_spec(name).mode == mode
    with pytest.raises(HostAPIError):
        decode_call(wire(name, {**payload, "extra": 1}, key=key))
    with pytest.raises(HostAPIError):
        decode_call(wire(name, payload, key=None if key else "operation_key_0001"))


@pytest.mark.parametrize("path", ["direct", "ref", "nullable", "oneOf", "array", "additional", "pointer"])
def test_units_follow_local_references_and_only_matching_branches(path):
    identifier = {"type": "string", "minLength": 1, "maxLength": 128, "x-max-bytes": 128}
    ref = {"$ref": "#/$defs/Id"}
    field = {"direct": identifier, "ref": ref, "nullable": {"anyOf": [ref, {"type": "null"}]},
             "oneOf": {"oneOf": [ref, {"type": "integer"}]},
             "array": {"type": "array", "items": ref},
             "additional": {"type": "object", "additionalProperties": ref},
             "pointer": {"$ref": "#/$defs/escaped~1Id"}}[path]
    schema = {**closed({"value": field}), "$defs": {"Id": identifier, "escaped/Id": identifier}}
    def wrap(text):
        return [text] if path == "array" else ({"name": text} if path == "additional" else text)
    assert validate_object({"value": wrap("中" * 42 + "aa")}, schema)
    with pytest.raises(HostAPIError):
        validate_object({"value": wrap("中" * 43)}, schema)
    if path == "nullable":
        assert validate_object({"value": None}, schema) == {"value": None}
    if path == "oneOf":
        assert validate_object({"value": 7}, schema) == {"value": 7}


def test_anyof_uses_a_successful_units_branch_not_all_shape_matches():
    schema = closed({"value": {"anyOf": [{"type": "string", "x-max-bytes": 1},
                                        {"type": "string", "x-max-bytes": 128}]}})
    assert validate_object({"value": "中文"}, schema) == {"value": "中文"}
    with pytest.raises(HostAPIError):
        validate_object({"value": "中" * 43}, schema)


def test_ref_units_preserve_sibling_constraints_and_unique_arrays():
    schema = {**closed({"ids": {"$ref": "#/$defs/Ids"}}), "$defs": {
        "Ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}}}
    with pytest.raises(HostAPIError):
        validate_object({"ids": ["same", "same"]}, schema)
    schema = {**closed({"id": {"$ref": "#/$defs/Id", "x-max-bytes": 4}}), "$defs": {
        "Id": {"type": "string", "x-max-bytes": 128}}}
    with pytest.raises(HostAPIError):
        validate_object({"id": "中文"}, schema)


@pytest.mark.parametrize("ref,defs", [
    ("https://example.invalid/schema", {}), ("file:///tmp/id.json", {}),
    ("#/$defs/missing", {}), ("#/$defs/A", {"A": {"$ref": "#/$defs/A"}}),
    ("#/$defs/A", {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}),
])
def test_external_missing_and_alias_cycles_fail_closed(ref, defs):
    with pytest.raises(HostAPIError):
        validate_object({"id": "abc"}, {**closed({"id": {"$ref": ref}}), "$defs": defs})


def test_recursive_json_union_allows_scalars_but_stays_bounded():
    module = catalog()
    schema = module.standalone_schema({"type": "object", "additionalProperties": {"$ref": "#/$defs/JsonValue"}})
    value = {"text": "中文", "int": 1, "bool": True, "null": None, "array": [{"value": 0.2}]}
    assert validate_object(value, schema) == value
    deep = {}
    for _ in range(40):
        deep = {"nested": deep}
    with pytest.raises(HostAPIError):
        validate_object(deep, schema)


def workflow_payload():
    return {"definition": {"descriptor": {"id": "workflow", "kind": "workflow", "name": "流程",
        "access_targets": [], "data_sources": [], "credential_refs": [{"credential_ref": "opaque-ref"}],
        "io_contract": {}}, "steps": [{"step_id": "step", "name": "检查", "mode": "tool_call",
                                      "config": {"tool_name": "read_file"}}]}}


@pytest.mark.parametrize("action", ["business.workflows.create", "business.workflows.update"])
def test_only_exact_workflow_descriptor_credential_reference_is_allowed(action):
    payload = workflow_payload()
    if action.endswith("update"):
        payload.update(workflow_ref="workflow", expected_revision=0)
    assert decode_call(wire(action, payload, key="operation_key_0001")).action == action
    mutations = [lambda p: p.update(owner_id="injected"),
                 lambda p: p["definition"]["descriptor"].update(owner_id="injected"),
                 lambda p: p["definition"]["descriptor"]["credential_refs"][0].update(credential_ref="中" * 43),
                 lambda p: p["definition"]["descriptor"]["credential_refs"][0].update(credential_ref={"owner_id": "bad"}),
                 lambda p: p["definition"]["descriptor"].update(metadata={"credential_ref": "bad"}),
                 lambda p: p["definition"]["steps"][0]["config"].update(credential_ref="bad"),
                 lambda p: p["definition"]["steps"][0]["config"].update(__trusted=True)]
    for mutate in mutations:
        changed = deepcopy(payload)
        mutate(changed)
        with pytest.raises(HostAPIError):
            decode_call(wire(action, changed, key="operation_key_0001"))
    with pytest.raises(HostAPIError):
        decode_call(wire("execute.tool", {"thread_id": "thread", "tool_name": "x", "arguments": {
            "definition": {"descriptor": {"credential_refs": [{"credential_ref": "bad"}]}}}},
            key="operation_key_0001"))


def test_result_validators_close_data_and_distinguish_accepted_from_completed():
    module = catalog()
    spec = module.business_action_spec("tasks.submit")
    accepted = {"task_id": "task", "request_id": "request", "operation_id": "op", "state": "accepted"}
    assert spec.validate_result(accepted, accepted=True) == accepted
    with pytest.raises(HostAPIError):
        spec.validate_result({**accepted, "extra": True}, accepted=True)
    assert spec.validate_result({**accepted, "operation_id": None})["operation_id"] is None
    with pytest.raises(HostAPIError):
        spec.validate_result({**accepted, "operation_id": None}, accepted=True)
    with pytest.raises(HostAPIError):
        module.business_action_spec("spaces.list").validate_result({}, accepted=True)
    scope = module.business_action_spec("workflows.scope.get")
    data = {"scope": {"scope_mode": "spaces", "space_ids": ["中" * 43]}, "scope_revision": 1,
            "origin": {"kind": "host", "source_ref": None, "revision": 1}}
    with pytest.raises(HostAPIError):
        scope.validate_result(data)


def test_new_business_port_is_optional_and_does_not_claim_handler_wiring():
    from agent_py_agent.agent.plugin_host.composition import domain_ready
    original = HostDomainPorts("conversation", "users", "execution", "observations")
    assert original.business is None
    marker = object()
    assert replace(original, business=marker).business is marker
    assert not domain_ready("business.tasks.list", HostDomainPorts())
    assert not domain_ready("business.events.stream", HostDomainPorts())


def schema_example(schema, root):
    """只生成合同形状样本；完整冻结摘要另验，不用这些样本宣称领域执行成功。"""
    if "$ref" in schema:
        target = root
        for token in schema["$ref"][2:].split("/"):
            target = target[token.replace("~1", "/").replace("~0", "~")]
        schema = {**target, **{key: value for key, value in schema.items() if key != "$ref"}}
    if "const" in schema:
        return deepcopy(schema["const"])
    if "enum" in schema:
        return deepcopy(schema["enum"][0])
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            return schema_example(schema[keyword][0], root)
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = kind[0]
    if kind == "object":
        return {name: schema_example(schema["properties"][name], root) for name in schema.get("required", [])}
    if kind == "array":
        return [schema_example(schema["items"], root) for _ in range(schema.get("minItems", 0))]
    if kind == "null":
        return None
    if kind == "boolean":
        return False
    if kind in {"integer", "number"}:
        return max(schema.get("minimum", 0), schema.get("exclusiveMinimum", -1) + 1)
    if schema.get("format") == "date-time":
        return "2026-11-01T00:00:00Z"
    if "pattern" in schema:
        return "a" * (64 if "64" in schema["pattern"] else max(schema.get("minLength", 1), 1))
    return "x" * max(schema.get("minLength", 1), 1)


def test_every_frozen_action_through_real_decode_native_and_result_validators():
    module = catalog()
    checked = []
    for action, spec in module.business_action_specs().items():
        payload_schema = spec.plugin_payload_schema
        payload = schema_example(payload_schema, payload_schema)
        key = "operation_key_0001" if spec.requires_idempotency else None
        call = decode_call(wire(spec.plugin_action, payload, key=key))
        assert call.action == spec.plugin_action
        with pytest.raises(HostAPIError):
            decode_call(wire(spec.plugin_action, {**payload, "unexpected": True}, key=key))
        for required in payload_schema["required"]:
            missing = {name: value for name, value in payload.items() if name != required}
            with pytest.raises(HostAPIError):
                decode_call(wire(spec.plugin_action, missing, key=key))
        native_schema = spec.native_request_schema
        native = schema_example(native_schema, native_schema)
        assert validate_object(native, native_schema) == native
        with pytest.raises(HostAPIError):
            validate_object({**native, "unexpected": True}, native_schema)
        result_schema = spec.result_schema
        result = schema_example(result_schema, result_schema)
        assert spec.validate_result(result) == result
        with pytest.raises(HostAPIError):
            spec.validate_result({**result, "unexpected": True})
        for required in result_schema["required"]:
            with pytest.raises(HostAPIError):
                spec.validate_result({name: value for name, value in result.items() if name != required})
        if spec.accepted_result_schema is not None:
            accepted_schema = spec.accepted_result_schema
            accepted = schema_example(accepted_schema, accepted_schema)
            assert spec.validate_result(accepted, accepted=True) == accepted
            with pytest.raises(HostAPIError):
                spec.validate_result({**accepted, "unexpected": True}, accepted=True)
        if "limit" in payload_schema.get("properties", {}):
            assert payload_schema["properties"]["limit"]["default"] == module.BUSINESS_LIST_DEFAULT_ROWS
            assert payload_schema["properties"]["limit"]["maximum"] == module.BUSINESS_LIST_MAX_ROWS
            assert call.payload["limit"] == module.BUSINESS_LIST_DEFAULT_ROWS
            with pytest.raises(HostAPIError):
                decode_call(wire(spec.plugin_action, {**payload, "limit": 101}, key=key))
        checked.append(action)
    assert len(checked) == 102


def test_local_root_pointer_and_matching_object_oneof_units():
    identifier = {"type": "string", "x-max-bytes": 4}
    schema = closed({"definition": identifier, "value": {"oneOf": [
        closed({"kind": {"const": "id"}, "id": {"$ref": "#/properties/definition"}}),
        closed({"kind": {"const": "count"}, "count": {"type": "integer"}})]}})
    assert validate_object({"definition": "x", "value": {"kind": "count", "count": 1}}, schema)
    with pytest.raises(HostAPIError):
        validate_object({"definition": "x", "value": {"kind": "id", "id": "中文"}}, schema)


def test_business_capability_uses_original_live_grant_issuer_and_revocation(tmp_path):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.plugin_host.authorization import (
        IngressIssuer,
        authorize_call,
        revalidate_call,
    )
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant

    # 原真实安装/激活/令牌链，不启动 HTTP 或插件进程，不构造替身 AuthorizedCall。
    api.set_host_api_base("http://127.0.0.1:8420")
    issuer = IngressIssuer()
    try:
        _, _, entry, ref = active_grant(tmp_path / "legacy", capabilities=["read"])
        token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
        call = decode_call(wire("business.system.capabilities", {}))
        with pytest.raises(HostAPIError) as denied:
            authorize_call(issuer.ingress(token), call)
        assert denied.value.code == "CAPABILITY_DENIED"
        _, _, entry, ref = active_grant(tmp_path / "business", capabilities=["business.read"])
        token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
        authorized = authorize_call(issuer.ingress(token), call)
        assert authorized.binding.capabilities == {"business.read"}
        revalidate_call(authorized)
        with pytest.raises(HostAPIError):
            revalidate_call(replace(authorized))
        issuer.close()
        with pytest.raises(HostAPIError):
            revalidate_call(authorized)
    finally:
        issuer.close()
        api.set_host_api_base(None)
