
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ToolHandlerOutcome

_BLOB_TOOL_OUTPUT_ARTIFACT_PARTS = ("blobs", "tool_outputs")


def is_tool_output_artifact_path(target: Path) -> bool:
    """Return whether a path is inside the canonical tool-output artifact tree.

    Both memory externalization wrappers (JSON) and resilient large-result
    archives (plain text) live below this structural path. Trust follows that
    provenance, not a filename extension.
    """

    return _path_has_parts(target, _BLOB_TOOL_OUTPUT_ARTIFACT_PARTS)


def tool_output_artifact_typo_hint(
    raw_path: str,
    workspace_root: Path,
    suggested: str,
    allowed_tools: list[str] | None = None,
) -> str:
    suggested_path = Path(suggested)
    if not is_tool_output_artifact_path(suggested_path):
        return ""
    return (
        "路径疑似拼写错误，已拒绝访问。"
        f" suspected_path_typo=true target={raw_path} workspace_root={workspace_root}"
        f" suggested_target={suggested}。"
        " 这是路径拼写错误，不是权限缺口；但目标是已外置的 tool-output artifact 文件。"
        " 请继续用 read_file 读取 suggested_target；read_file 会读取 artifact 正文并按行分页，"
        "同时保留其外部工具来源边界。"
    )


def tool_output_artifact_content(target: Path, roots: list[Path]) -> str:
    if target.suffix.lower() != ".json" or not is_tool_output_artifact_path(target):
        return ""
    for root in roots:
        artifact_root = root / Path(*_BLOB_TOOL_OUTPUT_ARTIFACT_PARTS)
        try:
            target.relative_to(artifact_root.resolve(strict=False))
        except ValueError:
            continue
        return _tool_output_content_from_json(target)
    return ""


def _path_has_parts(path: Path, parts: tuple[str, ...]) -> bool:
    values = path.parts
    size = len(parts)
    return any(tuple(values[index:index + size]) == parts for index in range(0, len(values) - size + 1))


def _tool_output_content_from_json(target: Path) -> str:
    try:
        payload: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("kind") != "tool_output":
        return ""
    content = payload.get("content")
    return content if isinstance(content, str) else ""


def mark_tool_output_artifact_result(result: ToolHandlerOutcome) -> ToolHandlerOutcome:
    """Tighten one filesystem result that exposes archived tool output."""

    policy = result.result_envelope.setdefault("tool_output_policy", {})
    if not isinstance(policy, dict):
        policy = {}
        result.result_envelope["tool_output_policy"] = policy
    policy.update({"trust": "external_data", "redaction": "default"})
    return result


def allowed_tools_hint_param(params: dict[str, object]) -> list[str] | None:
    value = params.get("__allowed_tools")
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []
