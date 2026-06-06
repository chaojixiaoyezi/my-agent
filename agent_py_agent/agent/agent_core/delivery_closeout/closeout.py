
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...artifacts.registry import registry_path
from ...backends import ModelResponse
from ...contracts.delivery_contract_doctor import (
    ContractDoctorReport,
    validate_delivery_contract,
)
from .._runtime_params import ToolLoopExecuteParams
from ..main_agent_delivery_progress_ledger import append_delivery_progress_event
from ..main_agent_delivery_tool_failure_recovery import attach_tool_failure_recovery_actions
from ..runtime.owner_roots import runtime_archive_roots
from ..tool_guard.local_progress import reset_local_progress_guard
from .artifacts import (
    DeliveryContractValidationRequest,
    _existing_report,
    _relative_report_ref,
    _required_artifacts,
    _validate_contract_artifacts,
    _write_report,
)
from .gate_recovery import failed_gate_payloads
from .gates import CloseoutGateRequest, attach_closeout_gates
from .nonterminal import write_non_terminal_closeout_report
from .progress import (
    DeliveryProgressContext,
    _enrich_delivery_progress,
    _should_block_on_no_progress,
)
from .task_progress_gate import task_progress_repair_message
from .uncontracted import (
    _current_run_task_output_artifacts,
    uncontracted_task_output_closeout_response,
)


@dataclass(frozen=True)
class MainAgentDeliveryCloseoutRequest:
    agent: object
    params: ToolLoopExecuteParams
    backend: str


def main_agent_delivery_closeout_response(request: MainAgentDeliveryCloseoutRequest) -> ModelResponse | None:
    contract = _delivery_contract(request.params)
    workspace_root = _workspace_root(request.agent)
    if not contract:
        if response := uncontracted_task_output_closeout_response(request, workspace_root):
            return response
        if _current_run_task_output_artifacts(request.params, workspace_root=workspace_root):
            return None
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
    if not gates_allowed:
        report["ok"] = False
        _write_report(workspace_root, report)
    append_delivery_progress_event(workspace_root, report, blocked=not gates_allowed)
    if not gates_allowed:
        return _failed_delivery_response(request, report, contract, workspace_root)
    reset_local_progress_guard(request.agent, request.params)
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


def _no_artifact_closeout_response(
    request: MainAgentDeliveryCloseoutRequest,
    contract: dict[str, Any],
    workspace_root: Path,
) -> ModelResponse | None:
    if not _allows_no_artifact_delivery(contract):
        report = _required_artifacts_missing_report(request, contract, workspace_root)
        report_ref = _write_report(workspace_root, report)
        report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
        _write_report(workspace_root, report)
        _append_failed_contract_context(request.params, report)
        return None
    report = _message_delivery_report(request, contract, workspace_root)
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    decisions = attach_closeout_gates(CloseoutGateRequest(request, report, contract, workspace_root))
    _write_report(workspace_root, report)
    if not _all_gates_allowed(decisions):
        report["ok"] = False
        _write_report(workspace_root, report)
        return _failed_delivery_response(request, report, contract, workspace_root)
    reset_local_progress_guard(request.agent, request.params)
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


def _all_gates_allowed(decisions: list[Any]) -> bool:
    return all(bool(getattr(decision, "allowed", False)) for decision in decisions)


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
            archive_tool_calls=_closeout_archive_tool_calls(closeout),
        )
    )
    enriched = _enrich_delivery_progress(
        report,
        _existing_report(workspace_root),
        DeliveryProgressContext(workspace_root=workspace_root, contract=contract, agent=closeout.agent),
    )
    return attach_tool_failure_recovery_actions(
        enriched,
        _closeout_archive_tool_calls(closeout),
        workspace_root,
    )


def _closeout_archive_tool_calls(closeout: MainAgentDeliveryCloseoutRequest) -> list[Any]:
    records = [
        item
        for item in list(getattr(closeout.params, "archive_tool_calls", []) or [])
        if isinstance(item, dict)
    ]
    records.extend(_disk_tool_output_records(closeout.agent, records))
    return _unique_archive_tool_calls(records)


def _disk_tool_output_records(agent: object, seed_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scope = _archive_scope(seed_records)
    for index_path in _tool_output_index_paths(agent, seed_records):
        rows.extend(_read_tool_output_index(index_path))
    if scope:
        rows = [row for row in rows if _record_matches_archive_scope(row, scope)]
    return rows


def _archive_scope(seed_records: list[dict[str, Any]]) -> dict[str, set[str]]:
    run_ids = {
        text
        for record in seed_records
        if (text := str(record.get("run_id") or "").strip())
    }
    task_ids = {
        text
        for record in seed_records
        if (text := str(record.get("task_id") or "").strip())
    }
    return {key: value for key, value in {"run_id": run_ids, "task_id": task_ids}.items() if value}


def _record_matches_archive_scope(row: dict[str, Any], scope: dict[str, set[str]]) -> bool:
    run_ids = scope.get("run_id") or set()
    task_ids = scope.get("task_id") or set()
    row_run = str(row.get("run_id") or "").strip()
    row_task = str(row.get("task_id") or "").strip()
    if run_ids and row_run in run_ids:
        return True
    if task_ids and row_task in task_ids:
        return True
    return False


def _tool_output_index_paths(agent: object, seed_records: list[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    for record in seed_records:
        for key in ("path", "artifact_ref", "source_artifact_ref", "source_output_path", "output_path"):
            paths.extend(_index_path_from_artifact_ref(record.get(key)))
    for root in runtime_archive_roots(agent):
        paths.append(Path(root) / "blobs" / "tool_outputs" / "index.jsonl")
    return _unique_existing_paths(paths)


def _index_path_from_artifact_ref(value: object) -> list[Path]:
    text = str(value or "").strip()
    if not text or "://" in text:
        return []
    try:
        path = Path(text).expanduser().resolve(strict=False)
    except OSError:
        return []
    parents = [path, *path.parents]
    return [parent / "index.jsonl" for parent in parents if parent.name == "tool_outputs"]


def _unique_existing_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve(strict=False)
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.is_file():
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _read_tool_output_index(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _unique_archive_tool_calls(records: list[dict[str, Any]]) -> list[Any]:
    unique: list[Any] = []
    seen: set[tuple[str, str, str, str]] = set()
    for record in records:
        key = (
            str(record.get("run_id") or ""),
            str(record.get("scoped_call_id") or ""),
            str(record.get("call_id") or ""),
            str(record.get("sha256") or ""),
        )
        if not any(key):
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


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


def _delivery_contract(params: ToolLoopExecuteParams) -> dict[str, Any]:
    value = params.delivery_contract
    if isinstance(value, dict):
        return dict(value)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    value = attrs.get("delivery_contract")
    return dict(value) if isinstance(value, dict) else {}


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


def _required_artifacts_missing_report(
    closeout: MainAgentDeliveryCloseoutRequest,
    contract: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": False,
        "case_id": str(contract.get("case_id") or ""),
        "request_id": closeout.params.request_id,
        "run_id": closeout.params.run_id,
        "task_id": closeout.params.task_id,
        "workspace_root": str(workspace_root),
        "canonical_artifact_registry_ref": _relative_report_ref(registry_path(workspace_root), workspace_root),
        "artifacts": [],
        "non_terminal": True,
        "reason": "required_artifacts_missing",
        "message_zh": "本次 submit_for_acceptance 已记录，但 delivery contract 没有可验收的 required artifact；请先写出用户要求的交付物再提交。",
        "contract_recovery": {
            "required_actions": [
                "materialize_user_requested_output_artifact",
                "write_real_deliverable_file",
                "submit_for_acceptance_after_artifact_exists",
            ],
        },
    }


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


def _repair_guidance(report: dict[str, Any]) -> dict[str, Any]:
    progress = report.get("delivery_progress")
    actions = progress.get("recovery_actions") if isinstance(progress, dict) else []
    task_progress_message = task_progress_repair_message(report)
    coverage_message = _target_coverage_repair_message(report)
    final_artifact_paths = _final_artifact_paths(report)
    if str(report.get("reason") or "") == "required_artifacts_missing":
        message = (
            "当前验收失败是因为没有 required artifact 可验收。请根据用户要求写出真实交付物文件，"
            "优先写到 delivery contract 或用户指定的输出路径；如果合同漏掉了用户指定路径，请按用户原话的输出路径写入，"
            "然后重新调用 submit_for_acceptance。"
        )
    elif coverage_message:
        message = coverage_message
    else:
        message = (
            "请根据 failed_artifacts、failed_gates 和 required_actions 自主选择下一步修复方式。"
            "如果还需要读取或搜索来确认上下文，可以继续做；但要尽快把结果落成可验收的本地产物，"
            "然后调用 submit_for_acceptance 提交验收。"
        )
    path_message = _final_artifact_path_message(final_artifact_paths)
    if path_message:
        message = f"{path_message} {message}"
    if task_progress_message:
        message = f"{task_progress_message} {message}"
    return {
        "mode": "closeout_rework",
        "required_actions": actions if isinstance(actions, list) else [],
        "final_artifact_paths": final_artifact_paths,
        "message_zh": message,
        "submit_when_ready": "submit_for_acceptance",
    }


def _final_artifact_paths(report: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in report.get("artifacts", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _final_artifact_path_message(paths: list[str]) -> str:
    if not paths:
        return ""
    payload = json.dumps(paths[:5], ensure_ascii=False)
    overflow = "" if len(paths) <= 5 else f" 等 {len(paths)} 个路径"
    return (
        f"本次返工必须更新最终交付路径 {payload}{overflow}；"
        "不要只修改 task output/work 或 .my_agent 内部副本。"
    )


def _target_coverage_repair_message(report: dict[str, Any]) -> str:
    status = report.get("target_coverage_status")
    if not isinstance(status, dict) or status.get("should_block") is not True:
        return ""
    hints = status.get("repair_hints") if isinstance(status.get("repair_hints"), list) else []
    hint_text = json.dumps(hints[:3], ensure_ascii=False, sort_keys=True)
    return (
        "当前验收失败是因为任务要求完整覆盖源材料，但 coverage ledger 还没有连续 read_file 覆盖证明。"
        "grep、awk、search_text、run_command 只能辅助定位或统计，不能替代完整阅读证明。"
        "下一步必须按 target_coverage_status.repair_hints 里的 recommended_tool_call 继续 read_file；"
        "读到 total_lines/total_chars 覆盖完成后，再更新最终产物并调用 submit_for_acceptance。"
        f" 当前可执行游标：{hint_text}"
    )


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


def _closeout_artifact_payload(item: dict[str, Any]) -> dict[str, object]:
    return {
        "artifact_id": item["artifact_id"],
        "kind": item["kind"],
        "path": item["path"],
        "ok": item["ok"],
    }


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


def _failed_artifact_payload(item: dict[str, Any]) -> dict[str, object]:
    return {
        "artifact_id": item.get("artifact_id", ""),
        "kind": item.get("kind", ""),
        "path": item.get("path", ""),
        "findings": item.get("acceptance_report", {}).get("findings", []),
    }


def _workspace_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve()


def _write_contract_doctor_report(workspace_root: Path, report: ContractDoctorReport) -> Path:
    path = workspace_root / ".agent_delivery" / "contract_doctor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


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
