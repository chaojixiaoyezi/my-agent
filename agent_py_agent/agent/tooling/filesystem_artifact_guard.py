# LLM: Keep file-read artifact guard out of filesystem tools so the read module stays small.
# 模块用途: 识别外置 tool-output artifact 包装文件，让普通 read_file 可读取其中正文。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TOOL_OUTPUT_ARTIFACT_PARTS = ("memory_archive", "artifacts", "tool_outputs")


# LLM: is_tool_output_artifact_path detects wrapper paths even before workspace-prefix validation succeeds.
# 函数用途: 判断路径文本是否指向 tool_outputs 下的 JSON artifact 包装文件；可用于拼错前缀时的恢复提示。
def is_tool_output_artifact_path(target: Path) -> bool:
    return target.suffix.lower() == ".json" and _path_has_parts(target, _TOOL_OUTPUT_ARTIFACT_PARTS)


# LLM: tool_output_artifact_typo_hint keeps typo recovery on the normal read_file path.
# 函数用途: 当模型把 tool-output artifact 路径前缀抄错时，提示用 suggested_target 继续 read_file。
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
        " 这是路径拼写错误，不是权限缺口；但目标是已外置的 tool-output artifact JSON 包装文件。"
        " 请继续用 read_file 读取 suggested_target；read_file 会读取 artifact 正文并按行分页。"
    )


# LLM: tool_output_artifact_content returns the body of a trusted wrapper without requiring index lookup.
# 函数用途: 如果 read_file 目标是 tool-output artifact 包装文件，读取其中 content 字段；普通文件返回空字符串。
def tool_output_artifact_content(target: Path, roots: list[Path]) -> str:
    if not is_tool_output_artifact_path(target):
        return ""
    for root in roots:
        artifact_root = root / Path(*_TOOL_OUTPUT_ARTIFACT_PARTS)
        try:
            target.relative_to(artifact_root.resolve(strict=False))
        except ValueError:
            continue
        return _tool_output_content_from_json(target)
    return ""


# LLM: _path_has_parts keeps artifact-wrapper detection independent of absolute workspace prefixes.
# 函数用途: 按路径片段判断是否包含 memory_archive/artifacts/tool_outputs 目录链。
def _path_has_parts(path: Path, parts: tuple[str, ...]) -> bool:
    values = path.parts
    size = len(parts)
    return any(tuple(values[index:index + size]) == parts for index in range(0, len(values) - size + 1))


# LLM: _tool_output_content_from_json is tolerant; invalid wrappers fall back to normal file reads.
# 函数用途: 解析 tool-output artifact 包装 JSON，只有 kind/content 形状正确时才返回正文。
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


# LLM: allowed_tools_hint_param extracts registry-injected authorization context for recovery hints only.
# 函数用途: 从 read_file 内部参数里读取当前 allowed_tools；不参与实际授权判断，只用于给模型更准确的下一步。
def allowed_tools_hint_param(params: dict[str, object]) -> list[str] | None:
    value = params.get("__allowed_tools")
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []
