# LLM: Keep file-read artifact guard out of filesystem tools so the read module stays small.
# 模块用途: 判断普通 read_file 是否误读外置 tool-output artifact 包装文件，并返回改用 read_artifact 的提示。

from __future__ import annotations

from pathlib import Path

_TOOL_OUTPUT_ARTIFACT_PARTS = ("memory_archive", "artifacts", "tool_outputs")


# LLM: tool_output_artifact_read_hint blocks accidental prompt-flood reads of externalized output wrappers.
# 函数用途: 判断 read_file 目标是否是 tool_outputs 下的 artifact JSON；若是则要求使用 read_artifact 切片读取。
def tool_output_artifact_read_hint(target: Path, roots: list[Path]) -> str:
    if target.suffix.lower() != ".json" or not _path_has_parts(target, _TOOL_OUTPUT_ARTIFACT_PARTS):
        return ""
    for root in roots:
        artifact_root = root / Path(*_TOOL_OUTPUT_ARTIFACT_PARTS)
        try:
            target.relative_to(artifact_root.resolve(strict=False))
            return (
                "这是已外置的工具输出 artifact JSON 包装文件，不能用 read_file 直接读取。"
                "请改用 read_artifact，并传 artifact_ref 为该路径或 call_id，再设置 max_chars 分片读取。"
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
