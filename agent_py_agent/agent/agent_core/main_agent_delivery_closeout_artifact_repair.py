# LLM: Artifact finding repair helpers turn validator facts into write-first recovery actions.
# 模块用途: 根据任意产物验收 findings 生成通用 repair action 和 bounded repair_targets，不从自然语言报告猜路径。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .main_agent_delivery_closeout_recovery_models import RecoveryActionLedger

_REPAIR_TARGET_SUFFIXES = {".css", ".html", ".htm", ".js", ".json", ".md", ".txt", ".yaml", ".yml"}
_MAX_FINDING_VALUES_PER_ACTION = 64


def append_artifact_finding_repair_actions(report: dict[str, Any], ledger: RecoveryActionLedger) -> None:
    for item in report.get("artifacts", []):
        if not isinstance(item, dict) or item.get("ok"):
            continue
        findings = list(artifact_findings(item))
        if not findings:
            continue
        action_key = f"ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED:{item.get('artifact_id') or item.get('path')}"
        if action_key in ledger.seen:
            continue
        ledger.seen.add(action_key)
        ledger.actions.append(_artifact_finding_repair_action(item, findings))


def failed_findings(report: dict[str, Any]):
    for item in report.get("artifacts", []):
        if item.get("ok"):
            continue
        yield from artifact_findings(item)


def artifact_findings(item: dict[str, Any]):
    findings = item.get("acceptance_report", {}).get("findings", [])
    yield from (finding for finding in findings if isinstance(finding, dict))


def _artifact_finding_repair_action(
    item: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, object]:
    return {
        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
        "category": "artifact",
        "retryable": True,
        "recommended_action": "repair_artifact_against_findings",
        "artifact_id": str(item.get("artifact_id") or ""),
        "artifact_kind": str(item.get("kind") or ""),
        "artifact_path": str(item.get("path") or ""),
        "finding_codes": _finding_values(findings, "code"),
        "finding_values": _finding_values(findings, "value"),
        "repair_targets": _repair_targets(item, findings),
        "write_tools": ["write_file", "replace_in_file", "file_write_session"],
        "recovery_hint": "产物验收已给出结构化 findings；优先修改对应产物文件，然后重新验收。",
    }


def _finding_values(findings: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    for finding in findings:
        value = str(finding.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return values[:_MAX_FINDING_VALUES_PER_ACTION]


def _repair_targets(item: dict[str, Any], findings: list[dict[str, Any]]) -> list[str]:
    artifact_path = Path(str(item.get("path") or "")).expanduser()
    candidates = _finding_file_targets(artifact_path, findings)
    if artifact_path.is_file():
        candidates.append(artifact_path)
    elif artifact_path.is_dir():
        candidates.extend(_existing_text_targets(artifact_path))
    return _unique_paths(candidates)[:8]


def _finding_file_targets(artifact_path: Path, findings: list[dict[str, Any]]) -> list[Path]:
    targets: list[Path] = []
    for value in [*_finding_values(findings, "location"), *_finding_values(findings, "value")]:
        head = _file_ref_head(value)
        if not head or Path(head).suffix.lower() not in _REPAIR_TARGET_SUFFIXES:
            continue
        target = _safe_artifact_related_path(artifact_path, head)
        if target is not None:
            targets.append(target)
    return targets


def _file_ref_head(value: str) -> str:
    head = value.split(":", 1)[0].split("#", 1)[0].strip()
    return head.replace("\\", "/")


def _existing_text_targets(artifact_path: Path) -> list[Path]:
    try:
        files = [
            path
            for path in artifact_path.rglob("*")
            if path.is_file() and path.suffix.lower() in _REPAIR_TARGET_SUFFIXES
        ]
    except OSError:
        return []
    return sorted(files, key=lambda path: (len(path.parts), str(path)))[:8]


def _safe_artifact_child(artifact_path: Path, rel: str) -> Path | None:
    if Path(rel).is_absolute():
        return None
    root = artifact_path if artifact_path.is_dir() else artifact_path.parent
    try:
        candidate = (root / rel).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _safe_artifact_related_path(artifact_path: Path, rel: str) -> Path | None:
    if Path(rel).is_absolute() or _has_parent_ref(rel):
        return None
    for root in _candidate_repair_roots(artifact_path):
        try:
            candidate = (root / rel).resolve()
            candidate.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if candidate.exists():
            return candidate
    return _safe_artifact_child(artifact_path, rel)


def _candidate_repair_roots(artifact_path: Path) -> list[Path]:
    start = artifact_path if artifact_path.is_dir() else artifact_path.parent
    roots = [start, *list(start.parents)]
    return roots[:8]


def _has_parent_ref(rel: str) -> bool:
    return any(part == ".." for part in Path(rel).parts)


def _unique_paths(paths: list[Path]) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for path in paths:
        value = str(path)
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


__all__ = [
    "append_artifact_finding_repair_actions",
    "artifact_findings",
    "failed_findings",
]
