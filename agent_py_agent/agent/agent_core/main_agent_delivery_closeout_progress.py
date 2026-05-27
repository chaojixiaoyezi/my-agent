# LLM: Delivery closeout progress helpers detect repeated failures from structured workspace facts.
# 模块用途: 计算失败指纹、工作区进展指纹和 no-progress 阈值，避免主代理在同一合同失败上空转。

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .delivery_closeout_config import delivery_closeout_config
from .main_agent_delivery_closeout_artifacts import _artifact_path
from .main_agent_delivery_closeout_recovery import _recovery_actions
from .main_agent_delivery_progress_roots import work_progress_roots


@dataclass(frozen=True)
class DeliveryProgressContext:
    workspace_root: Path
    contract: dict[str, Any]
    agent: object | None = None


# LLM: _enrich_delivery_progress records generic failure fingerprints and suggested recovery actions.
# 函数用途: 给 closeout 报告补充重复失败计数、工作区进展快照和通用恢复动作，避免长任务在同一失败上空转。
def _enrich_delivery_progress(
    report: dict[str, Any],
    previous_report: dict[str, Any],
    context: DeliveryProgressContext,
) -> dict[str, Any]:
    workspace_root = context.workspace_root
    contract = context.contract
    progress = _initial_progress(workspace_root, contract)
    if report["ok"]:
        report["delivery_progress"] = progress
        return report
    failure_fingerprint = _failure_fingerprint(report)
    work_fingerprint = str(progress["work_progress_fingerprint"])
    progress["failure_fingerprint"] = failure_fingerprint
    progress["recovery_actions"] = _recovery_actions(report, contract=contract, workspace_root=workspace_root)
    progress["unchanged_failure_count"] = _next_unchanged_failure_count(
        previous_report,
        failure_fingerprint=failure_fingerprint,
        work_fingerprint=work_fingerprint,
    )
    pending_targets = _pending_materialization_targets(contract, workspace_root)
    progress["pending_materialization_targets"] = pending_targets
    progress["no_progress_block_threshold"] = _no_progress_block_threshold(report, agent=context.agent)
    report["delivery_progress"] = progress
    return report


# LLM: _should_block_on_no_progress uses only structured delivery facts, never prompt prose, to stop a loop.
# 函数用途: 统一按 closeout retry budget 收口重复验收失败；返工建议由 closeout 上下文提供，不再交给独立 repair guard 豁免。
def _should_block_on_no_progress(
    report: dict[str, Any], *, contract: dict[str, Any], workspace_root: Path
) -> bool:
    progress = report.get("delivery_progress")
    if not isinstance(progress, dict):
        return False
    unchanged = _safe_int(progress.get("unchanged_failure_count"))
    if "no_progress_block_threshold" in progress:
        threshold = _progress_threshold_from_report(progress)
    else:
        threshold = _live_no_progress_threshold(report, contract, workspace_root)
    if threshold <= 0:
        return False
    return unchanged >= threshold


# LLM: _initial_progress builds the default progress envelope for every closeout run.
# 函数用途: 初始化 delivery_progress 的稳定字段，保证成功和失败报告结构一致。
def _initial_progress(workspace_root: Path, contract: dict[str, Any]) -> dict[str, object]:
    return {
        "failure_fingerprint": "",
        "work_progress_fingerprint": _work_progress_fingerprint(workspace_root, contract),
        "unchanged_failure_count": 0,
        "recovery_actions": [],
        "pending_materialization_targets": [],
        "no_progress_block_threshold": 0,
    }


# LLM: _next_unchanged_failure_count compares current and previous machine fingerprints.
# 函数用途: 判断是否仍是同一失败且工作区没有推进，是则递增，否则从 1 重新计数。
def _next_unchanged_failure_count(
    previous_report: dict[str, Any],
    *,
    failure_fingerprint: str,
    work_fingerprint: str,
) -> int:
    previous = _previous_progress_snapshot(previous_report)
    if previous_report.get("ok") is False and previous[:2] == (failure_fingerprint, work_fingerprint):
        return previous[2] + 1
    return 1


# LLM: _previous_progress_snapshot normalizes old closeout state into comparable primitives.
# 函数用途: 防御性读取上一轮 failure/work/count 字段，坏值按空状态处理。
def _previous_progress_snapshot(previous_report: dict[str, Any]) -> tuple[str, str, int]:
    previous_progress = previous_report.get("delivery_progress")
    if not isinstance(previous_progress, dict):
        return ("", "", 0)
    return (
        str(previous_progress.get("failure_fingerprint") or ""),
        str(previous_progress.get("work_progress_fingerprint") or ""),
        _safe_int(previous_progress.get("unchanged_failure_count")),
    )


# LLM: _live_no_progress_threshold recalculates threshold when older reports do not carry one.
# 函数用途: 兼容旧 closeout.json，同时仍以结构化 bootstrap target 和产物事实决定阻塞阈值。
def _live_no_progress_threshold(
    report: dict[str, Any],
    contract: dict[str, Any],
    workspace_root: Path,
) -> int:
    return _no_progress_block_threshold(report)


# LLM: _has_existing_failed_artifact distinguishes broken existing outputs from not-yet-materialized outputs.
# 函数用途: 若失败产物文件已存在，给修复更多轮次；如果完全没产物，保持较低阻塞阈值。
def _has_existing_failed_artifact(report: dict[str, Any]) -> bool:
    return any(
        not item.get("ok") and Path(str(item.get("path") or "")).exists()
        for item in report.get("artifacts", [])
    )


# LLM: _progress_threshold_from_report keeps no-progress blocking data-driven once the report already carries the threshold.
# 函数用途: 从 closeout 报告读取结构化阻塞阈值；坏值时回退为 0，让调用方继续按合同实时计算。
def _progress_threshold_from_report(progress: dict[str, Any]) -> int:
    return _safe_int(progress.get("no_progress_block_threshold"))


# LLM: _safe_int normalizes possibly bad JSON number fields.
# 函数用途: 把状态文件里可能被污染的数值字段转成 int，失败时回退 0。
def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# LLM: _pending_materialization_targets reads bootstrap targets as machine facts.
# 函数用途: 返回 bootstrap_contract 里仍未物化的目标路径，让多文件/分阶段任务不会过早判死。
def _pending_materialization_targets(contract: dict[str, Any], workspace_root: Path) -> list[dict[str, object]]:
    bootstrap = contract.get("bootstrap_contract")
    targets = bootstrap.get("materialization_targets") if isinstance(bootstrap, dict) else None
    if not isinstance(targets, list):
        return []
    return [
        target
        for item in targets
        if isinstance(item, dict)
        for target in [_materialization_target_record(item, workspace_root)]
        if target is not None and not target["exists"]
    ]


# LLM: _materialization_target_record normalizes one bootstrap target into an existence fact.
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


# LLM: _no_progress_block_threshold returns one of two generic closeout retry budgets.
# 函数用途: 统一验收返工阈值：缺产物和产物齐全但不合格两类；0 表示不按次数阻断。
def _no_progress_block_threshold(
    report_or_pending_targets: dict[str, Any] | list[dict[str, object]],
    *,
    total_target_count: int = 0,
    has_existing_failed_artifact: bool = False,
    agent: object | None = None,
) -> int:
    config = delivery_closeout_config(agent)
    if isinstance(report_or_pending_targets, dict):
        if not _failed_artifacts(report_or_pending_targets):
            return 0
        if _has_missing_artifact_failure(report_or_pending_targets):
            return config.missing_artifacts_retry_limit
        return config.invalid_artifacts_retry_limit
    if report_or_pending_targets:
        return config.missing_artifacts_retry_limit
    if has_existing_failed_artifact:
        return config.invalid_artifacts_retry_limit
    return 0


def _failed_artifacts(report: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, dict) and item.get("ok") is not True]


def _has_missing_artifact_failure(report: dict[str, Any]) -> bool:
    return any(_artifact_failure_is_missing(item) for item in _failed_artifacts(report))


def _artifact_failure_is_missing(item: dict[str, Any]) -> bool:
    path = str(item.get("path") or "").strip()
    if path and not Path(path).exists():
        return True
    return any(_finding_code_is_missing(finding.get("code")) for finding in _artifact_findings(item))


def _artifact_findings(item: dict[str, Any]) -> list[dict[str, Any]]:
    report = item.get("acceptance_report")
    findings = report.get("findings") if isinstance(report, dict) else None
    return [finding for finding in findings or [] if isinstance(finding, dict)]


def _finding_code_is_missing(value: object) -> bool:
    code = str(value or "").strip().upper()
    return (
        code == "ARTIFACT_MISSING"
        or code == "ARTIFACT_PATH_INVALID"
        or code.startswith("ARTIFACT_LOCATOR_")
    )


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
    failed = [
        _failed_artifact_fingerprint_record(item)
        for item in report.get("artifacts", [])
        if not item.get("ok")
    ]
    payload = json.dumps(failed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


# LLM: _failed_artifact_fingerprint_record extracts stable fields from one failed artifact result.
# 函数用途: 将单个失败产物转换成可排序、可哈希的结构，不包含自然语言说明。
def _failed_artifact_fingerprint_record(item: dict[str, Any]) -> dict[str, object]:
    rows = _finding_fingerprint_rows(item)
    return {
        "artifact_id": str(item.get("artifact_id") or ""),
        "kind": str(item.get("kind") or ""),
        "path": str(item.get("path") or ""),
        "findings": sorted(rows, key=lambda row: (row["code"], row["location"], row["value"])),
    }


# LLM: _finding_fingerprint_rows keeps only stable finding identity fields.
# 函数用途: 从 acceptance findings 中提取 code/location/value，避免 message 文案变化影响进展判断。
def _finding_fingerprint_rows(item: dict[str, Any]) -> list[dict[str, str]]:
    findings = item.get("acceptance_report", {}).get("findings", [])
    return [
        {
            "code": str(finding.get("code") or ""),
            "location": str(finding.get("location") or ""),
            "value": str(finding.get("value") or ""),
        }
        for finding in findings
        if isinstance(finding, dict)
    ]


# LLM: _work_progress_fingerprint tracks only user-work roots so memory/log churn does not fake progress.
# 函数用途: 只看 outputs、scripts、data 等工作根目录的文件状态，忽略 memory/archive 噪音，供无进展判断使用。
def _work_progress_fingerprint(workspace_root: Path, contract: dict[str, Any] | None = None) -> str:
    rows: list[dict[str, object]] = []
    for root in work_progress_roots(workspace_root, contract or {}):
        rows.extend(_work_progress_rows_for_root(root, workspace_root))
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


# LLM: _work_progress_rows_for_root summarizes one work root without reading unrelated runtime folders.
# 函数用途: 记录根目录存在性、目录子项和文件签名，供进展指纹统一哈希。
def _work_progress_rows_for_root(root: Path, workspace_root: Path) -> list[dict[str, object]]:
    if not root.exists():
        return [{"root": root.name, "exists": False}]
    if root.is_file():
        return [_file_progress_row(root, workspace_root)]
    rows = [_directory_progress_row(root, workspace_root)]
    rows.extend(_directory_progress_row(path, workspace_root) for path in sorted(root.rglob("*")) if path.is_dir())
    rows.extend(_file_progress_row(path, workspace_root) for path in sorted(root.rglob("*")) if path.is_file())
    return rows


# LLM: _directory_progress_row records directory presence and immediate child-set changes.
# 函数用途: 把目录自身也当成可见进展，避免“只建目录”被误判为无进展。
def _directory_progress_row(path: Path, workspace_root: Path) -> dict[str, object]:
    return {
        "root": path.parts[-1] if path == workspace_root else (path.relative_to(workspace_root).parts[0]),
        "path": str(path.relative_to(workspace_root)).replace("\\", "/"),
        "kind": "dir",
        "signature": _dir_signature(path, workspace_root),
    }


# LLM: _file_progress_row records a bounded content signature for one work file.
# 函数用途: 保留文件路径和哈希，识别真实内容变化，同时避免把完整产物塞进报告。
def _file_progress_row(path: Path, workspace_root: Path) -> dict[str, object]:
    return {
        "root": path.relative_to(workspace_root).parts[0],
        "path": str(path.relative_to(workspace_root)).replace("\\", "/"),
        "kind": "file",
        "signature": _file_signature(path),
    }


# LLM: _file_signature hashes file content in a bounded way so identical rewrites do not fake progress.
# 函数用途: 小文件全量哈希；大文件只取头尾片段和尺寸，既能识别真实变化，也避免 closeout 扫描太重。
def _file_signature(path: Path) -> str:
    stat = path.stat()
    size = stat.st_size
    if size <= 262_144:
        return f"{size}:{sha256(path.read_bytes()).hexdigest()}"
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
# 函数用途: 目录本身没有文件内容，所以用直接子项名称列表表示推进。
def _dir_signature(path: Path, workspace_root: Path) -> str:
    payload = json.dumps(
        {
            "path": str(path.relative_to(workspace_root)).replace("\\", "/"),
            "children": sorted(item.name for item in path.iterdir()),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()
