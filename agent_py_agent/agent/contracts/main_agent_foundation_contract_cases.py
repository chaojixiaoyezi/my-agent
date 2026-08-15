from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..agent_core.model.call_monitor import (
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
)
from ..contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from ..local_storage import LocalStore
from ..tooling._filesystem_patch import ApplyPatchTool
from ..tooling._filesystem_write import WriteFileTool
from ..tooling.executor import ToolExecutor, ToolExecutorRequest
from ..tooling.models import ToolRuntime, ToolRuntimeSnapshot, tool_schema_hash
from ..tooling.runtime_contracts import (
    ToolCall,
    ToolFailureFacts,
    ToolResult,
    ToolResultRef,
)
from .main_agent_foundation_models import MainAgentFoundationCaseResult


def case_model_call_ledger_timeout(workspace: Path) -> MainAgentFoundationCaseResult:
    ledger = ModelCallLedger()
    ledger.started(_started_params())
    ledger.timeout(_timeout_params())
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=12000,
            ledger=ledger,
            options=FirstTokenTimeoutOptions(max_timeout_seconds=300),
        )
    )
    evidence = workspace / "model_call_ledger_timeout" / "ledger.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "records": [item.to_dict() for item in ledger.records()],
        "first_token_estimate": estimate.to_dict(),
    }
    evidence.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    issues = _ledger_issues(ledger)
    return MainAgentFoundationCaseResult(
        case_id="model_call_ledger_timeout",
        title="模型调用账本/超时合同测试",
        status="FAILED" if issues else "PASSED",
        summary="模型请求 started/timeout 和首 token 动态预算都以结构化账本记录。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def case_canonical_tool_contract(workspace: Path) -> MainAgentFoundationCaseResult:
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    call = ToolCall(
        call_id="foundation-read-missing",
        tool_name="read_file",
        arguments={"path": "missing.txt"},
        source_protocol="native",
        schema_hash=tool_schema_hash(schema),
        run_id="foundation-run",
        turn_id="foundation-turn",
        attempt_id="foundation-attempt",
    )
    result = ToolResult.failed(
        call,
        "missing.txt",
        error_code="PATH_INVALID",
        failure_stage="execution",
        facts=ToolFailureFacts(
            handler_executed=True,
            refs=(ToolResultRef(kind="artifact", ref="logs/error.json"),),
        ),
    )
    evidence = workspace / "canonical_tool_contract" / "contract.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {"call": call.to_dict(), "result": result.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    issues: list[str] = []
    if result.error_code != "PATH_INVALID":
        issues.append("canonical tool result error taxonomy missing")
    if result.call_id != call.call_id or result.tool_name != call.tool_name:
        issues.append("canonical call/result pairing mismatch")
    if not result.refs or result.refs[0].ref != "logs/error.json":
        issues.append("canonical tool result ref missing")
    return MainAgentFoundationCaseResult(
        case_id="canonical_tool_contract",
        title="Canonical ToolCall/ToolResult 合同测试",
        status="FAILED" if issues else "PASSED",
        summary="工具调用/结果包含稳定身份、幂等键、错误分类和 refs，不读自然语言输出当事实。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def case_general_write_contract(workspace: Path) -> MainAgentFoundationCaseResult:
    case_dir = workspace / "general_write_contract"
    write_tool = WriteFileTool(case_dir)
    patch_tool = ApplyPatchTool(case_dir)
    tools = (write_tool, patch_tool)
    snapshot = ToolRuntimeSnapshot(
        run_id="foundation-write-run",
        runtimes=tuple(
            ToolRuntime(tool.model_spec, tool.runtime_policy, tool)
            for tool in tools
        ),
        available_tool_names=frozenset(tool.model_spec.name for tool in tools),
        unavailable_tools=(),
        allowed_tools=None,
    )
    operation_store = LocalStore(case_dir / "operations.db", enable_fts=False)
    text_result = _execute_foundation_tool(
        case_dir,
        snapshot,
        operation_store,
        "write_file",
        {"path": "out/report.txt", "content": "hello world\n"},
        index=1,
    )
    binary_result = _execute_foundation_tool(
        case_dir,
        snapshot,
        operation_store,
        "write_file",
        {"path": "out/blob.bin", "data_base64": "AAEC"},
        index=2,
    )
    patch_result = _execute_foundation_tool(
        case_dir,
        snapshot,
        operation_store,
        "apply_patch",
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: out/report.txt\n"
                "-hello world\n"
                "+hello patched world\n"
                "*** End Patch\n"
            ),
        },
        index=3,
    )
    evidence = _write_general_write_evidence(case_dir, text_result, binary_result, patch_result)
    issues = _general_write_issues(case_dir, text_result, binary_result, patch_result)
    return MainAgentFoundationCaseResult(
        case_id="general_write_contract",
        title="通用文件写入合同测试",
        status="FAILED" if issues else "PASSED",
        summary="write_file 可写文本/二进制完整文件，apply_patch 可做局部文本修改。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def _execute_foundation_tool(
    case_dir: Path,
    snapshot: ToolRuntimeSnapshot,
    operation_store: LocalStore,
    tool_name: str,
    arguments: dict[str, object],
    *,
    index: int,
) -> ToolResult:
    runtime = snapshot.runtime(tool_name)
    if runtime is None:
        raise RuntimeError(f"foundation runtime missing tool: {tool_name}")
    call = ToolCall(
        call_id=f"foundation-write-{index}",
        tool_name=tool_name,
        arguments=arguments,
        source_protocol="native",
        schema_hash=runtime.model_spec.schema_hash,
        run_id=snapshot.run_id,
        turn_id="foundation-write-turn",
        attempt_id="foundation-write-attempt",
    )
    return ToolExecutor().execute(
        ToolExecutorRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=case_dir,
            workspace_roots=(case_dir,),
            operation_store=operation_store,
            operation_store_required=True,
            operation_owner_id="foundation-contract",
        )
    ).result


def _started_params() -> ModelCallStartedParams:
    return ModelCallStartedParams(
        call_id="foundation-timeout",
        backend="test-backend",
        model="test-model",
        input_tokens=12000,
        output_tokens_estimate=800,
        request_id="foundation-request",
        run_id="foundation-run",
    )


def _timeout_params() -> ModelCallTimeoutParams:
    return ModelCallTimeoutParams(
        call_id="foundation-timeout",
        timeout_seconds=240,
        timeout_stage="provider_wall",
    )


def _ledger_issues(ledger: ModelCallLedger) -> list[str]:
    records = ledger.records()
    if records and records[0].status == "timed_out":
        return []
    return ["model call ledger did not record timeout"]


def _write_general_write_evidence(
    case_dir: Path, text_result: object, binary_result: object, patch_result: object
) -> Path:
    evidence = case_dir / "evidence.json"
    text_target = case_dir / "out" / "report.txt"
    binary_target = case_dir / "out" / "blob.bin"
    payload = {
        "write_text_ok": bool(getattr(text_result, "ok", False)),
        "write_binary_ok": bool(getattr(binary_result, "ok", False)),
        "patch_ok": bool(getattr(patch_result, "ok", False)),
        "text_target_ref": str(text_target),
        "binary_target_ref": str(binary_target),
        "text_sha256": hashlib.sha256(text_target.read_bytes()).hexdigest()
        if text_target.exists()
        else "",
        "binary_sha256": hashlib.sha256(binary_target.read_bytes()).hexdigest()
        if binary_target.exists()
        else "",
    }
    evidence.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return evidence


def _general_write_issues(
    case_dir: Path, text_result: object, binary_result: object, patch_result: object
) -> list[str]:
    issues: list[str] = []
    if not all(
        bool(getattr(item, "ok", False)) for item in (text_result, binary_result, patch_result)
    ):
        issues.append("generic write action failed")
    text_target = case_dir / "out" / "report.txt"
    binary_target = case_dir / "out" / "blob.bin"
    if (
        not text_target.exists()
        or text_target.read_text(encoding="utf-8") != "hello patched world\n"
    ):
        issues.append("text target content mismatch")
    if not binary_target.exists() or binary_target.read_bytes() != b"\x00\x01\x02":
        issues.append("binary target content mismatch")
    return issues


__all__ = [
    "case_general_write_contract",
    "case_model_call_ledger_timeout",
    "case_canonical_tool_contract",
]
