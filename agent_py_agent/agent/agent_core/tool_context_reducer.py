# LLM: Tool context reducer controls what tool results enter the next live prompt.
# 模块用途: 大工具输出进 prompt 前先降成 artifact 摘要和恢复锚点，避免撑爆上下文。

from __future__ import annotations

"""Live prompt reducer for tool execution results."""

import json

from ..tools import ToolExecutionResult


# LLM: render_tool_result_for_live_prompt is the ToolContextReducer boundary for one tool call.
# 函数用途: 根据归档记录决定下一轮 prompt 放完整工具结果还是外置 artifact 摘要。
def render_tool_result_for_live_prompt(result: ToolExecutionResult, archive_record: dict[str, object]) -> str:
    if not archive_record.get("output_externalized"):
        return result.render_for_prompt()
    status = "ok" if result.ok else "error"
    artifact_ref = str(archive_record.get("artifact_ref") or archive_record.get("output_path") or "")
    call_id = str(archive_record.get("id") or "")
    lines = [
        f"[tool={result.tool}; status={status}]",
        "完整工具输出已外置，live prompt 只保留摘要和恢复锚点。",
        f"- output_preview: {archive_record.get('output_preview', '')}",
        f"- output_path: {archive_record.get('output_path', '')}",
        f"- output_artifact_ref: {artifact_ref}",
        f"- output_call_id: {call_id}",
        f"- output_scoped_call_id: {archive_record.get('scoped_call_id', '')}",
        f"- read_artifact_hint: {_read_artifact_hint(artifact_ref or call_id, archive_record)}",
        f"- output_hash: {archive_record.get('output_hash', '')}",
        f"- output_size_bytes: {archive_record.get('output_size_bytes', 0)}",
    ]
    checkpoint = str(archive_record.get("fail_safe_checkpoint_path") or "")
    if checkpoint:
        lines.append(f"- fail_safe_checkpoint: {checkpoint}")
    return "\n".join(lines)


# LLM: _read_artifact_hint gives the next model turn a short stable ref instead of a long absolute path.
# 函数用途: 生成可直接复制的 read_artifact 示例；优先用 call_id，减少长路径抄错。
# LLM: _read_artifact_hint favors concrete/scoped refs so same call ids cannot cross runs.
# 函数用途: 给下一轮模型一个可复制的 read_artifact 示例；包含 run/task/request 作用域，避免 `17-1` 串到旧任务。
def _read_artifact_hint(ref: str, archive_record: dict[str, object]) -> str:
    payload = {"tool": "read_artifact", "artifact_ref": ref, "offset": 0, "max_chars": 4000}
    for key in ("run_id", "task_id", "request_id"):
        value = str(archive_record.get(key) or "")
        if value:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)
