
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...contracts.recovery import RecoveryAction
from .recovery_models import RecoveryActionLedger

_MAX_FINDING_VALUES_PER_ACTION = 64
_SAFE_REPAIR_SUFFIX_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,63}$")


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


def _artifact_validation_contract(item: dict[str, Any], contract: dict[str, Any]) -> dict[str, object]:
    artifact_id = str(item.get("artifact_id") or "").strip()
    for artifact in contract.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if artifact_id and artifact_id == str(artifact.get("artifact_id") or "").strip():
            return _validation_contract(artifact)
    return {}


def _validation_contract(artifact: dict[str, object]) -> dict[str, object]:
    value = artifact.get("validation_contract")
    return dict(value) if isinstance(value, dict) else {}


def _artifact_finding_repair_action(
    item: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, object]:
    return {
        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
        "category": "artifact",
        "retryable": True,
        "recommended_action": RecoveryAction.REPAIR_ARTIFACT_AGAINST_FINDINGS.value,
        "artifact_id": str(item.get("artifact_id") or ""),
        "artifact_kind": str(item.get("kind") or ""),
        "artifact_path": str(item.get("path") or ""),
        "finding_codes": _finding_values(findings, "code"),
        "finding_values": _finding_values(findings, "value"),
        "repair_targets": _repair_targets(item, findings),
        "write_tools": ["write_file", "apply_patch", "write_file"],
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
    for finding in findings:
        targets.extend(_finding_targets_for_one_finding(artifact_path, finding))
    return targets


def _finding_targets_for_one_finding(artifact_path: Path, finding: dict[str, Any]) -> list[Path]:
    targets: list[Path] = []
    for value in (str(finding.get("location") or ""), str(finding.get("value") or "")):
        target = _repair_target_for_ref(artifact_path, _file_ref_head(value), finding)
        if target is not None:
            targets.append(target)
    return targets


def _repair_target_for_ref(artifact_path: Path, head: str, finding: dict[str, Any]) -> Path | None:
    if not _is_repair_file_ref(artifact_path, head, finding):
        return None
    return _safe_artifact_related_path(artifact_path, head)


def _file_ref_head(value: str) -> str:
    head = value.split(":", 1)[0].split("#", 1)[0].strip()
    return head.replace("\\", "/")


def _existing_text_targets(artifact_path: Path) -> list[Path]:
    try:
        files = [
            path
            for path in artifact_path.rglob("*")
            if path.is_file() and _has_safe_repair_suffix(path)
        ]
    except OSError:
        return []
    return sorted(files, key=lambda path: (len(path.parts), str(path)))[:8]


def _is_repair_file_ref(artifact_path: Path, value: str, finding: dict[str, Any]) -> bool:
    if not value or not _has_safe_repair_suffix(Path(value)):
        return False
    if "/" in value or "\\" in value:
        return True
    target = _safe_artifact_related_path(artifact_path, value)
    if target and target.exists() and target.is_file():
        return True
    return _finding_declares_file_ref(finding)


def _finding_declares_file_ref(finding: dict[str, Any]) -> bool:
    code = str(finding.get("code") or "").upper()
    return "FILE" in code


def _has_safe_repair_suffix(path: Path) -> bool:
    return bool(path.name and _SAFE_REPAIR_SUFFIX_RE.fullmatch(path.suffix.lower()))


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
    workspace_ref = _workspace_relative_candidate(artifact_path, rel)
    if workspace_ref is not None:
        return workspace_ref
    for root in _candidate_repair_roots(artifact_path):
        try:
            candidate = (root / rel).resolve()
            candidate.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if candidate.exists():
            return candidate
    return _safe_artifact_child(artifact_path, rel)


def _workspace_relative_candidate(artifact_path: Path, rel: str) -> Path | None:
    first_part = Path(rel).parts[0] if Path(rel).parts else ""
    if not first_part:
        return None
    for root in _candidate_repair_roots(artifact_path):
        if candidate := _workspace_relative_candidate_from_root(root, rel, first_part):
            return candidate
    return None


def _workspace_relative_candidate_from_root(root: Path, rel: str, first_part: str) -> Path | None:
    try:
        existing_anchor = (root / first_part).resolve(strict=False)
        if not existing_anchor.exists() or not existing_anchor.is_dir():
            return None
        candidate = (root / rel).resolve(strict=False)
        candidate.relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return candidate


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
