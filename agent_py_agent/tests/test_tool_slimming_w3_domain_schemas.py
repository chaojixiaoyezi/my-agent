"""W3 延迟领域工具嵌套 Schema、使用提示、具体错误归因与提交后读回不重放运行时验收测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.ingestion.watch_tool_spec import build_watch_stream_model_spec
from agent_py_agent.agent.scheduler.delivery_policy import DELIVERY_SCHEMA
from agent_py_agent.agent.scheduler.rules import RULES_SCHEMA
from agent_py_agent.agent.scheduler.tool import build_schedule_tool_model_spec
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import asset_delivery_tool, feishu_auth_tool
from agent_py_agent.agent.tooling.asset_delivery_tool import AssetDeliveryTool
from agent_py_agent.agent.tooling.audit_group_tools import (
    AuditGroupBlackboxInvokeService,
    AuditGroupError,
    AuditGroupInvokeAssetTool,
    AuditGroupObservationService,
    AuditGroupQueryTool,
)
from agent_py_agent.agent.tooling.channel_messages_tool import ChannelMessagesTool
from agent_py_agent.agent.tooling.feishu_auth_tool import FeishuAuthTool
from agent_py_agent.agent.tooling.input_schema import validate_tool_input
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool, _outcome
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

_W3_TOOLS = (
    "remote_agent_push",
    "asset_delivery",
    "audit_group_query",
    "audit_group_invoke_asset",
    "package_build",
    "skill_transfer",
    "schedule",
    "tool_program",
    "feishu_cloud",
    "feishu_auth",
    "watch_stream",
    "channel_messages",
    "scripts",
    "datasources",
    "workflows",
)


def _schema_gaps(schema: dict) -> list[str]:
    missing: list[str] = []

    def walk(node: object, path: str, inherited: dict | None = None, seen: tuple[str, ...] = ()) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") and ref not in seen:
            target: object = schema
            for part in ref[2:].split("/"):
                if isinstance(target, dict):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            walk(target, path, inherited, (*seen, ref))
        props = node.get("properties", {})
        parent = inherited or {}
        if isinstance(props, dict):
            for name, child in props.items():
                effective = child
                if isinstance(child, dict) and isinstance(child.get("$ref"), str) and child["$ref"].startswith("#/"):
                    target = schema
                    for part in child["$ref"][2:].split("/"):
                        if isinstance(target, dict):
                            target = target[part.replace("~1", "/").replace("~0", "~")]
                    if isinstance(target, dict):
                        effective = {**target, **child}
                desc = (
                    (effective.get("description") if isinstance(effective, dict) else None)
                    or (parent.get(name, {}).get("description") if isinstance(parent.get(name), dict) else None)
                )
                if not isinstance(desc, str) or not desc.strip():
                    missing.append(f"{path}.{name}")
                walk(child, f"{path}.{name}", seen=seen)
        for keyword in ("oneOf", "anyOf", "allOf"):
            branches = node.get(keyword, [])
            if isinstance(branches, list):
                merged = {**parent, **(props if isinstance(props, dict) else {})}
                for idx, branch in enumerate(branches):
                    walk(branch, f"{path}/{keyword}/{idx}", merged, seen)
        for keyword in ("items", "additionalProperties"):
            sub = node.get(keyword)
            if isinstance(sub, dict):
                walk(sub, f"{path}/{keyword}", seen=seen)

    walk(schema, "$")
    return sorted(set(missing))


def test_domain_nested_schemas_and_examples(tmp_path: Path) -> None:
    raw_rules_snapshot = json.dumps(RULES_SCHEMA, sort_keys=True, ensure_ascii=False)
    raw_delivery_snapshot = json.dumps(DELIVERY_SCHEMA, sort_keys=True, ensure_ascii=False)

    for business_enabled in (False, True):
        workspace = tmp_path / f"ws_{business_enabled}"
        workspace.mkdir(parents=True, exist_ok=True)
        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / f"home_{business_enabled}"),
                business_spaces_enabled=business_enabled,
            ),
            workspace,
        )
        tools = agent.tools.tools
        for name in _W3_TOOLS:
            assert name in tools, f"missing registered tool {name}"
            spec = tools[name].model_spec
            gaps = _schema_gaps(spec.input_schema)
            assert gaps == [], f"{name} still has undescribed schema paths: {gaps}"
            assert spec.hints.use_cases, f"{name} missing use_cases"
            assert spec.hints.examples, f"{name} missing examples"
            valid_examples = [
                ex for ex in spec.hints.examples if validate_tool_input(json.loads(ex), spec.input_schema).ok
            ]
            assert len(valid_examples) == len(spec.hints.examples), f"{name} has invalid example(s)"

    # 验证各 watch_stream 运行表面嵌套 Schema 与示例均完整有效。
    for surface in ("ordinary", "audit_binding", "audit_source", "audit_coordinator", "audit_prepare"):
        watch_spec = build_watch_stream_model_spec(surface=surface)
        assert _schema_gaps(watch_spec.input_schema) == []
        for ex in watch_spec.hints.examples:
            assert validate_tool_input(json.loads(ex), watch_spec.input_schema).ok

    # 验证关键嵌套结构未被删减，且只读共享模块常量未被原位污染。
    assert json.dumps(RULES_SCHEMA, sort_keys=True, ensure_ascii=False) == raw_rules_snapshot
    assert json.dumps(DELIVERY_SCHEMA, sort_keys=True, ensure_ascii=False) == raw_delivery_snapshot
    sched_props = build_schedule_tool_model_spec().input_schema["properties"]
    assert "targets" in sched_props["delivery"]["properties"]
    assert "condition" in sched_props["rules"]["items"]["properties"]
    assert "access_targets" in ScriptsTool.model_spec.input_schema["properties"]["descriptor"]["properties"]
    assert "steps" in WorkflowsTool.model_spec.input_schema["properties"]["definition"]["properties"]
    assert "scope_mode" in WorkflowsTool.model_spec.input_schema["properties"]["scope"]["properties"]

    # 验证相邻编排与消息工具之间的区分引导（avoid_when）。
    scripts_avoid = " ".join(ScriptsTool.model_spec.hints.avoid_when)
    workflows_avoid = " ".join(WorkflowsTool.model_spec.hints.avoid_when)
    schedule_avoid = " ".join(build_schedule_tool_model_spec().hints.avoid_when)
    channel_avoid = " ".join(ChannelMessagesTool.model_spec.hints.avoid_when)
    assert "workflows" in scripts_avoid and "tool_program" in scripts_avoid and "schedule" in scripts_avoid
    assert "scripts" in workflows_avoid and "tool_program" in workflows_avoid and "schedule" in workflows_avoid
    assert "scripts" in schedule_avoid and "workflows" in schedule_avoid and "tool_program" in schedule_avoid
    assert "send_message" in channel_avoid and "session_search" in channel_avoid


def test_domain_errors_keep_specific_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_dir = tmp_path / "owners_root"
    root_dir.mkdir(parents=True, exist_ok=True)

    # 1. audit_group_query / audit_group_invoke_asset: 保留 validate_tool_input issues，区分只读/前置与分派后副作用。
    query_tool = AuditGroupQueryTool(root_dir, "local/caller")
    bad_query = query_tool.execute({"action": "owners", "limit": "not_an_int", "unexpected_field": 1})
    assert not bad_query.ok and bad_query.error_code == "TOOL_INVALID_ARGUMENTS"
    assert bad_query.effect_outcome == "not_started"
    assert bad_query.next_action
    bad_issues = bad_query.result_envelope["issues"]
    assert isinstance(bad_issues, list) and len(bad_issues) >= 1

    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.audit_group_tools.require_audit_group_member",
        lambda root, owner: None,
    )
    monkeypatch.setattr(
        AuditGroupObservationService,
        "list_owners",
        lambda self, caller, **kw: (_ for _ in ()).throw(OSError("simulated I/O error")),
    )
    query_err = query_tool.execute({"action": "owners"})
    assert not query_err.ok and query_err.error_code == "AUDIT_GROUP_UNAVAILABLE"
    assert query_err.effect_outcome == "not_started"
    assert query_err.result_envelope["error_details"]["exception_type"] == "OSError"
    assert query_err.next_action

    invoke_tool = AuditGroupInvokeAssetTool(root_dir, "local/caller")
    monkeypatch.setattr(
        AuditGroupBlackboxInvokeService,
        "invoke_asset",
        lambda self, caller, **kw: (_ for _ in ()).throw(OSError("simulated I/O error")),
    )
    invoke_post_err = invoke_tool.execute(
        {"source_owner": "local/provider", "asset_type": "script", "asset_id": "demo"}
    )
    assert not invoke_post_err.ok and invoke_post_err.error_code == "AUDIT_GROUP_UNAVAILABLE"
    assert invoke_post_err.effect_outcome == "unknown"
    assert invoke_post_err.retryable is False
    assert invoke_post_err.result_envelope["error_details"]["dispatched"] is True

    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.audit_group_tools.require_audit_group_member",
        lambda root, owner: (_ for _ in ()).throw(
            AuditGroupError("AUDIT_GROUP_FORBIDDEN", "not member", status_code=403)
        ),
    )
    invoke_pre_err = invoke_tool.execute(
        {"source_owner": "local/provider", "asset_type": "script", "asset_id": "demo"}
    )
    assert not invoke_pre_err.ok and invoke_pre_err.effect_outcome == "not_started"

    # 2. asset_delivery: 只读动作异常为 not_started，写动作异常为 unknown，均报告精确 action、异常类与下一步。
    delivery_tool = AssetDeliveryTool(root_dir, "local/caller")
    monkeypatch.setattr(
        asset_delivery_tool,
        "dispatch_delivery_action",
        lambda service, owner_id, params: (_ for _ in ()).throw(OSError("simulated I/O error")),
    )
    read_fail = delivery_tool.execute({"action": "inbox", "status_filter": "pending"})
    assert not read_fail.ok and read_fail.error_code == "ASSET_DELIVERY_FAILED"
    assert read_fail.effect_outcome == "not_started"
    assert read_fail.result_envelope["error_details"]["action"] == "inbox"
    assert read_fail.result_envelope["error_details"]["exception_type"] == "OSError"
    assert "inbox" in read_fail.next_action or "targets" in read_fail.next_action

    write_fail = delivery_tool.execute(
        {
            "action": "send",
            "recipient_owner_id": "local/target",
            "asset_kind": "script",
            "asset_id": "demo",
        }
    )
    assert not write_fail.ok and write_fail.error_code == "ASSET_DELIVERY_FAILED"
    assert write_fail.effect_outcome == "unknown"
    assert write_fail.retryable is False
    assert write_fail.result_envelope["error_details"]["action"] == "send"
    assert write_fail.result_envelope["error_details"]["exception_type"] == "OSError"
    assert write_fail.next_action

    # 3. feishu_auth: 前置未就绪/群上下文/禁网明确标记 not_started 并指向实际可用入口。
    feishu_ws = tmp_path / "feishu_ws"
    feishu_ws.mkdir(parents=True, exist_ok=True)
    feishu_agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "feishu_home")),
        feishu_ws,
    )
    auth_tool = FeishuAuthTool(feishu_agent)
    unconfigured = auth_tool.execute({"action": "authorize"})
    assert not unconfigured.ok and unconfigured.error_code == "TOOL_UNAVAILABLE"
    assert unconfigured.effect_outcome == "not_started"
    assert "permissions" in unconfigured.next_action
    assert unconfigured.result_envelope["error_details"]["available_entry"] == 'feishu_auth(action="permissions")'

    monkeypatch.setattr(auth_tool, "_oauth_availability", lambda: feishu_auth_tool.ToolAvailability.ready())
    net_blocked = auth_tool.execute({"action": "authorize", "__network_access_mode": "disabled"})
    assert not net_blocked.ok and net_blocked.error_code == "FEISHU_USER_AUTH_REQUIRED"
    assert net_blocked.effect_outcome == "not_started"
    assert net_blocked.result_envelope["error_details"]["reason_code"] == "NETWORK_DISABLED"
    assert "permissions" in net_blocked.next_action

    monkeypatch.setattr(feishu_auth_tool, "_assert_network", lambda agent, invocation_mode=None: None)
    monkeypatch.setattr(
        feishu_auth_tool,
        "request_oauth",
        lambda agent, invocation_mode=None: (_ for _ in ()).throw(
            ValueError("本人授权已有待处理请求，请完成或撤销原授权。")
        ),
    )
    pending_err = auth_tool.execute({"action": "authorize"})
    assert not pending_err.ok and pending_err.effect_outcome == "not_started"
    assert pending_err.result_envelope["error_details"]["reason_code"] == "OAUTH_PENDING_REQUEST_EXISTS"
    assert 'feishu_auth(action="status")' in pending_err.next_action

    # 4. channel_messages: 按错误码与就绪事实给出单一具体原因，不再罗列自相矛盾的五种可能。
    legacy_laundry_list = "请检查查询参数、功能开关、当前群归属、网络和飞书权限"
    cm_tool = ChannelMessagesTool(feishu_agent)
    cm_bad_args = cm_tool.execute({"action": "list"})
    assert not cm_bad_args.ok and cm_bad_args.error_code == "TOOL_INVALID_ARGUMENTS"
    assert cm_bad_args.effect_outcome == "not_started"
    assert legacy_laundry_list not in cm_bad_args.output
    assert cm_bad_args.result_envelope["error_details"]["cause"] == "invalid_query_parameters"

    cm_no_net = cm_tool.execute(
        {"action": "list", "start_time": 100, "end_time": 200, "__network_access_mode": "disabled"}
    )
    assert not cm_no_net.ok and cm_no_net.error_code == "NETWORK_DISABLED"
    assert cm_no_net.effect_outcome == "not_started"
    assert legacy_laundry_list not in cm_no_net.output
    assert cm_no_net.result_envelope["error_details"]["cause"] == "network_access_disabled"

    private_agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_kind="user", owner_provider="feishu"),
        config=feishu_agent.config,
    )
    cm_private_group = ChannelMessagesTool(private_agent).execute(
        {"action": "list", "start_time": 100, "end_time": 200}
    )
    assert not cm_private_group.ok
    assert cm_private_group.effect_outcome == "not_started"
    assert legacy_laundry_list not in cm_private_group.output
    assert cm_private_group.next_action


@pytest.mark.parametrize("kind", ["script", "workflow"])
def test_post_commit_readback_never_repeats_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    # 兼容检查：旧位置参数调用 _outcome 保持完全向后兼容。
    legacy = _outcome("scripts", {"ok": True}, True, "")
    assert legacy.ok and legacy.next_action == ""

    owner_home = tmp_path / f"owner_{kind}"
    owner_home.mkdir(parents=True, exist_ok=True)
    exec_root = tmp_path / f"exec_{kind}"
    exec_root.mkdir(parents=True, exist_ok=True)

    if kind == "script":
        store = ScriptStore(owner_home)
        tool = ScriptsTool(owner_home, store=store)
        tool_name = "scripts"
        params = {
            "action": "register",
            "script_id": "demo_asset",
            "code": 'print("{}")\n',
            "descriptor": {
                "name": "演示脚本",
                "access_targets": [],
                "data_sources": [],
                "credential_refs": [],
                "io_contract": {},
            },
        }
    else:
        store = WorkflowStore(owner_home)
        tool = WorkflowsTool(owner_home, store=store)
        tool_name = "workflows"
        wf_def = WorkflowDefinition.from_dict(
            {
                "descriptor": {
                    "id": "demo_asset",
                    "kind": "workflow",
                    "name": "演示工作流",
                    "access_targets": [],
                    "data_sources": [],
                    "credential_refs": [],
                    "io_contract": {},
                },
                "steps": [{"step_id": "s1", "name": "步骤一", "mode": "llm_skill_analysis", "skill_ids": []}],
            }
        )
        params = {"action": "save", "workflow_id": "demo_asset", "definition": wf_def.to_dict()}

    raw_write = store._write
    raw_get = store._get
    counts = {"writes": 0, "committed": False}

    def counted_write(*args, **kwargs):
        out = raw_write(*args, **kwargs)
        counts["writes"] += 1
        counts["committed"] = True
        return out

    def simulated_readback_get(*args, **kwargs):
        if counts["committed"]:
            raise OSError("simulated I/O error during post-commit readback")
        return raw_get(*args, **kwargs)

    monkeypatch.setattr(store, "_write", counted_write)
    monkeypatch.setattr(store, "_get", simulated_readback_get)

    execution = execute_canonical_test_call(
        exec_root,
        tools={tool_name: tool},
        tool_name=tool_name,
        arguments=params,
        allowed_tools=[tool_name],
    )
    result = execution.result
    assert counts["writes"] == 1, "post-commit readback failure must never repeat the primary write"

    monkeypatch.setattr(store, "_get", raw_get)
    assert raw_get("demo_asset") is not None, "primary write must remain committed on disk"

    assert not result.ok
    assert result.effect_outcome == "unknown"
    assert result.retryable is False
    if kind == "workflow":
        assert result.error_code == "WORKFLOW_RECONCILIATION_REQUIRED"
        assert result.metadata.get("reported_error_code") == "TOOL_POST_COMMIT_READBACK_FAILED"
    else:
        assert result.error_code == "TOOL_POST_COMMIT_READBACK_FAILED"

    next_action = result.metadata.get("next_action", "")
    assert isinstance(next_action, str) and "demo_asset" in next_action
    assert 'action="get"' in next_action or 'action="versions"' in next_action

    envelope = json.loads(result.output)
    assert envelope["committed"] is True
    assert envelope["failure_stage"] == "post_commit_readback"
    assert envelope["effect_outcome"] == "unknown"
    assert envelope["target_asset_id"] == "demo_asset"
    assert envelope["operation_action"] in {"register", "save"}
