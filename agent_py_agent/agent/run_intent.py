# LLM: Run intent is a soft path reminder, not a filesystem permission gate.
# 模块用途: 从明确产物合同和用户显式路径中提取本轮“写到哪/参考哪”的运行意图。

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_PATH_RE = re.compile(r"(~?/[^ \t\r\n，。；;：:、)）\]】\"'<>`]+)")
_MAX_FACT_FILES = 12


def build_run_intent(
    *,
    user_prompt: str,
    delivery_contract: dict[str, Any] | None,
    workspace_root: Path,
) -> dict[str, Any]:
    desired = desired_outputs_from_contract(delivery_contract)
    references = reference_roots_from_prompt(
        user_prompt,
        desired_outputs=desired,
        workspace_root=workspace_root,
    )
    return run_intent_payload(reference_roots=references, desired_outputs=desired)


def desired_outputs_from_contract(contract: dict[str, Any] | None) -> list[str]:
    if not isinstance(contract, dict):
        return []
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, list | tuple):
        return []
    outputs: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path = str(
            artifact.get("path")
            or artifact.get("preferred_path")
            or artifact.get("target_path")
            or artifact.get("output_path")
            or ""
        ).strip()
        if path:
            outputs.append(path)
    return _dedupe(outputs)


def reference_roots_from_prompt(
    user_prompt: str,
    *,
    desired_outputs: list[str],
    workspace_root: Path,
) -> list[str]:
    if not desired_outputs:
        return []
    desired_paths = [_resolve_path(path, workspace_root) for path in desired_outputs]
    roots: list[str] = []
    for raw in _PATH_RE.findall(user_prompt or ""):
        candidate = _resolve_path(raw, workspace_root)
        if not candidate.exists() or not candidate.is_dir():
            continue
        if any(_same_or_related(candidate, output) for output in desired_paths):
            continue
        roots.append(str(candidate))
    return _dedupe(roots)


def run_intent_payload(*, reference_roots: list[str], desired_outputs: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "run_intent.v1",
        "soft_only": True,
        "reference_roots": _field_payload(reference_roots),
        "desired_outputs": _field_payload(desired_outputs),
    }


def latest_run_intent(workspace_root: Path) -> dict[str, Any]:
    facts_dir = workspace_root / "memory_archive" / "runtime_facts"
    if not facts_dir.exists():
        return {}
    candidates = sorted(
        (path for path in facts_dir.glob("*/task.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[:_MAX_FACT_FILES]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        intent = payload.get("run_intent") if isinstance(payload, dict) else None
        if isinstance(intent, dict) and _items(intent.get("desired_outputs")):
            return intent
    return {}


def reference_write_feedback(*, workspace_root: Path, target: Path) -> dict[str, Any]:
    intent = latest_run_intent(workspace_root)
    desired = _items(intent.get("desired_outputs"))
    references = _items(intent.get("reference_roots"))
    if not desired or not references:
        return {}
    target = target.resolve(strict=False)
    desired_paths = [_resolve_path(path, workspace_root) for path in desired]
    if any(_same_or_related(target, output) for output in desired_paths):
        return {}
    reference_paths = [_resolve_path(path, workspace_root) for path in references]
    if not any(_is_relative_to(target, root) for root in reference_paths):
        return {}
    return {
        "severity": "soft",
        "blocking": False,
        "message": (
            "软提醒：这个写入路径看起来位于本轮参考目录内，不是明确的目标产物路径。"
            f"如果你正在写最终产物，请写到 desired_outputs：{'; '.join(desired[:5])}"
        ),
        "desired_outputs": desired[:8],
        "reference_roots": references[:8],
    }


def _field_payload(items: list[str]) -> dict[str, Any]:
    return {
        "items": _dedupe(items),
        "source_status": "recorded" if items else "not_recorded",
    }


def _items(value: Any) -> list[str]:
    payload = value if isinstance(value, dict) else {}
    raw = payload.get("items")
    if not isinstance(raw, list | tuple):
        return []
    return _dedupe([str(item).strip() for item in raw if str(item).strip()])


def _resolve_path(path: str, workspace_root: Path) -> Path:
    raw = Path(str(path).strip()).expanduser()
    if not raw.is_absolute():
        raw = workspace_root / raw
    return raw.resolve(strict=False)


def _same_or_related(path: Path, target: Path) -> bool:
    return path == target or _is_relative_to(path, target) or _is_relative_to(target, path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


__all__ = [
    "build_run_intent",
    "desired_outputs_from_contract",
    "latest_run_intent",
    "reference_roots_from_prompt",
    "reference_write_feedback",
    "run_intent_payload",
]
