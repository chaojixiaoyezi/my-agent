from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from agent_py_agent.agent.contracts.gates.tool_effects import args_hash_for_call
from agent_py_agent.agent.contracts.tool_protocol_v2 import normalize_tool_call
from agent_py_agent.agent.tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from agent_py_agent.agent.tooling.registry_execution import (
    ExecuteRegistryCallParams,
    execute_registry_call,
)


class EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        category="utility",
        effect="read_only",
        description="Return params for tests.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult("echo", True, json.dumps(params, sort_keys=True))


class MissingManifestTool(BaseTool):
    spec = ToolSpec(
        name="missing_manifest",
        category="utility",
        description="Intentionally incomplete manifest for tests.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult("missing_manifest", True, "should not execute")


def test_registry_execution_returns_runtime_gate_denial_for_unknown_tool(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "magic_tool", "value": 1},
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["status"] == "DENY"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "TOOL_NOT_REGISTERED"


def test_registry_execution_records_runtime_gate_allow_for_executed_tool(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "value": 1},
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
        )
    )

    assert result.ok is True
    assert result.result_envelope["runtime_gate"]["status"] == "ALLOW"
    assert result.result_envelope["runtime_gate"]["evidence"]["tool_name"] == "echo"
    assert "tool_rate_limit" in result.result_envelope["runtime_gate"]["evidence"]["executed_gates"]


def test_registry_execution_blocks_when_runtime_rate_limit_is_exhausted(tmp_path):
    payload = {"tool": "echo", "value": 1}
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload=payload,
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
            write_boundary={
                "now": 10.0,
                "tool_rate_limit_policy": {"max_calls": 1, "window_seconds": 60},
                "tool_rate_limit_records": [
                    {
                        "tool_name": "echo",
                        "args_hash": _args_hash_for_legacy_payload(payload),
                        "attempt_timestamps": [9.0],
                    }
                ],
            },
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "tool_rate_limit"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "TOOL_RATE_LIMIT_EXCEEDED"


# LLM: runtime write-boundary policies can explicitly disable tool rate limits with zero budgets.
# 函数用途: 验证 max_calls=0 和 failure_threshold=0 在 registry gate 里也表示不启用对应次数门。
def test_registry_execution_zero_rate_limit_policy_is_unlimited(tmp_path):
    payload = {"tool": "echo", "value": 1}
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload=payload,
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
            write_boundary={
                "now": 10.0,
                "tool_rate_limit_policy": {"max_calls": 0, "window_seconds": 60, "failure_threshold": 0},
                "tool_rate_limit_records": [
                    {
                        "tool_name": "echo",
                        "args_hash": _args_hash_for_legacy_payload(payload),
                        "attempt_timestamps": [1.0, 2.0, 3.0],
                        "consecutive_failures": 9,
                        "last_failure_at": 9.0,
                    }
                ],
            },
        )
    )

    assert result.ok is True


def test_registry_execution_blocks_tool_with_incomplete_manifest(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "missing_manifest", "value": 1},
            tools={"missing_manifest": MissingManifestTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "tool_manifest"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "TOOL_MANIFEST_EFFECT_MISSING"


def test_registry_execution_blocks_path_gate_before_tool_execute(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)

    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "path": "link"},
            tools={"echo": EchoTool()},
            workspace_root=workspace,
            workspace_roots=[workspace],
            expose_security_tools=False,
            security_tool_names=set(),
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "path_url_command"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "PATH_SYMLINK_ESCAPE_BLOCKED"


def test_registry_execution_blocks_dangerous_real_tool_without_approval(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "value": 1, "mode": "real", "idempotency_key": "idem-echo-dangerous"},
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
            write_boundary={"tool_effects": {"echo": "dangerous"}},
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["status"] == "NEED_APPROVAL"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "APPROVAL_REQUIRED"


def test_registry_execution_rejects_mismatched_approval_binding(tmp_path):
    payload = {
        "tool": "echo",
        "value": 1,
        "mode": "real",
        "run_id": "run-1",
        "operation_id": "op-1",
        "idempotency_key": "idem-echo-dangerous",
    }
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload=payload,
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
            write_boundary={
                "run_id": "run-1",
                "tool_effects": {"echo": "dangerous"},
                "approved_actions": [
                    {
                        "approval_id": "approval-1",
                        "status": "APPROVED",
                        "tool": "echo",
                        "run_id": "run-1",
                        "operation_id": "op-1",
                        "idempotency_key": "idem-echo-dangerous",
                        "args_hash": "sha256:not-this-call",
                    }
                ],
            },
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "approval_binding"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "APPROVAL_BINDING_MISMATCH"


def test_registry_execution_blocks_duplicate_idempotency_key_before_side_effect(tmp_path):
    payload = {
        "tool": "echo",
        "value": 1,
        "mode": "real",
        "operation_id": "op-2",
        "idempotency_key": "idem-echo-mutating",
    }
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload=payload,
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
            write_boundary={
                "tool_effects": {"echo": "mutating"},
                "idempotency_ledger": [
                    {
                        "idempotency_key": "idem-echo-mutating",
                        "args_hash": args_hash_for_call(
                            {
                                "value": 1,
                                "mode": "real",
                                "operation_id": "op-2",
                                "idempotency_key": "idem-echo-mutating",
                            }
                        ),
                        "operation_id": "op-1",
                        "status": "SUCCEEDED",
                    }
                ],
            },
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "idempotency_ledger"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "IDEMPOTENCY_REPLAY_REUSE_PREVIOUS_RESULT"


def test_delivery_closeout_report_contains_runtime_gate_decision(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((Path(tmp_path) / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["runtime_gate"]["status"] == "ALLOW"
    assert report["acceptance_gate"]["status"] == "ALLOW"
    assert report["runtime_gate"]["evidence"]["artifact_count"] == 1


def test_delivery_closeout_blocks_preexisting_artifact_without_tool_provenance(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    params = _delivery_closeout_params(archive_tool_calls=[])
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((Path(tmp_path) / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["runtime_gate"]["status"] == "NEED_REPAIR"
    assert report["runtime_gate"]["findings"][0]["code"] == "ARTIFACT_PROVENANCE_MISSING"


# LLM: Delivery closeout must run data quality gates when a structured quality contract is present.
# 函数用途: 验证文件和 provenance 都通过时，错误数据口径仍会阻止最终收口。
def test_delivery_closeout_blocks_metric_quality_contract_mismatch(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    _write_point_in_time_quality_source(tmp_path)
    params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
    _add_metric_quality_contract(params)
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((Path(tmp_path) / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["runtime_gate"]["status"] == "ALLOW"
    assert report["delivery_quality_gate"]["status"] == "NEED_REPAIR"
    assert report["delivery_quality_gate"]["findings"][0]["code"] == "METRIC_KIND_MISMATCH"
    assert report["delivery_quality_gate"]["recovery"]["status"] == "repair_required"
    recovery = report["contract_recovery"]
    assert recovery["status"] == "repair_required"
    action = recovery["actions"][0]
    assert action["code"] == "METRIC_KIND_MISMATCH"
    assert action["checkpoint_ref"] == "source_data.json"
    assert action["writer_tool"] == "write_file"
    assert action["recommended_action"] == "repair_structured_checkpoint_json"
    assert any(item["gate"] == "delivery_quality" for item in json.loads(params.tool_context[-1].split("\n", 1)[1])["failed_gates"])
    assert report["final_closeout_gate"]["allowed"] is False


# LLM: Artifact validation contracts are quality gates, not optional prompt hints.
# 函数用途: 验证 artifact.validation_contract 里的 staged metric 合同会自动进入 closeout quality gate。
def test_delivery_closeout_derives_quality_gate_from_artifact_validation_contract(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    _write_point_in_time_quality_source(tmp_path)
    params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
    params.delivery_contract["artifacts"][0]["validation_contract"] = _staged_metric_validation_contract()
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((Path(tmp_path) / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["delivery_quality_gate"]["status"] == "NEED_REPAIR"
    assert report["delivery_quality_gate"]["findings"][0]["code"] == "METRIC_KIND_MISMATCH"
    assert report["delivery_quality_gate"]["evidence"]["source_count"] == 1


# LLM: Multi-artifact closeout must not stop at the first quality contract.
# 函数用途: 验证多个 artifact 各自声明质量合同时，后续 artifact 的 metric 合同也会进入质量门。
def test_delivery_closeout_merges_quality_contracts_from_all_artifacts(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    _write_point_in_time_quality_source(tmp_path)
    params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
    params.delivery_contract["artifacts"] = [
        {
            "artifact_id": "out",
            "kind": "txt",
            "path": "out.txt",
            "validation_contract": {
                "evidence_contract": {"require_verified": True, "required_fields": ["safe_field"]},
                "staging_contract": {"source_json_ref": "source_data.json"},
            },
        },
        {
            "artifact_id": "out-2",
            "kind": "txt",
            "path": "out.txt",
            "validation_contract": _staged_metric_validation_contract(),
        },
    ]
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((Path(tmp_path) / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    codes = {item["code"] for item in report["delivery_quality_gate"]["findings"]}
    assert "METRIC_KIND_MISMATCH" in codes


def _staged_metric_validation_contract() -> dict[str, object]:
    return {
        "staging_contract": {"checkpoint_refs": ["source_data.json"], "source_json_ref": "source_data.json"},
        "evidence_contract": {
            "allowed_value_types": ["exact"],
            "require_verified": True,
            "required_fields": ["growth_count"],
        },
        "metric_contracts": [
            {
                "expected_kind": "time_window_delta",
                "field": "growth_count",
                "required_window": True,
            }
        ],
    }


def _write_point_in_time_quality_source(root: Path) -> None:
    (root / "source_data.json").write_text(json.dumps(_point_in_time_quality_payload(), ensure_ascii=False), encoding="utf-8")


def _point_in_time_quality_payload() -> dict[str, object]:
    return {
        "source_refs": [
            {
                "source_id": "src-current",
                "uri": "https://api.example.invalid/repositories",
                "reserved": {"metric_kind": "point_in_time_total"},
            }
        ],
        "claims": [
            {
                "claim_id": "claim-growth",
                "field": "growth_count",
                "value": 4210,
                "source_ids": ["src-current"],
                "verification_status": "VERIFIED",
                "value_type": "exact",
                "reserved": {"metric_kind": "point_in_time_total"},
            }
        ],
    }


def _add_metric_quality_contract(params: ToolLoopExecuteParams) -> None:
    params.delivery_contract["delivery_quality_payload_ref"] = "source_data.json"
    params.delivery_contract["delivery_quality_contract"] = {
        "evidence_contract": {
            "required_fields": ["growth_count"],
            "allowed_value_types": ["exact"],
            "require_verified": True,
        },
        "metric_contracts": [
            {
                "field": "growth_count",
                "expected_kind": "time_window_delta",
                "required_window": True,
            }
        ],
    }


def _delivery_closeout_params(*, archive_tool_calls: list[dict[str, object]]) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="make artifact",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=archive_tool_calls,
        delivery_contract={
            "case_id": "generic-artifact",
            "artifacts": [{"artifact_id": "out", "path": "out.txt", "kind": "txt"}],
        },
    )


def _write_file_archive_record() -> dict[str, object]:
    return {
        "tool": "write_file",
        "run_id": "run-1",
        "task_id": "task-1",
        "ok": True,
        "parameters": {"tool": "write_file", "path": "out.txt"},
        "runtime_gate": {
            "allowed": True,
            "status": "ALLOW",
            "evidence": {
                "tool_name": "write_file",
                "operation_id": "op-write-1",
                "idempotency_key": "idem-write-1",
            },
        },
    }


def _args_hash_for_legacy_payload(payload: dict[str, object]) -> str:
    args = {key: value for key, value in payload.items() if key not in {"tool", "kind"}}
    return args_hash_for_call(normalize_tool_call({**payload, "args": args}).input)
