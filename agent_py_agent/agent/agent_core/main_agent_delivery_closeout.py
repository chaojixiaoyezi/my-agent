# LLM: Main-agent delivery closeout stops productive real tasks once machine contracts pass.
# 模块用途: 工具轮后读取机器交付合同、验收产物并写收口报告，避免主代理产物已合格还继续跑到超时。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..backends import ModelResponse
from ..contracts.gates import (
    evaluate_acceptance_closeout_gate,
    evaluate_delivery_closeout_gate,
    evaluate_final_closeout_gate,
    evaluate_run_contract_gate,
    evaluate_state_transition_gate,
)
from ..tooling.file_write_session_inspection import open_file_write_sessions
from ._runtime_params import ToolLoopExecuteParams
from .main_agent_delivery_closeout_artifacts import (
    DeliveryContractValidationRequest,
    _existing_report,
    _relative_report_ref,
    _required_artifacts,
    _validate_contract_artifacts,
    _write_report,
)
from .main_agent_delivery_closeout_progress import (
    _enrich_delivery_progress,
    _should_block_on_no_progress,
)
from .main_agent_delivery_closeout_quality import delivery_quality_decision
from .tool_local_progress_guard import reset_local_progress_guard


# LLM: MainAgentDeliveryCloseoutRequest bundles post-tool-loop state for contract validation.
# 类用途: 保存主代理、工具循环参数和后端名，供交付收口逻辑在不扩散参数的情况下运行。
@dataclass(frozen=True)
class MainAgentDeliveryCloseoutRequest:
    agent: object
    params: ToolLoopExecuteParams
    backend: str


# LLM: CloseoutGateRequest bundles gate attachment inputs.
# 类用途: 避免 closeout gate helper 参数膨胀，把 report/contract/workspace 放进一个结构化请求。
@dataclass(frozen=True)
class CloseoutGateRequest:
    closeout: MainAgentDeliveryCloseoutRequest
    report: dict[str, Any]
    contract: dict[str, Any]
    workspace_root: Path


# LLM: main_agent_delivery_closeout_response returns a deterministic final response only after all required refs pass.
# 函数用途: 根据结构化 delivery_contract 验收必交产物；通过则停止工具循环，失败则写结构化反馈让模型修复。
def main_agent_delivery_closeout_response(request: MainAgentDeliveryCloseoutRequest) -> ModelResponse | None:
    contract = _delivery_contract(request.params)
    artifacts = _required_artifacts(contract)
    if not artifacts:
        return None
    workspace_root = _workspace_root(request.agent)
    if open_sessions := open_file_write_sessions(
        workspace_root,
        scope=_runtime_scope(request.params),
    ):
        _append_open_session_context(request.params, open_sessions)
        return None
    report = _delivery_report(request, contract, artifacts, workspace_root)
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    decisions = _attach_closeout_gates(CloseoutGateRequest(request, report, contract, workspace_root))
    _write_report(workspace_root, report)
    if not _all_gates_allowed(decisions):
        return _failed_delivery_response(request, report, contract, workspace_root)
    reset_local_progress_guard(request.agent, request.params)
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


# LLM: _attach_closeout_gates writes every mandatory gate payload into the closeout report.
# 函数用途: 统一添加 run/runtime/state/acceptance/final gate，主流程只看 gate 决策，不读自然语言。
def _attach_closeout_gates(request: CloseoutGateRequest) -> list[Any]:
    closeout = request.closeout
    run_contract_decision = evaluate_run_contract_gate(
        request.contract,
        scope={
            "request_id": closeout.params.request_id,
            "run_id": closeout.params.run_id,
            "task_id": closeout.params.task_id,
            "workspace_root": str(request.workspace_root),
        },
    )
    request.report["run_contract_gate"] = run_contract_decision.to_dict()
    gate_decision = evaluate_delivery_closeout_gate(request.report)
    request.report["runtime_gate"] = gate_decision.to_dict()
    state_decision = evaluate_state_transition_gate("RUNNING", "VERIFYING")
    request.report["state_gate"] = state_decision.to_dict()
    contract_hash = str(run_contract_decision.evidence.get("effective_contract_hash") or "")
    quality_decision = delivery_quality_decision(
        contract=request.contract,
        report=request.report,
        workspace_root=request.workspace_root,
        contract_hash=contract_hash,
    )
    request.report["delivery_quality_gate"] = quality_decision.to_dict()
    acceptance_decision = evaluate_acceptance_closeout_gate(
        {
            "final_status": "DONE",
            "verification_status": "PASSED" if gate_decision.allowed else "FAILED",
            "runtime_gate": gate_decision.to_dict(),
        }
    )
    request.report["acceptance_gate"] = acceptance_decision.to_dict()
    final_decision = evaluate_final_closeout_gate(request.report)
    request.report["final_closeout_gate"] = final_decision.to_dict()
    return [run_contract_decision, gate_decision, state_decision, quality_decision, acceptance_decision, final_decision]


# LLM: _all_gates_allowed keeps final closeout branching tied to gate decisions.
# 函数用途: 汇总 GateDecision.allowed 字段，不读取 findings/message 文本。
def _all_gates_allowed(decisions: list[Any]) -> bool:
    return all(bool(getattr(decision, "allowed", False)) for decision in decisions)


# LLM: _delivery_report validates artifacts and attaches progress state from the previous closeout report.
# 函数用途: 生成本轮 closeout 报告，并用上轮报告计算重复失败和工作区进展状态。
def _delivery_report(
    closeout: MainAgentDeliveryCloseoutRequest,
    contract: dict[str, Any],
    artifacts: list[dict[str, Any]],
    workspace_root: Path,
) -> dict[str, Any]:
    report = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=artifacts,
            workspace_root=workspace_root,
            params=closeout.params,
        )
    )
    return _enrich_delivery_progress(report, _existing_report(workspace_root), workspace_root, contract=contract)


# LLM: _failed_delivery_response decides whether to repair in-place or terminate no-progress loops.
# 函数用途: 将失败报告写回 tool_context；若同一失败已无进展则返回结构化阻塞响应。
def _failed_delivery_response(
    closeout: MainAgentDeliveryCloseoutRequest,
    report: dict[str, Any],
    contract: dict[str, Any],
    workspace_root: Path,
) -> ModelResponse | None:
    _append_failed_contract_context(closeout.params, report)
    if _should_block_on_no_progress(report, contract=contract, workspace_root=workspace_root):
        return ModelResponse(text=_blocked_closeout_text(report), backend=closeout.backend)
    return None


# LLM: _delivery_contract reads machine contracts from runtime fields, never from prompt prose.
# 函数用途: 优先读取 ToolLoopExecuteParams.delivery_contract；兼容读取 task_attributes.delivery_contract。
def _delivery_contract(params: ToolLoopExecuteParams) -> dict[str, Any]:
    value = params.delivery_contract
    if isinstance(value, dict):
        return dict(value)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    value = attrs.get("delivery_contract")
    return dict(value) if isinstance(value, dict) else {}


# LLM: _append_failed_contract_context feeds structured repair facts into the next model turn.
# 函数用途: 验收失败时追加 JSON 反馈，不把自然语言说明当机器事实。
def _append_failed_contract_context(params: ToolLoopExecuteParams, report: dict[str, Any]) -> None:
    params.tool_context.append(
        "[delivery-contract-check]\n"
        + json.dumps(
            {
                "ok": False,
                "report_ref": report.get("report_ref", ""),
                "failed_artifacts": [item for item in report["artifacts"] if not item["ok"]],
                "delivery_progress": report.get("delivery_progress", {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


# LLM: _append_open_session_context blocks delivery completion from open file-write manifests.
# 函数用途: 有未 finish/abort 的分块写入时，只追加结构化 session 事实，让下一轮先处理这些会话。
def _append_open_session_context(params: ToolLoopExecuteParams, sessions: list[dict[str, Any]]) -> None:
    params.tool_context.append(
        "[delivery-contract-open-file-write-sessions]\n"
        + json.dumps(
            {
                "ok": False,
                "reason": "open_file_write_sessions",
                "open_file_write_sessions": sessions,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


# LLM: _closeout_text makes the final message copyable while keeping the machine payload explicit.
# 函数用途: 生成主代理完成响应，告知上层工具循环不用再请求下一轮模型。
def _closeout_text(report: dict[str, Any]) -> str:
    payload = {
        "ok": True,
        "case_id": report.get("case_id", ""),
        "report_ref": report.get("report_ref", ""),
        "artifacts": [_closeout_artifact_payload(item) for item in report["artifacts"]],
    }
    return (
        "[MAIN_AGENT_DELIVERY_COMPLETE]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/MAIN_AGENT_DELIVERY_COMPLETE]\n结构化交付合同已通过，主代理停止继续工具循环。"
    )


# LLM: _closeout_artifact_payload keeps final closeout output refs-only and compact.
# 函数用途: 从验收报告中挑出 artifact_id/kind/path/ok，不把 acceptance 详情塞进最终回复。
def _closeout_artifact_payload(item: dict[str, Any]) -> dict[str, object]:
    return {
        "artifact_id": item["artifact_id"],
        "kind": item["kind"],
        "path": item["path"],
        "ok": item["ok"],
    }


# LLM: _blocked_closeout_text terminates deterministic no-progress loops without inventing task-specific contracts.
# 函数用途: 同一交付失败重复出现且工作区没有推进时，输出结构化阻塞结果。
def _blocked_closeout_text(report: dict[str, Any]) -> str:
    payload = {
        "ok": False,
        "reason": "delivery_contract_no_progress",
        "case_id": report.get("case_id", ""),
        "report_ref": report.get("report_ref", ""),
        "delivery_progress": report.get("delivery_progress", {}),
        "failed_artifacts": [_failed_artifact_payload(item) for item in report.get("artifacts", []) if not item.get("ok")],
    }
    return (
        "[MAIN_AGENT_DELIVERY_BLOCKED]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/MAIN_AGENT_DELIVERY_BLOCKED]\n结构化交付失败在同一状态下重复出现且没有新的工作进展，主代理停止继续工具循环。"
    )


# LLM: _failed_artifact_payload keeps blocked responses machine-readable without embedding artifact bodies.
# 函数用途: 从失败产物里提取 artifact_id/kind/path/findings，供恢复链路定位问题。
def _failed_artifact_payload(item: dict[str, Any]) -> dict[str, object]:
    return {
        "artifact_id": item.get("artifact_id", ""),
        "kind": item.get("kind", ""),
        "path": item.get("path", ""),
        "findings": item.get("acceptance_report", {}).get("findings", []),
    }


# LLM: _workspace_root reads the same tool registry root used by file tools.
# 函数用途: 获取主代理真实工作区；测试替身缺工具时退回 agent.root。
def _workspace_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve()


# LLM: _runtime_scope bundles closeout identity fields for file-write session inspection.
# 函数用途: 按 request/run/task 机器字段筛选未提交分块写入，避免旧 run 状态污染新任务。
def _runtime_scope(params: object) -> dict[str, str]:
    return {
        "request_id": str(getattr(params, "request_id", "") or ""),
        "run_id": str(getattr(params, "run_id", "") or ""),
        "task_id": str(getattr(params, "task_id", "") or ""),
    }


__all__ = [
    "DeliveryContractValidationRequest",
    "MainAgentDeliveryCloseoutRequest",
    "main_agent_delivery_closeout_response",
]
