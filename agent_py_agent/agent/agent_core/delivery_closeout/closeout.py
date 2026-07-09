
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
from ..run_task_workspace_writer import (
    current_run_task_workspace_root,
    sync_run_task_workspace_closeout,
)
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
from .gates import CloseoutGateRequest, attach_closeout_gates
from .progress import (
    DeliveryProgressContext,
    _enrich_delivery_progress,
    _should_block_on_no_progress,
    append_delivery_progress_event,
)
from .recovery import attach_tool_failure_recovery_actions, failed_gate_payloads
from .task_progress_gate import (
    coverage_incomplete_rework,
    task_progress_ledger_present,
    task_progress_repair_message,
)
from .uncontracted import (
    _current_run_task_output_artifacts,
    _one_shot_rework_blocks,
    _spawned_children_present,
    uncontracted_task_output_closeout_response,
)


@dataclass(frozen=True)
class MainAgentDeliveryCloseoutRequest:
    agent: object
    params: ToolLoopExecuteParams
    backend: str


def write_non_terminal_closeout_report(
    closeout: object,
    workspace_root: Path,
    *,
    reason: str,
    contract: dict[str, Any] | None = None,
) -> None:
    params = getattr(closeout, "params", None)
    contract = contract or {}
    report = {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": False,
        "case_id": str(contract.get("case_id") or ""),
        "request_id": str(getattr(params, "request_id", "") or ""),
        "run_id": str(getattr(params, "run_id", "") or ""),
        "task_id": str(getattr(params, "task_id", "") or ""),
        "workspace_root": str(workspace_root),
        "canonical_artifact_registry_ref": _relative_report_ref(registry_path(workspace_root), workspace_root),
        "artifacts": [],
        "non_terminal": True,
        "reason": reason,
        "message_zh": "本次 submit_for_acceptance 已记录，但没有足够的结构化交付合同可做最终验收；任务不因此终止。",
    }
    _write_report(workspace_root, report)


def _is_report_only_wake(params: object) -> bool:
    """后台调度触发(source=background_main_agent)且【无待验收交付契约】的主代理 run——其输出是
    给用户的状态/发现报告本身(盯守 findings、观察叫回、进度汇报),不是待验收的任务交付物,不走
    交付收尾门。否则会被 uncontracted 收尾门当"未契约任务输出"打回成 [MAIN_AGENT_DELIVERY_
    REWORK_REQUIRED] 内部信号,被通道层 _is_internal_signal 过滤、发不到用户飞书(真事件永远不落地)。

    有契约的交付轮(即便后台整合子代理产物)照常走完整 gate、不豁免——假 done 门不被绕过。

    ⚠️ 旧实现读 params.reason 判定,但收尾门这层的 ToolLoopExecuteParams【根本没有 reason 字段】
    → getattr 恒取到 ""→ 恒返回 False → 收尾门对上报轮从未真正豁免过(真机实锤:observation/
    urgent 真事件报告全被 REWORK 拦下、channel_message_id 为空发不出)。改用该层确有的结构字段
    source + delivery_contract(与 background_liveness.is_wake_capable_source 同一个 source 标记)。"""
    if str(getattr(params, "source", "") or "").strip() != "background_main_agent":
        return False
    return not getattr(params, "delivery_contract", None)


def main_agent_delivery_closeout_response(request: MainAgentDeliveryCloseoutRequest) -> ModelResponse | None:
    # 第4层根修:观察/urgent 上报轮(子代理 raise_event 的真事件叫回主代理上报)——其输出【就是给
    # 用户的报告本身】,不是"待验收的任务交付物"。无契约=正常,不该被 uncontracted 收尾门当"未契约
    # 任务输出"打回成 internal rework(真机实锤:真事件报告被这条拦下、channel=internal 发不到飞书)。
    # 这类叫回轮直接跳过交付收尾门,报告按 run 的正常投递路由(已修=飞书)发给 owner。
    if _is_report_only_wake(request.params):
        return None
    contract = _delivery_contract(request.params)
    workspace_root = _workspace_root(request.agent, request.params)
    if not contract:
        return _missing_contract_closeout_response(request, workspace_root)
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
        return _no_required_artifact_response(request, contract, workspace_root)
    report = _delivery_report(request, contract, artifacts, workspace_root)
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    decisions = attach_closeout_gates(CloseoutGateRequest(request, report, contract, workspace_root))
    verdict = _closeout_decision(request, report, decisions)
    blocked = verdict != "allow"
    if blocked:
        report["ok"] = False
    # _closeout_decision 可能往 report 注入 quality_advisories,这里统一落盘最终态。
    _write_report(workspace_root, report)
    append_delivery_progress_event(workspace_root, report, blocked=blocked)
    if verdict == "block":
        return _failed_delivery_response(request, report, contract, workspace_root)
    if verdict == "rework_once":
        return None
    sync_run_task_workspace_closeout(request.agent, request.params, report)
    reset_local_progress_guard(request.agent, request.params)
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


def _missing_contract_closeout_response(
    request: MainAgentDeliveryCloseoutRequest,
    workspace_root: Path,
) -> ModelResponse | None:
    if response := uncontracted_task_output_closeout_response(request, workspace_root):
        return response
    if _current_run_task_output_artifacts(request.params, workspace_root=workspace_root):
        return None
    # 出口合同(P2-1/P5-1):派过子代理【或立过 task_progress 账】的任务,uncontracted
    # 已经跑完完整 gate 并写出阻断报告(返回 None=阻断已注入,或一次性提醒额度已花、
    # 由上层诚实失败出口接管);这里不得用 non_terminal 报告覆盖它(§7-2 真机:solo 成品
    # 写在任务区外,空交付阻断报告曾被这行覆盖成 delivery_contract_missing,返工指引丢失)。
    # 注:原 _fake_done_empty_delivery_rework(全 done+证据不实存的一次对质)已被
    # uncontracted 的 _ledger_empty_delivery_rework 取代——判据更宽(立过账+交付区空,
    # 覆盖"成品实存但落错位置"的 §7-2 形态),同样幂等一次、二次放行诚实失败。
    if _spawned_children_present(request) or task_progress_ledger_present(request.agent, request.params):
        return None
    write_non_terminal_closeout_report(request, workspace_root, reason="delivery_contract_missing")
    return None


# LLM: 合同未声明任何 required artifact 时的分流(R7a 实锤:路由注入的空壳合同
#   曾压制 15 个真实产物——模型被"请先写出交付物"误导性打回,uncontracted 验收
#   链与 expected_outputs 对账门全被短路)。规则:存在当前 run 真实产物(写入
#   记录或交付区扫描,见 uncontracted 收集口)→ 回落 uncontracted 验收链,gate
#   全跑;真零产物 → 维持 required_artifacts_missing 打回。合同有无 coverage
#   不再影响这条分流(coverage gate 在 uncontracted 链内照常评估)。
# 函数用途: 合同是空壳时别拿合同压人——交付区有真货就正常验收,真没货才打回。
def _no_required_artifact_response(
    request: MainAgentDeliveryCloseoutRequest,
    contract: dict[str, Any],
    workspace_root: Path,
) -> ModelResponse | None:
    if _current_run_task_output_artifacts(request.params, workspace_root=workspace_root):
        if response := uncontracted_task_output_closeout_response(request, workspace_root):
            return response
        return None
    return _no_artifact_closeout_response(request, contract, workspace_root)


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
    verdict = _closeout_decision(request, report, decisions)
    if verdict != "allow":
        report["ok"] = False
    # _closeout_decision 可能往 report 注入 quality_advisories,这里统一落盘最终态。
    _write_report(workspace_root, report)
    if verdict == "rework_once":
        return None
    if verdict == "block":
        return _failed_delivery_response(request, report, contract, workspace_root)
    sync_run_task_workspace_closeout(request.agent, request.params, report)
    reset_local_progress_guard(request.agent, request.params)
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


# 交付判定四档(对齐 uncontracted 标杆"只拦客观事实"哲学,取代旧的全 12 门一票否决):
#   L0 模型自判完成(默认放行);L1 客观事实阻断(唯一能 BLOCK);L2 验证证据一次性
#   提醒(幂等·永不死锁);L3 纯 advisory(永不 block)。
# L1 能阻断的 gate(与 uncontracted 同源的客观事实):closeout 账本/作用域结构损坏
#   (run_contract)、子代理未终态/孤儿/open capreq(subagent_aggregation)、防编造门
#   (fact_evidence/source_fact_consistency,无人值守必需,绝不砍)。产物打不开/占位
#   空壳/误写系统目录由 _artifact_blocks 直接按产物 ok 计算(与 uncontracted
#   artifact_blocks 同款)。delivery_quality/task_progress/source_volume/acceptance/
#   final/state/runtime 这些质量·数量·复合门一律不进 L1。
_L1_BLOCKING_GATES = frozenset(
    {
        "run_contract",
        "subagent_aggregation",
        "fact_evidence",
        "source_fact_consistency",
    }
)

# L2 目标覆盖一次性提醒的幂等标记(照搬 verification_evidence 双出口·二次放行,永不死锁)。
_TARGET_COVERAGE_REWORK_MARKER = "[contracted-target-coverage-rework]"


# 函数用途: 按四档裁决 contracted closeout 的退出动作(取代 _all_gates_allowed 全门必过)。
#   "block"=客观事实/目标覆盖一次性打回(走 contracted 返工链);"rework_once"=声明
#   缺口/验证证据一次性提醒(已注入双出口,非终态返工);"allow"=放行(L3 未达标只写
#   进 quality_advisories,不影响退出)。
def _closeout_decision(
    request: MainAgentDeliveryCloseoutRequest,
    report: dict[str, Any],
    decisions: list[Any],
) -> str:
    if _artifact_blocks(report) or _runtime_gate_artifact_blocked(report) or _l1_gate_blocked(decisions):
        return "block"
    # L2 目标覆盖(声明驱动·一次性):覆盖不全第一次打回,同形态第二次放行进 advisory。
    if _target_coverage_rework_pending(request.params, report):
        return "block"
    # L2 一次性提醒(expected_outputs 自我声明缺口 + 任务要求跑测试却无证据):幂等一次。
    expected_outputs_decision = _decision_for_gate(decisions, "expected_outputs_reconciliation")
    if _one_shot_rework_blocks(request, report, expected_outputs_decision):
        return "rework_once"
    # L2 覆盖对账一次性提醒(A3:模型自声明 coverage 范围没对完账就收口 → 打回一次)。
    if coverage_incomplete_rework(request.params, report):
        return "rework_once"
    # L3 质量/数量/进度类未达标:ok 仍放行,全部 gate 事实与 advisory 保留在报告里供把关。
    if any(not bool(getattr(decision, "allowed", False)) for decision in decisions):
        report["quality_advisories"] = failed_gate_payloads(report)
    return "allow"


# runtime(delivery_closeout)复合门里属于"覆盖/新鲜度"的 finding——单独走 L2 一次性
#   提醒,不计入 L1 客观阻断;其余 finding(产物验收/出处/占位/误写系统目录)均为 L1。
_TARGET_COVERAGE_FINDING_CODES = frozenset(
    {
        "TARGET_COVERAGE_MISSING",
        "FINAL_ARTIFACT_STALE_AFTER_REQUIRED_COVERAGE",
    }
)


# 函数用途: 产物级客观阻断(R3 占位空壳/打不开/误写系统目录)——任一产物 ok!=True 即拦。
def _artifact_blocks(report: dict[str, Any]) -> bool:
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    return any(isinstance(item, dict) and item.get("ok") is not True for item in artifacts)


# 函数用途: runtime(delivery_closeout)复合门里的产物级客观阻断(出处缺失/占位/伪造/
#   误写系统目录——产物 ok 字段看不见、但运行时门可见)。剔除纯覆盖/新鲜度类 finding
#   (那些走 L2 一次性提醒);含任一产物级 finding(或门未放行却无可辨识 finding)即拦。
def _runtime_gate_artifact_blocked(report: dict[str, Any]) -> bool:
    gate = report.get("runtime_gate")
    if not isinstance(gate, dict) or gate.get("allowed") is True:
        return False
    findings = [item for item in (gate.get("findings") or []) if isinstance(item, dict)]
    non_coverage = [
        item for item in findings if str(item.get("code") or "") not in _TARGET_COVERAGE_FINDING_CODES
    ]
    return bool(non_coverage) or not findings


# 函数用途: L1 客观事实门是否有任一未放行(防编造/子代理收口/账本作用域结构)。
def _l1_gate_blocked(decisions: list[Any]) -> bool:
    return any(
        getattr(decision, "gate", "") in _L1_BLOCKING_GATES and not bool(getattr(decision, "allowed", False))
        for decision in decisions
    )


# 函数用途: 按 gate 名取决策(与 decisions 列表顺序解耦,避免靠下标取门)。
def _decision_for_gate(decisions: list[Any], gate: str) -> Any:
    return next((decision for decision in decisions if getattr(decision, "gate", "") == gate), None)


# 函数用途: 目标覆盖(should_block/freshness/projection 未达标)L2 一次性提醒是否待发。
#   第一次返回 True 打回并落幂等标记;标记已在则返回 False 转 advisory(同形态绝不死锁)。
def _target_coverage_rework_pending(params: ToolLoopExecuteParams, report: dict[str, Any]) -> bool:
    if not _target_coverage_incomplete(report):
        return False
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return True
    if any(_TARGET_COVERAGE_REWORK_MARKER in str(item) for item in context):
        return False
    context.append(
        _TARGET_COVERAGE_REWORK_MARKER
        + "\n"
        + json.dumps({"ok": False, "reason": "target_coverage_incomplete"}, ensure_ascii=False, sort_keys=True)
    )
    return True


# 函数用途: 目标覆盖三路客观信号——目录/来源覆盖缺、最终产物早于覆盖、覆盖投影未呈现。
def _target_coverage_incomplete(report: dict[str, Any]) -> bool:
    for key in ("target_coverage_status", "target_coverage_freshness_status"):
        status = report.get(key)
        if isinstance(status, dict) and status.get("should_block") is True:
            return True
    projection = report.get("target_coverage_projection_gate")
    return isinstance(projection, dict) and projection.get("allowed") is not True


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
            coverage_workspace_root=_coverage_workspace_root(closeout.agent),
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
    task_root = current_run_task_workspace_root(agent)
    if task_root is not None:
        paths.append(task_root / "work" / "blobs" / "tool_outputs" / "index.jsonl")
        paths.extend(_task_agent_tool_output_index_paths(task_root))
    for root in runtime_archive_roots(agent):
        paths.append(Path(root) / "blobs" / "tool_outputs" / "index.jsonl")
    return _unique_existing_paths(paths)


def _task_agent_tool_output_index_paths(task_root: Path) -> list[Path]:
    agents_root = task_root / "work" / "agents"
    if not agents_root.is_dir():
        return []
    try:
        return sorted(agents_root.glob("*/blobs/tool_outputs/index.jsonl"))
    except OSError:
        return []


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
    seen: dict[tuple[str, str, str, str], int] = {}
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
            unique[seen[key]] = _merge_archive_record(unique[seen[key]], record)
            continue
        seen[key] = len(unique)
        unique.append(dict(record))
    return unique


def _merge_archive_record(existing: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in incoming.items():
        if _archive_value_missing(merged.get(key)) and not _archive_value_missing(value):
            merged[key] = value
    return merged


def _archive_value_missing(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


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
                "target_coverage_freshness_status": report.get("target_coverage_freshness_status", {}),
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
    final_artifact_paths = _final_artifact_paths(report)
    message = _repair_guidance_message(report)
    message = _prepend_optional_message(message, _final_artifact_path_message(final_artifact_paths))
    message = _prepend_optional_message(message, task_progress_repair_message(report))
    return {
        "mode": "closeout_rework",
        "required_actions": actions if isinstance(actions, list) else [],
        "final_artifact_paths": final_artifact_paths,
        "message_zh": message,
        "submit_when_ready": "submit_for_acceptance",
    }


def _repair_guidance_message(report: dict[str, Any]) -> str:
    if str(report.get("reason") or "") == "required_artifacts_missing":
        return (
            "当前验收失败是因为没有 required artifact 可验收。请根据用户要求写出真实交付物文件，"
            "优先写到 delivery contract 或用户指定的输出路径；如果合同漏掉了用户指定路径，请按用户原话的输出路径写入，"
            "然后重新调用 submit_for_acceptance。"
        )
    if coverage_message := _target_coverage_repair_message(report):
        return coverage_message
    if freshness_message := _target_coverage_freshness_repair_message(report):
        return freshness_message
    return (
        "请根据 failed_artifacts、failed_gates 和 required_actions 自主选择下一步修复方式。"
        "如果还需要读取或搜索来确认上下文，可以继续做；但要尽快把结果落成可验收的本地产物，"
        "然后调用 submit_for_acceptance 提交验收。"
    )


def _prepend_optional_message(message: str, prefix: str) -> str:
    return f"{prefix} {message}" if prefix else message


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
        "下一步必须按 target_coverage_status.repair_hints 里的 recommended_tool_call 继续补覆盖；"
        "缺目录覆盖时先 list_files 找候选文件，缺长文件覆盖时继续 read_file 游标。"
        "覆盖完成后，再更新最终产物并调用 submit_for_acceptance。"
        f" 当前可执行游标：{hint_text}"
    )


def _target_coverage_freshness_repair_message(report: dict[str, Any]) -> str:
    status = report.get("target_coverage_freshness_status")
    if not isinstance(status, dict) or status.get("should_block") is not True:
        return ""
    stale = status.get("stale_artifacts") if isinstance(status.get("stale_artifacts"), list) else []
    stale_text = json.dumps(stale[:5], ensure_ascii=False, sort_keys=True)
    latest = str(status.get("latest_required_coverage_created_at") or "")
    return (
        "当前验收失败是因为最终交付物早于最后一次必要源码覆盖读取。"
        "这说明你已经补到了新证据，但还没有把最终报告/交付物更新到最新证据之后。"
        "下一步不要继续重复读取；请更新最终交付物，把已读证据汇总进去，然后重新调用 submit_for_acceptance。"
        f" latest_required_coverage_created_at={latest}; stale_artifacts={stale_text}"
    )


def _closeout_text(report: dict[str, Any]) -> str:
    payload = {
        "ok": True,
        "case_id": report.get("case_id", ""),
        "report_ref": report.get("report_ref", ""),
        "artifacts": [_closeout_artifact_payload(item) for item in report["artifacts"]],
        "target_coverage_status": report.get("target_coverage_status", {}),
        "target_coverage_freshness_status": report.get("target_coverage_freshness_status", {}),
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


def _workspace_root(agent: object, params: object | None = None) -> Path:
    task_root = current_run_task_workspace_root(agent, params)
    if task_root is not None:
        return task_root
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve()


def _coverage_workspace_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve(strict=False)


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
