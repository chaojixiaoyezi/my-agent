# LLM: Main-agent foundation contract cases cover hard runtime contracts used by real E2E.
# 模块用途: 提供模型调用账本、工具协议 v2 和大文件分块写入的确定性验收用例。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..agent_core.model_call_monitor import (
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
)
from ..contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from ..tooling.file_write_session import FileWriteSessionTool
from .main_agent_foundation_models import MainAgentFoundationCaseResult
from .tool_protocol_v2 import (
    normalize_tool_call,
    normalize_tool_result,
    validate_tool_call,
    validate_tool_result,
)


# LLM: FileWriteSessionCaseArtifacts bundles tool results from one chunked write scenario.
# 类用途: 保存大文件分块写入用例的 begin/append/finish 结果和目标路径。
@dataclass(frozen=True)
class FileWriteSessionCaseArtifacts:
    begin: object
    append_0: object
    duplicate: object
    append_1: object
    finish: object
    target: Path


# LLM: case_model_call_ledger_timeout proves provider timing facts are structured.
# 函数用途: 写入模型调用 started/timeout 账本，并验证动态首 token 预算会落到证据文件。
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


# LLM: case_tool_protocol_v2_envelope proves tool facts use machine envelopes.
# 函数用途: 规范化工具调用和失败结果，验证 operation/idempotency/error/artifact 字段齐全。
def case_tool_protocol_v2_envelope(workspace: Path) -> MainAgentFoundationCaseResult:
    call = normalize_tool_call(
        {
            "tool": "read_file",
            "input": {"path": "missing.txt"},
            "artifact_refs": [{"artifact_id": "input-ref", "path": "missing.txt"}],
        }
    )
    result = normalize_tool_result(
        {
            "tool": "read_file",
            "ok": False,
            "operation_ref": call.operation_ref().to_dict(),
            "error": {"error_type": "PATH_INVALID", "message": "missing.txt"},
            "artifact_refs": [{"artifact_id": "error-log", "path": "logs/error.json"}],
        }
    )
    findings = validate_tool_call(call) + validate_tool_result(result)
    evidence = workspace / "tool_protocol_v2_envelope" / "envelope.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {"call": call.to_dict(), "result": result.to_dict(), "findings": findings},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    issues = list(findings)
    if result.error is None or result.error.error_type != "PATH_INVALID":
        issues.append("tool result error taxonomy missing")
    return MainAgentFoundationCaseResult(
        case_id="tool_protocol_v2_envelope",
        title="工具协议 v2 envelope 测试",
        status="FAILED" if issues else "PASSED",
        summary="工具调用/结果包含 operation、幂等键、错误分类和 artifact refs，不读自然语言输出当事实。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


# LLM: case_file_write_session_contract checks chunked writes without large inline tool bodies.
# 函数用途: 通过 begin/append/finish 写文件，验证重复 chunk 幂等和最终原子提交。
def case_file_write_session_contract(workspace: Path) -> MainAgentFoundationCaseResult:
    case_dir = workspace / "file_write_session_contract"
    tool = FileWriteSessionTool(case_dir, max_chunk_chars=16)
    begin = tool.execute({"action": "begin", "target_path": "out/report.txt"})
    session_id = str(begin.result_envelope.get("session_id", ""))
    append_0 = tool.execute(
        {"action": "append", "session_id": session_id, "chunk_index": 0, "content": "hello "}
    )
    duplicate = tool.execute(
        {"action": "append", "session_id": session_id, "chunk_index": 0, "content": "hello "}
    )
    append_1 = tool.execute(
        {"action": "append", "session_id": session_id, "chunk_index": 1, "content": "world"}
    )
    finish = tool.execute({"action": "finish", "session_id": session_id})
    target = case_dir / "out" / "report.txt"
    artifacts = FileWriteSessionCaseArtifacts(begin, append_0, duplicate, append_1, finish, target)
    evidence = _write_file_session_evidence(case_dir, artifacts)
    issues = _file_session_issues(artifacts)
    return MainAgentFoundationCaseResult(
        case_id="file_write_session_contract",
        title="大文件分块写入合同测试",
        status="FAILED" if issues else "PASSED",
        summary="file_write_session 能分块写入、重复 chunk 幂等，并在 finish 时原子提交目标文件。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


# LLM: _started_params keeps the ledger test case body focused on behavior.
# 函数用途: 返回 foundation 超时用例的模型调用 started 参数。
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


# LLM: _timeout_params keeps timeout facts structured and reusable in the case.
# 函数用途: 返回 provider_wall 超时事件参数。
def _timeout_params() -> ModelCallTimeoutParams:
    return ModelCallTimeoutParams(
        call_id="foundation-timeout",
        timeout_seconds=240,
        timeout_stage="provider_wall",
    )


# LLM: _ledger_issues checks model-call ledger case outcomes without reading prose.
# 函数用途: 根据结构化账本状态判断模型调用超时用例是否通过。
def _ledger_issues(ledger: ModelCallLedger) -> list[str]:
    records = ledger.records()
    if records and records[0].status == "timed_out":
        return []
    return ["model call ledger did not record timeout"]


# LLM: _write_file_session_evidence persists only envelopes and refs, not hidden prompt text.
# 函数用途: 写入 file_write_session 确定性用例的证据 JSON。
def _write_file_session_evidence(case_dir: Path, artifacts: FileWriteSessionCaseArtifacts) -> Path:
    evidence = case_dir / "evidence.json"
    payload = {
        "begin": artifacts.begin.result_envelope,
        "append_0": artifacts.append_0.result_envelope,
        "duplicate": artifacts.duplicate.result_envelope,
        "append_1": artifacts.append_1.result_envelope,
        "finish": artifacts.finish.result_envelope,
        "target_ref": str(artifacts.target),
        "target_size": artifacts.target.stat().st_size if artifacts.target.exists() else 0,
        "target_sha256": hashlib.sha256(artifacts.target.read_bytes()).hexdigest()
        if artifacts.target.exists()
        else "",
    }
    evidence.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return evidence


# LLM: _file_session_issues checks chunk session behavior through tool result envelopes.
# 函数用途: 判断 begin/append/finish、重复 chunk 幂等和最终文件内容是否符合合同。
def _file_session_issues(artifacts: FileWriteSessionCaseArtifacts) -> list[str]:
    issues: list[str] = []
    results = (
        artifacts.begin,
        artifacts.append_0,
        artifacts.duplicate,
        artifacts.append_1,
        artifacts.finish,
    )
    if not all(item.ok for item in results):
        issues.append("file_write_session action failed")
    if not artifacts.duplicate.result_envelope.get("duplicate"):
        issues.append("duplicate chunk was not idempotent")
    if (
        not artifacts.target.exists()
        or artifacts.target.read_text(encoding="utf-8") != "hello world"
    ):
        issues.append("final target content mismatch")
    return issues


__all__ = [
    "case_file_write_session_contract",
    "case_model_call_ledger_timeout",
    "case_tool_protocol_v2_envelope",
]
