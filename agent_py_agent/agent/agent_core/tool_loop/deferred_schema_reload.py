# LLM: 只读工具归档里的结构化事实（ok、tool_execution.failure_stage）和本快照的可见性，不解析模型或工具输出正文；
#   唯一副作用是把工具名加进 params.loaded_tool_names（与 tool_search 同一个“下一次模型调用临时可见”机制，用一次即清）。
#   只在 tool_default_deferral_enabled 打开时生效，开关关闭时行为逐字不变。改动同步 test_tool_default_deferral 与其真实链路用例。
# 模块用途: 模型没先 tool_search 就直接调用收起的工具、在参数校验阶段失败时，把这个工具的完整参数定义加到下一次请求，
#   让模型下一步照定义重试，而不是在没有定义的情况下反复猜参数。
from __future__ import annotations

from ...contracts.error_taxonomy import error_contract
from ...contracts.recovery import RecoveryAction
from ...tooling.models import ToolFailureStage


# LLM: 条件全部来自结构化字段：开关、调用失败、参数出错（失败阶段是 validation，或错误码的错误合同推荐动作是修参数——工具在
#   执行阶段自己判参数无效也算）、工具在本快照里、且下一次请求本来不会带它的定义（显式 allowed_tools 的回合全量可见，自然不触发）。
#   命中时改 params.loaded_tool_names 并返回一行宿主提示，否则返回空串。
# 函数用途: 收起的工具被“盲调”且参数校验失败时，让下一次请求带上它的完整定义，并给模型一句提示。
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
    if tool in loaded or snapshot.runtime(tool) is None:
        return ""
    visible = registry.model_visible_specs(
        allowed_tools=getattr(params, "allowed_tools", None), loaded_tool_names=loaded, runtime_snapshot=snapshot,
    )
    if any(spec.name == tool for spec in visible):
        return ""
    loaded.add(tool)
    return f"\n[tool-schema-loaded] {tool} 的完整参数定义已随下一次请求提供，请按定义重新调用。"


# LLM: 只认结构化事实：宿主 Schema 校验阶段失败，或错误码在错误合同里登记的推荐动作是修参数（未知码按 UNKNOWN_ERROR 不算）。
# 函数用途: 判断这次失败是不是“参数给错了”，是的话才值得把完整定义带给下一次请求。
def _argument_failure(stage: object, error_code: object) -> bool:
    if stage == ToolFailureStage.VALIDATION.value:
        return True
    code = str(error_code or "").strip()
    return bool(code) and error_contract(code).recommended_action == RecoveryAction.REPAIR_TOOL_ARGUMENTS.value


__all__ = ["reload_schema_after_blind_call"]
