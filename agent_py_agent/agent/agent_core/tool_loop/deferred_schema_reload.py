# LLM: 只读工具归档里的结构化事实（ok、tool_execution.failure_stage）和本快照的可见性，不解析模型或工具输出正文；
#   副作用仅为本轮加载名和schema_reload展示事实；线程账沿原post-archive写口，不阻断原结果归档。
#   只在 tool_default_deferral_enabled 打开时生效，开关关闭时行为逐字不变。改动同步 test_tool_default_deferral 与其真实链路用例。
# 模块用途: 模型没先 tool_search 就直接调用收起的工具、在参数校验阶段失败时，把这个工具的完整参数定义加到下一次请求，
#   让模型下一步照定义重试，而不是在没有定义的情况下反复猜参数。
from __future__ import annotations

import json

from ...contracts.error_taxonomy import error_contract
from ...contracts.recovery import RecoveryAction
from ...conversation.tool_discovery import (
    build_discovered_tool_micro_schemas,
)
from ...tooling.models import ToolFailureStage


# LLM: 条件全部来自结构化字段：开关、调用失败、参数出错（失败阶段是 validation，或错误码的错误合同推荐动作是修参数——工具在
#   执行阶段自己判参数无效也算）、工具在本快照里、且下一次请求本来不会带它的定义（显式 allowed_tools 的回合全量可见，自然不触发）。
#   当前params和snapshot允许集合取交集；命中时兼容list/set保序补名，持久化延至原归档边界。
# 函数用途: 盲调参数失败后直接补下轮完整定义，同时在结果尾部放准确参数速查，省掉额外搜索。
def reload_schema_after_blind_call(agent: object, params: object, archive_record: dict[str, object]) -> str:
    registry = getattr(agent, "tools", None)
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    if snapshot is None or getattr(registry, "catalog_default_deferral_enabled", False) is not True:
        return ""
    tool = str(archive_record.get("tool") or "").strip()
    execution = archive_record.get("tool_execution")
    stage = execution.get("failure_stage") if isinstance(execution, dict) else ""
    if not tool or archive_record.get("ok") is not False or not _argument_failure(stage, archive_record.get("error_code")):
        return ""
    loaded = params.loaded_tool_names
    runtime = snapshot.runtime(tool)
    allowed = getattr(params, "allowed_tools", None)
    snapshot_allowed = getattr(snapshot, "allowed_tools", None)
    if (tool in loaded or runtime is None or (allowed is not None and tool not in allowed)
            or (snapshot_allowed is not None and tool not in snapshot_allowed)
            or getattr(getattr(runtime, "availability", None), "available", True) is not True
            or getattr(getattr(runtime, "exposure", None), "model_visible", True) is not True):
        return ""
    visible = registry.model_visible_specs(
        allowed_tools=getattr(params, "allowed_tools", None), loaded_tool_names=loaded, runtime_snapshot=snapshot,
    )
    if any(spec.name == tool for spec in visible):
        return ""
    if isinstance(loaded, list):
        loaded.append(tool)
    else:
        loaded.add(tool)
    state = getattr(params, "live_archive_state", None)
    if isinstance(state, dict):
        order = list(state.get("loaded_tool_order") or ())
        if tool not in order:
            order.append(tool)
        state["loaded_tool_order"] = tuple(order)
    hint = f"\n[tool-schema-loaded] {tool} 的完整参数定义已随下一次请求提供，请按定义重新调用。"
    schemas = build_discovered_tool_micro_schemas(snapshot, [tool], max_tools=1)
    if schemas:
        details = {"loaded_micro_schema": schemas[0], "schema_auto_activated_for_next_turn": True}
        archive_record["schema_reload"] = details
        hint += "\n" + json.dumps(details, ensure_ascii=False, separators=(",", ":"))
    return hint


# LLM: 只认结构化事实：宿主 Schema 校验阶段失败，或错误码在错误合同里登记的推荐动作是修参数（未知码按 UNKNOWN_ERROR 不算）。
# 函数用途: 判断这次失败是不是“参数给错了”，是的话才值得把完整定义带给下一次请求。
def _argument_failure(stage: object, error_code: object) -> bool:
    if stage == ToolFailureStage.VALIDATION.value:
        return True
    code = str(error_code or "").strip()
    return bool(code) and error_contract(code).recommended_action == RecoveryAction.REPAIR_TOOL_ARGUMENTS.value


__all__ = ["reload_schema_after_blind_call"]
