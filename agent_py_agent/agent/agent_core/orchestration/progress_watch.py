"""子代理进展的只读分类：只看结构化事实，说不清就报 unknown。"""

# LLM: 分类只读已授权代理树快照里的结构化字段（状态、未决申请数、阻塞项、工具失败连续次数、进展/心跳时间），
#   可选再看一次已登记仓库的 git 观察；不唤醒、取消、停止、创建、批准或转交任何工作，也不解析自然语言正文。
#   规则：进程/心跳在不等于在干活；没收到回复不等于没进展；计时本身只给出 no_progress 观察，不触发任何动作。
# 模块用途: 给 list_agents 的每个节点附上 progress 分类及其来源和时间。

from __future__ import annotations

from typing import Any

from ...subagents.models import TaskStatus

SCHEMA_VERSION = "progress_watch.v1"
# LLM: 最近一次真实进展事件距今超过它，且心跳仍新鲜时，才可能报 no_progress；它只是观察阈值，不触发动作。
# 常量用途: 判定“进展事件停滞”的时长。
PROGRESS_STALE_AFTER_SECONDS = 600
# LLM: 心跳超过它没更新就不能证明执行体还在，状态只能是 unknown，不能当作在干活或停滞。
# 常量用途: 判定心跳是否仍可作为存活证据的时长。
HEARTBEAT_STALE_AFTER_SECONDS = 300
# LLM: 一次 list_agents 最多分类这么多节点，其余节点标 NOT_CLASSIFIED_LIMIT，保持单次读取有界。
# 常量用途: 限制一次快照里参与进展分类的节点数。
MAX_PROGRESS_CLASSIFIED_NODES_COUNT = 64
# LLM: 同一工具连续失败达到这个次数且执行体仍存活，才报 retrying；一次失败只是失败，不代表在重试。
# 常量用途: 判定“正在重试”的连续失败次数。
RETRYING_MIN_FAILURES_COUNT = 2

_ACTIVE = frozenset({TaskStatus.RUNNING.value})
_NOT_STARTED = frozenset({TaskStatus.PLANNING.value, TaskStatus.PENDING.value})
_BLOCKED = frozenset({TaskStatus.BLOCKED.value})
_DONE = frozenset({TaskStatus.DONE.value})
_PAUSED = frozenset({TaskStatus.PAUSED.value})
_ENDED = frozenset({TaskStatus.FAILED.value, TaskStatus.CANCELLED.value, TaskStatus.ABANDONED.value,
                    TaskStatus.TAKEN_OVER.value, TaskStatus.TIMEOUT.value, TaskStatus.CHANNEL_ERROR.value})


# LLM: 输入是 agent_tree 快照节点（已授权、只读字典）；now 由调用方给出，结果带上每个来源的取值和时间，
#   缺字段或类型不对时如实报 unknown + 原因码，不拿缺省值当健康。
# 函数用途: 把一个节点的结构化事实分类成 working / waiting_* / retrying / no_progress / completed / ended / unknown。
def classify_node_progress(node: dict[str, Any], *, now: float) -> dict[str, object]:
    status = node.get("status")
    if not isinstance(status, str) or not status:
        return _result("unknown", "STATUS_UNREADABLE", now, {})
    progress_at = _time(node.get("last_progress_at"))
    heartbeat_at = _time(node.get("heartbeat_at"))
    sources = {
        "status": status,
        "last_progress_at": progress_at,
        "heartbeat_at": heartbeat_at,
        "progress_age_seconds": _age(now, progress_at),
        "heartbeat_age_seconds": _age(now, heartbeat_at),
    }
    if status in _DONE:
        return _result("completed", "TERMINAL_DONE", now, sources)
    if status in _PAUSED:
        return _result("unknown", "PAUSED_BY_CONTROL", now, sources)
    if status not in _ACTIVE | _NOT_STARTED | _BLOCKED:
        # 失败、取消、放弃、被接替等其它终态或未知状态：不算完成，也不猜它还在跑。
        return _result("ended" if _is_terminal(status) else "unknown",
                       "TERMINAL_NOT_DONE" if _is_terminal(status) else "STATUS_UNRECOGNIZED", now, sources)
    contract = node.get("tool_contract")
    open_requests = contract.get("open_request_count") if isinstance(contract, dict) else None
    if type(open_requests) is not int:
        return _result("unknown", "CAPABILITY_FACTS_UNREADABLE", now, sources)
    sources["open_capability_requests"] = open_requests
    if open_requests > 0:
        return _result("waiting_permission", "OPEN_CAPABILITY_REQUEST", now, sources)
    blockers = node.get("blockers")
    if not isinstance(blockers, list):
        return _result("unknown", "BLOCKER_FACTS_UNREADABLE", now, sources)
    sources["blockers_count"] = len(blockers)
    if blockers or status in _BLOCKED:
        return _result("waiting_dependency", "BLOCKED" if status in _BLOCKED else "BLOCKERS_RECORDED", now, sources)
    if status in _NOT_STARTED:
        return _result("unknown", "NOT_STARTED", now, sources)
    alive = sources["heartbeat_age_seconds"] is not None and sources["heartbeat_age_seconds"] <= HEARTBEAT_STALE_AFTER_SECONDS
    if not alive:
        return _result("unknown", "HEARTBEAT_STALE_OR_MISSING", now, sources)
    failures = _failure_count(node.get("recent_tool_failure"))
    sources["consecutive_tool_failures"] = failures
    if failures >= RETRYING_MIN_FAILURES_COUNT:
        return _result("retrying", "REPEATED_TOOL_FAILURE", now, sources)
    age = sources["progress_age_seconds"]
    if age is None:
        # 活着但从没有过进展事件：没有证据说明在干活，也没有证据说明停滞。
        return _result("unknown", "NO_PROGRESS_EVENT_YET", now, sources)
    if age <= PROGRESS_STALE_AFTER_SECONDS:
        return _result("working", "RECENT_PROGRESS_EVENT", now, sources)
    return _result("no_progress", "PROGRESS_EVENTS_STALE", now, sources)


# LLM: 节点数超过上限时只分类前面的节点，其余明确标出未分类，不把“没分类”说成健康或停滞。
# 函数用途: 给一组节点逐个附上进展分类。
def classify_nodes(nodes: list[dict[str, Any]], *, now: float) -> list[dict[str, object]]:
    results = []
    for index, node in enumerate(nodes):
        if index >= MAX_PROGRESS_CLASSIFIED_NODES_COUNT:
            results.append(_result("unknown", "NOT_CLASSIFIED_LIMIT", now, {}))
        else:
            results.append(classify_node_progress(node, now=now))
    return results


# LLM: git 观察只能把“说不清/停滞”提升为有据的 working（HEAD 或改动文件数相对上次观察变化），
#   观察失败或未启用只记录在 sources.git 里，绝不把不可用当作有进展或没进展。
# 函数用途: 把一次可选 git 观察合并进节点分类结果。
def merge_git_observation(result: dict[str, object], observation: dict[str, object] | None,
                          previous: dict[str, object] | None) -> dict[str, object]:
    if not observation:
        return result
    merged = {**result, "sources": {**dict(result.get("sources") or {}), "git": dict(observation)}}
    if observation.get("error_code") or not previous or previous.get("error_code"):
        return merged
    changed = (observation.get("head") != previous.get("head")
               or observation.get("dirty_paths_count") != previous.get("dirty_paths_count"))
    if changed and result.get("state") in {"unknown", "no_progress"} and result.get("reason") not in {
            "STATUS_UNREADABLE", "NOT_CLASSIFIED_LIMIT"}:
        merged.update(state="working", reason="GIT_CHANGED_SINCE_PREVIOUS")
    return merged


# LLM: 时间字段只接受正数数字，0/缺失/布尔都视为没有这个事实。
# 函数用途: 规范化时间戳字段。
def _time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


# LLM: 时钟回拨时年龄记 0，不报负数。
# 函数用途: 计算距今秒数。
def _age(now: float, at: float | None) -> float | None:
    return None if at is None else round(max(0.0, now - at), 3)


# LLM: 只读结构化的 consecutive_failures 整数，缺失按 0。
# 函数用途: 取最近工具失败的连续次数。
def _failure_count(value: object) -> int:
    count = value.get("consecutive_failures") if isinstance(value, dict) else 0
    return count if type(count) is int and count > 0 else 0


# LLM: 终态集合沿 TaskStatus 现有取值，不收录别名。
# 函数用途: 判断状态是否是非完成的终态。
def _is_terminal(status: str) -> bool:
    return status in _ENDED


# LLM: 结果总带 schema、观察时间与来源，供父级区分“来源过时”和“已证实的进展”。
# 函数用途: 组装分类结果。
def _result(state: str, reason: str, now: float, sources: dict[str, object]) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "state": state, "reason": reason, "observed_at": round(now, 3),
            "effect": "read_only", "sources": sources}


__all__ = [
    "HEARTBEAT_STALE_AFTER_SECONDS", "MAX_PROGRESS_CLASSIFIED_NODES_COUNT", "PROGRESS_STALE_AFTER_SECONDS",
    "SCHEMA_VERSION", "classify_node_progress", "classify_nodes", "merge_git_observation",
]
