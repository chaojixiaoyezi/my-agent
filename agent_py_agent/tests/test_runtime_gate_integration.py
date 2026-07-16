from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._finalization_service import FinalizationService
from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext, ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.contracts.gates.tool_effects import args_hash_for_call
from agent_py_agent.agent.contracts.tool_protocol_v2 import normalize_tool_call
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
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
        )
    )

    assert result.ok is True
    assert result.result_envelope["runtime_gate"]["status"] == "ALLOW"
    assert result.result_envelope["runtime_gate"]["evidence"]["tool_name"] == "echo"
    assert "tool_rate_limit" in result.result_envelope["runtime_gate"]["evidence"]["executed_gates"]


def test_finalization_delivery_record_requires_explicit_ok() -> None:
    from agent_py_agent.agent.agent_core._finalization_service import (
        _has_successful_delivery_record,
    )

    missing_ok = _delivery_closeout_params(archive_tool_calls=[{"tool": "write_file", "path": "out.txt"}])
    explicit_ok = _delivery_closeout_params(archive_tool_calls=[{"tool": "write_file", "path": "out.txt", "ok": True}])
    source_read = _delivery_closeout_params(archive_tool_calls=[{"tool": "read_file", "path": "source.txt", "ok": True}])

    assert _has_successful_delivery_record(missing_ok) is False
    assert _has_successful_delivery_record(explicit_ok) is True
    assert _has_successful_delivery_record(source_read) is False


def test_delivery_closeout_rejects_read_file_as_artifact_provenance(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("existing input, not produced", encoding="utf-8")
    params = _delivery_closeout_params(
        archive_tool_calls=[
            {
                "tool": "read_file",
                "run_id": "run-1",
                "task_id": "task-1",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "out.txt"},
                "runtime_gate": {
                    "allowed": True,
                    "status": "ALLOW",
                    "evidence": {
                        "tool_name": "read_file",
                        "operation_id": "op-read-1",
                        "idempotency_key": "idem-read-1",
                    },
                },
            }
        ]
    )
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["artifacts"][0]["provenance"]["code"] == "ARTIFACT_PROVENANCE_MISSING"
    assert report["runtime_gate"]["allowed"] is False


def test_registry_execution_blocks_when_runtime_rate_limit_is_exhausted(tmp_path):
    payload = {"tool": "echo", "value": 1}
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload=payload,
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            write_boundary={
                "now": 10.0,
                "tool_rate_limit_policy": {"max_calls": 1, "window_seconds": 60},
                "tool_rate_limit_records": [
                    {
                        "tool_name": "echo",
                        "args_hash": _args_hash_for_runtime_tool_payload(payload),
                        "attempt_timestamps": [9.0],
                    }
                ],
            },
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "tool_rate_limit"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "TOOL_RATE_LIMIT_EXCEEDED"


def test_registry_execution_zero_rate_limit_policy_is_unlimited(tmp_path):
    payload = {"tool": "echo", "value": 1}
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload=payload,
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            write_boundary={
                "now": 10.0,
                "tool_rate_limit_policy": {"max_calls": 0, "window_seconds": 60, "failure_threshold": 0},
                "tool_rate_limit_records": [
                    {
                        "tool_name": "echo",
                        "args_hash": _args_hash_for_runtime_tool_payload(payload),
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
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "tool_manifest"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "TOOL_MANIFEST_EFFECT_MISSING"


def test_registry_execution_blocks_path_gate_before_tool_execute(tmp_path):
    workspace = tmp_path / "workspace"
    danger = tmp_path / "danger"
    workspace.mkdir()
    danger.mkdir()
    outside = danger / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)

    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "path": "link"},
            tools={"echo": EchoTool()},
            workspace_root=workspace,
            workspace_roots=[workspace],
            path_dangerous_roots=[str(danger)],
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["gate"] == "path_url_command"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_registry_execution_preserves_command_parse_failure_for_model_repair(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "command": "python -c 'print(1)"},
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
        )
    )

    assert result.ok is False
    assert result.error_code == "COMMAND_PARSE_FAILED"
    assert result.reported_error_code == "COMMAND_PARSE_FAILED"
    assert result.retryable is True
    assert result.recommended_action == "repair_tool_call"
    assert result.result_envelope["runtime_gate"]["gate"] == "path_url_command"


def test_registry_execution_blocks_dangerous_real_tool_without_approval(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "value": 1, "mode": "real", "idempotency_key": "idem-echo-dangerous"},
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
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
            write_boundary={
                "run_id": "run-1",
                "tool_effects": {"echo": "dangerous"},
                    "approved_actions": [
                        {
                            "approval_id": "approval-1",
                            "status": "APPROVED",
                            "tool_name": "echo",
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
            write_boundary={
                "tool_effects": {"echo": "mutating"},
                "idempotency_ledger": [
                    {
                        "idempotency_key": "idem-echo-mutating",
                        "args_hash": args_hash_for_call(
                            {
                                "value": 1,
                                "mode": "real",
                            }
                        ),
                        "operation_id": "op-1",
                        "status": "DONE",
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


def test_delivery_closeout_rejects_preexisting_artifact_without_current_run_provenance(tmp_path):
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
    assert report["final_closeout_gate"]["status"] == "NEED_REPAIR"


def test_delivery_closeout_prefers_child_write_over_parent_read_provenance(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    child_write = {
        "tool": "write_file",
        "run_id": "subagent-1",
        "task_id": "run-1",
        "ok": True,
        "created_at": "2026-06-09T00:00:01+00:00",
        "call_id": "child-write-1",
        "parameters": {"tool": "write_file", "path": "out.txt"},
    }
    parent_read = {
        "tool": "read_file",
        "run_id": "run-1",
        "task_id": "run-1",
        "ok": True,
        "created_at": "2026-06-09T00:00:02+00:00",
        "call_id": "parent-read-1",
        "parameters": {"tool": "read_file", "path": "out.txt"},
    }
    params = _delivery_closeout_params(archive_tool_calls=[child_write, parent_read])
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((Path(tmp_path) / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    provenance = report["artifacts"][0]["provenance"]
    assert provenance["tool_name"] == "write_file"
    assert provenance["run_id"] == "subagent-1"
    assert provenance["task_id"] == "run-1"


def test_tool_round_auto_closeout_after_delivery_completion_hint(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
    params.tool_context.append("[delivery-completion-soft-hint]\n{}")
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in response.text


def test_tool_round_auto_closeout_for_uncontracted_task_output_report(tmp_path):
    task_root = tmp_path / "tasks" / "2026-06-03" / "all-agent"
    output_dir = task_root / "output"
    output = output_dir / "all-agent-源码结构分析报告.md"
    output.parent.mkdir(parents=True)
    output.write_text("finished artifact", encoding="utf-8")
    params = replace(
        _delivery_closeout_params(
            archive_tool_calls=[
                {
                    **_write_file_archive_record(),
                    "parameters": {"tool": "write_file", "path": str(output)},
                    "path": str(output),
                }
            ]
        ),
        delivery_contract=None,
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(output_dir),
                "work_dir": str(task_root / "work"),
            }
        },
    )
    params.tool_context.append("[delivery-completion-soft-hint]\n{}")
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in response.text
    assert "uncontracted_task_output" in response.text
    registry = task_root / "data" / "artifacts" / "registry.jsonl"
    rows = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[-1]["path"] == str(output)
    assert rows[-1]["status"] == "ready"


def test_finalization_auto_closeout_for_final_response_after_uncontracted_task_output(tmp_path):
    task_root = tmp_path / "tasks" / "2026-06-06" / "all-agent-run-1"
    output_dir = task_root / "output"
    output = output_dir / "all-agent-源码分析报告.md"
    output.parent.mkdir(parents=True)
    output.write_text("finished artifact", encoding="utf-8")
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)

    result = FinalizationService(agent).finalize(_finalize_context_with_task_output(task_root, output_dir, output))

    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
    assert "uncontracted_task_output" in result.response
    assert result.memory_compact_auto_status == "skipped_after_delivery_complete"


def test_finalization_replaces_done_claim_when_uncontracted_closeout_fails(tmp_path):
    task_root = tmp_path / "tasks" / "2026-06-08" / "all-agent-run-1"
    output_dir = task_root / "output"
    output = output_dir / "all-agent-源码分析报告.md"
    output.parent.mkdir(parents=True)
    output.write_text("partial artifact", encoding="utf-8")
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    ctx = _finalize_context_with_task_output(task_root, output_dir, output)
    partial_record = {
        **ctx.archive_tool_calls[0],
        "parameters": {
            **ctx.archive_tool_calls[0]["parameters"],
            "__partial_unclosed_write": True,
        },
    }
    ctx = replace(
        ctx,
        final_response=ModelResponse(text="任务已完成并提交验收。", backend="test"),
        archive_tool_calls=[partial_record],
    )

    result = FinalizationService(agent).finalize(ctx)

    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
    assert "[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]" in result.response
    assert "ARTIFACT_LAST_WRITE_PARTIAL_UNCLOSED" in result.response
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is False


def test_finalization_rechecks_stale_completion_marker_against_open_task_ledger(tmp_path):
    task_root = tmp_path / "tasks" / "2026-07-15" / "人类可读任务名"
    output_dir = task_root / "output"
    output = output_dir / "partial.md"
    output.parent.mkdir(parents=True)
    output.write_text("partial artifact", encoding="utf-8")
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    progress_root = Path(agent.home_paths.owner_home_dir)
    from agent_py_agent.agent.task_progress import write_task_progress

    write_task_progress(
        progress_root,
        "task-1",
        {
            "items": [
                {
                    "id": "integrate",
                    "title": "整合子任务并生成最终报告",
                    "status": "in_progress",
                }
            ]
        },
    )
    (task_root / ".agent_delivery").mkdir()
    (task_root / ".agent_delivery" / "closeout.json").write_text(
        json.dumps(
            {
                "ok": True,
                "request_id": "older-request",
                "run_id": "older-run",
                "task_id": "task-1",
            }
        ),
        encoding="utf-8",
    )
    ctx = replace(
        _finalize_context_with_task_output(task_root, output_dir, output),
        final_response=ModelResponse(
            text="[MAIN_AGENT_DELIVERY_COMPLETE]\n{}\n[/MAIN_AGENT_DELIVERY_COMPLETE]",
            backend="test",
        ),
        request_id="current-request",
        run_id="bg-main-thread-current",
        task_id="task-1",
        source="background_main_agent",
    )

    result = FinalizationService(agent).finalize(ctx)

    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
    assert "[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]" in result.response
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    gate = report["task_progress_closeout_gate"]
    assert gate["evidence"]["open_count"] == 1


def test_finalization_waits_for_required_coverage_and_artifact_before_auto_closeout(tmp_path):
    task_root = tmp_path / "tasks" / "2026-06-07" / "long-read"
    output_dir = task_root / "output"
    output_dir.mkdir(parents=True)
    source = tmp_path / "data" / "source.txt"
    source.parent.mkdir()
    source.write_text("abcdef", encoding="utf-8")
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    ctx = replace(
        _finalize_context_with_task_output(task_root, output_dir, output_dir / "report.md"),
        final_response=ModelResponse(text="继续读取中", backend="test"),
        executed_tools=["read_file"],
        archive_tool_calls=[
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "run-1",
                "task_id": "task-1",
                "parameters": {"path": str(source)},
                "read_window": {
                    "kind": "char_window",
                    "offset": 0,
                    "next_offset": 3,
                    "total_chars": 6,
                },
            }
        ],
        delivery_contract={
            "artifacts": [
                {
                    "artifact_id": "report",
                    "required": True,
                    "preferred_path": str(output_dir / "report.md"),
                    "kind": "md",
                }
            ],
            "target_coverage_contract": {
                "enforcement": "required",
                "coverage_requirement": "full_source_read",
                "target_items": [
                    {
                        "target_id": str(source),
                        "source_path": str(source),
                        "coverage_kind": "full_source_read",
                        "enforcement": "required",
                    }
                ],
            },
        },
    )

    result = FinalizationService(agent).finalize(ctx)

    assert result.response == "继续读取中"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_tool_round_auto_closeout_for_relative_uncontracted_task_output_report(tmp_path):
    task_root = tmp_path / "tasks" / "2026-06-03" / "all-agent"
    output_dir = task_root / "output"
    output = output_dir / "architecture_analysis_report.md"
    output.parent.mkdir(parents=True)
    output.write_text("finished artifact", encoding="utf-8")
    relative_output = output.relative_to(tmp_path)
    params = replace(
        _delivery_closeout_params(
            archive_tool_calls=[
                {
                    **_write_file_archive_record(),
                    "parameters": {"tool": "write_file", "path": str(relative_output)},
                    "path": str(relative_output),
                }
            ]
        ),
        delivery_contract=None,
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(output_dir),
                "work_dir": str(task_root / "work"),
            }
        },
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in response.text
    assert "uncontracted_task_output" in response.text


def test_tool_round_auto_closeout_resolves_output_alias_archive_record(tmp_path):
    task_root = tmp_path / "tasks" / "2026-06-06" / "task"
    output_dir = task_root / "output"
    output = output_dir / "report.md"
    output.parent.mkdir(parents=True)
    output.write_text("finished artifact", encoding="utf-8")
    params = replace(
        _delivery_closeout_params(
            archive_tool_calls=[
                {
                    **_write_file_archive_record(),
                    "parameters": {"tool": "write_file", "path": "output/report.md"},
                    "path": "output/report.md",
                }
            ]
        ),
        delivery_contract=None,
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(output_dir),
                "work_dir": str(task_root / "work"),
            }
        },
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in response.text
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["artifacts"][0]["path"] == str(output)
    registry = task_root / "data" / "artifacts" / "registry.jsonl"
    rows = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[-1]["path"] == str(output)


def test_tool_round_auto_closeout_for_uncontracted_user_requested_output_report(tmp_path):
    task_root = tmp_path / "tasks" / "2026-06-03" / "all-agent"
    output_dir = task_root / "output"
    requested_dir = tmp_path / "requested"
    output = requested_dir / "all-agent-最终验收报告.md"
    output.parent.mkdir(parents=True)
    output.write_text("finished artifact", encoding="utf-8")
    params = replace(
        _delivery_closeout_params(
            archive_tool_calls=[
                {
                    **_write_file_archive_record(),
                    "parameters": {"tool": "write_file", "path": str(output)},
                    "path": str(output),
                }
            ]
        ),
        user_prompt=f"请把最终验收报告写到 {output}，写完提交验收。",
        delivery_contract=None,
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(output_dir),
                "work_dir": str(task_root / "work"),
            }
        },
    )
    params.tool_context.append("[delivery-completion-soft-hint]\n{}")
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in response.text
    assert "uncontracted_user_requested_output" in response.text


def test_delivery_closeout_reports_metric_quality_contract_mismatch_as_warning(tmp_path):
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

    assert response is not None
    assert report["runtime_gate"]["status"] == "ALLOW"
    assert report["delivery_quality_gate"]["status"] == "ALLOW"
    assert "METRIC_KIND_MISMATCH" in report["delivery_quality_gate"]["evidence"]["warning_codes"]
    assert report["final_closeout_gate"]["allowed"] is True


def test_delivery_closeout_keeps_metric_quality_mismatch_advisory_even_when_enforcement_required(tmp_path):
    # 交付判定四档统一(本次重构):delivery_quality 是质量门 → L3 advisory。即便
    # enforcement=required,质量不达标也只把事实写进报告/quality_advisories,不再硬卡
    # 退出——唯一能 BLOCK 的是 L1 客观事实(产物打不开/子代理未收口/防编造/账本损坏)。
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    _write_point_in_time_quality_source(tmp_path)
    params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
    _add_metric_quality_contract(params)
    params.delivery_contract["delivery_quality_contract"]["enforcement"] = "required"
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((Path(tmp_path) / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["ok"] is True
    # 门本身仍判 NEED_REPAIR(消费层降级,不改门返回值),事实保留供把关。
    assert report["delivery_quality_gate"]["status"] == "NEED_REPAIR"
    assert report["delivery_quality_gate"]["findings"][0]["code"] == "METRIC_KIND_MISMATCH"
    assert any(
        item.get("gate") == "delivery_quality" for item in report.get("quality_advisories", [])
    )


def test_delivery_quality_legacy_enforcement_aliases_stay_advisory(tmp_path):
    for enforcement in ("hard", "block", "blocking"):
        case_root = tmp_path / enforcement
        case_root.mkdir()
        (case_root / "out.txt").write_text("finished artifact", encoding="utf-8")
        _write_point_in_time_quality_source(case_root)
        params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
        _add_metric_quality_contract(params)
        params.delivery_contract["delivery_quality_contract"]["enforcement"] = enforcement
        agent = SimpleNamespace(root=case_root, tools=SimpleNamespace(workspace_root=case_root))

        response = main_agent_delivery_closeout_response(
            MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
        )
        report = json.loads((case_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

        assert response is not None
        assert report["delivery_quality_gate"]["status"] == "ALLOW"
        assert "METRIC_KIND_MISMATCH" in report["delivery_quality_gate"]["evidence"]["warning_codes"]
        assert report["final_closeout_gate"]["allowed"] is True


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

    assert response is not None
    assert report["delivery_quality_gate"]["status"] == "ALLOW"
    assert "METRIC_KIND_MISMATCH" in report["delivery_quality_gate"]["evidence"]["warning_codes"]
    assert report["final_closeout_gate"]["allowed"] is True


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

    assert response is not None
    codes = set(report["delivery_quality_gate"]["evidence"]["warning_codes"])
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
                "metric_kind": "point_in_time_total",
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
                "metric_kind": "point_in_time_total",
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


def _finalize_context_with_task_output(task_root: Path, output_dir: Path, output: Path) -> FinalizeContext:
    archive_record = {**_write_file_archive_record(), "parameters": {"tool": "write_file", "path": str(output)}, "path": str(output)}
    return FinalizeContext(
        user_prompt="分析 all-agent 项目并写报告",
        final_prompt="",
        final_response=ModelResponse(text=f"已完成，报告在 {output}", backend="test"),
        memories=[],
        executed_tools=["write_file"],
        archive_tool_calls=[archive_record],
        routed_context=SimpleNamespace(matches=[], required_read_paths=[], candidate_paths=[]),
        resume_context_result=None,
        runtime_injections=[],
        compression_snapshot_id="",
        compression_snapshot_path="",
        compression_applied=False,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        source="test",
        do_save=False,
        task_attributes={"run_workspace": {"task_root": str(task_root), "output_dir": str(output_dir), "work_dir": str(task_root / "work")}},
        recovery_task_refs=None,
        recovery_content_paths=None,
        recovery_next_actions=None,
        tool_rounds=3,
        delivery_contract=None,
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


def _args_hash_for_runtime_tool_payload(payload: dict[str, object]) -> str:
    protocol_keys = {
        "artifact_refs",
        "call_id",
        "idempotency_key",
        "kind",
        "metadata",
        "operation_id",
        "run_id",
        "schema_version",
        "tool",
    }
    args = {key: value for key, value in payload.items() if key not in protocol_keys}
    return args_hash_for_call(normalize_tool_call({"tool_name": str(payload.get("tool") or ""), "input": args}).input)
