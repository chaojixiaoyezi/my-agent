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
    return calls


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


def _source_param_name(tool: str) -> str:
    return {
        "data_to_workbook": "source_json_path",
        "markdown_to_pdf": "source_markdown_path",
    }.get(tool, "source_path")


def _json_hint(action: dict[str, object]) -> object:
    for key in ("evidence_shape_hint", "checkpoint_shape_hint"):
        if value := _parse_json_text(str(action.get(key) or "").strip()):
            return value
    return {}


def _parse_json_text(text: str) -> object:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


__all__ = ["required_tool_calls"]
