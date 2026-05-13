# LLM: Keep file-read artifact guard out of filesystem tools so the read module stays small.
# 模块用途: 判断普通 read_file 是否误读外置 tool-output artifact 包装文件，并返回改用 read_artifact 的提示。

from __future__ import annotations

from pathlib import Path

_TOOL_OUTPUT_ARTIFACT_PARTS = ("memory_archive", "artifacts", "tool_outputs")


# LLM: is_tool_output_artifact_path detects wrapper paths even before workspace-prefix validation succeeds.
# 函数用途: 判断路径文本是否指向 tool_outputs 下的 JSON artifact 包装文件；可用于拼错前缀时的恢复提示。
def is_tool_output_artifact_path(target: Path) -> bool:
    return target.suffix.lower() == ".json" and _path_has_parts(target, _TOOL_OUTPUT_ARTIFACT_PARTS)


# LLM: tool_output_artifact_typo_hint routes typo-repaired wrapper paths to the artifact reader.
# 函数用途: 当模型把 tool-output artifact 路径前缀抄错时，返回 read_artifact 恢复提示而不是 read_file 重试提示。
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
        f" 不要用 read_file 或 suggested_target 重试，请改用 read_artifact，artifact_ref={suggested_path.name}，"
        "offset=0，max_chars=4000；需要更多内容再分页读取。"
        + _read_artifact_permission_hint(allowed_tools)
    )


# LLM: tool_output_artifact_read_hint blocks accidental prompt-flood reads of externalized output wrappers.
# 函数用途: 判断 read_file 目标是否是 tool_outputs 下的 artifact JSON；若是则要求使用 read_artifact 切片读取。
def tool_output_artifact_read_hint(target: Path, roots: list[Path], allowed_tools: list[str] | None = None) -> str:
    if not is_tool_output_artifact_path(target):
        return ""
    for root in roots:
        artifact_root = root / Path(*_TOOL_OUTPUT_ARTIFACT_PARTS)
        try:
            target.relative_to(artifact_root.resolve(strict=False))
            return (
                "这是已外置的工具输出 artifact JSON 包装文件，不能用 read_file 直接读取。"
                "请改用 read_artifact，并传 artifact_ref 为该路径或 call_id，再设置 max_chars 分片读取。"
                + _read_artifact_permission_hint(allowed_tools)
            )
        except ValueError:
            continue
    return ""


# LLM: _path_has_parts keeps artifact-wrapper detection independent of absolute workspace prefixes.
# 函数用途: 按路径片段判断是否包含 memory_archive/artifacts/tool_outputs 目录链。
def _path_has_parts(path: Path, parts: tuple[str, ...]) -> bool:
    values = path.parts
    size = len(parts)
    return any(tuple(values[index:index + size]) == parts for index in range(0, len(values) - size + 1))


# LLM: _read_artifact_permission_hint adapts wrapper recovery advice to the current tool catalog.
# 函数用途: 如果当前上下文没有授权 read_artifact，提示上报 capability_request，而不是让子代理继续空转。
def _read_artifact_permission_hint(allowed_tools: list[str] | None) -> str:
    if allowed_tools is None:
        return ""
    allowed = {str(item) for item in allowed_tools if str(item).strip()}
    if "read_artifact" in allowed:
        return " 当前上下文已授权 read_artifact。"
    return " 当前执行上下文未授权 read_artifact；请向父级上报 capability_request，请求 artifact 读取能力。"


# LLM: allowed_tools_hint_param extracts registry-injected authorization context for recovery hints only.
# 函数用途: 从 read_file 内部参数里读取当前 allowed_tools；不参与实际授权判断，只用于给模型更准确的下一步。
def allowed_tools_hint_param(params: dict[str, object]) -> list[str] | None:
    value = params.get("__allowed_tools")
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []
