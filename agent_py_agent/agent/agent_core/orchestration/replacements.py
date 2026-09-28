from __future__ import annotations

import time

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...subagents.models import TaskStatus, task_replacement_successor

_REPLACEMENT_RECORD_SUCCESS = frozenset({"recorded", "already_recorded"})


# LLM: Replacement preflight trusts only explicit source run ids and canonical
# parent/takeover state. It never treats matching goals, covers, or outputs as authority.
# 已被接替的判断统一读 takeover_by/superseded_by（models.task_replacement_successor），第二次接替同一来源一律拒绝。
# 函数用途: 在创建新 child 前确认每个被接管 run 存在、属于同一直属父级且尚未被别人接管或取代。
def validate_create_replacements(agent: object, task_params: list[object]) -> list[dict[str, object]]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return [{"reason": "subagent_manager_unavailable"}]
    issues: list[dict[str, object]] = []
    claimed: dict[str, int] = {}
    edges = [
        (index, str(getattr(params, "parent_id", "") or "").strip(), source_id)
        for index, params in enumerate(task_params)
        for source_id in _params_replacement_source_ids(params)
    ]
    for index, parent_id, source_id in edges:
        previous_index = claimed.get(source_id)
        if previous_index is not None:
            issues.append({"index": index, "source_run_id": source_id, "reason": "duplicate_replacement_source",
                           "first_item_index": previous_index})
            continue
        claimed[source_id] = index
        issues.extend(_source_issues(manager, source_id, index, parent_id))
    return issues


# LLM: 单个来源的预检：读不到、不是同一直属父级、已被接替（takeover_by 或 superseded_by）各给一条结构化问题。只读。
# 函数用途: 检查一个被接替的旧 run 能否由当前父级接替，返回发现的问题列表。
def _source_issues(manager: object, source_id: str, index: int, parent_id: str) -> list[dict[str, object]]:
    try:
        source = manager.load(source_id)
    except Exception as exc:
        return [{"index": index, "source_run_id": source_id, "reason": "source_unavailable",
                 "error_type": type(exc).__name__}]
    issues: list[dict[str, object]] = []
    source_parent = str(getattr(source, "parent_id", "") or "").strip()
    if source_parent != parent_id:
        issues.append({"index": index, "source_run_id": source_id, "reason": "source_not_direct_sibling",
                       "source_parent_id": source_parent, "expected_parent_id": parent_id})
    successor, disposition = task_replacement_successor(source)
    if successor:
        issues.append({"index": index, "source_run_id": source_id, "reason": "source_already_taken_over",
                       "takeover_by": successor, "disposition": disposition})
    return issues


# LLM: A replacement child may enter lifecycle publication only when every
# requested source edge was recorded or was already recorded to that exact child.
# 函数用途: 判断接管记录是否全部成功，供启动前的原子闸使用。
def replacement_records_allow_start(records: list[dict[str, object]]) -> bool:
    return all(str(record.get("status") or "") in _REPLACEMENT_RECORD_SUCCESS for record in records)


# LLM: A failed replacement edge must leave newly materialized children in a
# canonical terminal state before lifecycle publication; reused runs are never rewritten.
# 函数用途: 接管记录失败时取消本批尚未启动的新 child，避免留下永久占容量的 PLANNING 幻影。
def cancel_unstarted_replacement_tasks(
    manager: object,
    resolutions: list[object],
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    now = time.time()
    for resolution in resolutions:
        if bool(getattr(resolution, "reused", False)):
            continue
        task = getattr(resolution, "task", None)
        run_id = str(getattr(task, "id", "") or "").strip()
        if task is None or not run_id:
            continue
        try:
            task.status = TaskStatus.CANCELLED.value
            task.updated_at = now
            attrs = dict(getattr(task, "attributes", {}) or {})
            attrs["creation_abort"] = {
                "code": "SUBAGENT_REPLACEMENT_RECORD_FAILED",
                "at": now,
                "replacement_records": [dict(record) for record in records],
            }
            task.attributes = attrs
            manager.save(task)
            results.append({"run_id": run_id, "status": "cancelled_before_start"})
        except Exception as exc:
            results.append(
                {
                    "run_id": run_id,
                    "status": "cancel_failed",
                    "error_type": type(exc).__name__,
                }
            )
    return results


# LLM: 接替编号的唯一归一化口径：只认结构化列表或逗号串，逐项去掉首尾空白并丢掉空项；不认别名、不看正文。
#   创建前预检、接管落账与运行时一次性编排守卫都走这一个函数，不能各判各的。纯计算。
# 函数用途: 把 replacement_for_run_ids 的原始值归一成非空的 run_id 列表。
def replacement_source_ids(values: object) -> list[str]:
    return string_list(values, TOOL_TEXT_LIST_OPTIONS)


# LLM: CreateRunParams stores replacement ids inside its structured attributes;
# this reader accepts no aliases and does not inspect goal text.
# 函数用途: 从规范化创建参数里读取要接管的旧 run_id。
def _params_replacement_source_ids(params: object) -> list[str]:
    attrs = getattr(params, "attributes", None)
    values = attrs.get("replacement_for_run_ids") if isinstance(attrs, dict) else None
    return replacement_source_ids(values)


# LLM: Takeover edges are persisted from explicit structured ids only; callers must gate lifecycle publication on every returned status.
# 函数用途: 为新建 child 逐条写入显式接管关系，并返回每条边的真实落账结果。
def record_create_replacements(agent, tasks: list) -> list[dict[str, object]]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return []
    records: list[dict[str, object]] = []
    for task in tasks:
        replacement_id = str(getattr(task, "id", "") or "").strip()
        if not replacement_id:
            continue
        for source_id in _replacement_source_ids(task):
            records.append(_record_single_replacement(manager, source_id, replacement_id))
    return records


# LLM: Source ids come only from the created task's normalized attributes and are de-duplicated without aliases.
# 函数用途: 读取一名 replacement child 明确声明接管的旧 run 编号。
def _replacement_source_ids(task: object) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return []
    values = replacement_source_ids(attrs.get("replacement_for_run_ids"))
    task_id = str(getattr(task, "id", "") or "").strip()
    unique: list[str] = []
    for value in values:
        if value and value != task_id and value not in unique:
            unique.append(value)
    return unique


# LLM: One takeover write is monotonic and exposes missing/conflicting/storage failures as structured status rather than raising into startup.
# 只有重新读取的权威状态里接替者确实是本次 replacement 时才回报 recorded，并带 disposition（taken_over/superseded）；
# 写入被还原或没落盘时回报 not_persisted，不能冒充成功。
# 函数用途: 精确记录一条旧 run 到新 run 的接管边，供创建闸决定是否允许新 child 启动。
def _record_single_replacement(manager, source_id: str, replacement_id: str) -> dict[str, object]:
    from ...subagents.services.takeover.record import TakeoverNotPersistedError

    edge = {"source_run_id": source_id, "replacement_run_id": replacement_id}
    try:
        source = manager.load(source_id)
    except Exception as exc:
        return {**edge, "status": "source_missing", "error": f"{type(exc).__name__}: {exc}"}
    existing, disposition = task_replacement_successor(source)
    if existing == replacement_id:
        return {**edge, "status": "already_recorded", "disposition": disposition}
    if existing:
        return {**edge, "status": "already_taken_over", "takeover_by": existing, "disposition": disposition}
    try:
        manager.record_takeover(
            source_id,
            take_over_by=replacement_id,
            reason="explicit replacement declared by create_subagents",
            locked_files=[],
        )
    except TakeoverNotPersistedError as exc:
        return {**edge, "status": "not_persisted", "disposition": exc.disposition}
    except Exception as exc:
        return {**edge, "status": "record_failed", "error": f"{type(exc).__name__}: {exc}"}
    return _persisted_replacement_record(manager, edge)


# LLM: 回报前重新读取来源的权威状态；接替者不是本次 replacement（写入被还原、没落盘或读不到）就回报 not_persisted。只读。
# 函数用途: 按真实落盘结果生成一条接替回执。
def _persisted_replacement_record(manager, edge: dict[str, str]) -> dict[str, object]:
    try:
        successor, disposition = task_replacement_successor(manager.load(edge["source_run_id"]))
    except Exception as exc:
        return {**edge, "status": "not_persisted", "error": f"{type(exc).__name__}: {exc}"}
    if successor != edge["replacement_run_id"]:
        return {**edge, "status": "not_persisted", "disposition": disposition}
    return {**edge, "status": "recorded", "disposition": disposition}


__all__ = [
    "cancel_unstarted_replacement_tasks",
    "record_create_replacements",
    "replacement_records_allow_start",
    "validate_create_replacements",
]
