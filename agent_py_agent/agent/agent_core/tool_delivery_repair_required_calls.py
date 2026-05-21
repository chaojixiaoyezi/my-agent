# LLM: delivery repair required calls turn machine recovery actions into executable tool skeletons.
# 模块用途: 从 recovery_actions 生成 required_tool_calls，让模型看到下一步应调用的结构化工具参数。

from __future__ import annotations

import json

_CREATE_OR_REPLACE_FINDING_CODES = frozenset(
    {
        "ARTIFACT_MISSING",
        "STATIC_SITE_MISSING_REQUIRED_FILES",
    }
)
_FULL_REWRITE_FINDING_CODES = frozenset(
    {
        "STATIC_SITE_FORM_BINDING_HITS",
        "STATIC_SITE_HTML_STRUCTURE_HITS",
        "STATIC_SITE_INERT_CONTROL_HITS",
        "STATIC_SITE_MISSING_DOM_ID_HITS",
        "STATIC_SITE_MISSING_JS_API_HITS",
    }
)
_MAX_REPAIR_FINDING_VALUES = 64


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
    intent = _artifact_repair_intent(action)
    tool = _artifact_repair_tool(action, intent=intent)
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
        {
            "tool": tool,
            "path": target,
            "finding_values": finding_values[:_MAX_REPAIR_FINDING_VALUES],
            "mutation_intent": intent,
        }
        for target in target_values[:4]
    ]


# LLM: _artifact_repair_tool picks a deterministic patch-capable tool from the action manifest.
# 函数用途: 根据结构化 finding code 选择局部替换、创建或整文件重写工具，不读验收文案。
def _artifact_repair_tool(action: dict[str, object], *, intent: str) -> str:
    tools = action.get("write_tools")
    values = [str(item) for item in tools if str(item)] if isinstance(tools, list) else []
    if intent in {"create_or_replace", "rewrite"}:
        for candidate in ("write_file", "file_write_session"):
            if candidate in values:
                return candidate
    for candidate in ("replace_in_file", "write_file", "file_write_session"):
        if candidate in values:
            return candidate
    return values[0] if values else ""


# LLM: _artifact_repair_intent classifies the mutation shape from stable finding codes.
# 函数用途: 缺文件要创建，HTML 结构损坏要整文件重写，其他问题默认局部修补。
def _artifact_repair_intent(action: dict[str, object]) -> str:
    raw_codes = action.get("finding_codes")
    codes = {str(code) for code in raw_codes if str(code)} if isinstance(raw_codes, list) else set()
    if codes & _CREATE_OR_REPLACE_FINDING_CODES:
        return "create_or_replace"
    if codes & _FULL_REWRITE_FINDING_CODES:
        return "rewrite"
    return "patch"


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
