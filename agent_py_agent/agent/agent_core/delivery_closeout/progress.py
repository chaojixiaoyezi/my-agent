
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..main_agent_delivery_progress_roots import work_progress_roots
from .artifacts import _artifact_path
from .config import delivery_closeout_config
from .recovery import _recovery_actions


@dataclass(frozen=True)
class DeliveryProgressContext:
    workspace_root: Path
    contract: dict[str, Any]
    agent: object | None = None


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


def _initial_progress(workspace_root: Path, contract: dict[str, Any]) -> dict[str, object]:
    return {
        "failure_fingerprint": "",
        "work_progress_fingerprint": _work_progress_fingerprint(workspace_root, contract),
        "unchanged_failure_count": 0,
        "recovery_actions": [],
        "pending_materialization_targets": [],
        "no_progress_block_threshold": 0,
    }


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


def _previous_progress_snapshot(previous_report: dict[str, Any]) -> tuple[str, str, int]:
    previous_progress = previous_report.get("delivery_progress")
    if not isinstance(previous_progress, dict):
        return ("", "", 0)
    return (
        str(previous_progress.get("failure_fingerprint") or ""),
        str(previous_progress.get("work_progress_fingerprint") or ""),
        _safe_int(previous_progress.get("unchanged_failure_count")),
    )


def _live_no_progress_threshold(
    report: dict[str, Any],
    contract: dict[str, Any],
    workspace_root: Path,
) -> int:
    return _no_progress_block_threshold(report)


def _has_existing_failed_artifact(report: dict[str, Any]) -> bool:
    return any(
        not item.get("ok") and Path(str(item.get("path") or "")).exists()
        for item in report.get("artifacts", [])
    )


def _progress_threshold_from_report(progress: dict[str, Any]) -> int:
    return _safe_int(progress.get("no_progress_block_threshold"))


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def _materialization_target_count(contract: dict[str, Any]) -> int:
    bootstrap = contract.get("bootstrap_contract")
    targets = bootstrap.get("materialization_targets") if isinstance(bootstrap, dict) else None
    if not isinstance(targets, list):
        return 0
    return sum(1 for item in targets if isinstance(item, dict))


def _failure_fingerprint(report: dict[str, Any]) -> str:
    failed = [
        _failed_artifact_fingerprint_record(item)
        for item in report.get("artifacts", [])
        if not item.get("ok")
    ]
    payload = json.dumps(failed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _failed_artifact_fingerprint_record(item: dict[str, Any]) -> dict[str, object]:
    rows = _finding_fingerprint_rows(item)
    return {
        "artifact_id": str(item.get("artifact_id") or ""),
        "kind": str(item.get("kind") or ""),
        "path": str(item.get("path") or ""),
        "findings": sorted(rows, key=lambda row: (row["code"], row["location"], row["value"])),
    }


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


def _work_progress_fingerprint(workspace_root: Path, contract: dict[str, Any] | None = None) -> str:
    rows: list[dict[str, object]] = []
    for root in work_progress_roots(workspace_root, contract or {}):
        rows.extend(_work_progress_rows_for_root(root, workspace_root))
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _work_progress_rows_for_root(root: Path, workspace_root: Path) -> list[dict[str, object]]:
    if not root.exists():
        return [{"root": root.name, "exists": False}]
    if root.is_file():
        return [_file_progress_row(root, workspace_root)]
    rows = [_directory_progress_row(root, workspace_root)]
    rows.extend(_directory_progress_row(path, workspace_root) for path in sorted(root.rglob("*")) if path.is_dir())
    rows.extend(_file_progress_row(path, workspace_root) for path in sorted(root.rglob("*")) if path.is_file())
    return rows


def _directory_progress_row(path: Path, workspace_root: Path) -> dict[str, object]:
    return {
        "root": path.parts[-1] if path == workspace_root else (path.relative_to(workspace_root).parts[0]),
        "path": str(path.relative_to(workspace_root)).replace("\\", "/"),
        "kind": "dir",
        "signature": _dir_signature(path, workspace_root),
    }


def _file_progress_row(path: Path, workspace_root: Path) -> dict[str, object]:
    return {
        "root": path.relative_to(workspace_root).parts[0],
        "path": str(path.relative_to(workspace_root)).replace("\\", "/"),
        "kind": "file",
        "signature": _file_signature(path),
    }


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
