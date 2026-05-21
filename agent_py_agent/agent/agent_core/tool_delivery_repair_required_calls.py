# LLM: delivery repair required calls turn machine recovery actions into executable tool skeletons.
# 模块用途: 从 recovery_actions 生成 required_tool_calls，让模型看到下一步应调用的结构化工具参数。

from __future__ import annotations

import json


# LLM: required_tool_calls returns compact tool skeletons derived from recovery action fields only.
# 函数用途: 将 writer_tool/builder_tool、checkpoint/source/output refs 转成可执行工具调用模板。
def required_tool_calls(required_actions: list[dict[str, object]]) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for action in required_actions:
        if writer := _writer_call(action):
            calls.append(writer)
        if builder := _builder_call(action):
            calls.append(builder)
        calls.extend(_artifact_repair_calls(action))
    return calls


# LLM: _writer_call keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _writer_call(action: dict[str, object]) -> dict[str, object]:
    tool = str(action.get("writer_tool") or "").strip()
    path = str(action.get("checkpoint_ref") or "").strip()
    if not tool or not path:
        return {}
    call: dict[str, object] = {"tool": tool, "path": path}
    if hint := _json_hint(action):
        call["data"] = hint
    if str(action.get("recommended_action") or "") == "repair_evidence_refs":
        call["merge_existing"] = True
    return call


# LLM: _builder_call keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _builder_call(action: dict[str, object]) -> dict[str, object]:
    tool = str(action.get("builder_tool") or "").strip()
    output_ref = str(action.get("output_ref") or "").strip()
    source_ref = str(action.get("source_ref") or "").strip()
    if not tool or not output_ref:
        return {}
    call: dict[str, object] = {"tool": tool, "path": output_ref}
    if source_ref:
        call[_source_param_name(tool)] = source_ref
    return call


# LLM: _artifact_repair_calls exposes executable targets for failed artifact findings.
# 函数用途: 将 repair_targets/write_tools 变成模型可直接调用的补丁工具骨架，避免 required_tool_calls 为空。
def _artifact_repair_calls(action: dict[str, object]) -> list[dict[str, object]]:
    if str(action.get("recommended_action") or "") != "repair_artifact_against_findings":
        return []
    tool = _artifact_repair_tool(action)
    if not tool:
        return []
    targets = action.get("repair_targets")
    target_values = [str(item) for item in targets if str(item)] if isinstance(targets, list) else []
    if not target_values:
        artifact_path = str(action.get("artifact_path") or "").strip()
        target_values = [artifact_path] if artifact_path else []
    findings = action.get("finding_values")
    finding_values = [str(item) for item in findings if str(item)] if isinstance(findings, list) else []
    return [
        {"tool": tool, "path": target, "finding_values": finding_values[:20]}
        for target in target_values[:4]
    ]


# LLM: _artifact_repair_tool picks a deterministic patch-capable tool from the action manifest.
# 函数用途: 优先用 replace_in_file，其次 write_file/file_write_session，完全由 write_tools 结构化字段决定。
def _artifact_repair_tool(action: dict[str, object]) -> str:
    tools = action.get("write_tools")
    values = [str(item) for item in tools if str(item)] if isinstance(tools, list) else []
    for candidate in ("replace_in_file", "write_file", "file_write_session"):
        if candidate in values:
            return candidate
    return values[0] if values else ""


# LLM: _source_param_name keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _source_param_name(tool: str) -> str:
    return {
        "data_to_workbook": "source_json_path",
        "markdown_to_pdf": "source_markdown_path",
    }.get(tool, "source_path")


# LLM: _json_hint keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _json_hint(action: dict[str, object]) -> object:
    for key in ("evidence_shape_hint", "checkpoint_shape_hint"):
        if value := _parse_json_text(str(action.get(key) or "").strip()):
            return value
    return {}


# LLM: _parse_json_text keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _parse_json_text(text: str) -> object:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


__all__ = ["required_tool_calls"]
