# LLM: Tool context reducer controls what tool results enter the next live prompt.
# 模块用途: 大工具输出进 prompt 前先降成 artifact 摘要和恢复锚点，避免撑爆上下文。

from __future__ import annotations

"""Live prompt reducer for tool execution results."""

from ..tools import ToolExecutionResult


# LLM: render_tool_result_for_live_prompt is the ToolContextReducer boundary for one tool call.
# 函数用途: 根据归档记录决定下一轮 prompt 放完整工具结果还是外置 artifact 摘要。
def render_tool_result_for_live_prompt(result: ToolExecutionResult, archive_record: dict[str, object]) -> str:
    if not archive_record.get("output_externalized"):
        return result.render_for_prompt()
    status = "ok" if result.ok else "error"
    lines = [
        f"[tool={result.tool}; status={status}]",
        "完整工具输出已外置，live prompt 只保留摘要和恢复锚点。",
        f"- output_preview: {archive_record.get('output_preview', '')}",
        f"- output_path: {archive_record.get('output_path', '')}",
        f"- output_hash: {archive_record.get('output_hash', '')}",
        f"- output_size_bytes: {archive_record.get('output_size_bytes', 0)}",
    ]
    checkpoint = str(archive_record.get("fail_safe_checkpoint_path") or "")
    if checkpoint:
        lines.append(f"- fail_safe_checkpoint: {checkpoint}")
    return "\n".join(lines)
