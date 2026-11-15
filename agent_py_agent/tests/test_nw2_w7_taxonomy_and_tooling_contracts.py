"""NW2 W7 合同回归测试：错误分类、未知结果头尾截断与工具 Schema 收敛。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.create_session_task import (
    build_create_session_task_model_spec,
)
from agent_py_agent.agent.agent_core.orchestration.tools.send_session_message import (
    build_send_session_message_model_spec,
)
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.recovery import RecoveryAction
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.io.sqlite_connection import sqlite_error_details
from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.recovery_guard import (
    RuntimeDatabaseDeferred,
    RuntimeDatabaseRecoveryRequired,
    runtime_db_recovery_error,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_identity
from agent_py_agent.agent.runtime_errors import fallback_error_code
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.tool_operation_coordinator import _unknown_outcome_result
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.business_scope import WORKFLOW_ACTION_FIELDS


def _registry_params(workspace: Path, *, live_config: object | None = None) -> ToolRegistryParams:
    return ToolRegistryParams(
        workspace_root=workspace,
        max_chars=4000,
        max_entries=100,
        max_matches=50,
        web_max_chars=4000,
        http_timeout=10,
        catalog_limit=20,
        retrieval_limit=5,
        vector_search_enabled=False,
        live_config=live_config,
    )


def test_runtime_db_recovery_required_exception_and_taxonomy_contract(tmp_path: Path) -> None:
    err = RuntimeDatabaseRecoveryRequired("candidate_missing_authority_table")
    assert isinstance(err, RuntimeDatabaseDeferred)
    assert err.sqlite_errorcode == sqlite3.SQLITE_CORRUPT
    assert err.sqlite_errorname == "SQLITE_CORRUPT"
    assert err.status == "requires_recovery"
    assert err.code == "RUNTIME_DB_RECOVERY_REQUIRED"
    assert err.error_code == "RUNTIME_DB_RECOVERY_REQUIRED"
    assert fallback_error_code(err) == "RUNTIME_DB_RECOVERY_REQUIRED"

    details = sqlite_error_details(err)
    assert details["sqlite_primary_code"] == sqlite3.SQLITE_CORRUPT
    assert details["category"] == "structural"

    deferred = runtime_db_recovery_error({"status": "deferred", "reason": "recovery_pending"})
    assert type(deferred) is RuntimeDatabaseDeferred
    assert deferred.error_code == "RUNTIME_DB_RECOVERY_DEFERRED"

    required = runtime_db_recovery_error({"status": "requires_recovery", "error": {"type": "DatabaseError"}})
    assert type(required) is RuntimeDatabaseRecoveryRequired
    assert required.reason == "DatabaseError"
    assert required.runtime_db_recovery["status"] == "requires_recovery"

    contract = error_contract("RUNTIME_DB_RECOVERY_REQUIRED")
    assert contract.code == "RUNTIME_DB_RECOVERY_REQUIRED"
    assert contract.category == "storage"
    assert contract.retryable is False
    assert contract.recommended_action == RecoveryAction.MANUAL_REVIEW.value
    assert "scripts/runtime_db_salvage.py" in contract.recovery_hint
    assert "备份" in contract.recovery_hint
    assert "<" not in contract.recovery_hint and ">" not in contract.recovery_hint

    client_msg = gateway_client_error_message("RUNTIME_DB_RECOVERY_REQUIRED")
    assert "scripts/runtime_db_salvage.py" in client_msg
    assert "备份" in client_msg
    assert "稍后重试" not in client_msg


def test_requires_recovery_raises_terminal_error_in_schema_and_startup(tmp_path: Path) -> None:
    db = tmp_path / "owners/local/main/runtime.db"
    RuntimeRepository(db)
    identity = runtime_db_identity(db)
    salvage._save_state(
        db,
        {
            "status": "requires_recovery",
            "reason": "authority_check_failed",
            "identity": list(identity),
        },
        "requires_recovery",
    )

    with pytest.raises(RuntimeDatabaseRecoveryRequired) as startup_exc:
        salvage.heal_runtime_db_at_startup(db)
    assert startup_exc.value.error_code == "RUNTIME_DB_RECOVERY_REQUIRED"
    assert startup_exc.value.runtime_db_recovery["status"] == "requires_recovery"

    with pytest.raises(RuntimeDatabaseRecoveryRequired) as open_exc:
        RuntimeRepository(db)._heal_runtime_db_before_open()
    assert open_exc.value.error_code == "RUNTIME_DB_RECOVERY_REQUIRED"
    assert open_exc.value.runtime_db_recovery["status"] == "requires_recovery"


def test_deferred_status_keeps_retryable_exception_in_startup_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "owners/local/main/runtime.db"
    RuntimeRepository(db)
    salvage._marker(db).touch()
    monkeypatch.setattr(
        salvage,
        "recover_runtime_database",
        lambda *_args, **_kwargs: {"status": "deferred", "reason": "recovery_pending"},
    )

    with pytest.raises(RuntimeDatabaseDeferred) as startup_exc:
        salvage.heal_runtime_db_at_startup(db)
    assert type(startup_exc.value) is RuntimeDatabaseDeferred
    assert startup_exc.value.error_code == "RUNTIME_DB_RECOVERY_DEFERRED"

    with pytest.raises(RuntimeDatabaseDeferred) as open_exc:
        RuntimeRepository(db)._heal_runtime_db_before_open()
    assert type(open_exc.value) is RuntimeDatabaseDeferred
    assert open_exc.value.error_code == "RUNTIME_DB_RECOVERY_DEFERRED"


def test_unknown_outcome_preview_preserves_head_800_and_tail_1200() -> None:
    class _Req:
        tool_name = "run_command"

    short_text = "x" * 2000
    short_outcome = _unknown_outcome_result(
        _Req(),
        ToolHandlerOutcome("run_command", False, short_text, error_code="COMMAND_FAILED", handler_executed=True),
    )
    assert json.loads(short_outcome.output)["reported_output_preview"] == short_text

    head = "HEAD_ANCHOR:" + ("a" * 788)
    middle = "MIDDLE_SKIP_TOKEN:" + ("m" * 2482)
    tail = ("z" * 1170) + ":TAIL_TRACEBACK_END_9876543210"
    assert len(head) == 800 and len(tail) == 1200
    full_text = head + middle + tail
    long_outcome = _unknown_outcome_result(
        _Req(),
        ToolHandlerOutcome("run_command", False, full_text, error_code="COMMAND_FAILED", handler_executed=True),
    )
    preview = json.loads(long_outcome.output)["reported_output_preview"]
    assert preview == f"{head}\n...[omitted {len(middle)} chars]...\n{tail}"
    assert "HEAD_ANCHOR:" in preview
    assert "TAIL_TRACEBACK_END_9876543210" in preview
    assert "MIDDLE_SKIP_TOKEN:" not in preview

    # 长 JSON 工具输出中 stderr_preview 若处于 800..len-1200 区间，也必须前置保留异常原因。
    large_json = json.dumps(
        {
            "script_id": "s1",
            "stdout_preview": "p" * 700,
            "stderr_preview": ("stack...\n" * 18) + "RuntimeError: buried-failure-signal",
            "metadata_blob": "q" * 1600,
        },
        ensure_ascii=False,
    )
    assert len(large_json) > 2000
    json_outcome = _unknown_outcome_result(
        _Req(),
        ToolHandlerOutcome("scripts", False, large_json, error_code="TOOL_EXECUTION_FAILED", handler_executed=True),
    )
    assert "RuntimeError: buried-failure-signal" in json.loads(json_outcome.output)["reported_output_preview"]


def test_registry_dynamically_hides_and_restores_business_workflow_actions(tmp_path: Path) -> None:
    def _workflow_spec(snapshot, *, catalog: bool = False):
        source = snapshot.catalog_specs if catalog else snapshot.specs
        return next(spec for spec in source if spec.name == "workflows")

    # 无 live_config 的独立注册表保持默认开启，维持既有向后兼容。
    standalone = ToolRegistry(_registry_params(tmp_path, live_config=None))
    standalone.register(WorkflowsTool(tmp_path))
    standalone_actions = set(
        _workflow_spec(standalone.runtime_snapshot()).input_schema["properties"]["action"]["enum"]
    )
    assert set(WORKFLOW_ACTION_FIELDS) <= standalone_actions

    live_config = SimpleNamespace(business_spaces_enabled=True, enable_tools=True)
    registry = ToolRegistry(_registry_params(tmp_path, live_config=live_config))
    registry.register(WorkflowsTool(tmp_path))
    enabled_spec = _workflow_spec(registry.runtime_snapshot())
    assert set(WORKFLOW_ACTION_FIELDS) <= set(enabled_spec.input_schema["properties"]["action"]["enum"])

    # 动态热切换为 False 时，同一 registry 实例下一轮快照立即剔除 21 个点号业务动作（含 catalog_specs）。
    live_config.business_spaces_enabled = False
    disabled_snapshot = registry.runtime_snapshot()
    disabled_spec = _workflow_spec(disabled_snapshot)
    disabled_catalog_spec = _workflow_spec(disabled_snapshot, catalog=True)
    for checked_spec in (disabled_spec, disabled_catalog_spec):
        disabled_actions = set(checked_spec.input_schema["properties"]["action"]["enum"])
        assert set(WORKFLOW_ACTION_FIELDS).isdisjoint(disabled_actions)
        assert {"list", "get", "save", "run", "preview", "inspect_run", "resume"} <= disabled_actions
        for branch in checked_spec.input_schema.get("oneOf", ()):
            props = branch.get("properties") if isinstance(branch, dict) else None
            act_prop = props.get("action") if isinstance(props, dict) else None
            if isinstance(act_prop, dict) and isinstance(act_prop.get("enum"), list):
                assert set(WORKFLOW_ACTION_FIELDS).isdisjoint(act_prop["enum"])

    # 再切换回 True 时，同一 registry 立即恢复全部点号业务动作。
    live_config.business_spaces_enabled = True
    restored_snapshot = registry.runtime_snapshot()
    assert set(WORKFLOW_ACTION_FIELDS) <= set(
        _workflow_spec(restored_snapshot).input_schema["properties"]["action"]["enum"]
    )
    assert set(WORKFLOW_ACTION_FIELDS) <= set(
        _workflow_spec(restored_snapshot, catalog=True).input_schema["properties"]["action"]["enum"]
    )


@pytest.mark.parametrize(
    ("action", "params", "expected_target"),
    [
        ("workflows.list", {}, "list"),
        ("workflows.get", {"workflow_ref": "wf-1"}, "get"),
        ("workflows.run", {"workflow_ref": "wf-1", "idempotency_key": "k-run"}, "run"),
        ("workflows.preview", {"workflow_ref": "wf-1"}, "preview"),
        ("workflow_runs.get", {"workflow_run_id": "run-1"}, "inspect_run"),
        (
            "workflow_runs.resume",
            {"workflow_run_id": "run-1", "idempotency_key": "k-res", "expected_revision": 0},
            "resume",
        ),
        (
            "workflows.enable",
            {"workflow_ref": "wf-1", "idempotency_key": "k-en", "expected_revision": 0},
            'self_map(section="business")',
        ),
    ],
)
def test_workflows_business_host_unavailable_emits_structured_next_action(
    tmp_path: Path, action: str, params: dict[str, object], expected_target: str
) -> None:
    tool = WorkflowsTool(tmp_path)
    spec = tool.model_spec
    action_desc = spec.input_schema["properties"]["action"]["description"]
    assert "不带点号" in action_desc and "workflow_id" in action_desc
    assert "workflows.*/workflow_runs.*" in action_desc and "idempotency_key" in action_desc
    assert "scripts" in " ".join(spec.hints.avoid_when)

    outcome = tool.execute({"action": action, **params})
    assert outcome.ok is False
    assert outcome.error_code == "WORKFLOW_BUSINESS_HOST_UNAVAILABLE"
    assert outcome.effect_outcome == "not_started"
    assert expected_target in outcome.next_action

    result_data = json.loads(outcome.output)
    assert result_data["ok"] is False
    assert result_data["error_code"] == "WORKFLOW_BUSINESS_HOST_UNAVAILABLE"
    assert result_data["effect_outcome"] == "not_started"
    assert result_data["retryable"] is False
    assert expected_target in result_data["next_action"]

    contract = error_contract("WORKFLOW_BUSINESS_HOST_UNAVAILABLE")
    assert contract.category == "state"
    assert contract.retryable is False
    assert contract.recommended_action == RecoveryAction.CHANGE_STRATEGY.value
    assert "<" not in contract.recovery_hint and ">" not in contract.recovery_hint


def test_workflows_host_not_ready_precedence_preserved_when_provider_wired(tmp_path: Path) -> None:
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    def _disabled_provider():
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "business spaces disabled")

    tool = WorkflowsTool(tmp_path, business_binding_provider=_disabled_provider)
    denied = tool.execute({"action": "workflows.list", "space_id": "sp-1"})
    assert denied.ok is False
    assert denied.error_code == "HOST_NOT_READY"
    assert "WORKFLOW_BUSINESS_HOST_UNAVAILABLE" not in denied.output


def test_user_config_schema_guides_compact_edits_to_compact_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    admin = SimpleNamespace(
        root=tmp_path,
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"),
        config=SimpleNamespace(),
    )
    tool = UserConfigTool(admin)
    spec = tool.model_spec
    assert "compact 百分比" not in spec.description
    key_desc = spec.input_schema["properties"]["key"]["description"]
    assert "conversation_history_max_turns" in key_desc
    assert "memory_compact_trigger_percent" not in key_desc
    examples_text = " ".join(spec.hints.examples)
    assert "conversation_history_max_turns" in examples_text
    assert "memory_compact_trigger_percent" not in examples_text
    avoid_text = " ".join(spec.hints.avoid_when)
    assert "compact_policy" in avoid_text
    assert "set_thread" in avoid_text and "set_user" in avoid_text

    # 显式传入 memory_compact_trigger_percent 的兼容查询依然可用。
    view_outcome = tool.execute({"action": "view", "key": "memory_compact_trigger_percent"})
    assert view_outcome.ok is True


def test_session_orchestration_tool_schemas_reference_list_owner_sessions() -> None:
    msg_spec = build_send_session_message_model_spec()
    task_spec = build_create_session_task_model_spec()

    for spec in (msg_spec, task_spec):
        desc = spec.input_schema["properties"]["target_thread_id"]["description"]
        assert "list_owner_sessions" in desc
        assert "list_sessions" not in desc
        assert "/sessions threads" not in desc
