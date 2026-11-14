"""W1 业务域工具归并、严格分支校验、人工审批保留、历史回执同源与后台唤醒动作上界回归测试。"""
from __future__ import annotations

import json
import threading
import time
from io import BytesIO
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation.background_tool_policy import (
    SESSION_TASK_WAKE_ALLOWED_TOOLS,
    BackgroundToolPolicyRequest,
    background_tool_policy_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_api import (
    _business_action_effect,
    _sample_business_request,
    inspect_registered_business_receipt,
    register_business_tools,
)
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec
from agent_py_agent.agent.plugin_host.business_action_specs import (
    business_action_spec,
    business_action_specs,
)
from agent_py_agent.agent.plugin_host.schemas import canonical_json
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.input_schema import validate_tool_input
from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.tooling.versioned_input_decoder import (
    business_domain_actions,
    business_domain_tool,
    decode_retired_tool_call,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_registry_test_call,
)


def _make_full_business_agent(
    tmp_path,
    monkeypatch,
    *,
    enable_self_map: bool = True,
    all_105_ready: bool = False,
) -> SimpleAgent:
    home = tmp_path / f"home-{enable_self_map}-{all_105_ready}"
    ws = tmp_path / f"ws-{enable_self_map}-{all_105_ready}"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(home),
        memory_path="memory.jsonl",
        execution_mode="local_unmanaged",
        business_spaces_enabled=True,
        enable_self_map=enable_self_map,
    )
    agent = SimpleAgent(config, ws)
    agent.config.business_host_ports_enabled = True
    agent.business_operation_store = agent.tools.operation_store
    agent.business_approval_decider = lambda req_body, actor=None: {
        "approval_id": req_body["approval_id"],
        "state": "approved" if req_body["decision"] == "approve" else "rejected",
        "decided_by": getattr(actor, "actor_id", "local-user"),
    }
    if all_105_ready:
        agent.business_governance = SimpleNamespace()
        agent.business_projection = SimpleNamespace(query=lambda *a, **k: {})
    agent._local_business_services_cache = None
    register_business_tools(agent)
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "local-user",
            "channel": "tui",
            "channel_conversation_id": "w1-test-conv",
            "channel_user_id": "local-user",
            "owner_id": agent.home_paths.owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
        }
    )
    agent._current_run_params = RunParams(
        source="gateway",
        request_id="w1-req-0001",
        task_attributes={"conversation_thread_id": thread.thread_id},
    )
    return agent


def _schema_undescribed_paths(schema: dict) -> list[str]:
    missing: list[str] = []

    def walk(node, path: str, inherited: dict | None = None, seen: tuple[str, ...] = ()) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") and ref not in seen:
            target = schema
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            walk(target, path, inherited, (*seen, ref))
        props = node.get("properties", {})
        parent = inherited or {}
        for name, child in props.items():
            effective = child
            if isinstance(child, dict) and "$ref" in child and child["$ref"].startswith("#/"):
                target = schema
                for part in child["$ref"][2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                effective = {**target, **child}
            desc = effective.get("description") or parent.get(name, {}).get("description")
            if not isinstance(desc, str) or not desc.strip():
                missing.append(f"{path}.{name}")
            walk(child, f"{path}.{name}", seen=seen)
        for comb in ("oneOf", "anyOf", "allOf"):
            for idx, branch in enumerate(node.get(comb, [])):
                walk(branch, f"{path}/{comb}/{idx}", {**parent, **props}, seen)
        for sub in ("items", "additionalProperties"):
            if isinstance(node.get(sub), dict):
                walk(node[sub], f"{path}/{sub}", seen=seen)

    walk(schema, "$")
    return sorted(set(missing))


def test_all_105_action_routes_and_effects(tmp_path, monkeypatch):
    v13_names = ("sessions.space.get", "sessions.space.set", "workflows.health.get")
    all_specs = [*business_action_specs().values(), *(v13_action_spec(name) for name in v13_names)]
    assert len(all_specs) == 105

    domain_map = business_domain_actions()
    assert len(domain_map["business_space"]) == 40
    assert len(domain_map["business_execution"]) == 26
    assert len(domain_map["business_activity"]) == 38
    assert domain_map["business_assets_versions_accept"] == ("assets.versions.accept",)
    assert sum(len(v) for v in domain_map.values()) == 105

    agent = _make_full_business_agent(tmp_path, monkeypatch, all_105_ready=True)
    snapshot = agent.tools.runtime_snapshot(run_id="accept-policy-run")

    for target_name in (
        "business_space",
        "business_execution",
        "business_activity",
        "business_assets_versions_accept",
        "read_business_asset",
    ):
        runtime = snapshot.runtime(target_name)
        assert runtime is not None and runtime.availability.available, target_name
        assert _schema_undescribed_paths(runtime.model_spec.input_schema) == [], target_name
        assert runtime.model_spec.hints.examples, target_name
        for ex in runtime.model_spec.hints.examples:
            assert validate_tool_input(json.loads(ex), runtime.model_spec.input_schema).ok, (target_name, ex)

    for domain_name in ("business_space", "business_execution", "business_activity"):
        enum_actions = snapshot.runtime(domain_name).model_spec.input_schema["properties"]["action"]["enum"]
        assert tuple(enum_actions) == domain_map[domain_name]
        assert "assets.versions.accept" not in enum_actions

    for spec in all_specs:
        target = business_domain_tool(spec.action)
        runtime = snapshot.runtime(target)
        assert runtime is not None
        sample = _sample_business_request(spec)
        assert runtime.handler.validate_invocation(sample, None) is None, spec.action
        resolved = tool_effect_for_runtime_policy(runtime.runtime_policy, sample)
        expected_effect = _business_action_effect(spec)
        assert resolved == expected_effect, (spec.action, resolved, expected_effect)
        if spec.action in {"approvals.decide", "policies.apply", "assets.versions.accept"}:
            assert resolved == "dangerous"
        elif spec.metadata["effect"] == "read":
            assert resolved == "read_only"
        elif spec.metadata["effect"] == "execute":
            assert resolved == "dangerous"
        else:
            assert resolved == "mutating"


def test_acceptance_always_requires_human(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context
    from agent_py_agent.agent.gateway_parts.business_artifacts_http import upload_artifact
    from agent_py_agent.agent.gateway_parts.permission_bridge import (
        write_gateway_permission_decision,
    )
    from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    agent = _make_full_business_agent(tmp_path, monkeypatch, all_105_ready=True)
    space_call = execute_registry_test_call(
        agent.tools,
        "business_space",
        {
            "action": "spaces.create",
            "request_id": "req-space-accept",
            "idempotency_key": "idem-space-accept-0001",
            "params": {"name": "Acceptance Space"},
        },
        call_id="call-space-accept-01",
        operation_id="op-space-accept-01",
        idempotency_key="idem-space-accept-0001",
    )
    assert space_call.ok, space_call.output
    space = json.loads(space_call.output)["data"]
    space_id = space["space_id"]

    context, services = local_business_context(agent, model=True)
    uploaded = upload_artifact(
        BytesIO(b"accepted content body\n"),
        {"space_id": space_id, "idempotency_key": "idem-upload-accept-01"},
        "spec.txt",
        context,
        services,
    )
    ref = {k: uploaded[k] for k in ("artifact_id", "version_ref", "content_sha256")}
    asset_call = execute_registry_test_call(
        agent.tools,
        "business_space",
        {
            "action": "assets.register",
            "request_id": "req-asset-reg",
            "idempotency_key": "idem-asset-reg-0001",
            "params": {
                "space_id": space_id,
                "name": "Spec Doc",
                "kind": "document",
                "artifact_ref": ref,
            },
        },
        call_id="call-asset-reg-01",
        operation_id="op-asset-reg-01",
        idempotency_key="idem-asset-reg-0001",
    )
    assert asset_call.ok, asset_call.output
    asset_res = json.loads(asset_call.output)
    assert asset_res["error"] is None, asset_res
    asset_id = asset_res["data"]["asset_id"]
    version_ref = ref["version_ref"]

    accept_req = {
        "action": "assets.versions.accept",
        "request_id": "req-accept-0001",
        "idempotency_key": "idem-accept-version-0001",
        "expected_revision": 0,
        "params": {
            "space_id": space_id,
            "asset_id": asset_id,
            "version_ref": version_ref,
        },
    }

    # 1) business_space 绝不允许混入或代执行 assets.versions.accept。
    wrong_family = execute_registry_test_call(
        agent.tools,
        "business_space",
        accept_req,
        call_id="call-wrong-family-01",
        operation_id="op-wrong-family-01",
        idempotency_key="idem-wrong-family-0001",
    )
    assert not wrong_family.ok
    assert wrong_family.effect_outcome == "not_started"
    direct_wrong_family = agent.tools.tools["business_space"].validate_invocation(accept_req, None)
    assert direct_wrong_family is not None and not direct_wrong_family.ok
    assert "business_assets_versions_accept" in (
        direct_wrong_family.recovery_hint + direct_wrong_family.output
    )

    # 2) ActionPolicy 在 ask、auto 乃至 admin_full_access=True 下均强制要求真实人类确认。
    snapshot = agent.tools.runtime_snapshot(run_id="accept-policy-run")
    accept_rt = snapshot.runtime("business_assets_versions_accept")
    assert accept_rt is not None
    policy_engine = ActionPolicy()
    call = canonical_test_call(
        snapshot,
        "business_assets_versions_accept",
        accept_req,
        call_id="call-accept-1",
        operation_id="op-accept-1",
        idempotency_key="idem-accept-version-0001",
    )
    for mode, full_admin in (("ask", False), ("auto", False), ("auto", True)):
        decision = policy_engine.decide(
            ActionPolicyRequest(
                call=call,
                runtime_snapshot=snapshot,
                workspace_root=tmp_path,
                approval_mode=mode,
                admin_full_access=full_admin,
                owner_id=agent.home_paths.owner_id,
            )
        )
        assert decision.status == "ask", (mode, full_admin)
        assert decision.resolved_effect == "dangerous"
        assert decision.evidence.get("human_confirmation_required") is True
    assert not accept_rt.handler.execute(accept_req).ok

    # 3) approvals.decide 与 policies.apply 具有 dangerous 地板，但不强制 require_human=True。
    space_rt = snapshot.runtime("business_space")
    assert space_rt is not None
    for gov_action in ("approvals.decide", "policies.apply"):
        gov_args = _sample_business_request(business_action_spec(gov_action))
        gov_call = canonical_test_call(
            snapshot,
            "business_space",
            gov_args,
            call_id=f"call-{gov_action}",
            operation_id=f"op-{gov_action}",
            idempotency_key=gov_args["idempotency_key"],
        )
        ask_dec = policy_engine.decide(
            ActionPolicyRequest(
                call=gov_call,
                runtime_snapshot=snapshot,
                workspace_root=tmp_path,
                approval_mode="ask",
                owner_id=agent.home_paths.owner_id,
            )
        )
        auto_dec = policy_engine.decide(
            ActionPolicyRequest(
                call=gov_call,
                runtime_snapshot=snapshot,
                workspace_root=tmp_path,
                approval_mode="auto",
                owner_id=agent.home_paths.owner_id,
            )
        )
        assert ask_dec.status == "ask" and ask_dec.resolved_effect == "dangerous"
        assert auto_dec.status == "allow" and auto_dec.resolved_effect == "dangerous"

    # 4) 真实人类批准绑定签发后可正常完成资产版本采用。
    accept_tool = accept_rt.handler
    pending = execute_canonical_test_call(
        agent.root,
        tools={accept_tool.model_spec.name: accept_tool},
        tool_name=accept_tool.model_spec.name,
        arguments=accept_req,
        operation_id="op-accept-human-1",
        idempotency_key="idem-accept-version-0001",
        operation_owner_id=agent.home_paths.owner_id,
    )
    assert pending.decision.status == "ask" and pending.decision.evidence["human_confirmation_required"]
    req_obj = build_tool_approval_request(
        pending.call,
        request_id="req-accept-0001",
        round_number=1,
        call_index=0,
        description="确认正式采用资产版本",
        require_human=True,
        owner_id=agent.home_paths.owner_id,
    )
    chunk_path = tmp_path / "human-approval-chunks.jsonl"
    writer = BufferedChunkStreamWriter(chunk_path, interactive_approvals=True)
    decision_holder: dict = {}
    worker = threading.Thread(target=lambda: decision_holder.update(writer.request_permission(req_obj.to_dict())))
    worker.start()
    deadline = time.monotonic() + 3
    while not chunk_path.exists() or "permission_requested" not in chunk_path.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    write_gateway_permission_decision(
        chunk_path,
        req_obj,
        ToolApprovalDecision(req_obj.permission_id, "approved"),
    )
    worker.join(3)
    assert not worker.is_alive()
    binding = req_obj.approved_binding(ToolApprovalDecision.from_mapping(decision_holder))

    accepted = execute_canonical_test_call(
        agent.root,
        tools={accept_tool.model_spec.name: accept_tool},
        tool_name=accept_tool.model_spec.name,
        arguments=accept_req,
        write_boundary={"approved_actions": [binding]},
        operation_id="op-accept-human-1",
        idempotency_key="idem-accept-version-0001",
        operation_owner_id=agent.home_paths.owner_id,
    )
    assert accepted.result.ok, accepted.result.output
    assert json.loads(accepted.result.output)["data"]["accepted_version"] == version_ref


def test_exact_branch_validation_before_claim(tmp_path, monkeypatch):
    agent = _make_full_business_agent(tmp_path, monkeypatch)
    claims: list[object] = []
    orig_claim = agent.tools.operation_store.claim_tool_operation

    def spy_claim(*args, **kwargs):
        claims.append((args, kwargs))
        return orig_claim(*args, **kwargs)

    monkeypatch.setattr(agent.tools.operation_store, "claim_tool_operation", spy_claim)

    invalid_cases = [
        # 分支外多余字段
        (
            "business_space",
            {
                "action": "spaces.create",
                "request_id": "bad-extra-1",
                "idempotency_key": "idem-bad-extra-0001",
                "params": {"name": "x", "undeclared_field": 1},
            },
        ),
        # 缺少条件必填 expected_revision
        (
            "business_space",
            {
                "action": "spaces.update",
                "request_id": "bad-rev-1",
                "idempotency_key": "idem-bad-rev-00001",
                "params": {"space_id": "sp-1", "name": "updated"},
            },
        ),
        # uniqueItems 重复元素（展示层投影为 x-business-unique-items，预检按原 native schema 拦在 claim 前）
        (
            "business_space",
            {
                "action": "spaces.packages.attach",
                "request_id": "bad-uniq-1",
                "idempotency_key": "idem-bad-uniq-0001",
                "expected_revision": 0,
                "params": {"space_id": "sp-1", "package_refs": ["pkg-a", "pkg-a"]},
            },
        ),
        # x-max-bytes 超限（ASCII 字符数 <= maxLength 但多字节 UTF-8 超 x-max-bytes）
        (
            "business_space",
            {
                "action": "spaces.get",
                "request_id": "bad-bytes-1",
                "params": {"space_id": "界" * 60},
            },
        ),
        # params 内注入宿主身份字段
        (
            "business_space",
            {
                "action": "spaces.create",
                "request_id": "bad-id-inj-1",
                "idempotency_key": "idem-bad-id-000001",
                "params": {"name": "ok", "owner_id": "other-owner"},
            },
        ),
    ]

    for idx, (tool_name, bad_req) in enumerate(invalid_cases):
        res = execute_registry_test_call(
            agent.tools,
            tool_name,
            bad_req,
            call_id=f"preclaim-{idx}",
            operation_id=f"op-preclaim-{idx}",
            idempotency_key=f"idem-exec-preclaim-{idx:04d}",
        )
        assert not res.ok
        assert res.error_code in {
            "TOOL_INVALID_ARGUMENTS",
            "TOOL_PARAMETER_REQUIRED",
            "TOOL_UNKNOWN_PARAMETER",
            "TOOL_PARAMETER_TYPE_MISMATCH",
        }
        assert res.effect_outcome == "not_started"
        assert claims == [], f"claim_tool_operation was called on invalid case {idx}"

    # 直接调用 validate_invocation 时必须保留字段路径、约束及不含秘密的可直接验证示例。
    space_tool = agent.tools.tools["business_space"]
    direct_bad = space_tool.validate_invocation(invalid_cases[2][1], None)
    assert direct_bad is not None and not direct_bad.ok
    assert direct_bad.effect_outcome == "not_started"
    details = direct_bad.result_envelope["error_details"]
    assert details["field_path"] == "$.params.package_refs"
    assert details["constraint"] == "uniqueItems"
    example = details["example"]
    assert validate_tool_input(example, space_tool.model_spec.input_schema).ok

    # 校验器意外内部异常走 W0 的 TOOL_INVOCATION_VALIDATOR_FAILED 非可重试契约且不进入 claim。
    monkeypatch.setattr(
        space_tool,
        "validate_invocation",
        lambda _params, _ctx: (_ for _ in ()).throw(RuntimeError("unexpected validator error")),
    )
    broken = execute_registry_test_call(
        agent.tools,
        "business_space",
        {
            "action": "spaces.create",
            "request_id": "validator-crash-1",
            "idempotency_key": "idem-validator-crash-01",
            "params": {"name": "never-claimed"},
        },
        call_id="validator-crash-call",
        operation_id="op-validator-crash",
        idempotency_key="idem-validator-crash-01",
    )
    assert not broken.ok
    assert broken.error_code == "TOOL_INVOCATION_VALIDATOR_FAILED"
    assert broken.effect_outcome == "not_started"
    assert broken.retryable is False
    assert claims == []


@pytest.mark.parametrize("enable_self_map", [False, True])
def test_workflow_envelope_is_preserved(tmp_path, monkeypatch, enable_self_map):
    agent = _make_full_business_agent(tmp_path, monkeypatch, enable_self_map=enable_self_map)
    activity = agent.tools.tools["business_activity"]
    enum_actions = set(activity.model_spec.input_schema["properties"]["action"]["enum"])
    dotted_workflows = {
        act for act in business_domain_actions()["business_activity"] if act.startswith(("workflows.", "workflow_runs."))
    }
    assert "workflows.health.get" in dotted_workflows
    assert len(dotted_workflows - {"workflows.health.get"}) == 21
    assert dotted_workflows <= enum_actions

    wf_list = execute_registry_test_call(
        agent.tools,
        "business_activity",
        {"action": "workflows.list", "request_id": "wf-req-list-1", "params": {}},
        call_id="call-wf-list-1",
    )
    assert wf_list.ok, wf_list.output
    list_env = json.loads(wf_list.output)
    assert list_env["request_id"] == "wf-req-list-1"
    assert list_env["error"] is None
    assert "items" in list_env["data"]

    # V13 workflows.health.get 亦并入 business_activity 且保留 workflow_ref 必填约束。
    missing_wf_ref = execute_registry_test_call(
        agent.tools,
        "business_activity",
        {"action": "workflows.health.get", "request_id": "wf-req-health-1", "params": {}},
        call_id="call-wf-health-1",
    )
    assert not missing_wf_ref.ok
    assert missing_wf_ref.effect_outcome == "not_started"

    legacy_get = execute_registry_test_call(
        agent.tools,
        "business_workflows_cleanup_policy_get",
        {"request_id": "wf-legacy-get-01", "params": {}},
        call_id="call-wf-legacy-get-01",
    )
    assert legacy_get.ok, legacy_get.output
    legacy_env = json.loads(legacy_get.output)
    assert legacy_env["request_id"] == "wf-legacy-get-01"
    current_policy = dict(legacy_env["data"]["policy"])

    # 16..128 位幂等键约束原样保留，过短幂等键在执行前拒绝。
    short_idem = execute_registry_test_call(
        agent.tools,
        "business_activity",
        {
            "action": "workflows.cleanup.policy.set",
            "request_id": "wf-short-idem",
            "idempotency_key": "short",
            "expected_revision": 0,
            "params": {"policy": {**current_policy, "orphan_after_days": 30}},
        },
        call_id="call-wf-short-1",
        operation_id="op-wf-short",
        idempotency_key="short-idem-outer-0001",
    )
    assert not short_idem.ok
    assert short_idem.effect_outcome == "not_started"

    # 合法 16..128 位幂等键与原业务请求信封（request_id + params）在规范入口与历史别名入口一致工作。
    valid_set = execute_registry_test_call(
        agent.tools,
        "business_activity",
        {
            "action": "workflows.cleanup.policy.set",
            "request_id": "wf-policy-set-01",
            "idempotency_key": "wf-policy-idem-0001",
            "expected_revision": 0,
            "params": {"policy": {**current_policy, "orphan_after_days": 45}},
        },
        call_id="call-wf-policy-set-01",
        operation_id="op-wf-policy-set-01",
        idempotency_key="wf-policy-idem-0001",
    )
    assert valid_set.ok, valid_set.output
    set_env = json.loads(valid_set.output)
    assert set_env["request_id"] == "wf-policy-set-01"
    assert set_env["effect_outcome"] == "committed"

    legacy_get_after = execute_registry_test_call(
        agent.tools,
        "business_workflows_cleanup_policy_get",
        {"request_id": "wf-legacy-get-02", "params": {}},
        call_id="call-wf-legacy-get-02",
    )
    assert legacy_get_after.ok, legacy_get_after.output
    assert json.loads(legacy_get_after.output)["data"]["policy"]["orphan_after_days"] == 45


def test_legacy_operation_receipt_identity(tmp_path, monkeypatch):
    agent = _make_full_business_agent(tmp_path, monkeypatch)
    space_tool = agent.tools.tools["business_space"]

    canonical_args = {
        "action": "spaces.create",
        "request_id": "receipt-same-01",
        "idempotency_key": "shared-space-idem-0001",
        "params": {"name": "Unified Identity Space"},
    }
    legacy_input = {
        "request_id": "receipt-same-01",
        "idempotency_key": "shared-space-idem-0001",
        "params": {"name": "Unified Identity Space"},
    }
    decoded = decode_retired_tool_call(
        "business_spaces_create",
        legacy_input,
        available=frozenset(agent.tools.tools),
    )
    assert decoded is not None
    target_tool, decoded_args, action_name = decoded
    assert target_tool == "business_space"
    assert action_name == "spaces.create"
    assert canonical_json(decoded_args) == canonical_json(canonical_args)
    assert space_tool.operation_identity_for(canonical_args) == "business_spaces_create"
    assert space_tool.operation_identity_for(decoded_args) == "business_spaces_create"

    from agent_py_agent.agent.gateway_parts import business_api as business_api_mod

    handler_calls: list[dict] = []
    orig_execute_business = business_api_mod.execute_business

    def spy_execute_business(req, *args, **kwargs):
        if isinstance(req, dict) and req.get("action") == "spaces.create":
            handler_calls.append(dict(req))
        return orig_execute_business(req, *args, **kwargs)

    monkeypatch.setattr(business_api_mod, "execute_business", spy_execute_business)

    first = execute_registry_test_call(
        agent.tools,
        "business_space",
        canonical_args,
        run_id="w1-run-0001",
        call_id="call-shared-space-1",
        operation_id="op-shared-space-1",
        idempotency_key="shared-space-idem-0001",
    )
    assert first.ok, first.output
    assert len(handler_calls) == 1
    first_space_id = json.loads(first.output)["data"]["space_id"]

    saved = agent.tools.operation_store.get_tool_operation(
        owner_id=agent.home_paths.owner_id,
        run_id="w1-run-0001",
        operation_id="op-shared-space-1",
    )
    assert saved is not None
    assert saved.tool == "business_spaces_create"

    replayed = execute_registry_test_call(
        agent.tools,
        "business_spaces_create",
        legacy_input,
        run_id="w1-run-0001",
        call_id="call-shared-space-2",
        operation_id="op-shared-space-1",
        idempotency_key="shared-space-idem-0001",
    )
    assert replayed.ok, replayed.output
    assert len(handler_calls) == 1
    assert json.loads(replayed.output)["data"]["space_id"] == first_space_id

    listed = json.loads(
        execute_registry_test_call(
            agent.tools,
            "business_space",
            {"action": "spaces.list", "request_id": "list-after-replay", "params": {}},
            call_id="call-list-after-replay",
        ).output
    )["data"]["items"]
    assert [row["space_id"] for row in listed] == [first_space_id]

    snapshot = agent.tools.runtime_snapshot(run_id="receipt-snapshot-run")
    runtime = snapshot.runtime("business_space")
    receipt = inspect_registered_business_receipt(runtime, canonical_args)
    assert receipt is not None
    assert receipt["state"] == "completed"
    assert receipt["target_refs"] == [first_space_id]

    forged = SimpleNamespace(handler=SimpleNamespace(inspect_operation_receipt=lambda _req: {"forged": True}))
    assert inspect_registered_business_receipt(forged, canonical_args) is None


def test_wake_action_ceiling(tmp_path, monkeypatch):
    agent = _make_full_business_agent(tmp_path, monkeypatch)
    decision = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(reason="session_task")
    )
    allowed = set(decision.allowed_tools)
    assert {"business_space", "business_execution"} <= allowed
    assert "business_activity" not in allowed
    assert "business_assets_versions_accept" not in allowed

    expected_space_actions = {
        "spaces.get",
        "spaces.list",
        "spaces.capabilities.resolve",
        "spaces.usage.list",
        "spaces.packages.catalog",
    }
    expected_exec_actions = {
        "background_agents.list",
        "background_agents.get",
        "tasks.assign",
        "tasks.follow_up",
        "tasks.get",
        "tasks.list",
        "messages.list",
    }
    expected_tokens = {f"business_space:{a}" for a in expected_space_actions} | {
        f"business_execution:{a}" for a in expected_exec_actions
    }
    assert expected_tokens <= set(SESSION_TASK_WAKE_ALLOWED_TOOLS)
    assert expected_tokens <= allowed
    for excluded in (
        "business_execution:tasks.submit",
        "business_execution:background_agents.create",
        "business_space:spaces.create",
        "business_space:approvals.decide",
        "business_space:policies.apply",
    ):
        assert excluded not in allowed

    snapshot = agent.tools.runtime_snapshot(allowed_tools=list(decision.allowed_tools))
    space_rt = snapshot.runtime("business_space")
    exec_rt = snapshot.runtime("business_execution")
    assert space_rt is not None and exec_rt is not None
    assert set(space_rt.model_spec.input_schema["properties"]["action"]["enum"]) == expected_space_actions
    assert set(exec_rt.model_spec.input_schema["properties"]["action"]["enum"]) == expected_exec_actions
    assert snapshot.runtime("business_activity") is None
    assert snapshot.runtime("business_assets_versions_accept") is None

    ok_read = execute_registry_test_call(
        agent.tools,
        "business_space",
        {"action": "spaces.list", "request_id": "wake-ok-1", "params": {}},
        allowed_tools=list(decision.allowed_tools),
    )
    assert ok_read.ok, ok_read.output

    blocked_write = execute_registry_test_call(
        agent.tools,
        "business_space",
        {
            "action": "spaces.create",
            "request_id": "wake-denied-1",
            "idempotency_key": "wake-denied-idem-0001",
            "params": {"name": "Forbidden In Wake"},
        },
        allowed_tools=list(decision.allowed_tools),
        operation_id="op-wake-denied-1",
        idempotency_key="wake-denied-idem-0001",
    )
    assert not blocked_write.ok
    assert blocked_write.effect_outcome == "not_started"

    # 任务级 allowed_tools 传入裸域工具名时只能维持上界，绝不能放宽唤醒动作上界；disabled_tools 可继续收窄单动作。
    kept_ceiling = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(
            reason="session_task",
            policy_snapshot={"allowed_tools": ["business_space"]},
            owner_policy=SimpleNamespace(disabled_tools=("business_spaces_get",)),
        )
    )
    narrowed_snap = agent.tools.runtime_snapshot(allowed_tools=list(kept_ceiling.allowed_tools))
    narrowed_space = narrowed_snap.runtime("business_space")
    assert narrowed_space is not None
    assert set(narrowed_space.model_spec.input_schema["properties"]["action"]["enum"]) == (
        expected_space_actions - {"spaces.get"}
    )
