"""LLM: Field normalization helpers for subagent manager data models.

新手说明:
这个模块从 manager_base.py 中提取出来，负责把外部传入的
松散字典/列表数据规范化成强类型的数据模型实例。主要处理
QualityContract、ContextManifest、context_packs 以及从
目标文本中自动提取写入目录路径。
"""

from __future__ import annotations

import re
from dataclasses import fields


def _field_names(model: type) -> set[str]:
    """LLM: Return the set of field names for a dataclass type.

    新手说明:
    获取一个 dataclass 类的所有字段名集合，用于从字典中
    筛选有效字段，避免传入多余 key 导致构造报错。
    """
    return {item.name for item in fields(model)}


def _list_value(value: object) -> list[object]:
    """LLM: Coerce a value into a list — None becomes [], tuples become lists.

    新手说明:
    把各种形式的值统一转成 list：None 变空列表，tuple 转 list，
    已经是 list 的直接返回，其它类型包装成单元素列表。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _string_list_value(value: object) -> list[str]:
    """LLM: Coerce a value into a list of non-empty strings.

    新手说明:
    把值转成字符串列表，并过滤掉 None 和空字符串。
    常用于规范化 QualityContract 和 ContextManifest 中的
    字符串列表字段。
    """
    return [str(item) for item in _list_value(value) if item not in (None, "")]


def _normalize_quality_contract(value: object) -> "QualityContract":
    """LLM: Normalize a value into a QualityContract instance.

    新手说明:
    把外部传入的 QualityContract 数据规范化成强类型实例。
    如果已经是 QualityContract 直接返回；如果是字典则筛选
    有效字段并确保列表字段和布尔字段类型正确；其它情况
    返回默认空实例。
    """
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


def _normalize_context_manifest(value: object) -> "ContextManifest":
    """LLM: Normalize a value into a ContextManifest instance.

    新手说明:
    把外部传入的 ContextManifest 数据规范化成强类型实例。
    如果已经是 ContextManifest 直接返回；如果是字典则筛选
    有效字段、确保列表字段类型正确、token_budget 转整数；
    其它情况返回默认空实例。
    """
    from .models import ContextManifest

    if isinstance(value, ContextManifest):
        return value
    if not isinstance(value, dict):
        return ContextManifest()
    payload = {key: value[key] for key in _field_names(ContextManifest) if key in value}
    for key in ["task_pack_refs", "required_read_paths", "omitted_context"]:
        payload[key] = _string_list_value(payload.get(key))
    try:
        payload["token_budget"] = int(payload.get("token_budget") or 0)
    except (TypeError, ValueError):
        payload["token_budget"] = 0
    return ContextManifest(**payload)


def _normalize_context_packs(value: object) -> list[dict[str, object]]:
    """LLM: Normalize a value into a list of context pack dictionaries.

    新手说明:
    把 context_packs 参数规范化成字典列表：单个字典包装成
    单元素列表，非列表类型返回空列表，列表中非字典元素
    被过滤掉。
    """
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# LLM: patterns for extracting directory paths from user goal text.
_DIR_PATTERN = re.compile(r"(?:/[\w.\-]+){2,}")
_HOME_DIR_PATTERN = re.compile(r"(?:~/[\w.\-]+(?:/[\w.\-]+)*)")
_ABSOLUTE_DIR_PATTERN = re.compile(r"(?:/[\w.\-]+(?:/[\w.\-]+)*)(?=/|$)")


def _extract_write_dirs(goal: str) -> list[str]:
    """LLM: Extract directory paths from user goal text for auto write-permission.

    新手说明:
    从用户目标文本中用正则提取目录路径，用于自动授权子代理写入。
    匹配 /path/to/dir 形式的绝对路径和 ~/path 形式的 home 目录路径，
    去重后返回路径列表。
    """
    dirs: list[str] = []
    for pattern in [_DIR_PATTERN, _HOME_DIR_PATTERN]:
        for match in pattern.finditer(goal):
            path = match.group().strip()
            if path and path not in dirs:
                dirs.append(path)
    return dirs
