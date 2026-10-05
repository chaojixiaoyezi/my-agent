
from __future__ import annotations

import json
from pathlib import Path

from ..common.logical_reference_ids import is_logical_reference_segment
from .models import ToolHandlerOutcome

_BLOB_TOOL_OUTPUT_ARTIFACT_PARTS = ("blobs", "tool_outputs")


# LLM: This typed exception preserves a cross-tool recovery decision through generic path parsing.
# 类用途: 标记目标是工具输出包装文件，调用方应改走 read_artifact 而非继续猜路径。
class ToolOutputArtifactRedirectError(ValueError):
    """Signal that a filesystem path must be consumed through read_artifact."""


def is_tool_output_artifact_path(target: Path) -> bool:
    """Return whether a path is inside the canonical tool-output artifact tree.

    Both memory externalization wrappers (JSON) and resilient large-result
    archives (plain text) live below this structural path. Trust follows that
    provenance, not a filename extension.
    """

    return _path_has_parts(target, _BLOB_TOOL_OUTPUT_ARTIFACT_PARTS)


# LLM: A typo that structurally targets the tool-output tree is a tool-surface redirect, not a
# generic missing path; callers must preserve its dedicated error code.
# 函数用途: 识别错误前缀下的工具输出包装路径，并生成 read_artifact 的恢复信息。
def tool_output_artifact_typo_hint(
    raw_path: str,
    _workspace_root: Path,
    suggested: str,
    _allowed_tools: list[str] | None = None,
) -> str:
    suggested_path = Path(suggested)
    if not is_tool_output_artifact_path(suggested_path):
        return ""
    return tool_output_artifact_read_redirect(
        raw_path,
        suggested_path,
        suspected_path_typo=True,
    )


# LLM: Tool-output wrappers have one model-facing reader. Return a logical basename ref so an
# incorrect absolute prefix cannot be recursively rebased into the current task directory.
# 函数用途: 把普通文件读取重定向到 read_artifact，并给出可直接照抄的结构化调用。
def tool_output_artifact_read_redirect(
    raw_path: str,
    candidate: Path,
    *,
    suspected_path_typo: bool = False,
) -> str:
    artifact_ref = candidate.name or Path(raw_path).name
    payload = {
        "ok": False,
        "error": "TOOL_OUTPUT_REQUIRES_READ_ARTIFACT",
        "requested_path": raw_path,
        "suspected_path_typo": suspected_path_typo,
        "recovery_tool": "read_artifact",
        "artifact_ref": artifact_ref,
        "retry_same_tool": False,
        "suggested_tool_call": {
            "tool": "read_artifact",
            "artifact_ref": artifact_ref,
            "offset": 0,
            "max_chars": 4000,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _path_has_parts(path: Path, parts: tuple[str, ...]) -> bool:
    values = path.parts
    size = len(parts)
    return any(tuple(values[index:index + size]) == parts for index in range(0, len(values) - size + 1))


def mark_tool_output_artifact_result(result: ToolHandlerOutcome) -> ToolHandlerOutcome:
    """Tighten one filesystem result that exposes archived tool output."""

    policy = result.result_envelope.setdefault("tool_output_policy", {})
    if not isinstance(policy, dict):
        policy = {}
        result.result_envelope["tool_output_policy"] = policy
    policy.update({"trust": "external_data", "redaction": "default"})
    return result


# LLM: 相对写路径里出现"编号 + 冒号 + call 编号"的段，说明模型把归档的逻辑引用（scoped_call_id）
#   当成了目录名；写工具必须在任何解析/落盘之前拒绝，否则会在 cwd（可能是 owner home 根）下
#   创建垃圾目录。绝对路径不受影响，blobs/tool_outputs 的既有重定向保持原样。
# 函数用途: 返回逻辑引用误当写路径时的结构化拒绝消息；没有命中返回空串。
def logical_reference_write_path_error(raw_path: str | Path) -> str:
    raw = str(raw_path or "")
    if not raw or Path(raw).is_absolute():
        return ""
    for segment in Path(raw).parts:
        if is_logical_reference_segment(segment):
            return (
                f"写入被阻止: 路径段 {segment} 是逻辑引用编号（工具输出/会话编号），不是文件或目录。"
                "读取该工具输出请用 read_artifact（artifact_ref 照抄工具结果里的 output_scoped_call_id）；"
                "写产物请写到任务工作区（例如 tasks/<日期>/<任务>/ 下的相对路径）。"
            )
    return ""
