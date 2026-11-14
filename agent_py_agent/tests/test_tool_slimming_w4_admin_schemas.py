"""W4 验收单测：管理类工具嵌套 Schema 说明、无秘密有效示例与写前/探测/持久化副作用溯源。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.model_profile_tool import ManageModelsTool
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings import embedding_selection, parameter_changes
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.thread_model_selection import default_model_profile_id
from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
from agent_py_agent.agent.tooling.compact_policy_tool import CompactPolicyTool
from agent_py_agent.agent.tooling.controlled_exec import (
    ControlledExecTool,
    ControlledExecToolRequest,
    execute_controlled_exec_tool,
)
from agent_py_agent.agent.tooling.input_schema import validate_tool_input
from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy
from agent_py_agent.agent.tooling.runtime_governance_tool import RuntimeGovernanceTool
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_model_profiles import Host


def _undescribed_paths(schema: dict) -> list[str]:
    """递归扫描 Schema 中所有缺少非空字符串 description 的属性路径。"""
    out: list[str] = []

    def walk(node: object, path: str, inherited: dict | None = None, seen: tuple[str, ...] = ()) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") and ref not in seen:
            target: object = schema
            for part in ref[2:].split("/"):
                assert isinstance(target, dict)
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
                        assert isinstance(target, dict)
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                    assert isinstance(target, dict)
                    effective = {**target, **child}
                description = (
                    effective.get("description") if isinstance(effective, dict) else None
                ) or parent.get(name, {}).get("description")
                if not isinstance(description, str) or not description.strip():
                    out.append(f"{path}.{name}")
                walk(child, f"{path}.{name}", seen=seen)
        for keyword in ("oneOf", "anyOf", "allOf"):
            branches = node.get(keyword, [])
            if isinstance(branches, list):
                merged = {**parent, **(props if isinstance(props, dict) else {})}
                for index, child in enumerate(branches):
                    walk(child, f"{path}/{keyword}/{index}", merged, seen)
        for keyword in ("items", "additionalProperties"):
            child = node.get(keyword)
            if isinstance(child, dict):
                walk(child, f"{path}/{keyword}", seen=seen)

    walk(schema, "$")
    return sorted(set(out))


def _admin_agent(tmp_path: Path, user_yaml: Path | None = None) -> SimpleNamespace:
    home_root = tmp_path / "home"
    home_root.mkdir(parents=True, exist_ok=True)
    home = SimpleNamespace(
        root=home_root,
        owner_home_dir=home_root,
        owner_provider="local",
        owner_kind="main",
        owner_id="main",
    )
    config = AgentConfig(max_tokens=65536)
    if user_yaml is not None:
        config.config_path = str(user_yaml)
    return SimpleNamespace(
        root=tmp_path,
        home_paths=home,
        config=config,
        tools=SimpleNamespace(owner_type="main_agent", owner_id="local/main"),
    )


def _user_agent(tmp_path: Path, owner_id: str = "alice") -> SimpleNamespace:
    home_root = tmp_path / "home"
    owner_dir = home_root / "owners" / "local" / owner_id
    owner_dir.mkdir(parents=True, exist_ok=True)
    home = SimpleNamespace(
        root=home_root,
        owner_home_dir=owner_dir,
        owner_provider="local",
        owner_kind="user",
        owner_id=f"local/{owner_id}",
    )
    return SimpleNamespace(
        root=tmp_path,
        home_paths=home,
        config=AgentConfig(max_tokens=32768),
        tools=SimpleNamespace(owner_type="user", owner_id=f"local/{owner_id}"),
    )


def _model_host(tmp_path: Path, owner: str = "local/main", *, admin: bool = True) -> Host:
    tmp_path.mkdir(parents=True, exist_ok=True)
    host = Host(tmp_path / "config", owner)
    if admin:
        host.home_paths.owner_provider = "local"
        host.home_paths.owner_kind = "main"
        host.home_paths.owner_id = "local/main"
    else:
        host.home_paths.owner_provider = "local"
        host.home_paths.owner_kind = "user"
        host.home_paths.owner_id = f"local/{owner}"
    user_yaml = tmp_path / f"{owner.replace('/', '_')}_agent.yaml"
    user_yaml.write_text('agent_name: "w4-host"\n', encoding="utf-8")
    host.config = AgentConfig(
        config_path=str(user_yaml),
        api_base="https://api.same-host.test/v1",
        model_backend="openai_compatible",
    )
    host.conversation_store = ConversationStore(
        tmp_path / owner.replace("/", "_"),
        model_default=lambda: default_model_profile_id(host),
    )
    return host


def test_admin_nested_schemas_and_examples(tmp_path: Path) -> None:
    admin = _admin_agent(tmp_path)
    regular_user = _user_agent(tmp_path)
    model_host = _model_host(tmp_path)

    full_user_config = UserConfigTool(admin)
    restricted_user_config = UserConfigTool(regular_user)
    admin_controls = AdminControlsTool(admin)
    manage_models = ManageModelsTool(model_host)
    runtime_governance = RuntimeGovernanceTool(
        agent=admin,
        gateway_workspace=tmp_path / "gw",
        is_admin=True,
        owner_id="local",
    )
    compact_policy = CompactPolicyTool(admin)
    controlled_exec = ControlledExecTool()

    specs = {
        "user_config_full": full_user_config.model_spec,
        "user_config_restricted": restricted_user_config.model_spec,
        "admin_controls": admin_controls.model_spec,
        "manage_models": manage_models.model_spec,
        "runtime_governance": runtime_governance.model_spec,
        "compact_policy": compact_policy.model_spec,
        "controlled_exec": controlled_exec.model_spec,
    }

    forbidden_secret_markers = ("sk-", "Bearer ", "<用户给的密钥>", '"password":', '"api_key":')
    for label, spec in specs.items():
        assert _undescribed_paths(spec.input_schema) == [], label
        assert spec.hints.use_cases, f"{label} missing use_cases"
        assert spec.hints.examples, f"{label} missing examples"
        for raw_example in spec.hints.examples:
            for marker in forbidden_secret_markers:
                assert marker not in raw_example, f"{label} example leaked secret marker {marker}"
            parsed = json.loads(raw_example)
            validation = validate_tool_input(parsed, spec.input_schema)
            assert validation.ok, f"{label} invalid example {raw_example}: {validation.issues}"

    # 校验管理身份、普通用户范围与子代理限制保持不退化。
    assert "set" in full_user_config.model_spec.input_schema["properties"]["action"]["enum"]
    assert "set" not in restricted_user_config.model_spec.input_schema["properties"]["action"]["enum"]
    assert not AdminControlsTool(regular_user).availability().available

    token = set_current_subagent_context(admin, run_id="subagent-w4", task_attributes={})
    try:
        assert not AdminControlsTool(admin).availability().available
        assert not CompactPolicyTool(admin).execute({"action": "status"}).ok
        assert not RuntimeGovernanceTool(
            agent=admin,
            gateway_workspace=tmp_path / "gw",
            is_admin=True,
            owner_id="local",
        ).execute({"action": "set_maintenance_lock", "admin_authorized": True}).ok
    finally:
        restore_current_subagent_context(admin, token)

    child_token = set_current_subagent_context(model_host, run_id="subagent-models-w4", task_attributes={})
    try:
        assert not ManageModelsTool(model_host).availability().available
    finally:
        restore_current_subagent_context(model_host, child_token)


def test_pre_effect_config_rejection_has_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)

    # 1. controlled_exec 缺少宿主可信 write_boundary 时必须在启动任何进程前返回 not_started 并指明宿主入口。
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.controlled_exec.execute_shell_command",
        lambda *_a, **_k: pytest.fail("pre-effect refusal must not invoke execute_shell_command"),
    )
    direct = ControlledExecTool().execute({"command": "pwd", "apply": True})
    assert direct.ok is False
    assert direct.error_code == "TOOL_EXECUTION_FAILED"
    assert direct.effect_outcome == "not_started"
    assert direct.failure_stage == "runtime_gate"
    assert "write_boundary" in direct.output
    assert "invoke_registry_tool" in direct.next_action

    no_grant = execute_controlled_exec_tool(
        ControlledExecToolRequest(params={"command": "pwd", "apply": True}, workspace_root=tmp_path, write_boundary={})
    )
    assert no_grant.ok is False
    assert no_grant.error_code == "WRITE_FORBIDDEN"
    assert no_grant.effect_outcome == "not_started"

    # 2. user_config 写前拒绝必须沉淀为 not_started（终态 failed），不锁死操作账本；持久化异常保留 unknown。
    user_yaml = tmp_path / "user_config.yaml"
    initial_text = 'agent_name: "w4-agent"\n'
    user_yaml.write_text(initial_text, encoding="utf-8")
    admin = _admin_agent(tmp_path, user_yaml)
    tool = UserConfigTool(admin)

    bad_action = tool.execute({"action": "unsupported_action"})
    assert bad_action.ok is False and bad_action.effect_outcome == "not_started"

    op_store = LocalStore(tmp_path / "operations.sqlite3")
    pre_write_cases = [
        ("call-boundary", {"action": "set", "key": "gateway_bind_host", "value": "127.0.0.1"}, "TOOL_PERMISSION_DENIED"),
        ("call-unknown", {"action": "set", "key": "nonexistent_w4_key", "value": "1"}, "TOOL_INVALID_ARGUMENTS"),
        ("call-invalid", {"action": "set", "key": "max_tokens", "value": "-9"}, "TOOL_INVALID_ARGUMENTS"),
        ("call-not-overridden", {"action": "reset", "key": "max_tokens"}, "TOOL_INVALID_ARGUMENTS"),
        ("call-missing-change", {"action": "revert", "change_id": "deadbeef"}, "TOOL_INVALID_ARGUMENTS"),
    ]
    for call_id, args, expected_code in pre_write_cases:
        execution = execute_canonical_test_call(
            tmp_path,
            tools={"user_config": tool},
            tool_name="user_config",
            arguments=args,
            operation_store=op_store,
            operation_store_required=True,
            call_id=call_id,
            operation_id=f"op-{call_id}",
            idempotency_key=f"idem-{call_id}",
        )
        assert execution.result.ok is False, call_id
        assert execution.result.error_code == expected_code, call_id
        assert execution.result.effect_outcome == "not_started", call_id
        assert execution.result.operation is not None
        assert execution.result.operation.status == "failed", call_id

    assert user_yaml.read_text(encoding="utf-8") == initial_text
    assert parameter_changes.parameter_history(user_path=user_yaml) == []

    # 证明写前拒绝未留下 unknown 阻塞，后续合法写入在同一 scope 上正常提交。
    valid_exec = execute_canonical_test_call(
        tmp_path,
        tools={"user_config": tool},
        tool_name="user_config",
        arguments={"action": "set", "key": "max_tokens", "value": "32768"},
        operation_store=op_store,
        operation_store_required=True,
        call_id="call-valid-set",
        operation_id="op-valid-set",
        idempotency_key="idem-valid-set",
    )
    assert valid_exec.result.ok is True, valid_exec.result.output
    assert valid_exec.result.effect_outcome == "confirmed"
    assert load_config(user_yaml).max_tokens == 32768

    # 写前 CAS 版本冲突同样证明未落盘（STALE_VERSION + not_started）。
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.user_config_tool.set_parameter",
        lambda *_a, **_k: {"ok": False, "code": "CONFIG_REVISION_CONFLICT", "error": "配置文件已被并发更新"},
    )
    cas_exec = execute_canonical_test_call(
        tmp_path,
        tools={"user_config": tool},
        tool_name="user_config",
        arguments={"action": "set", "key": "max_tokens", "value": "16384"},
        operation_store=op_store,
        operation_store_required=True,
        call_id="call-cas-conflict",
        operation_id="op-cas-conflict",
        idempotency_key="idem-cas-conflict",
    )
    assert cas_exec.result.ok is False
    assert cas_exec.result.error_code == "STALE_VERSION"
    assert cas_exec.result.effect_outcome == "not_started"
    assert cas_exec.result.operation is not None and cas_exec.result.operation.status == "failed"

    # 真实写入失败、回滚不完整与缺少写前阶段证明的 PARAMETER_NOT_EFFECTIVE 必须保持 unknown 且禁止自动重试。
    for index, raw_code in enumerate(
        ("USER_CONFIG_WRITE_FAILED", "CONFIG_ROLLBACK_INCOMPLETE", "PARAMETER_NOT_EFFECTIVE")
    ):
        monkeypatch.setattr(
            "agent_py_agent.agent.tooling.user_config_tool.set_parameter",
            lambda *_a, _c=raw_code, **_k: {"ok": False, "code": _c, "error": f"simulated I/O error: {_c}"},
        )
        uncertain_home = tmp_path / f"uncertain-{index}"
        uncertain_home.mkdir(parents=True, exist_ok=True)
        isolated_store = LocalStore(uncertain_home / "ops.sqlite3")
        uncertain_exec = execute_canonical_test_call(
            tmp_path,
            tools={"user_config": tool},
            tool_name="user_config",
            arguments={"action": "set", "key": "request_timeout", "value": "120"},
            operation_store=isolated_store,
            operation_store_required=True,
            call_id=f"call-uncertain-{index}",
            operation_id=f"op-uncertain-{index}",
            idempotency_key=f"idem-uncertain-{index}",
        )
        assert uncertain_exec.result.ok is False
        assert uncertain_exec.result.effect_outcome == "unknown"
        assert uncertain_exec.result.retryable is False
        assert uncertain_exec.result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
        assert uncertain_exec.result.operation is not None
        assert uncertain_exec.result.operation.status == "unknown"
        unknown_body = json.loads(uncertain_exec.result.output)
        assert unknown_body["reported_error_code"] == raw_code


def test_probe_and_embedding_effects_differ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    admin_host = _model_host(tmp_path / "admin", "local/main", admin=True)
    member_host = _model_host(tmp_path / "member", "alice", admin=False)
    admin_tool = ManageModelsTool(admin_host)
    member_tool = ManageModelsTool(member_host)

    # 先写入一个对话主模型和一个同主机 embedding 模型，以及一个异主机 embedding 模型。
    chat_add = admin_tool.execute(
        {
            "action": "add",
            "profile": {
                "model_name": "chat-main",
                "model_backend": "openai_compatible",
                "api_base": "https://api.same-host.test/v1",
                "api_key": "test-credential-value-1",
                "model_context_window_tokens": 128000,
            },
        }
    )
    assert chat_add.ok
    chat_id = json.loads(chat_add.output)["profile_id"]
    assert admin_tool.execute({"action": "set_default", "profile_id": chat_id}).ok
    embedding_selection.remember_startup_chat_host(admin_host)

    same_host_emb = admin_tool.execute(
        {
            "action": "add",
            "profile": {
                "model_name": "embed-same",
                "model_backend": "openai_compatible",
                "api_base": "https://api.same-host.test/v1",
                "api_key": "test-credential-value-2",
                "model_context_window_tokens": 8192,
                "capability": "embedding",
            },
        }
    )
    assert same_host_emb.ok
    same_emb_id = json.loads(same_host_emb.output)["profile_id"]

    other_host_emb = admin_tool.execute(
        {
            "action": "add",
            "profile": {
                "model_name": "embed-other",
                "model_backend": "openai_compatible",
                "api_base": "https://api.other-host.test/v1",
                "api_key": "test-credential-value-3",
                "model_context_window_tokens": 8192,
                "capability": "embedding",
            },
        }
    )
    assert other_host_emb.ok
    other_emb_id = json.loads(other_host_emb.output)["profile_id"]

    assert tool_effect_for_runtime_policy(ManageModelsTool.runtime_policy, {"action": "probe"}) == "read_only"
    assert tool_effect_for_runtime_policy(ManageModelsTool.runtime_policy, {"action": "discover"}) == "read_only"
    assert tool_effect_for_runtime_policy(ManageModelsTool.runtime_policy, {"action": "set_embedding"}) == "mutating"
    assert tool_effect_for_runtime_policy(ManageModelsTool.runtime_policy, {"action": "disable_embedding"}) == "mutating"

    op_store = LocalStore(tmp_path / "models-ops.sqlite3")
    # 1. probe 与 discover 是只读远端探测：失败属于执行期服务商事实，不建 mutating 操作锁，更不标成 unknown。
    monkeypatch.setattr(
        "agent_py_agent.agent.settings.model_provider_network.get_backend",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("simulated provider probe failure")),
    )
    probe_exec = execute_canonical_test_call(
        tmp_path,
        tools={"manage_models": admin_tool},
        tool_name="manage_models",
        arguments={"action": "probe", "profile_id": chat_id},
        operation_store=op_store,
        operation_store_required=True,
        call_id="probe-fail",
        operation_id="op-probe-fail",
        idempotency_key="idem-probe-fail",
    )
    assert probe_exec.decision.resolved_effect == "read_only"
    assert probe_exec.result.ok is False
    assert probe_exec.result.error_code == "MODEL_PROBE_FAILED"
    assert probe_exec.result.failure_stage == "execution"
    assert probe_exec.result.effect_outcome == "not_started"
    assert probe_exec.result.retryable is False
    assert probe_exec.result.operation is None

    monkeypatch.setattr(
        "agent_py_agent.agent.settings.model_provider_network.get_json",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("simulated provider discover failure")),
    )
    discover_exec = execute_canonical_test_call(
        tmp_path,
        tools={"manage_models": admin_tool},
        tool_name="manage_models",
        arguments={
            "action": "discover",
            "connection": {
                "model_backend": "openai_compatible",
                "api_base": "https://api.same-host.test/v1",
                "api_key": "test-credential-value-1",
            },
        },
        operation_store=op_store,
        operation_store_required=True,
        call_id="discover-fail",
        operation_id="op-discover-fail",
        idempotency_key="idem-discover-fail",
    )
    assert discover_exec.decision.resolved_effect == "read_only"
    assert discover_exec.result.ok is False
    assert discover_exec.result.error_code == "MODEL_DISCOVER_FAILED"
    assert discover_exec.result.failure_stage == "execution"
    assert discover_exec.result.effect_outcome == "not_started"
    assert discover_exec.result.operation is None

    # 2. embedding 写前拒绝（非管理员权限边界、主机不一致）必须作为 mutating 写前失败（not_started / failed）结清。
    denied_emb = execute_canonical_test_call(
        tmp_path,
        tools={"manage_models": member_tool},
        tool_name="manage_models",
        arguments={"action": "disable_embedding"},
        operation_store=op_store,
        operation_store_required=True,
        call_id="emb-member-denied",
        operation_id="op-emb-member-denied",
        idempotency_key="idem-emb-member-denied",
    )
    assert denied_emb.decision.resolved_effect == "mutating"
    assert denied_emb.result.ok is False
    assert denied_emb.result.error_code == "TOOL_PERMISSION_DENIED"
    assert denied_emb.result.failure_stage == "validation"
    assert denied_emb.result.effect_outcome == "not_started"
    assert denied_emb.result.operation is not None and denied_emb.result.operation.status == "failed"

    host_diff_emb = execute_canonical_test_call(
        tmp_path,
        tools={"manage_models": admin_tool},
        tool_name="manage_models",
        arguments={"action": "set_embedding", "profile_id": other_emb_id},
        operation_store=op_store,
        operation_store_required=True,
        call_id="emb-host-diff",
        operation_id="op-emb-host-diff",
        idempotency_key="idem-emb-host-diff",
    )
    assert host_diff_emb.decision.resolved_effect == "mutating"
    assert host_diff_emb.result.ok is False
    assert host_diff_emb.result.error_code == "EMBEDDING_HOST_DIFFERS"
    assert host_diff_emb.result.failure_stage == "validation"
    assert host_diff_emb.result.effect_outcome == "not_started"
    assert host_diff_emb.result.operation is not None and host_diff_emb.result.operation.status == "failed"

    # 写前拒绝后，同主机合法 set_embedding 仍可正常落盘并结清为 confirmed。
    ok_emb = execute_canonical_test_call(
        tmp_path,
        tools={"manage_models": admin_tool},
        tool_name="manage_models",
        arguments={"action": "set_embedding", "profile_id": same_emb_id},
        operation_store=op_store,
        operation_store_required=True,
        call_id="emb-ok",
        operation_id="op-emb-ok",
        idempotency_key="idem-emb-ok",
    )
    assert ok_emb.result.ok is True, ok_emb.result.output
    assert ok_emb.result.effect_outcome == "confirmed"
    assert ok_emb.result.operation is not None and ok_emb.result.operation.status == "succeeded"

    # 3. embedding 持久化阶段出现半写入/写入异常时必须保持保守 unknown，禁止自动重放并保留原报错误码。
    monkeypatch.setattr(
        embedding_selection,
        "apply_embedding_choice",
        lambda *_a, **_k: {
            "ok": False,
            "error_code": "USER_CONFIG_WRITE_FAILED",
            "change_ids": ["partial-step-1"],
            "message": "simulated I/O error during second config key write",
        },
    )
    partial_emb = execute_canonical_test_call(
        tmp_path,
        tools={"manage_models": admin_tool},
        tool_name="manage_models",
        arguments={"action": "set_embedding", "profile_id": same_emb_id},
        operation_store=op_store,
        operation_store_required=True,
        call_id="emb-partial-fail",
        operation_id="op-emb-partial-fail",
        idempotency_key="idem-emb-partial-fail",
    )
    assert partial_emb.decision.resolved_effect == "mutating"
    assert partial_emb.result.ok is False
    assert partial_emb.result.effect_outcome == "unknown"
    assert partial_emb.result.retryable is False
    assert partial_emb.result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert partial_emb.result.operation is not None and partial_emb.result.operation.status == "unknown"
    partial_body = json.loads(partial_emb.result.output)
    assert partial_body["reported_error_code"] == "USER_CONFIG_WRITE_FAILED"
