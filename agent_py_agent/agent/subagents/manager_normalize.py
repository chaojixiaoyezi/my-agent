# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Field normalization helpers for subagent manager data models.

新手说明:
这个模块从 manager_base.py 中提取出来，负责把外部传入的
松散字典/列表数据规范化成强类型的数据模型实例。主要处理
QualityContract、ContextManifest、context_packs 以及从
目标文本中自动提取写入目录路径。
"""

from __future__ import annotations

import re
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
    for key in ["task_pack_refs", "required_read_paths", "omitted_context"]:
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


# LLM: patterns for extracting directory paths from user goal text.
_DIR_PATTERN = re.compile(r"(?<![\w.\-])(?:/[\w.\-]+){2,}")
_HOME_DIR_PATTERN = re.compile(r"(?:~/[\w.\-]+(?:/[\w.\-]+)*)")
_ABSOLUTE_DIR_PATTERN = re.compile(r"(?<![\w.\-])(?:/[\w.\-]+(?:/[\w.\-]+)*)(?=/|$)")
_URL_PATTERN = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>]+")


# LLM: _extract_write_dirs 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 从目标文本提取本地目录写入根，跳过 URL，避免图片/API 地址被误当成本地授权路径。
def _extract_write_dirs(goal: str) -> list[str]:
    dirs: list[str] = []
    for path in _iter_write_dir_matches(goal):
        if path and path not in dirs:
            dirs.append(path)
    return dirs


# LLM: _iter_write_dir_matches ignores URL spans before yielding local path candidates.
# 函数用途: 遍历本地目录候选；URL 内部的 `//host/path` 等片段不应进入写入根。
def _iter_write_dir_matches(goal: str):
    url_spans = _url_spans(goal)
    for match in _write_dir_candidate_matches(goal):
        if not _overlaps_url(match.start(), match.end(), url_spans):
            yield _trim_write_dir_candidate(match.group())


# LLM: _write_dir_candidate_matches keeps legacy regex iteration shallow for code-size guard.
# 函数用途: 统一产出目录候选 match，让 URL 过滤和正则遍历分开。
def _write_dir_candidate_matches(goal: str):
    for pattern in [_DIR_PATTERN, _HOME_DIR_PATTERN]:
        yield from pattern.finditer(goal)


# LLM: _url_spans records URL ranges for legacy normalization helpers.
# 函数用途: 返回 goal 中 URL 的字符范围，供目录候选过滤使用。
def _url_spans(goal: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _URL_PATTERN.finditer(goal)]


# LLM: _overlaps_url checks whether a candidate path belongs to a URL.
# 函数用途: 判断目录候选是否落在 URL 范围内；落入则不参与自动写入根。
def _overlaps_url(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


# LLM: _trim_write_dir_candidate keeps legacy normalization aligned with services.base.
# 函数用途: 清理目录候选末尾标点，避免自动写入根带上自然语言句号。
def _trim_write_dir_candidate(raw: str) -> str:
    return raw.strip().rstrip(".,;:，。；：、)]}）】")
