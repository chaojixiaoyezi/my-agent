"""W6 集成验收测试：原生与 HTTP 往返契约、104 项双版本别名迁移、只读动作上界、单故障副作用重放与提示词快照复用。"""
from __future__ import annotations

import errno
import http.client
import json
import os
import threading
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import (
    _dedup_native_recommendations_section,
)
from agent_py_agent.agent.agent_core.orchestration.tool_grants import (
    subagent_allowed_tools,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.backends.openai_chat import _openai_tools_from_native
from agent_py_agent.agent.backends.tool_schema import (
    tool_model_specs_to_anthropic_tools,
)
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation.background_tool_policy import (
    BackgroundToolPolicyRequest,
    background_tool_policy_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import business_api as business_api_mod
from agent_py_agent.agent.gateway_parts.business_api import (
    _business_action_effect,
    _sample_business_request,
    inspect_registered_business_receipt,
    local_business_context,
    register_business_tools,
)
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec
from agent_py_agent.agent.gateway_parts.business_artifacts_http import upload_artifact
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.permission_bridge import (
    write_gateway_permission_decision,
)
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage.tool_operations import (
    TOOL_OPERATION_FAILED,
    TOOL_OPERATION_SUCCEEDED,
    TOOL_OPERATION_UNKNOWN,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.plugin_host.business_action_specs import (
    business_action_specs,
)
from agent_py_agent.agent.plugin_host.schemas import canonical_json
from agent_py_agent.agent.prompting_parts.builder import PromptBuildRequest, ToolSections
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
    ToolPolicyRequest,
    scheduled_child_tools,
)
from agent_py_agent.agent.tooling import _filesystem_patch as fs_patch_module
from agent_py_agent.agent.tooling import registry as registry_module
from agent_py_agent.agent.tooling._filesystem_write import (
    _atomic_write_bytes as real_atomic_write_bytes,
)
from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.input_schema import validate_tool_input
from agent_py_agent.agent.tooling.mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPToolInfo,
    _normalize_call_result,
)
from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool
from agent_py_agent.agent.tooling.models import (
    ToolModelSpec,
    ToolRuntimeSnapshot,
    tool_effect_for_runtime_policy,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.registry_auth import (
    action_ceilings,
    intersect_tool_grants,
    normalize_tool_grants,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
    tool_arguments_hash,
)
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.versioned_input_decoder import (
    business_domain_actions,
    business_domain_tool,
    decode_retired_tool_call,
    retired_tool_aliases,
)
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_canonical_test_call,
    execute_registry_test_call,
)

_V13_ACTION_NAMES = ("sessions.space.get", "sessions.space.set", "workflows.health.get")


def _all_105_business_specs() -> list[Any]:
    return [
        *business_action_specs().values(),
        *(v13_action_spec(name) for name in _V13_ACTION_NAMES),
    ]


def _make_business_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tag: str,
    business_spaces_enabled: bool = True,
    enable_self_map: bool = True,
    all_105_ready: bool = True,
    user_id: str = "local-user",
) -> SimpleAgent:
    home = tmp_path / f"home-{tag}"
    ws = tmp_path / f"ws-{tag}"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / f"os-home-{tag}"))
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(home),
        memory_path="memory.jsonl",
        execution_mode="local_unmanaged",
        business_spaces_enabled=business_spaces_enabled,
        enable_self_map=enable_self_map,
        gateway_http_socket_timeout_seconds=15.0,
        gateway_http_queue_timeout_seconds=15.0,
        gateway_http_birth_gate_timeout_seconds=10.0,
    )
    agent = SimpleAgent(config, ws)
    if business_spaces_enabled:
        agent.config.business_host_ports_enabled = True
        agent.business_operation_store = agent.tools.operation_store
        agent.business_approval_decider = lambda req_body, actor=None: {
            "approval_id": req_body["approval_id"],
            "state": "approved" if req_body["decision"] == "approve" else "rejected",
            "decided_by": getattr(actor, "actor_id", user_id),
        }
        if all_105_ready:
            agent.business_governance = SimpleNamespace()
            agent.business_projection = SimpleNamespace(query=lambda *_a, **_k: {})
        agent._local_business_services_cache = None
        register_business_tools(agent)
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": user_id,
            "channel": "tui",
            "channel_conversation_id": f"conv-{tag}",
            "channel_user_id": user_id,
            "owner_id": agent.home_paths.owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
        }
    )
    agent._current_run_params = RunParams(
        source="gateway",
        request_id=f"req-{tag}",
        task_attributes={"conversation_thread_id": thread.thread_id},
    )
    return agent


def _http_business_transport(server: GatewayHTTPServer):
    port = server.server.server_address[1]
    token = server._local_client_credential

    def _send(path: str, request_body: dict[str, Any], timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        headers = {
            "Content-Type": "application/json",
            "Connection": "close",
            "X-Gateway-Token": token,
        }
        try:
            conn.request(
                "POST",
                path,
                json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
                headers,
            )
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    return _send


def test_native_and_http_wire_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1) 验证 105 个业务动作的精确分域映射（40 + 26 + 38 + 1）
    all_specs = _all_105_business_specs()
    assert len(all_specs) == 105
    domain_map = business_domain_actions()
    assert len(domain_map["business_space"]) == 40
    assert len(domain_map["business_execution"]) == 26
    assert len(domain_map["business_activity"]) == 38
    assert domain_map["business_assets_versions_accept"] == ("assets.versions.accept",)
    assert sum(len(items) for items in domain_map.values()) == 105

    # 2) 验证 business_spaces_enabled=False 与 enable_self_map=False/True 及就绪门禁下的原生序列化
    disabled_agent = _make_business_agent(
        tmp_path,
        monkeypatch,
        tag="biz-off",
        business_spaces_enabled=False,
    )
    unready_agent = _make_business_agent(
        tmp_path,
        monkeypatch,
        tag="biz-unready",
        business_spaces_enabled=True,
        enable_self_map=False,
        all_105_ready=False,
    )
    full_agent = _make_business_agent(
        tmp_path,
        monkeypatch,
        tag="biz-on",
        business_spaces_enabled=True,
        enable_self_map=True,
        all_105_ready=True,
    )
    server = GatewayHTTPServer(
        0,
        gateway_paths_from_root(full_agent.home_paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=full_agent),
    )
    server.start()
    try:
        # 2a) 关闭业务空间时无业务域工具暴露，原生 Anthropic/OpenAI 转换均合法且零重复
        off_snap = disabled_agent.tools.runtime_snapshot(run_id="run-wire-off")
        assert [n for n in sorted(off_snap.available_tool_names) if n.startswith("business_")] == []
        off_anthropic = tool_model_specs_to_anthropic_tools(
            disabled_agent.tools.specs(include_orchestration=True, runtime_snapshot=off_snap)
        )
        off_openai = _openai_tools_from_native(off_anthropic)
        assert len(off_anthropic) == len(off_openai) == 63

        # 2b) enable_self_map=False 且治理/投影未就绪时：21 个点号工作流动作 + workflows.health.get 仍在 business_activity，
        #     而依赖就绪服务的治理动作与未满足门禁的动作按真实就绪状态准确收缩
        unready_snap = unready_agent.tools.runtime_snapshot(run_id="run-wire-unready")
        unready_activity = unready_snap.runtime("business_activity")
        assert unready_activity is not None and unready_activity.availability.available
        unready_activity_enum = set(unready_activity.model_spec.input_schema["properties"]["action"]["enum"])
        dotted_wf = {
            act for act in domain_map["business_activity"] if act.startswith(("workflows.", "workflow_runs."))
        }
        assert len(dotted_wf) == 22
        assert dotted_wf <= unready_activity_enum
        unready_space_enum = set(
            unready_snap.runtime("business_space").model_spec.input_schema["properties"]["action"]["enum"]
        )
        assert "policies.apply" not in unready_space_enum
        assert "spaces.list" in unready_space_enum

        # 2c) 全部 105 动作就绪时：原生 Anthropic 与 OpenAI 完整序列化、受限授权缩窄 Schema 序列化
        full_snap = full_agent.tools.runtime_snapshot(run_id="run-wire-full")
        assert "read_business_asset" in full_snap.available_tool_names
        assert {
            "business_space",
            "business_execution",
            "business_activity",
            "business_assets_versions_accept",
        } <= full_snap.available_tool_names
        full_specs = full_agent.tools.specs(runtime_snapshot=full_snap)
        anthropic_full = tool_model_specs_to_anthropic_tools(full_specs)
        openai_full = _openai_tools_from_native(anthropic_full)
        by_anthropic = {item["name"]: item for item in anthropic_full}
        by_openai = {item["function"]["name"]: item["function"] for item in openai_full}
        for domain_name in ("business_space", "business_execution", "business_activity"):
            assert tuple(by_anthropic[domain_name]["input_schema"]["properties"]["action"]["enum"]) == domain_map[domain_name]
            assert tuple(by_openai[domain_name]["parameters"]["properties"]["action"]["enum"]) == domain_map[domain_name]
            assert "assets.versions.accept" not in by_anthropic[domain_name]["input_schema"]["properties"]["action"]["enum"]

        narrow_snap = full_agent.tools.runtime_snapshot(
            allowed_tools=["read_file", "business_spaces_list", "business_spaces_get"],
            run_id="run-wire-narrow",
        )
        narrow_anthropic = {
            item["name"]: item
            for item in tool_model_specs_to_anthropic_tools(full_agent.tools.specs(runtime_snapshot=narrow_snap))
        }
        assert narrow_anthropic["business_space"]["input_schema"]["properties"]["action"]["enum"] == [
            "spaces.get",
            "spaces.list",
        ]
        assert len(narrow_anthropic["business_space"]["input_schema"]["oneOf"]) == 2

        # 2d) 校验全部 105 动作的示例请求在各自目标工具上通过预检且风险等级精确对齐
        for spec in all_specs:
            target_tool = business_domain_tool(spec.action)
            rt = full_snap.runtime(target_tool)
            assert rt is not None
            sample_req = _sample_business_request(spec)
            assert rt.handler.validate_invocation(sample_req, None) is None, spec.action
            assert tool_effect_for_runtime_policy(rt.runtime_policy, sample_req) == _business_action_effect(spec)

        # 2e) 自定义 x-max-bytes / x-business-unique-items 在进入 operation_store claim 前被确定性拦截
        claims: list[Any] = []
        orig_claim = full_agent.tools.operation_store.claim_tool_operation
        monkeypatch.setattr(
            full_agent.tools.operation_store,
            "claim_tool_operation",
            lambda *a, **kw: (claims.append(1), orig_claim(*a, **kw))[1],
        )
        over_bytes = execute_registry_test_call(
            full_agent.tools,
            "business_space",
            {"action": "spaces.get", "request_id": "wire-bytes-1", "params": {"space_id": "界" * 60}},
            call_id="wire-bytes-call",
            operation_id="wire-bytes-op",
            idempotency_key="wire-bytes-idem-0001",
        )
        dup_items = execute_registry_test_call(
            full_agent.tools,
            "business_space",
            {
                "action": "spaces.packages.attach",
                "request_id": "wire-uniq-1",
                "idempotency_key": "wire-uniq-idem-0001",
                "expected_revision": 0,
                "params": {"space_id": "sp-1", "package_refs": ["pkg-dup", "pkg-dup"]},
            },
            call_id="wire-uniq-call",
            operation_id="wire-uniq-op",
            idempotency_key="wire-uniq-idem-0001",
        )
        assert not over_bytes.ok and over_bytes.effect_outcome == "not_started"
        assert not dup_items.ok and dup_items.effect_outcome == "not_started"
        assert claims == []
        monkeypatch.setattr(full_agent.tools.operation_store, "claim_tool_operation", orig_claim)

        # 3) 真实本地 HTTP 客户端 -> Gateway /client/business -> execute_business -> 标准信封解析往返，并与原生工具共享状态
        client = BusinessClient(_http_business_transport(server))
        prepared_create = client.prepare(
            "spaces.create",
            {"name": "WireHTTP空间"},
            idempotency_key="wire-http-space-idem-0001",
        )
        created_receipt = client.send(prepared_create)
        assert created_receipt["ok"] is True, created_receipt
        assert created_receipt["http_status"] == 200
        assert created_receipt["state"] == "completed"
        assert created_receipt["effect_outcome"] == "committed"
        space_id = created_receipt["data"]["space_id"]

        list_receipt = client.call("spaces.list", {})
        assert list_receipt["ok"] is True
        assert any(item["space_id"] == space_id for item in list_receipt["data"]["items"])

        tool_get = execute_registry_test_call(
            full_agent.tools,
            "business_space",
            {"action": "spaces.get", "request_id": "wire-native-get-01", "params": {"space_id": space_id}},
            call_id="wire-native-get-call",
        )
        assert tool_get.ok, tool_get.output
        tool_get_data = json.loads(tool_get.output)["data"]
        fetched_space = tool_get_data.get("space", tool_get_data)
        assert fetched_space["space_id"] == space_id

        # 4) 独立工具 business_assets_versions_accept 在 ask/auto/admin_full_access=True 下均强制要求人类确认，签票后完成采用并可经 read_business_asset 读回
        ctx, services = local_business_context(full_agent, model=True)
        uploaded = upload_artifact(
            BytesIO(b"wire-verified asset content\n"),
            {"space_id": space_id, "idempotency_key": "wire-upload-idem-0001"},
            "wire_doc.txt",
            ctx,
            services,
        )
        artifact_ref = {k: uploaded[k] for k in ("artifact_id", "version_ref", "content_sha256")}
        reg_asset = execute_registry_test_call(
            full_agent.tools,
            "business_space",
            {
                "action": "assets.register",
                "request_id": "wire-reg-asset-01",
                "idempotency_key": "wire-reg-asset-idem-0001",
                "params": {
                    "space_id": space_id,
                    "name": "Wire Asset",
                    "kind": "document",
                    "artifact_ref": artifact_ref,
                },
            },
            call_id="wire-reg-asset-call",
            operation_id="wire-reg-asset-op",
            idempotency_key="wire-reg-asset-idem-0001",
        )
        assert reg_asset.ok, reg_asset.output
        asset_id = json.loads(reg_asset.output)["data"]["asset_id"]
        accept_req = {
            "action": "assets.versions.accept",
            "request_id": "wire-accept-01",
            "idempotency_key": "wire-accept-idem-0001",
            "expected_revision": 0,
            "params": {
                "space_id": space_id,
                "asset_id": asset_id,
                "version_ref": artifact_ref["version_ref"],
            },
        }
        accept_rt = full_snap.runtime("business_assets_versions_accept")
        assert accept_rt is not None
        accept_call = canonical_test_call(
            full_snap,
            "business_assets_versions_accept",
            accept_req,
            call_id="wire-accept-call",
            operation_id="wire-accept-op",
            idempotency_key="wire-accept-idem-0001",
        )
        policy_engine = ActionPolicy()
        for approval_mode, admin_full in (("ask", False), ("auto", False), ("auto", True)):
            dec = policy_engine.decide(
                ActionPolicyRequest(
                    call=accept_call,
                    runtime_snapshot=full_snap,
                    workspace_root=tmp_path,
                    approval_mode=approval_mode,
                    admin_full_access=admin_full,
                    owner_id=full_agent.home_paths.owner_id,
                )
            )
            assert dec.status == "ask", (approval_mode, admin_full)
            assert dec.evidence.get("human_confirmation_required") is True

        pending_exec = execute_canonical_test_call(
            full_agent.root,
            tools={"business_assets_versions_accept": accept_rt.handler},
            tool_name="business_assets_versions_accept",
            arguments=accept_req,
            operation_id="wire-accept-human-op",
            idempotency_key="wire-accept-idem-0001",
            operation_owner_id=full_agent.home_paths.owner_id,
        )
        assert pending_exec.decision.status == "ask"
        approval_req = build_tool_approval_request(
            pending_exec.call,
            request_id="wire-accept-01",
            round_number=1,
            call_index=0,
            description="确认采用业务资产版本",
            require_human=True,
            owner_id=full_agent.home_paths.owner_id,
        )
        chunk_file = tmp_path / "wire-human-approval.jsonl"
        writer = BufferedChunkStreamWriter(chunk_file, interactive_approvals=True)
        holder: dict[str, Any] = {}
        worker = threading.Thread(
            target=lambda: holder.update(writer.request_permission(approval_req.to_dict()))
        )
        worker.start()
        deadline = time.monotonic() + 3.0
        while not chunk_file.exists() or "permission_requested" not in chunk_file.read_text(encoding="utf-8"):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        write_gateway_permission_decision(
            chunk_file,
            approval_req,
            ToolApprovalDecision(approval_req.permission_id, "approved"),
        )
        worker.join(3.0)
        assert not worker.is_alive()
        approved_binding = approval_req.approved_binding(ToolApprovalDecision.from_mapping(holder))

        accepted_exec = execute_canonical_test_call(
            full_agent.root,
            tools={"business_assets_versions_accept": accept_rt.handler},
            tool_name="business_assets_versions_accept",
            arguments=accept_req,
            write_boundary={"approved_actions": [approved_binding]},
            operation_id="wire-accept-human-op",
            idempotency_key="wire-accept-idem-0001",
            operation_owner_id=full_agent.home_paths.owner_id,
        )
        assert accepted_exec.result.ok, accepted_exec.result.output
        assert json.loads(accepted_exec.result.output)["data"]["accepted_version"] == artifact_ref["version_ref"]
    finally:
        server.stop()
        disabled_agent.tools.close_mcp_clients()
        unready_agent.tools.close_mcp_clients()
        full_agent.tools.close_mcp_clients()


def test_all_aliases_both_host_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    aliases = retired_tool_aliases()
    domain_map = business_domain_actions()
    assert len(aliases) == 104
    assert "business_assets_versions_accept" not in aliases

    spec_by_action = {spec.action: spec for spec in _all_105_business_specs()}
    agent = _make_business_agent(tmp_path, monkeypatch, tag="aliases-104", all_105_ready=True)
    try:
        seed_space_res = execute_registry_test_call(
            agent.tools,
            "business_space",
            {
                "action": "spaces.create",
                "request_id": "req-alias-seed-space",
                "idempotency_key": "idem-alias-seed-space-01",
                "params": {"name": "AliasLiveSpace"},
            },
            call_id="call-alias-seed-space",
            operation_id="op-alias-seed-space",
            idempotency_key="idem-alias-seed-space-01",
        )
        assert seed_space_res.ok, seed_space_res.output
        live_space_id = json.loads(seed_space_res.output)["data"]["space_id"]

        snapshot = agent.tools.runtime_snapshot(run_id="run-aliases-104")
        available = snapshot.available_tool_names

        # 1) 在可信宿主版本 1 与 2 下遍历全部 104 个退役别名：验证精确解码、携带历史 input_migration、operation_identity_for 与 args_hash 完全一致
        for host_version in (1, 2):
            seen_actions: set[str] = set()
            for idx, (legacy_name, (expected_domain, expected_action)) in enumerate(sorted(aliases.items())):
                assert expected_action in domain_map[expected_domain]
                seen_actions.add(expected_action)
                spec = spec_by_action[expected_action]
                canonical_req = _sample_business_request(spec)
                if legacy_name == "business_tasks_list":
                    canonical_req = {**canonical_req, "params": {"filter": {"space_id": live_space_id}}}
                legacy_req = {k: v for k, v in canonical_req.items() if k != "action"}

                decoded = decode_retired_tool_call(legacy_name, legacy_req, available=available)
                assert decoded is not None, legacy_name
                target_tool, decoded_args, decoded_action = decoded
                assert target_tool == expected_domain
                assert decoded_action == expected_action
                assert canonical_json(decoded_args) == canonical_json(canonical_req)

                domain_handler = snapshot.runtime(expected_domain).handler
                expected_op_identity = f"business_{expected_action.replace('.', '_')}"
                assert expected_op_identity == legacy_name
                assert domain_handler.operation_identity_for(canonical_req) == expected_op_identity
                assert domain_handler.operation_identity_for(decoded_args) == expected_op_identity
                assert tool_arguments_hash(decoded_args) == tool_arguments_hash(canonical_req)

                # 通过 ToolExecutor 验证宿主协议版本 1/2 实时输入解码保留 call_id/operation_id/idempotency_key/request_id/expected_revision/params 与 prior_migration
                if idx < 6 or legacy_name in {
                    "business_spaces_list",
                    "business_background_agents_list",
                    "business_workflows_list",
                }:
                    base_call = canonical_test_call(
                        snapshot,
                        expected_domain,
                        canonical_req,
                        call_id=f"c-v{host_version}-{idx}",
                        operation_id=f"op-v{host_version}-{idx}",
                        idempotency_key=canonical_req.get("idempotency_key") or f"idem-v{host_version}-{idx:04d}",
                    )
                    carried_call = replace(
                        base_call,
                        tool_name=legacy_name,
                        arguments=legacy_req,
                        input_migration={"carried_from": f"pending-v{host_version}-{idx}"},
                    )
                    if legacy_name in {
                        "business_spaces_list",
                        "business_background_agents_list",
                        "business_workflows_list",
                    }:
                        exec_out = agent.tools.execute_tool(
                            carried_call,
                            write_boundary=None,
                            runtime_snapshot=snapshot,
                            trusted_run_context={
                                "tool_input_version": host_version,
                                "run_scope": {"run_id": "run-aliases-104"},
                            },
                        )
                        assert exec_out.result.ok is True, (host_version, legacy_name, exec_out.result.output)
                        assert exec_out.call.tool_name == expected_domain
                        assert exec_out.call.call_id == f"c-v{host_version}-{idx}"
                        assert exec_out.call.operation_id == f"op-v{host_version}-{idx}"
                        assert exec_out.call.arguments["request_id"] == canonical_req["request_id"]
                        assert exec_out.call.arguments["params"] == canonical_req["params"]
                        assert exec_out.call.input_migration["requested_tool_name"] == legacy_name
                        assert exec_out.call.input_migration["canonical_tool_name"] == expected_domain
                        assert exec_out.call.input_migration["prior_migration"] == {
                            "carried_from": f"pending-v{host_version}-{idx}"
                        }
            assert len(seen_actions) == 104

        # 2) 冲突 action 与目标域不可用路由均在进入 claim 前返回 effect_outcome == "not_started"
        base_call = canonical_test_call(
            snapshot,
            "business_space",
            {"action": "spaces.list", "request_id": "alias-base", "params": {}},
        )
        for host_version in (1, 2):
            conflict_op = f"op-conflict-v{host_version}"
            conflict_res = (
                ToolExecutor()
                .execute(
                    ToolExecutorRequest(
                        call=replace(
                            base_call,
                            call_id=f"c-conflict-v{host_version}",
                            tool_name="business_spaces_create",
                            arguments={"action": "spaces.archive", "request_id": "r-conf", "params": {"name": "x"}},
                            operation_id=conflict_op,
                            idempotency_key=f"idem-conflict-v{host_version}-0001",
                        ),
                        runtime_snapshot=snapshot,
                        workspace_root=tmp_path,
                        operation_store=agent.tools.operation_store,
                        operation_store_required=True,
                        operation_owner_id=agent.home_paths.owner_id,
                        trusted_run_context={
                            "tool_input_version": host_version,
                            "run_scope": {"run_id": "run-aliases-104"},
                        },
                    )
                )
                .result
            )
            assert not conflict_res.ok
            assert conflict_res.effect_outcome == "not_started"
            assert (
                agent.tools.operation_store.get_tool_operation(
                    owner_id=agent.home_paths.owner_id,
                    run_id="run-aliases-104",
                    operation_id=conflict_op,
                )
                is None
            )

        no_biz_snap = agent.tools.runtime_snapshot(allowed_tools=["read_file"], run_id="run-no-biz")
        unavail_res = agent.tools.execute_tool(
            replace(
                canonical_test_call(no_biz_snap, "read_file", {"path": "README.md"}),
                call_id="c-unavail-alias",
                tool_name="business_spaces_list",
                arguments={"request_id": "r-unavail", "params": {}},
            ),
            write_boundary=None,
            runtime_snapshot=no_biz_snap,
        ).result
        assert not unavail_res.ok
        assert unavail_res.effect_outcome == "not_started"

        # 3) 已退役死工具 business_spaces 返回非执行型重定向且零副作用
        retired_spaces = agent.tools.execute_tool(
            replace(
                base_call,
                call_id="c-dead-spaces",
                tool_name="business_spaces",
                arguments={"action": "spaces.create", "params": {"name": "retired"}},
            ),
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert not retired_spaces.ok
        assert retired_spaces.metadata.get("reported_error_code") == "TOOL_RETIRED_REDIRECT"
        assert retired_spaces.effect_outcome == "not_started"
        assert retired_spaces.retryable is False

        # 4) 发现投影（tool_search）返回 renamed_aliases/redirect_only 且不突破动作上界
        search_snap = agent.tools.runtime_snapshot(
            allowed_tools=["tool_search", "business_spaces_list"],
            run_id="run-alias-discovery",
        )
        search_res = agent.tools.execute_tool(
            canonical_test_call(
                search_snap,
                "tool_search",
                {"action": "search", "names": ["business_spaces", "business_spaces_list", "business_spaces_create"]},
            ),
            write_boundary=None,
            runtime_snapshot=search_snap,
        ).result
        assert search_res.ok, search_res.output
        search_body = json.loads(search_res.output)
        assert search_body["loaded_for_next_model_call"] == ["business_space"]
        alias_map = {row["requested"]: row for row in search_body["renamed_aliases"]}
        assert alias_map["business_spaces"]["redirect_only"] is True
        assert alias_map["business_spaces_list"]["authorized"] is True
        assert alias_map["business_spaces_create"]["authorized"] is False

        # 5) 历史以叶名称写入的已完成操作与真实业务回执对账：绝不重复触发底层 execute_business
        exec_calls: list[dict[str, Any]] = []
        orig_execute = business_api_mod.execute_business

        def _spy_exec(req: Any, *a: Any, **kw: Any) -> Any:
            if isinstance(req, dict) and req.get("action") == "spaces.create":
                exec_calls.append(dict(req))
            return orig_execute(req, *a, **kw)

        monkeypatch.setattr(business_api_mod, "execute_business", _spy_exec)
        create_canon = {
            "action": "spaces.create",
            "request_id": "req-hist-01",
            "idempotency_key": "idem-hist-space-0001",
            "params": {"name": "HistoricalReplaySpace"},
        }
        first_live = execute_registry_test_call(
            agent.tools,
            "business_spaces_create",
            {k: v for k, v in create_canon.items() if k != "action"},
            run_id="run-hist-replay",
            call_id="call-hist-1",
            operation_id="op-hist-1",
            idempotency_key="idem-hist-space-0001",
        )
        assert first_live.ok, first_live.output
        assert len(exec_calls) == 1
        created_space_id = json.loads(first_live.output)["data"]["space_id"]

        # 使用规范域工具名重放同一 operation_id，命中已完成历史记录且不重复执行
        replay_live = execute_registry_test_call(
            agent.tools,
            "business_space",
            create_canon,
            run_id="run-hist-replay",
            call_id="call-hist-2",
            operation_id="op-hist-1",
            idempotency_key="idem-hist-space-0001",
        )
        assert replay_live.ok, replay_live.output
        assert len(exec_calls) == 1
        assert json.loads(replay_live.output)["data"]["space_id"] == created_space_id

        # 单独种入旧版本叶记录并验证规范调用命中重放且不触发 handler
        seeded_canon = {
            "action": "spaces.create",
            "request_id": "req-hist-seeded",
            "idempotency_key": "idem-hist-seeded-0001",
            "params": {"name": "SeededLegacySpace"},
        }
        seeded_call = canonical_test_call(
            snapshot,
            "business_space",
            seeded_canon,
            call_id="call-seeded-canon",
            operation_id="op-seeded-legacy",
            idempotency_key="idem-hist-seeded-0001",
        )
        claim = agent.tools.operation_store.claim_tool_operation(
            ToolOperationClaimRequest(
                owner_id=agent.home_paths.owner_id,
                run_id="run-aliases-104",
                task_id="run-aliases-104",
                operation_id="op-seeded-legacy",
                tool="business_spaces_create",
                args_hash=seeded_call.args_hash,
                idempotency_key="idem-hist-seeded-0001",
                idempotency_scope="operation",
                idempotency_namespace="business_spaces_create",
                holder=new_tool_operation_holder(),
                lease_expires_at=9999999999.0,
            )
        )
        agent.tools.operation_store.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id=agent.home_paths.owner_id,
                run_id="run-aliases-104",
                operation_id="op-seeded-legacy",
                holder_id=claim.record.holder_id,
                generation=claim.record.generation,
                status=TOOL_OPERATION_SUCCEEDED,
                result={
                    "schema_version": "tool_execution_result.v1",
                    "tool": "business_spaces_create",
                    "ok": True,
                    "output": json.dumps({"state": "completed", "data": {"space_id": "sp-seeded-01"}}),
                    "handler_executed": True,
                    "effect_outcome": "committed",
                },
            )
        )
        seeded_replay = agent.tools.execute_tool(
            seeded_call,
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert seeded_replay.ok is True
        assert len(exec_calls) == 1

        # 原始业务回执查询按可信已注册实例准确找回，拒绝未注册伪造实例
        receipt = inspect_registered_business_receipt(snapshot.runtime("business_space"), create_canon)
        assert receipt is not None
        assert receipt["state"] == "completed"
        assert receipt["target_refs"] == [created_space_id]
    finally:
        agent.tools.close_mcp_clients()


def test_read_only_action_ceiling_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alpha_agent = _make_business_agent(
        tmp_path,
        monkeypatch,
        tag="ceil-alpha",
        all_105_ready=True,
        user_id="user-alpha",
    )
    beta_agent = _make_business_agent(
        tmp_path,
        monkeypatch,
        tag="ceil-beta",
        all_105_ready=False,
        user_id="user-beta",
    )
    try:
        # 先在 alpha 作用域创建一个空间，用于跨作用域隔离与只读核验
        created_alpha = execute_registry_test_call(
            alpha_agent.tools,
            "business_space",
            {
                "action": "spaces.create",
                "request_id": "alpha-seed-01",
                "idempotency_key": "alpha-seed-idem-0001",
                "params": {"name": "Alpha Private Space"},
            },
            call_id="alpha-seed-call",
            operation_id="alpha-seed-op",
            idempotency_key="alpha-seed-idem-0001",
        )
        assert created_alpha.ok, created_alpha.output
        alpha_space_id = json.loads(created_alpha.output)["data"]["space_id"]

        # 1) 确定性授权交集矩阵：孤立 token 不授权、子级裸工具名不突破父级只读上界、preset 合并与调度派工单调收窄
        assert normalize_tool_grants(["business_space:spaces.create"]) == []
        parent_ro_grants = normalize_tool_grants(
            [
                "read_file",
                "list_tools",
                "business_spaces_list",
                "business_spaces_get",
                "business_tasks_list",
                "business_workflows_list",
                "business_policies_get",
            ]
        )
        child_attempt_widen = [
            "read_file",
            "tool_search",
            "business_space",
            "business_execution",
            "business_activity",
            "business_assets_versions_accept",
        ]
        effective_grants = intersect_tool_grants(child_attempt_widen, parent_ro_grants)
        assert "business_assets_versions_accept" not in effective_grants
        assert action_ceilings(effective_grants)["tool_search"] == frozenset({"catalog"})
        assert action_ceilings(effective_grants)["business_space"] == frozenset(
            {"policies.get", "spaces.get", "spaces.list"}
        )
        assert action_ceilings(effective_grants)["business_execution"] == frozenset({"tasks.list"})
        assert action_ceilings(effective_grants)["business_activity"] == frozenset({"workflows.list"})

        preset_grants = subagent_allowed_tools(
            {
                "allowed_tools": ["business_spaces_list", "business_tasks_list", "list_tools"],
                "tool_preset": "read_only",
            }
        )
        assert action_ceilings(preset_grants)["business_space"] == frozenset({"spaces.list"})
        assert action_ceilings(preset_grants)["business_execution"] == frozenset({"tasks.list"})
        assert action_ceilings(preset_grants)["tool_search"] == frozenset({"catalog"})

        sched_grants = scheduled_child_tools(
            ToolPolicyRequest(
                parent_tools=parent_ro_grants,
                spec=SimpleNamespace(role="worker", allowed_tools=child_attempt_widen, attributes={}),
                extra_write_roots=[],
                goal="只读盘点空间与工作流列表",
                role="worker",
            )
        )
        assert action_ceilings(sched_grants)["business_space"] <= frozenset(
            {"policies.get", "spaces.get", "spaces.list"}
        )

        # 后台唤醒交集 + legacy 叶名称禁用（disabled_tools）精准减掉单个动作且不放宽写动作
        wake_dec = background_tool_policy_decision(
            request=BackgroundToolPolicyRequest(
                reason="session_task",
                policy_snapshot={"allowed_tools": ["business_space", "business_execution"]},
                owner_policy=SimpleNamespace(disabled_tools=("business_spaces_get", "business_tasks_assign")),
            )
        )
        wake_ceilings = action_ceilings(list(wake_dec.allowed_tools))
        assert "spaces.get" not in wake_ceilings["business_space"]
        assert "spaces.create" not in wake_ceilings["business_space"]
        assert "tasks.assign" not in wake_ceilings["business_execution"]
        assert "tasks.submit" not in wake_ceilings["business_execution"]

        # 2) 在两个不同作用域与不同服务就绪度（alpha 治理就绪 vs beta 治理未就绪）下验证有效 Schema 0 写动作进入：
        #    - 包含未就绪动作上界（policies.get）在 beta 上 fail-closed 进入 unavailable_tools；
        #    - 对已就绪只读上界（spaces.list/spaces.get/tasks.list/workflows.list），两个作用域均只暴露 read_only 分支且拒绝一切写动作
        beta_unready_gov_snap = beta_agent.tools.runtime_snapshot(
            allowed_tools=effective_grants,
            run_id="run-beta-unready-gov",
        )
        assert beta_unready_gov_snap.runtime("business_space") is None
        assert any(
            name == "business_space" and code == "TOOL_ACTION_NOT_PERMITTED"
            for name, code, _reason in beta_unready_gov_snap.unavailable_tools
        )

        common_ro_grants = intersect_tool_grants(
            child_attempt_widen,
            normalize_tool_grants(
                [
                    "read_file",
                    "list_tools",
                    "business_spaces_list",
                    "business_spaces_get",
                    "business_tasks_list",
                    "business_workflows_list",
                ]
            ),
        )
        spec_by_action = {spec.action: spec for spec in _all_105_business_specs()}
        for scope_label, target_agent, grants_for_scope, expected_space_actions in (
            ("alpha-ready", alpha_agent, effective_grants, {"policies.get", "spaces.get", "spaces.list"}),
            ("beta-unready", beta_agent, common_ro_grants, {"spaces.get", "spaces.list"}),
        ):
            ro_snap = target_agent.tools.runtime_snapshot(
                allowed_tools=grants_for_scope,
                run_id=f"run-{scope_label}",
            )
            assert ro_snap.runtime("business_assets_versions_accept") is None
            space_rt = ro_snap.runtime("business_space")
            exec_rt = ro_snap.runtime("business_execution")
            act_rt = ro_snap.runtime("business_activity")
            assert space_rt is not None and exec_rt is not None and act_rt is not None
            assert set(space_rt.model_spec.input_schema["properties"]["action"]["enum"]) == expected_space_actions
            assert set(exec_rt.model_spec.input_schema["properties"]["action"]["enum"]) == {"tasks.list"}
            assert set(act_rt.model_spec.input_schema["properties"]["action"]["enum"]) == {"workflows.list"}

            for rt in (space_rt, exec_rt, act_rt):
                schema = rt.model_spec.input_schema
                enum_actions = schema["properties"]["action"]["enum"]
                assert len(schema["oneOf"]) == len(enum_actions)
                for act_name in enum_actions:
                    sample = _sample_business_request(spec_by_action[act_name])
                    resolved_eff = tool_effect_for_runtime_policy(rt.runtime_policy, sample)
                    assert resolved_eff == "read_only", (scope_label, rt.model_spec.name, act_name, resolved_eff)

            # 尝试在只读快照下执行任何写动作或危险动作，均在 handler 执行前以 not_started 拒绝
            for domain_name, forbidden_req in (
                (
                    "business_space",
                    {
                        "action": "spaces.create",
                        "request_id": f"forbid-space-{scope_label}",
                        "idempotency_key": f"forbid-space-idem-{scope_label}",
                        "params": {"name": "BlockedWrite"},
                    },
                ),
                (
                    "business_execution",
                    {
                        "action": "tasks.submit",
                        "request_id": f"forbid-task-{scope_label}",
                        "idempotency_key": f"forbid-task-idem-{scope_label}",
                        "params": {"space_id": alpha_space_id, "goal": "blocked"},
                    },
                ),
                (
                    "business_activity",
                    {
                        "action": "workflows.cleanup.policy.set",
                        "request_id": f"forbid-wf-{scope_label}",
                        "idempotency_key": f"forbid-wf-idem-{scope_label}",
                        "expected_revision": 0,
                        "params": {"policy": {"orphan_after_days": 10}},
                    },
                ),
            ):
                denied = execute_registry_test_call(
                    target_agent.tools,
                    domain_name,
                    forbidden_req,
                    allowed_tools=grants_for_scope,
                    call_id=f"call-{domain_name}-{scope_label}",
                    operation_id=f"op-{domain_name}-{scope_label}",
                    idempotency_key=forbidden_req["idempotency_key"],
                )
                assert not denied.ok
                assert denied.effect_outcome == "not_started"

        # 3) 跨作用域隔离核验：alpha 只读可见自身空间，beta 只读列表看不到 alpha 的空间且点查返回错误
        alpha_list = json.loads(
            execute_registry_test_call(
                alpha_agent.tools,
                "business_space",
                {"action": "spaces.list", "request_id": "ro-alpha-list", "params": {}},
                allowed_tools=effective_grants,
            ).output
        )
        assert [row["space_id"] for row in alpha_list["data"]["items"]] == [alpha_space_id]

        beta_list = json.loads(
            execute_registry_test_call(
                beta_agent.tools,
                "business_space",
                {"action": "spaces.list", "request_id": "ro-beta-list", "params": {}},
                allowed_tools=common_ro_grants,
            ).output
        )
        assert beta_list["data"]["items"] == []
        beta_cross_get = json.loads(
            execute_registry_test_call(
                beta_agent.tools,
                "business_space",
                {"action": "spaces.get", "request_id": "ro-beta-cross", "params": {"space_id": alpha_space_id}},
                allowed_tools=common_ro_grants,
            ).output
        )
        assert beta_cross_get["error"] is not None
    finally:
        alpha_agent.tools.close_mcp_clients()
        beta_agent.tools.close_mcp_clients()


def test_effect_replay_single_faults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "fault-ws"
    workspace.mkdir()
    persona_dir = workspace / "persona_protected"
    persona_dir.mkdir()
    (persona_dir / "SOUL.md").write_text("calm style\n", encoding="utf-8")

    op_store = LocalStore(tmp_path / "w6_faults.db", enable_fts=False)
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace,
            max_chars=10_000,
            max_entries=100,
            max_matches=100,
            web_max_chars=10_000,
            http_timeout=1,
            catalog_limit=100,
            retrieval_limit=10,
            vector_search_enabled=False,
            operation_store=op_store,
            operation_store_required=True,
            operation_owner_id="owner-w6",
            protected_persona_root=str(persona_dir),
            tool_write_inline_max_chars=64,
        )
    )
    try:
        snapshot = registry.runtime_snapshot(run_id="run-w6-faults")

        # 故障 1：apply_patch 首文件提交成功后第二个文件遇到单点模拟 I/O 错误 -> TOOL_PARTIAL_COMMIT，重放不二次写盘
        first_file = workspace / "mod_a.py"
        second_file = workspace / "mod_b.py"
        first_file.write_text("a = 1\n", encoding="utf-8")
        second_file.write_text("b = 1\n", encoding="utf-8")
        second_attempts = {"count": 0}

        def _fail_second_before_publish(target: Path, data: bytes, **kwargs: Any) -> Any:
            if target == second_file:
                second_attempts["count"] += 1
                raise OSError(errno.EIO, "simulated I/O error on second patch file")
            return real_atomic_write_bytes(target, data, **kwargs)

        monkeypatch.setattr(fs_patch_module, "_atomic_write_bytes", _fail_second_before_publish)
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: mod_a.py\n"
            "-a = 1\n"
            "+a = 2\n"
            "*** Update File: mod_b.py\n"
            "-b = 1\n"
            "+b = 2\n"
            "*** End Patch\n"
        )
        patch_call = canonical_test_call(
            snapshot,
            "apply_patch",
            {"patch": patch_text},
            operation_id="op-w6-partial",
            idempotency_key="idem-w6-partial",
        )
        patch_res = registry.execute_tool(patch_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert not patch_res.ok
        assert patch_res.error_code == "TOOL_PARTIAL_COMMIT"
        assert patch_res.effect_outcome == "failed"
        assert patch_res.retryable is False
        assert patch_res.metadata["handler_details"]["files_modified"] == ["mod_a.py"]
        assert patch_res.metadata["handler_details"]["failed_path"] == "mod_b.py"
        assert "read_file" in str(patch_res.metadata.get("next_action") or "")
        assert patch_res.render_for_prompt().count("[tool-next-action]") == 1
        assert (
            op_store.get_tool_operation(owner_id="owner-w6", run_id="run-w6-faults", operation_id="op-w6-partial").status
            == TOOL_OPERATION_FAILED
        )

        patch_replay = registry.execute_tool(patch_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert second_attempts["count"] == 1
        assert patch_replay.operation is not None and patch_replay.operation.replayed is True
        assert patch_replay.error_code == "TOOL_PARTIAL_COMMIT"
        assert patch_replay.retryable is False
        assert patch_replay.metadata.get("next_action") == patch_res.metadata.get("next_action")
        monkeypatch.setattr(fs_patch_module, "_atomic_write_bytes", real_atomic_write_bytes)

        # 故障 2：写前策略拒绝（edit_file 受保护 SOUL.md、write_file 二进制扩展名与系统台账写保护）-> effect_outcome == "not_started"，绝不误判为 unknown
        for suffix, refusal_tool, bad_write_args, expected_code in (
            (
                "persona",
                "edit_file",
                {"path": "persona_protected/SOUL.md", "old_string": "calm", "new_string": "strict"},
                "PERSONA_WRITE_REQUIRES_TOOL",
            ),
            (
                "candidate-pdf",
                "write_file",
                {"path": "report.pdf", "content": "binary extension via text content"},
                "ARTIFACT_VALIDATION_FAILED",
            ),
            (
                "ledger",
                "write_file",
                {"path": "memory_archive/task_progress/progress.json", "content": "{}"},
                "SYSTEM_LEDGER_WRITE_BLOCKED",
            ),
        ):
            refuse_call = canonical_test_call(
                snapshot,
                refusal_tool,
                bad_write_args,
                operation_id=f"op-w6-refuse-{suffix}",
                idempotency_key=f"idem-w6-refuse-{suffix}",
            )
            refuse_res = registry.execute_tool(
                refuse_call,
                write_boundary=None,
                runtime_snapshot=snapshot,
            ).result
            assert not refuse_res.ok
            assert refuse_res.error_code == expected_code
            assert refuse_res.effect_outcome == "not_started"
            assert refuse_res.error_code != "TOOL_OPERATION_OUTCOME_UNKNOWN"
            saved_refusal = op_store.get_tool_operation(
                owner_id="owner-w6",
                run_id="run-w6-faults",
                operation_id=f"op-w6-refuse-{suffix}",
            )
            assert saved_refusal is not None and saved_refusal.status == TOOL_OPERATION_FAILED
        assert (persona_dir / "SOUL.md").read_text(encoding="utf-8") == "calm style\n"

        # 故障 3：预检校验器内部异常 -> TOOL_INVOCATION_VALIDATOR_FAILED，effect_outcome == "not_started"，0 次 claim
        write_tool = registry.tools["write_file"]
        monkeypatch.setattr(
            write_tool,
            "validate_invocation",
            lambda _params, _ctx: (_ for _ in ()).throw(RuntimeError("simulated validator defect sk-1234567890abcdefghijklmnop")),
        )
        val_call = canonical_test_call(
            snapshot,
            "write_file",
            {"path": "never_written.txt", "content": "ok\n"},
            operation_id="op-w6-val-exc",
            idempotency_key="idem-w6-val-exc",
        )
        val_res = registry.execute_tool(val_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert not val_res.ok
        assert val_res.error_code == "TOOL_INVOCATION_VALIDATOR_FAILED"
        assert val_res.effect_outcome == "not_started"
        assert val_res.retryable is False
        assert "sk-1234567890abcdefghijklmnop" not in json.dumps(val_res.metadata, ensure_ascii=False)
        assert op_store.get_tool_operation(owner_id="owner-w6", run_id="run-w6-faults", operation_id="op-w6-val-exc") is None
        monkeypatch.undo()

        # 故障 4：提交后读回单点 I/O 失败 -> TOOL_POST_COMMIT_READBACK_FAILED，effect_outcome == "unknown"，retryable=False，重放写次数恒为 1；
        #         同时验证写成功后置输出归档失败保留 TOOL_OUTPUT_ARCHIVE_FAILED 诊断且不重执行主写入
        owner_home = tmp_path / "script-owner-home"
        owner_home.mkdir()
        script_store = ScriptStore(owner_home)
        scripts_tool = ScriptsTool(owner_home, store=script_store)
        raw_write = script_store._write
        raw_get = script_store._get
        write_counter = {"writes": 0, "committed": False}

        def _counted_script_write(*a: Any, **kw: Any) -> Any:
            out = raw_write(*a, **kw)
            write_counter["writes"] += 1
            write_counter["committed"] = True
            return out

        def _failing_readback_get(*a: Any, **kw: Any) -> Any:
            if write_counter["committed"]:
                raise OSError(errno.EIO, "simulated I/O error on post-commit readback")
            return raw_get(*a, **kw)

        monkeypatch.setattr(script_store, "_write", _counted_script_write)
        monkeypatch.setattr(script_store, "_get", _failing_readback_get)
        script_args = {
            "action": "register",
            "script_id": "w6_demo_script",
            "code": 'print("{}")\n',
            "descriptor": {
                "name": "W6 演示脚本",
                "access_targets": [],
                "data_sources": [],
                "credential_refs": [],
                "io_contract": {},
            },
        }
        rb_first = execute_canonical_test_call(
            workspace,
            tools={"scripts": scripts_tool},
            tool_name="scripts",
            arguments=script_args,
            allowed_tools=["scripts"],
            run_id="run-w6-faults",
            operation_id="op-w6-readback",
            idempotency_key="idem-w6-readback",
            operation_owner_id="owner-w6",
            operation_store=op_store,
            operation_store_required=True,
        ).result
        assert not rb_first.ok
        assert rb_first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
        assert rb_first.metadata.get("reported_error_code") == "TOOL_POST_COMMIT_READBACK_FAILED"
        assert rb_first.effect_outcome == "unknown"
        assert rb_first.retryable is False
        assert "w6_demo_script" in str(rb_first.metadata.get("next_action") or "")
        assert write_counter["writes"] == 1
        assert (
            op_store.get_tool_operation(owner_id="owner-w6", run_id="run-w6-faults", operation_id="op-w6-readback").status
            == TOOL_OPERATION_UNKNOWN
        )

        rb_replay = execute_canonical_test_call(
            workspace,
            tools={"scripts": scripts_tool},
            tool_name="scripts",
            arguments=script_args,
            allowed_tools=["scripts"],
            run_id="run-w6-faults",
            operation_id="op-w6-readback",
            idempotency_key="idem-w6-readback",
            operation_owner_id="owner-w6",
            operation_store=op_store,
            operation_store_required=True,
        ).result
        assert write_counter["writes"] == 1
        assert not rb_replay.ok
        assert rb_replay.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
        assert rb_replay.effect_outcome == "not_started"
        assert rb_replay.retryable is False
        assert (
            op_store.get_tool_operation(owner_id="owner-w6", run_id="run-w6-faults", operation_id="op-w6-readback").status
            == TOOL_OPERATION_UNKNOWN
        )

        # 后置输出归档异常保留 TOOL_OUTPUT_ARCHIVE_FAILED 诊断且主文件仅写入一次
        arch_call = canonical_test_call(
            snapshot,
            "write_file",
            {"path": "archived_ok.txt", "content": "hello\n"},
            operation_id="op-w6-arch-warn",
            idempotency_key="idem-w6-arch-warn",
        )

        def _broken_archiver(_call: Any, _raw: object) -> Any:
            raise OSError(errno.ENOSPC, "simulated archive storage full")

        arch_res = registry.execute_tool(
            arch_call,
            write_boundary=None,
            runtime_snapshot=snapshot,
            output_archiver=_broken_archiver,
        ).result
        assert arch_res.ok is False
        assert arch_res.error_code == "TOOL_OUTPUT_ARCHIVE_FAILED"
        assert arch_res.effect_outcome == "confirmed"
        assert arch_res.retryable is False
        assert arch_res.operation is not None and arch_res.operation.status == "succeeded"
        assert (workspace / "archived_ok.txt").read_text(encoding="utf-8") == "hello\n"

        # 故障 5：不确定 MCP 传输中断（unknown / retryable=False / 重放不重发）与远端明确 isError=True（MCP_REMOTE_ERROR / failed / retryable=False）
        mcp_calls: list[tuple[str, dict[str, Any]]] = []

        class _InProcessMCPClient:
            def __init__(self, cfg: MCPServerConfig, mode: str) -> None:
                self.config = cfg
                self.mode = mode
                self.publication = None

            def is_running(self) -> bool:
                return True

            def call_tool(self, name: str, arguments: dict[str, Any], **_options: Any) -> dict[str, Any]:
                mcp_calls.append((self.mode, dict(arguments)))
                if self.mode == "transport_unknown":
                    raise MCPError(
                        "connection closed after request write",
                        code="MCP_TRANSPORT_DISCONNECTED",
                        effect_outcome="unknown",
                    )
                return _normalize_call_result(
                    {
                        "content": [{"type": "text", "text": "window handle invalid: target closed"}],
                        "isError": True,
                    }
                )

        mcp_info = MCPToolInfo(
            "click_screen",
            "点击屏幕坐标",
            {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        )
        mcp_cfg = MCPServerConfig(name="computer_use", command="py", tool_effects={"click_screen": "mutating"})

        unk_proxy = build_proxy_tool(_InProcessMCPClient(mcp_cfg, "transport_unknown"), "computer_use", mcp_info, effect="mutating")
        unk_mcp_res = execute_canonical_test_call(
            workspace,
            tools={unk_proxy.model_spec.name: unk_proxy},
            tool_name=unk_proxy.model_spec.name,
            arguments={"x": 100, "y": 200},
            run_id="run-w6-faults",
            operation_id="op-w6-mcp-unk",
            idempotency_key="idem-w6-mcp-unk",
            operation_owner_id="owner-w6",
            operation_store=op_store,
            operation_store_required=True,
        ).result
        assert not unk_mcp_res.ok
        assert unk_mcp_res.effect_outcome == "unknown"
        assert unk_mcp_res.retryable is False
        assert unk_mcp_res.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
        unk_mcp_replay = execute_canonical_test_call(
            workspace,
            tools={unk_proxy.model_spec.name: unk_proxy},
            tool_name=unk_proxy.model_spec.name,
            arguments={"x": 100, "y": 200},
            run_id="run-w6-faults",
            operation_id="op-w6-mcp-unk",
            idempotency_key="idem-w6-mcp-unk",
            operation_owner_id="owner-w6",
            operation_store=op_store,
            operation_store_required=True,
        ).result
        assert not unk_mcp_replay.ok and unk_mcp_replay.retryable is False
        assert [m for m, _ in mcp_calls].count("transport_unknown") == 1

        err_proxy = build_proxy_tool(_InProcessMCPClient(mcp_cfg, "remote_is_error"), "computer_use", mcp_info, effect="mutating")
        err_mcp_res = execute_canonical_test_call(
            workspace,
            tools={err_proxy.model_spec.name: err_proxy},
            tool_name=err_proxy.model_spec.name,
            arguments={"x": 120, "y": 240},
            run_id="run-w6-faults",
            operation_id="op-w6-mcp-err",
            idempotency_key="idem-w6-mcp-err",
            operation_owner_id="owner-w6",
            operation_store=op_store,
            operation_store_required=True,
        ).result
        assert not err_mcp_res.ok
        assert err_mcp_res.error_code == "TOOL_RUNTIME_ERROR"
        assert err_mcp_res.metadata.get("reported_error_code") == "MCP_REMOTE_ERROR"
        assert err_mcp_res.effect_outcome == "failed"
        assert err_mcp_res.retryable is False
        assert "window handle invalid" in err_mcp_res.output
        assert err_mcp_res.metadata.get("next_action")
    finally:
        registry.close_mcp_clients()


def test_prompt_snapshot_reuse_and_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _measure(enabled: bool) -> tuple[SimpleAgent, dict[str, Any]]:
        ag = _make_business_agent(
            tmp_path,
            monkeypatch,
            tag=f"metrics-{enabled}",
            business_spaces_enabled=enabled,
            all_105_ready=enabled,
        )
        snap = ag.tools.runtime_snapshot(run_id=f"metrics-run-{enabled}")
        visible = ag.tools.specs(include_orchestration=True, runtime_snapshot=snap)
        upfront = ag.tools.model_visible_specs(runtime_snapshot=snap)

        def _chars(specs: list[ToolModelSpec]) -> int:
            return sum(
                len(
                    json.dumps(
                        {"name": s.name, "description": s.description, "input_schema": s.input_schema},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                for s in specs
            )

        return ag, {
            "registered": len(ag.tools.tools),
            "visible": len(visible),
            "upfront": len(upfront),
            "upfront_chars": _chars(upfront),
            "visible_chars": _chars(visible),
            "business_names": sorted(s.name for s in visible if s.name.startswith("business_")),
        }

    off_agent, off_metrics = _measure(False)
    on_agent, on_metrics = _measure(True)
    try:
        # 1) 精确核验默认注册表工具数量与分层可见指标
        assert off_metrics["registered"] == 70
        assert off_metrics["visible"] == 63
        assert off_metrics["upfront"] == 29
        assert off_metrics["business_names"] == []
        assert 30_000 <= off_metrics["upfront_chars"] < off_metrics["visible_chars"]

        # 注意：_make_business_agent(all_105_ready=True) 同时就绪了 business_assets_versions_accept，
        # 因此再核对出厂默认 SimpleAgent(business_spaces_enabled=True) 的精确基线计数（73 / 66 / 29）
        default_home = tmp_path / "default-biz-home"
        monkeypatch.setenv("MY_AGENT_HOME", str(default_home))
        default_on_agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(default_home),
                memory_path="memory.jsonl",
                business_spaces_enabled=True,
            ),
            tmp_path / "default-biz-ws",
        )
        try:
            def_snap = default_on_agent.tools.runtime_snapshot(run_id="default-on-snap")
            def_visible = default_on_agent.tools.specs(include_orchestration=True, runtime_snapshot=def_snap)
            def_upfront = default_on_agent.tools.model_visible_specs(runtime_snapshot=def_snap)
            assert len(default_on_agent.tools.tools) == 73
            assert len(def_visible) == 66
            assert len(def_upfront) == 29
            assert sorted(s.name for s in def_visible if s.name.startswith("business_")) == [
                "business_activity",
                "business_execution",
                "business_space",
            ]
        finally:
            default_on_agent.tools.close_mcp_clients()

        assert on_metrics["upfront"] == 29
        assert {
            "business_activity",
            "business_execution",
            "business_space",
            "business_assets_versions_accept",
        } <= set(on_metrics["business_names"])

        # 2) 验证活跃回合在 tool_search 加载新 Schema 后与受限授权变化后自动去重 # Recommended Tools，保留非工具段落
        snap = on_agent.tools.runtime_snapshot(run_id="dedup-turn")
        query = "查询业务执行任务列表并发起跨代理交接板协作 business_execution manage_handoff_board"
        initial_rec = on_agent.tools.render_recommended_tools_section(
            query,
            tool_protocol="native",
            runtime_snapshot=snap,
        )
        assert "business_execution" in initial_rec or "manage_handoff_board" in initial_rec
        assert "tool_search" not in initial_rec
        assert "read_file" not in initial_rec

        protocol = ToolProtocolSnapshot(
            "dedup-turn",
            "native",
            ProviderToolCapability(
                provider="echo",
                endpoint="local://echo",
                model="echo",
                stream=False,
                native_supported=True,
                evidence="w6-wire-test",
            ),
        )
        combined_section = initial_rec + "\n\n# Capability Packages\n- keep-business-pkg"
        loop_params = SimpleNamespace(
            tool_protocol_snapshot=protocol,
            tool_runtime_snapshot=snap,
            allowed_tools=None,
            loaded_tool_names={"business_execution", "manage_handoff_board"},
            live_archive_state={"loaded_tool_order": ["business_execution", "manage_handoff_board"]},
            turn_trigger=None,
            tool_recommendations_section=combined_section,
        )
        prompt_req = PromptBuildRequest(
            user_prompt=query,
            memories=[],
            tools=ToolSections(
                tool_recommendations_section=combined_section,
                native_tool_use=True,
            ),
        )
        deduped_req = _dedup_native_recommendations_section(on_agent, loop_params, prompt_req)
        assert "business_execution" not in deduped_req.tools.tool_recommendations_section
        assert "manage_handoff_board" not in deduped_req.tools.tool_recommendations_section
        assert "# Capability Packages\n- keep-business-pkg" in deduped_req.tools.tool_recommendations_section

        restricted_snap = on_agent.tools.runtime_snapshot(
            allowed_tools=["read_file", "tool_search", "business_spaces_list"],
            run_id="restricted-turn",
        )
        restricted_rec = on_agent.tools.render_recommended_tools_section(
            query,
            allowed_tools=["read_file", "tool_search", "business_spaces_list"],
            tool_protocol="native",
            runtime_snapshot=restricted_snap,
        )
        assert "business_execution" not in restricted_rec
        assert "manage_handoff_board" not in restricted_rec

        # 3) 验证未变回合的原型复用、无工作区 os.walk 重扫以及有界静态动作 Schema 缓存（零动态 run_id/owner_id 键）
        monkeypatch.setattr(
            os,
            "walk",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected workspace os.walk during snapshot reuse")),
        )
        snap_a = on_agent.tools.runtime_snapshot(
            allowed_tools=["read_file", "business_spaces_list", "business_spaces_get"],
            run_id="reuse-run-a",
        )
        snap_b = on_agent.tools.runtime_snapshot(
            allowed_tools=["read_file", "business_spaces_list", "business_spaces_get"],
            run_id="reuse-run-b",
        )
        assert isinstance(snap_a, ToolRuntimeSnapshot)
        spec_a = snap_a.runtime("business_space").model_spec
        spec_b = snap_b.runtime("business_space").model_spec
        assert spec_a is spec_b

        all_space_actions = list(business_domain_actions()["business_space"])
        for idx in range(registry_module._NARROWED_MODEL_SPEC_MAX_ENTRIES + 24):
            pair = [
                f"business_space:{all_space_actions[idx % len(all_space_actions)]}",
                f"business_space:{all_space_actions[(idx + 3) % len(all_space_actions)]}",
            ]
            on_agent.tools.runtime_snapshot(
                allowed_tools=["business_space", *pair],
                run_id=f"bounded-cache-run-{uuid4().hex}",
            )
        with registry_module._NARROWED_MODEL_SPEC_LOCK:
            cache_keys = list(registry_module._NARROWED_MODEL_SPEC_CACHE.keys())
        assert len(cache_keys) <= registry_module._NARROWED_MODEL_SPEC_MAX_ENTRIES
        for tool_key, rev_key, act_tuple in cache_keys:
            assert isinstance(tool_key, str) and isinstance(rev_key, str) and isinstance(act_tuple, tuple)
            assert "bounded-cache-run-" not in rev_key
    finally:
        off_agent.tools.close_mcp_clients()
        on_agent.tools.close_mcp_clients()
