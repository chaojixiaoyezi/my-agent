# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Field normalization helpers for subagent manager data models.

新手说明:
这个模块从 manager_base.py 中提取出来，负责把外部传入的
松散字典/列表数据规范化成强类型的数据模型实例。主要处理
QualityContract、ContextManifest 和 context_packs。
"""

from __future__ import annotations

from dataclasses import fields


# LLM: _field_names 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理字段names相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


# LLM: _list_value 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 读取或查询value需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


# LLM: _string_list_value 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理stringlistvalue相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _string_list_value(value: object) -> list[str]:
    return [str(item) for item in _list_value(value) if item not in (None, "")]


# LLM: _normalize_quality_contract 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化qualitycontract的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_quality_contract(value: object) -> QualityContract:
    from .models import QualityContract

    if isinstance(value, QualityContract):
        return value
    if not isinstance(value, dict):
        return QualityContract()
    payload = {key: value[key] for key in _field_names(QualityContract) if key in value}
    for key in [
        "failure_conditions",
        "forbidden_delivery",
        "must_check",
        "sampling_plan",
        "evidence_required",
        "allowed_degradation",
    ]:
        payload[key] = _string_list_value(payload.get(key))
    payload["cannot_self_accept"] = bool(payload.get("cannot_self_accept", True))
    payload["parent_final_gate"] = bool(payload.get("parent_final_gate", True))
    return QualityContract(**payload)


# LLM: _normalize_context_manifest 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化上下文manifest的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_context_manifest(value: object) -> ContextManifest:
    from .models import ContextManifest

    if isinstance(value, ContextManifest):
        return value
    if not isinstance(value, dict):
        return ContextManifest()
    payload = {key: value[key] for key in _field_names(ContextManifest) if key in value}
    # LLM: hint_read_paths survives persistence as soft read guidance, not a startup dependency gate.
    # 函数用途: 归一化 refs-first 读线索，避免恢复后丢失父级给子代理的参考路径。
    for key in ["task_pack_refs", "required_read_paths", "hint_read_paths", "omitted_context"]:
        payload[key] = _string_list_value(payload.get(key))
    try:
        payload["token_budget"] = int(payload.get("token_budget") or 0)
    except (TypeError, ValueError):
        payload["token_budget"] = 0
    return ContextManifest(**payload)


# LLM: _normalize_context_packs 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化上下文packs的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_context_packs(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
