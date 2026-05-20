# LLM: Main-agent delivery closeout stops productive real tasks once machine contracts pass.
# 模块用途: 工具轮后读取机器交付合同、验收产物并写收口报告，避免主代理产物已合格还继续跑到超时。

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..backends import ModelResponse
from ..contracts.artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from ..contracts.error_taxonomy import error_contract
from ..contracts.staged_checkpoint_acceptance import (
    json_checkpoint_status,
    staged_json_evidence_findings,
)
from ..tooling.file_write_session_inspection import open_file_write_sessions
from ._runtime_params import ToolLoopExecuteParams

CLOSEOUT_DIR = ".agent_delivery"
CLOSEOUT_REPORT = "closeout.json"
_WRITE_FIRST_RECOVERY_ACTIONS = {
    "invoke_builder_tool",
    "materialize_checkpoint",
    "repair_evidence_refs",
    "repair_structured_checkpoint_json",
    "write_non_empty_structured_rows",
}


# LLM: MainAgentDeliveryCloseoutRequest bundles post-tool-loop state for contract validation.
# 类用途: 保存主代理、工具循环参数和后端名，供交付收口逻辑在不扩散参数的情况下运行。
@dataclass(frozen=True)
class MainAgentDeliveryCloseoutRequest:
    agent: object
    params: ToolLoopExecuteParams
    backend: str


# LLM: DeliveryContractValidationRequest keeps artifact validation inputs bundled and extensible.
# 类用途: 集中保存交付合同、必交产物、工作区和运行参数，避免 helper 参数继续增长。
@dataclass(frozen=True)
class DeliveryContractValidationRequest:
    contract: dict[str, Any]
    artifacts: list[dict[str, Any]]
    workspace_root: Path
    params: ToolLoopExecuteParams


# LLM: main_agent_delivery_closeout_response returns a deterministic final response only after all required refs pass.
# 函数用途: 根据结构化 delivery_contract 验收必交产物；通过则停止工具循环，失败则写结构化反馈让模型修复。
def main_agent_delivery_closeout_response(request: MainAgentDeliveryCloseoutRequest) -> ModelResponse | None:
    contract = _delivery_contract(request.params)
    artifacts = _required_artifacts(contract)
    if not artifacts:
        return None
    workspace_root = _workspace_root(request.agent)
    if open_sessions := open_file_write_sessions(workspace_root):
        _append_open_session_context(request.params, open_sessions)
        return None
    previous_report = _existing_report(workspace_root)
    report = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=artifacts,
            workspace_root=workspace_root,
            params=request.params,
        )
    )
    report = _enrich_delivery_progress(report, previous_report, workspace_root, contract=contract)
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    _write_report(workspace_root, report)
    if not report["ok"]:
        _append_failed_contract_context(request.params, report)
        if _should_block_on_no_progress(report, contract=contract, workspace_root=workspace_root):
            return ModelResponse(text=_blocked_closeout_text(report), backend=request.backend)
        return None
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


# LLM: _delivery_contract reads machine contracts from runtime fields, never from prompt prose.
# 函数用途: 优先读取 ToolLoopExecuteParams.delivery_contract；兼容读取 task_attributes.delivery_contract。
def _delivery_contract(params: ToolLoopExecuteParams) -> dict[str, Any]:
    value = params.delivery_contract
    if isinstance(value, dict):
        return dict(value)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    value = attrs.get("delivery_contract")
    return dict(value) if isinstance(value, dict) else {}


# LLM: _required_artifacts keeps optional outputs from forcing deterministic closeout.
# 函数用途: 返回合同里 required 不为 false 的产物项，缺少列表时不触发收口。
def _required_artifacts(contract: dict[str, Any]) -> list[dict[str, Any]]:
    raw = contract.get("artifacts")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("required") is not False]


# LLM: _validate_contract_artifacts converts artifact refs into a single machine-readable delivery report.
# 函数用途: 对每个必交产物按结构化路径验收，汇总 ok、finding 和运行范围字段。
def _validate_contract_artifacts(request: DeliveryContractValidationRequest) -> dict[str, Any]:
    results = [_validate_artifact_item(item, request.workspace_root) for item in request.artifacts]
    return {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": all(item["ok"] for item in results),
        "case_id": str(request.contract.get("case_id") or ""),
        "request_id": request.params.request_id,
        "run_id": request.params.run_id,
        "task_id": request.params.task_id,
        "workspace_root": str(request.workspace_root),
        "artifacts": results,
    }


# LLM: _existing_report reuses the prior closeout snapshot so repeated failures can be detected generically.
# 函数用途: 读取上一次 closeout.json；不存在或损坏时返回空对象，不让交付收口崩掉。
def _existing_report(workspace_root: Path) -> dict[str, Any]:
    path = workspace_root / CLOSEOUT_DIR / CLOSEOUT_REPORT
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _enrich_delivery_progress records generic failure fingerprints and suggested recovery actions.
# 函数用途: 给 closeout 报告补充重复失败计数、工作区进展快照和通用恢复动作，避免长任务在同一失败上空转。
def _enrich_delivery_progress(
    report: dict[str, Any],
    previous_report: dict[str, Any],
    workspace_root: Path,
    *,
    contract: dict[str, Any],
) -> dict[str, Any]:
    progress = {
        "failure_fingerprint": "",
        "work_progress_fingerprint": _work_progress_fingerprint(workspace_root),
        "unchanged_failure_count": 0,
        "recovery_actions": [],
        "pending_materialization_targets": [],
        "no_progress_block_threshold": 2,
    }
    if report["ok"]:
        report["delivery_progress"] = progress
        return report
    failure_fingerprint = _failure_fingerprint(report)
    progress["failure_fingerprint"] = failure_fingerprint
    progress["recovery_actions"] = _recovery_actions(report, contract=contract, workspace_root=workspace_root)
    previous_progress = previous_report.get("delivery_progress")
    previous_fingerprint = ""
    previous_work_fingerprint = ""
    previous_count = 0
    if isinstance(previous_progress, dict):
        previous_fingerprint = str(previous_progress.get("failure_fingerprint") or "")
        previous_work_fingerprint = str(previous_progress.get("work_progress_fingerprint") or "")
        try:
            previous_count = int(previous_progress.get("unchanged_failure_count") or 0)
        except (TypeError, ValueError):
            previous_count = 0
    if (
        previous_report.get("ok") is False
        and previous_fingerprint == failure_fingerprint
        and previous_work_fingerprint == progress["work_progress_fingerprint"]
    ):
        progress["unchanged_failure_count"] = previous_count + 1
    else:
        progress["unchanged_failure_count"] = 1
    pending_targets = _pending_materialization_targets(contract, workspace_root)
    progress["pending_materialization_targets"] = pending_targets
    has_existing_failed_artifact = any(
        not item.get("ok") and Path(str(item.get("path") or "")).exists()
        for item in report.get("artifacts", [])
    )
    progress["no_progress_block_threshold"] = _no_progress_block_threshold(
        pending_targets,
        total_target_count=_materialization_target_count(contract),
        has_existing_failed_artifact=has_existing_failed_artifact,
    )
    report["delivery_progress"] = progress
    return report


# LLM: _validate_artifact_item validates one contract artifact without reading natural-language acceptance prose.
# 函数用途: 解析 preferred_path/path，执行通用产物验收，并把报告转成稳定 JSON 字段。
def _validate_artifact_item(item: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    raw_path = str(item.get("preferred_path") or item.get("path") or "")
    path = _artifact_path(raw_path, workspace_root)
    if path is None:
        return _path_failure(item, raw_path, "ARTIFACT_PATH_INVALID")
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=workspace_root,
            validation_contract=_validation_contract(item),
        )
    ).to_dict()
    return {
        "artifact_id": str(item.get("artifact_id") or ""),
        "kind": str(item.get("kind") or report.get("artifact_kind") or ""),
        "path": str(path),
        "ok": bool(report.get("ok")),
        "acceptance_report": report,
    }


# LLM: _artifact_path keeps contract paths bounded to the current workspace.
# 函数用途: 把相对路径落到 workspace_root 下；绝对路径必须仍位于 workspace_root 内。
def _artifact_path(raw_path: str, workspace_root: Path) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    path = candidate.resolve(strict=False) if candidate.is_absolute() else (workspace_root / candidate).resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    return path


# LLM: _path_failure gives missing or escaped artifact refs the same report shape as validator failures.
# 函数用途: 生成路径无效时的结构化产物验收结果，方便后续修复流程统一消费。
def _path_failure(item: dict[str, Any], raw_path: str, code: str) -> dict[str, Any]:
    finding = {
        "code": code,
        "severity": "hard",
        "message": "Artifact path is missing or outside the workspace.",
        "location": raw_path,
        "value": raw_path,
    }
    return {
        "artifact_id": str(item.get("artifact_id") or ""),
        "kind": str(item.get("kind") or ""),
        "path": raw_path,
        "ok": False,
        "acceptance_report": {"ok": False, "artifact_ref": raw_path, "artifact_kind": "", "findings": [finding]},
    }


# LLM: _validation_contract extracts machine-only artifact acceptance options from one contract item.
# 函数用途: 将 expected_artifacts 里的 validation_contract 传给底层验收器，不解析自然语言说明。
def _validation_contract(item: dict[str, Any]) -> dict[str, object]:
    value = item.get("validation_contract")
    return dict(value) if isinstance(value, dict) else {}


# LLM: _write_report persists the latest delivery check for audit and resume without embedding artifact bodies.
# 函数用途: 写 `.agent_delivery/closeout.json`，让真实 E2E 和用户排查能看到机器验收结果。
def _write_report(workspace_root: Path, report: dict[str, Any]) -> Path:
    path = workspace_root / CLOSEOUT_DIR / CLOSEOUT_REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


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
        "artifacts": [
            {
                "artifact_id": item["artifact_id"],
                "kind": item["kind"],
                "path": item["path"],
                "ok": item["ok"],
            }
            for item in report["artifacts"]
        ],
    }
    return (
        "[MAIN_AGENT_DELIVERY_COMPLETE]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/MAIN_AGENT_DELIVERY_COMPLETE]\n结构化交付合同已通过，主代理停止继续工具循环。"
    )


# LLM: _blocked_closeout_text terminates deterministic no-progress loops without inventing task-specific contracts.
# 函数用途: 同一交付失败重复出现且工作区没有推进时，输出结构化阻塞结果，让父级快速收口为失败并进入恢复链路。
def _blocked_closeout_text(report: dict[str, Any]) -> str:
    payload = {
        "ok": False,
        "reason": "delivery_contract_no_progress",
        "case_id": report.get("case_id", ""),
        "report_ref": report.get("report_ref", ""),
        "delivery_progress": report.get("delivery_progress", {}),
        "failed_artifacts": [
            {
                "artifact_id": item.get("artifact_id", ""),
                "kind": item.get("kind", ""),
                "path": item.get("path", ""),
                "findings": item.get("acceptance_report", {}).get("findings", []),
            }
            for item in report.get("artifacts", [])
            if not item.get("ok")
        ],
    }
    return (
        "[MAIN_AGENT_DELIVERY_BLOCKED]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/MAIN_AGENT_DELIVERY_BLOCKED]\n结构化交付失败在同一状态下重复出现且没有新的工作进展，主代理停止继续工具循环。"
    )


# LLM: _workspace_root reads the same tool registry root used by file tools.
# 函数用途: 获取主代理真实工作区；测试替身缺工具时退回 agent.root。
def _workspace_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve()


# LLM: _relative_report_ref keeps reports portable in stdout and test fixtures.
# 函数用途: 把 closeout 报告路径尽量显示为工作区相对路径。
def _relative_report_ref(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return str(path)


# LLM: _should_block_on_no_progress uses only structured delivery facts, never prompt prose, to stop a loop.
# 函数用途: 成品都已落地时保持严格阻塞；仍缺结构化目标时适度放宽，让多文件/分阶段任务有机会补齐产物。
def _should_block_on_no_progress(
    report: dict[str, Any], *, contract: dict[str, Any], workspace_root: Path
) -> bool:
    progress = report.get("delivery_progress")
    if not isinstance(progress, dict):
        return False
    if _has_write_first_recovery_action(progress):
        return False
    try:
        unchanged = int(progress.get("unchanged_failure_count") or 0)
    except (TypeError, ValueError):
        return False
    threshold = _progress_threshold_from_report(progress)
    if threshold <= 0:
        has_existing_failed_artifact = any(
            not item.get("ok") and Path(str(item.get("path") or "")).exists()
            for item in report.get("artifacts", [])
        )
        threshold = _no_progress_block_threshold(
            _pending_materialization_targets(contract, workspace_root),
            total_target_count=_materialization_target_count(contract),
            has_existing_failed_artifact=has_existing_failed_artifact,
        )
    if unchanged >= threshold and _has_write_first_recovery_action(progress):
        return False
    return unchanged >= threshold


# LLM: write-first recovery actions are handled by tool_delivery_repair_guard before terminal no-progress blocking.
# 函数用途: 判断 closeout 是否已经给出必须先写入/修复/构建的结构化动作；这类动作要先进入修复 guard，而不是立即熔断。
def _has_write_first_recovery_action(progress: dict[str, Any]) -> bool:
    actions = progress.get("recovery_actions")
    if not isinstance(actions, list):
        return False
    return any(
        isinstance(item, dict)
        and bool(item.get("retryable", True))
        and str(item.get("recommended_action") or "").strip() in _WRITE_FIRST_RECOVERY_ACTIONS
        for item in actions
    )


# LLM: _progress_threshold_from_report keeps no-progress blocking data-driven once the report already carries the threshold.
# 函数用途: 从 closeout 报告读取结构化阻塞阈值；坏值时回退为 0，让调用方继续按合同实时计算。
def _progress_threshold_from_report(progress: dict[str, Any]) -> int:
    try:
        return int(progress.get("no_progress_block_threshold") or 0)
    except (TypeError, ValueError):
        return 0


# LLM: _pending_materialization_targets reads bootstrap targets as machine facts so closeout can tell "still building" from "stuck".
# 函数用途: 返回 bootstrap_contract 里仍未物化的目标路径；这样多文件/分阶段任务不会在研究或补第二个文件前被过早判死。
def _pending_materialization_targets(contract: dict[str, Any], workspace_root: Path) -> list[dict[str, object]]:
    bootstrap = contract.get("bootstrap_contract")
    targets = bootstrap.get("materialization_targets") if isinstance(bootstrap, dict) else None
    if not isinstance(targets, list):
        return []
    pending: list[dict[str, object]] = []
    for item in targets:
        if not isinstance(item, dict):
            continue
        target = _materialization_target_record(item, workspace_root)
        if target is None or target["exists"]:
            continue
        pending.append(target)
    return pending


# LLM: _materialization_target_record normalizes one bootstrap target into an existence fact without reading prompt prose.
# 函数用途: 把 contract target 转成统一记录，后续既能写回 closeout，也能给模型结构化提示还缺哪些文件。
def _materialization_target_record(item: dict[str, Any], workspace_root: Path) -> dict[str, object] | None:
    path = _artifact_path(str(item.get("workspace_relative_path") or item.get("resolved_path") or ""), workspace_root)
    if path is None:
        return None
    return {
        "artifact_id": str(item.get("artifact_id") or ""),
        "kind": str(item.get("kind") or ""),
        "target_type": str(item.get("target_type") or ""),
        "workspace_relative_path": str(item.get("workspace_relative_path") or ""),
        "resolved_path": str(path),
        "exists": path.exists(),
    }


# LLM: _no_progress_block_threshold raises the retry budget only while bootstrap targets are still missing.
# 函数用途: 统一 no-progress 阈值：成品都已落地时保持严格；一个目标都还没落地时给更宽预算；已有部分阶段产物但还没收口时，也保留额外轮次继续完成。
def _no_progress_block_threshold(
    pending_targets: list[dict[str, object]], *, total_target_count: int, has_existing_failed_artifact: bool
) -> int:
    if not pending_targets:
        if has_existing_failed_artifact:
            return 4
        return 2
    if total_target_count > 0 and len(pending_targets) >= total_target_count:
        return 6
    return 5


# LLM: _materialization_target_count keeps the closeout retry budget based on structured bootstrap facts only.
# 函数用途: 统计 bootstrap_contract.materialization_targets 数量，让系统区分“一个目标都没落地”和“只差最后几个目标”。
def _materialization_target_count(contract: dict[str, Any]) -> int:
    bootstrap = contract.get("bootstrap_contract")
    targets = bootstrap.get("materialization_targets") if isinstance(bootstrap, dict) else None
    if not isinstance(targets, list):
        return 0
    return sum(1 for item in targets if isinstance(item, dict))


# LLM: _failure_fingerprint turns failed artifact findings into a stable machine comparison key.
# 函数用途: 把失败产物及 finding code/location/value 归一后哈希，后续判断是否还是同一失败。
def _failure_fingerprint(report: dict[str, Any]) -> str:
    failed = []
    for item in report.get("artifacts", []):
        if item.get("ok"):
            continue
        finding_rows = []
        for finding in item.get("acceptance_report", {}).get("findings", []):
            if not isinstance(finding, dict):
                continue
            finding_rows.append(
                {
                    "code": str(finding.get("code") or ""),
                    "location": str(finding.get("location") or ""),
                    "value": str(finding.get("value") or ""),
                }
            )
        failed.append(
            {
                "artifact_id": str(item.get("artifact_id") or ""),
                "kind": str(item.get("kind") or ""),
                "path": str(item.get("path") or ""),
                "findings": sorted(finding_rows, key=lambda row: (row["code"], row["location"], row["value"])),
            }
        )
    payload = json.dumps(failed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


# LLM: _work_progress_fingerprint tracks only user-work roots so memory/log churn does not fake progress.
# 函数用途: 只看 outputs、scripts、data 等工作根目录的文件状态，忽略 memory/archive 噪音，供无进展判断使用。
def _work_progress_fingerprint(workspace_root: Path) -> str:
    roots = [workspace_root / "outputs", workspace_root / "scripts", workspace_root / "data"]
    rows: list[dict[str, object]] = []
    for root in roots:
        if not root.exists():
            rows.append({"root": root.name, "exists": False})
            continue
        rows.append(
            {
                "root": root.name,
                "path": str(root.relative_to(workspace_root)).replace("\\", "/"),
                "kind": "dir",
                "signature": _dir_signature(root, workspace_root),
            }
        )
        for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: str(item)):
            rows.append(
                {
                    "root": root.name,
                    "path": str(path.relative_to(workspace_root)).replace("\\", "/"),
                    "kind": "dir",
                    "signature": _dir_signature(path, workspace_root),
                }
            )
        for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: str(item)):
            rows.append(
                {
                    "root": root.name,
                    "path": str(path.relative_to(workspace_root)).replace("\\", "/"),
                    "kind": "file",
                    "signature": _file_signature(path),
                }
            )
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


# LLM: _file_signature hashes file content in a bounded way so identical rewrites do not fake progress.
# 函数用途: 小文件全量哈希；大文件只取头尾片段和尺寸，既能识别真实变化，也避免 closeout 扫描太重。
def _file_signature(path: Path) -> str:
    stat = path.stat()
    size = stat.st_size
    if size <= 262_144:
        data = path.read_bytes()
        return f"{size}:{sha256(data).hexdigest()}"
    with path.open("rb") as handle:
        head = handle.read(65_536)
        if size > 65_536:
            handle.seek(max(0, size - 65_536))
        tail = handle.read(65_536)
    digest = sha256()
    digest.update(head)
    digest.update(tail)
    digest.update(str(size).encode("utf-8"))
    return f"{size}:{digest.hexdigest()}"


# LLM: _dir_signature treats directory creation and child-set changes as real progress for delivery loops.
# 函数用途: 目录本身没有文件内容，所以用直接子项名称列表表示推进，避免“只建目录”被误判成没进展。
def _dir_signature(path: Path, workspace_root: Path) -> str:
    children = sorted(item.name for item in path.iterdir())
    payload = json.dumps(
        {
            "path": str(path.relative_to(workspace_root)).replace("\\", "/"),
            "children": children,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


# LLM: _recovery_actions lifts failed finding codes into generic next-step hints instead of task-specific prose.
# 函数用途: 给交付失败补充通用恢复动作，让模型和父级都能结构化知道下一步该做什么；合同里的 staged/builder/checkpoint 恢复动作也在这里统一生成。
def _recovery_actions(
    report: dict[str, Any],
    *,
    contract: dict[str, Any],
    workspace_root: Path,
) -> list[dict[str, object]]:
    actions: list[dict[str, str | bool]] = []
    seen: set[str] = set()
    for item in report.get("artifacts", []):
        if item.get("ok"):
            continue
        for finding in item.get("acceptance_report", {}).get("findings", []):
            if not isinstance(finding, dict):
                continue
            recovery_contract = error_contract(_recovery_error_code(str(finding.get("code") or "")))
            if recovery_contract.code in seen:
                continue
            seen.add(recovery_contract.code)
            actions.append(
                {
                    "code": recovery_contract.code,
                    "category": recovery_contract.category,
                    "retryable": recovery_contract.retryable,
                    "recommended_action": recovery_contract.recommended_action,
                    "recovery_hint": recovery_contract.recovery_hint,
                }
            )
    actions.extend(_contract_recovery_actions(contract, workspace_root=workspace_root, seen=seen))
    if actions:
        return actions
    contract = error_contract("ACCEPTANCE_FAILED")
    return [
        {
            "code": contract.code,
            "category": contract.category,
            "retryable": contract.retryable,
            "recommended_action": contract.recommended_action,
            "recovery_hint": contract.recovery_hint,
        }
    ]


# LLM: _contract_recovery_actions derives next steps from machine contracts and workspace facts only.
# 函数用途: 当阶段文件、builder tool、checkpoint refs 已在结构化合同里定义时，生成对应恢复动作，避免模型只围着 artifact_missing 打转。
def _contract_recovery_actions(
    contract: dict[str, Any],
    *,
    workspace_root: Path,
    seen: set[str],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for artifact in _required_artifacts(contract):
        validation_contract = _validation_contract(artifact)
        staging = validation_contract.get("staging_contract")
        if not isinstance(staging, dict):
            continue
        artifact_path = _artifact_path(
            str(artifact.get("preferred_path") or artifact.get("path") or ""),
            workspace_root,
        )
        artifact_exists = bool(artifact_path and artifact_path.exists())
        builder_tool = str(staging.get("builder_tool") or "").strip()
        source_ref = str(staging.get("source_json_ref") or "").strip()
        output_ref = str(staging.get("workbook_ref") or artifact.get("preferred_path") or "").strip()
        source_path = _artifact_path(source_ref, workspace_root) if source_ref else None
        required_columns = _required_columns(validation_contract.get("required_columns"))
        source_shape_hint = _checkpoint_shape_hint(staging, source_ref)
        if source_ref and source_path is not None and source_path.exists():
            _append_checkpoint_quality_action(
                actions,
                seen,
                source_ref,
                source_path,
                required_columns=required_columns,
                checkpoint_shape_hint=source_shape_hint,
            )
            if _append_staged_evidence_actions(actions, seen, source_ref, workspace_root, validation_contract):
                continue
        if builder_tool and source_path is not None and source_path.exists() and not artifact_exists:
            if json_checkpoint_status(source_path, required_columns=required_columns).get("code") != "OK":
                continue
            action_code = "STAGING_BUILDER_READY"
            if action_code not in seen:
                seen.add(action_code)
                actions.append(
                    {
                        "code": action_code,
                        "category": "artifact",
                        "retryable": True,
                        "recommended_action": "invoke_builder_tool",
                        "builder_tool": builder_tool,
                        "source_ref": source_ref,
                        "output_ref": output_ref,
                        "recovery_hint": "阶段数据已经落地；优先调用 builder tool 继续物化最终产物。",
                    }
                )
        checkpoint_refs = staging.get("checkpoint_refs")
        if not isinstance(checkpoint_refs, list):
            continue
        for ref in checkpoint_refs:
            ref_text = str(ref or "").strip()
            if not ref_text:
                continue
            checkpoint_path = _artifact_path(ref_text, workspace_root)
            if checkpoint_path is None or checkpoint_path.exists():
                if checkpoint_path is not None and checkpoint_path.exists() and checkpoint_path.suffix.lower() == ".json":
                    _append_checkpoint_quality_action(
                        actions,
                        seen,
                        ref_text,
                        checkpoint_path,
                        required_columns=required_columns,
                        checkpoint_shape_hint=_checkpoint_shape_hint(staging, ref_text),
                    )
                continue
            action_code = "STAGING_CHECKPOINT_MISSING"
            if action_code not in seen:
                seen.add(action_code)
                actions.append(
                    {
                        "code": action_code,
                        "category": "artifact",
                        "retryable": True,
                        "recommended_action": "materialize_checkpoint",
                        "checkpoint_ref": ref_text,
                        "recovery_hint": "先把缺失的阶段文件真实写出来，可以先写最小有效骨架，再继续补内容。",
                        "checkpoint_shape_hint": _checkpoint_shape_hint(staging, ref_text),
                    }
                )
            break
    return actions


# LLM: _append_checkpoint_quality_action promotes existing staged JSON quality issues into structured recovery actions.
# 函数用途: 当 checkpoint 文件已存在但 JSON 为空或损坏时，直接产出恢复动作，避免模型误以为可以继续 builder/下一阶段。
def _append_checkpoint_quality_action(
    actions: list[dict[str, object]],
    seen: set[str],
    checkpoint_ref: str,
    checkpoint_path: Path,
    *,
    required_columns: list[str] | None = None,
    checkpoint_shape_hint: str = "",
) -> None:
    status = json_checkpoint_status(checkpoint_path, required_columns=required_columns)
    action_code = str(status.get("code") or "")
    if action_code == "OK" or action_code in seen:
        return
    contract = error_contract(action_code)
    seen.add(action_code)
    action: dict[str, object] = {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
        "checkpoint_ref": checkpoint_ref,
    }
    if required_columns:
        action["required_columns"] = required_columns
    if checkpoint_shape_hint:
        action["checkpoint_shape_hint"] = checkpoint_shape_hint
    parse_error = str(status.get("parse_error") or "")
    if parse_error:
        action["parse_error"] = parse_error
    missing_columns = str(status.get("missing_columns") or "")
    if missing_columns:
        action["missing_columns"] = missing_columns
    actions.append(action)


# LLM: _checkpoint_shape_hint reads per-checkpoint structure hints so recovery actions can tell the model what shape to write.
# 函数用途: 从 staging_contract.checkpoint_shape_hints 中取出指定 checkpoint 的结构提示文本。
def _checkpoint_shape_hint(staging: dict[str, Any], checkpoint_ref: str) -> str:
    hints = staging.get("checkpoint_shape_hints")
    if not isinstance(hints, dict):
        return ""
    return str(hints.get(checkpoint_ref) or "").strip()


# LLM: _append_staged_evidence_actions converts staged evidence findings into structured recovery actions.
# 函数用途: 把 source_refs/claims 这类阶段证据问题写成 recovery_actions，供 closeout 和 repair guard 复用。
def _append_staged_evidence_actions(
    actions: list[dict[str, object]],
    seen: set[str],
    checkpoint_ref: str,
    workspace_root: Path,
    validation_contract: dict[str, object],
) -> bool:
    evidence_contract = validation_contract.get("evidence_contract")
    if not isinstance(evidence_contract, dict):
        return False
    findings = staged_json_evidence_findings(checkpoint_ref, workspace_root, evidence_contract)
    for finding in findings:
        action_code = str(finding.get("code") or "")
        if not action_code or action_code in seen:
            continue
        contract = error_contract(action_code)
        seen.add(action_code)
        action: dict[str, object] = {
            "code": contract.code,
            "category": contract.category,
            "retryable": contract.retryable,
            "recommended_action": contract.recommended_action,
            "recovery_hint": contract.recovery_hint,
            "checkpoint_ref": checkpoint_ref,
        }
        if field := str(finding.get("field") or ""):
            action["field"] = field
        if claim_id := str(finding.get("claim_id") or ""):
            action["claim_id"] = claim_id
        actions.append(action)
    return bool(findings)


# LLM: _required_columns normalizes contract-declared table columns without reading prompt prose.
# 函数用途: 从 validation_contract.required_columns 读取结构化列名，供阶段 JSON 和最终 workbook 共用同一列要求。
def _required_columns(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [column for item in value if (column := str(item).strip())]


# LLM: _recovery_error_code groups validator-specific findings under generic recovery contracts.
# 函数用途: 把 HTML/XLSX/PDF 等专用 finding code 归并到通用错误类型，避免再长专项合同分支。
def _recovery_error_code(code: str) -> str:
    upper = str(code or "").upper()
    if upper in {"ARTIFACT_MISSING", "ARTIFACT_EMPTY"}:
        return "ARTIFACT_MISSING"
    if upper.startswith("STAGED_"):
        return upper
    if upper.startswith("PATH_"):
        return upper
    if upper.startswith("SPREADSHEET_SOURCE_"):
        return upper
    if upper.startswith("EVIDENCE_"):
        return upper
    if upper.startswith("XLSX_") or upper.startswith("CSV_") or upper.startswith("JSON_") or upper.startswith("PDF_"):
        return "ACCEPTANCE_FAILED"
    if upper.startswith("HTML_"):
        return "ACCEPTANCE_FAILED"
    return "ACCEPTANCE_FAILED"
