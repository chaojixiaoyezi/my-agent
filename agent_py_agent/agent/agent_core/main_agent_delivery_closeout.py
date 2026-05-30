# LLM: Main-agent delivery closeout stops productive real tasks once machine contracts pass.
# 模块用途: 工具轮后读取机器交付合同、验收产物并写收口报告，避免主代理产物已合格还继续跑到超时。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..artifacts.registry import registry_path
from ..backends import ModelResponse
from ..contracts.delivery_contract_doctor import ContractDoctorReport, validate_delivery_contract
from ._runtime_params import ToolLoopExecuteParams
from .main_agent_delivery_closeout_artifacts import (
    DeliveryContractValidationRequest,
    _existing_report,
    _relative_report_ref,
    _required_artifacts,
    _validate_contract_artifacts,
    _write_report,
)
from .main_agent_delivery_closeout_gate_recovery import failed_gate_payloads
from .main_agent_delivery_closeout_gates import CloseoutGateRequest, attach_closeout_gates
from .main_agent_delivery_closeout_nonterminal import write_non_terminal_closeout_report
from .main_agent_delivery_closeout_progress import (
    DeliveryProgressContext,
    _enrich_delivery_progress,
    _should_block_on_no_progress,
)
from .main_agent_delivery_progress_ledger import append_delivery_progress_event
from .main_agent_delivery_tool_failure_recovery import attach_tool_failure_recovery_actions
from .tool_local_progress_guard import reset_local_progress_guard


# LLM: MainAgentDeliveryCloseoutRequest bundles post-tool-loop state for contract validation.
# 类用途: 保存主代理、工具循环参数和后端名，供交付收口逻辑在不扩散参数的情况下运行。
@dataclass(frozen=True)
class MainAgentDeliveryCloseoutRequest:
    agent: object
    params: ToolLoopExecuteParams
    backend: str


# LLM: main_agent_delivery_closeout_response returns a deterministic final response only after all required refs pass.
# 函数用途: 根据结构化 delivery_contract 验收必交产物；通过则停止工具循环，失败则写结构化反馈让模型修复。
def main_agent_delivery_closeout_response(request: MainAgentDeliveryCloseoutRequest) -> ModelResponse | None:
    contract = _delivery_contract(request.params)
    workspace_root = _workspace_root(request.agent)
    if not contract:
        write_non_terminal_closeout_report(request, workspace_root, reason="delivery_contract_missing")
        return None
    doctor = validate_delivery_contract(contract, workspace_root=workspace_root)
    _write_contract_doctor_report(workspace_root, doctor)
    if not doctor.ok:
        if _has_contract_doctor_context(request.params):
            return None
        _append_contract_doctor_context(request.params, doctor)
        return None
    contract = dict(doctor.normalized_contract or contract)
    artifacts = _required_artifacts(contract)
    if not artifacts:
        return _no_artifact_closeout_response(request, contract, workspace_root)
    report = _delivery_report(request, contract, artifacts, workspace_root)
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    decisions = attach_closeout_gates(CloseoutGateRequest(request, report, contract, workspace_root))
    _write_report(workspace_root, report)
    gates_allowed = _all_gates_allowed(decisions)
    append_delivery_progress_event(workspace_root, report, blocked=not gates_allowed)
    if not gates_allowed:
        return _failed_delivery_response(request, report, contract, workspace_root)
    reset_local_progress_guard(request.agent, request.params)
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


# LLM: _no_artifact_closeout_response handles explicit message-only delivery without nesting main flow.
# 函数用途: 合同声明无需磁盘产物时验收消息型交付；否则继续让模型工作。
def _no_artifact_closeout_response(
    request: MainAgentDeliveryCloseoutRequest,
    contract: dict[str, Any],
    workspace_root: Path,
) -> ModelResponse | None:
    if not _allows_no_artifact_delivery(contract):
        write_non_terminal_closeout_report(request, workspace_root, contract=contract, reason="required_artifacts_missing")
        return None
    report = _message_delivery_report(request, contract, workspace_root)
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    decisions = attach_closeout_gates(CloseoutGateRequest(request, report, contract, workspace_root))
    _write_report(workspace_root, report)
    if not _all_gates_allowed(decisions):
        return _failed_delivery_response(request, report, contract, workspace_root)
    reset_local_progress_guard(request.agent, request.params)
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


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
    enriched = _enrich_delivery_progress(
        report,
        _existing_report(workspace_root),
        DeliveryProgressContext(workspace_root=workspace_root, contract=contract, agent=closeout.agent),
    )
    return attach_tool_failure_recovery_actions(
        enriched,
        list(getattr(closeout.params, "archive_tool_calls", []) or []),
        workspace_root,
    )


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


# LLM: _allows_no_artifact_delivery handles legitimate answer/channel-only tasks without weakening file tasks.
# 函数用途: 只有合同显式声明无需落盘时，才允许 artifacts 为空也进入 closeout。
def _allows_no_artifact_delivery(contract: dict[str, Any]) -> bool:
    for key in ("requires_artifact", "artifact_required", "requires_disk_artifact", "disk_artifact_required"):
        if contract.get(key) is False:
            return True
    mode = str(contract.get("delivery_mode") or contract.get("output_mode") or "").strip().lower()
    return mode in {"message", "answer", "summary", "no_artifact", "no-artifact", "none"}


def _message_delivery_report(
    closeout: MainAgentDeliveryCloseoutRequest,
    contract: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": True,
        "case_id": str(contract.get("case_id") or ""),
        "request_id": closeout.params.request_id,
        "run_id": closeout.params.run_id,
        "task_id": closeout.params.task_id,
        "workspace_root": str(workspace_root),
        "delivery_mode": str(contract.get("delivery_mode") or "message"),
        "canonical_artifact_registry_ref": _relative_report_ref(registry_path(workspace_root), workspace_root),
        "artifacts": [],
        "message_delivery": {
            "ok": True,
            "reason": "contract_explicitly_allows_no_disk_artifact",
        },
    }


# LLM: _append_contract_doctor_context feeds malformed contract facts into the next model turn.
# 函数用途: 合同本身坏掉时写入结构化 Doctor 报告，要求入口物化层返工，而不是静默跳过验收。
def _append_contract_doctor_context(params: ToolLoopExecuteParams, report: ContractDoctorReport) -> None:
    payload = report.to_dict()
    payload.pop("normalized_contract", None)
    params.tool_context.append(
        "[delivery-contract-doctor]\n"
        + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _has_contract_doctor_context(params: ToolLoopExecuteParams) -> bool:
    return any(str(item).startswith("[delivery-contract-doctor]") for item in params.tool_context)


# LLM: _append_failed_contract_context feeds structured repair facts into the next model turn.
# 函数用途: 验收失败时追加 JSON 反馈，不把自然语言说明当机器事实。
def _append_failed_contract_context(params: ToolLoopExecuteParams, report: dict[str, Any]) -> None:
    params.tool_context.append(
        "[delivery-contract-check]\n"
        + json.dumps(
            {
                "ok": False,
                "report_ref": report.get("report_ref", ""),
                "failed_artifacts": [item for item in report.get("artifacts", []) if isinstance(item, dict) and not item.get("ok")],
                "failed_gates": failed_gate_payloads(report),
                "target_coverage_status": report.get("target_coverage_status", {}),
                "contract_recovery": report.get("contract_recovery", {}),
                "delivery_progress": report.get("delivery_progress", {}),
                "repair_guidance": _repair_guidance(report),
                "rework_message_zh": "这是交付返工，不是任务终止。请按 repair_guidance.required_actions 和 failed_artifacts 修复后重新验收；只有 status=blocked 或需要用户输入时才停止自动返工。",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


# LLM: _repair_guidance keeps delivery rework advice inside closeout feedback.
# 函数用途: 把验收 finding、recovery_actions 和下一步建议统一放到 closeout 上下文，避免独立 delivery repair 门干预工具选择。
def _repair_guidance(report: dict[str, Any]) -> dict[str, Any]:
    progress = report.get("delivery_progress")
    actions = progress.get("recovery_actions") if isinstance(progress, dict) else []
    return {
        "mode": "closeout_rework",
        "required_actions": actions if isinstance(actions, list) else [],
        "message_zh": (
            "请根据 failed_artifacts、failed_gates 和 required_actions 自主选择下一步修复方式。"
            "如果还需要读取或搜索来确认上下文，可以继续做；但要尽快把结果落成可验收的本地产物，"
            "然后调用 submit_for_acceptance 提交验收。"
        ),
        "submit_when_ready": "submit_for_acceptance",
    }

# LLM: _closeout_text makes the final message copyable while keeping the machine payload explicit.
# 函数用途: 生成主代理完成响应，告知上层工具循环不用再请求下一轮模型。
def _closeout_text(report: dict[str, Any]) -> str:
    payload = {
        "ok": True,
        "case_id": report.get("case_id", ""),
        "report_ref": report.get("report_ref", ""),
        "artifacts": [_closeout_artifact_payload(item) for item in report["artifacts"]],
        "target_coverage_status": report.get("target_coverage_status", {}),
    }
    return (
        "[MAIN_AGENT_DELIVERY_COMPLETE]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/MAIN_AGENT_DELIVERY_COMPLETE]\n交付验收通过。结构化交付合同已通过，主代理停止继续工具循环。"
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
        "contract_recovery": report.get("contract_recovery", {}),
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


# LLM: _write_contract_doctor_report persists entry-gate findings next to closeout reports.
# 函数用途: 记录最新合同结构自检结果，方便离线回放和用户审计。
def _write_contract_doctor_report(workspace_root: Path, report: ContractDoctorReport) -> Path:
    path = workspace_root / ".agent_delivery" / "contract_doctor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


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
