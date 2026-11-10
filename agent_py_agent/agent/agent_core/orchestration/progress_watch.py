"""子代理进展的只读分类：只看结构化事实，说不清就报 unknown。"""

# LLM: 分类只读已授权代理树快照里的结构化字段（状态、未决申请数、阻塞项、工具失败连续次数、进展/心跳时间），
#   可选再看一次已登记仓库的 git 观察；不唤醒、取消、停止、创建、批准或转交任何工作，也不解析自然语言正文。
#   规则：进程/心跳在不等于在干活；没收到回复不等于没进展；计时本身只给出 no_progress 观察，不触发任何动作。
#   ORCH03-R1/R3（PRO-MSG-024）：list_agents 传入冻结的 ProgressReadContext（同一观察时间、配置的子代理上限、每个
#   子代理的必需来源及可读性、可选 git 关联观察）。必需来源（运行读取、当前 attempt、工具失败账、工具审批、Todo）
#   任何一项读不出就只能是 unknown，覆盖标 partial；不适用（例如本地未托管运行）与读失败分开记。没有上下文的旧调用
#   仍做纯投影，但覆盖固定标 partial，不冒充完整。
#   例外（有意保留）：完成/终态/暂停/未决能力申请/工具审批等待/阻塞这些由独立结构事实证明的结论排在可读性门之前，
#   所以可能出现 completed 或 waiting_* 同时 coverage=partial；覆盖字段始终如实，读不全时绝不标 full。重试只认来源里
#   当前 attempt 的失败段（与可读性同一次读取），不看跨 attempt 的展示字段。
# 模块用途: 给 list_agents 的每个节点附上 progress 分类及其来源和时间。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ...subagents.models import TaskStatus

SCHEMA_VERSION = "progress_watch.v1"
# LLM: 最近一次真实进展事件距今超过它，且心跳仍新鲜时，才可能报 no_progress；它只是观察阈值，不触发动作。
# 常量用途: 判定“进展事件停滞”的时长。
PROGRESS_STALE_AFTER_SECONDS = 600
# LLM: 心跳超过它没更新就不能证明执行体还在，状态只能是 unknown，不能当作在干活或停滞。
# 常量用途: 判定心跳是否仍可作为存活证据的时长。
HEARTBEAT_STALE_AFTER_SECONDS = 300
# LLM: 配置 orchestration_progress_check_child_limit 为 0 时的默认上限；超出的节点标 NOT_CLASSIFIED_LIMIT。
# 常量用途: 一次快照里参与进展分类的默认节点数。
DEFAULT_PROGRESS_CHECK_CHILD_LIMIT_COUNT = 20
# 必需来源；任何一项不是 read / not_applicable，节点就只能是 unknown。
REQUIRED_PROGRESS_SOURCES = ("runtime_read", "attempt", "tool_failures", "tool_approvals", "task_progress")
_SOURCE_OK = frozenset({"read", "not_applicable"})
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


# LLM: 由已授权的 list_agents 调用方构造并冻结；字段都是宿主读到的结构化事实，不含模型参数。child_limit<=0 或
#   config_error 非空表示上限不可用：不分类任何节点（全部 unknown），绝不退化成不限。sources/git 按 run_id 索引。
# 类用途: 一次 list_agents 读取的进展观察上下文。
@dataclass(frozen=True)
class ProgressReadContext:
    now: float
    child_limit: int
    config_error: str = ""
    sources: Mapping[str, Mapping[str, Mapping[str, object]]] = field(default_factory=dict)
    git: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


# LLM: 输入是 agent_tree 快照节点（已授权、只读字典）；now 由调用方给出，结果带上每个来源的取值和时间，
#   缺字段或类型不对时如实报 unknown + 原因码，不拿缺省值当健康。
# 函数用途: 把一个节点的结构化事实分类成 working / waiting_* / retrying / no_progress / completed / ended / unknown。
def classify_node_progress(node: dict[str, Any], *, now: float,
                           sources: Mapping[str, Mapping[str, object]] | None = None) -> dict[str, object]:
    result = _classify(node, now=now, sources=sources)
    result["coverage"] = "full" if sources is not None and _all_sources_ok(sources) else "partial"
    if sources is not None:
        result["sources"] = {**dict(result.get("sources") or {}),
                             "required": {name: dict(sources.get(name) or {"state": "missing"})
                                          for name in REQUIRED_PROGRESS_SOURCES}}
    return result


# LLM: 规则顺序：状态门（完成/暂停/终态/未知）→ 能力申请 → 工具审批 → 阻塞 → 必需来源可读性 → 未开始 → 当前 attempt
#   → 心跳 → 正在进行的同码连续失败 → 进展事件新旧。没有 sources 时跳过来源相关规则（旧纯投影）。
# 函数用途: 按结构化事实给一个节点分类。
def _classify(node: dict[str, Any], *, now: float,
              sources: Mapping[str, Mapping[str, object]] | None) -> dict[str, object]:
    status = node.get("status")
    if not isinstance(status, str) or not status:
        return _result("unknown", "STATUS_UNREADABLE", now, {})
    progress_at = _time(node.get("last_progress_at"))
    heartbeat_at = _time(node.get("heartbeat_at"))
    facts: dict[str, object] = {
        "status": status,
        "last_progress_at": progress_at,
        "heartbeat_at": heartbeat_at,
        "progress_age_seconds": _age(now, progress_at),
        "heartbeat_age_seconds": _age(now, heartbeat_at),
    }
    if status in _DONE:
        return _result("completed", "TERMINAL_DONE", now, facts)
    if status in _PAUSED:
        return _result("unknown", "PAUSED_BY_CONTROL", now, facts)
    if status not in _ACTIVE | _NOT_STARTED | _BLOCKED:
        # 失败、取消、放弃、被接替等其它终态或未知状态：不算完成，也不猜它还在跑。
        return _result("ended" if _is_terminal(status) else "unknown",
                       "TERMINAL_NOT_DONE" if _is_terminal(status) else "STATUS_UNRECOGNIZED", now, facts)
    contract = node.get("tool_contract")
    open_requests = contract.get("open_request_count") if isinstance(contract, dict) else None
    if type(open_requests) is not int:
        return _result("unknown", "CAPABILITY_FACTS_UNREADABLE", now, facts)
    facts["open_capability_requests"] = open_requests
    if open_requests > 0:
        return _result("waiting_permission", "OPEN_CAPABILITY_REQUEST", now, facts)
    approvals = dict((sources or {}).get("tool_approvals") or {})
    if approvals.get("state") == "read" and type(approvals.get("pending")) is int and approvals["pending"] > 0:
        facts["pending_tool_approvals"] = approvals["pending"]
        return _result("waiting_permission", "PENDING_TOOL_APPROVAL", now, facts)
    blockers = node.get("blockers")
    if not isinstance(blockers, list):
        return _result("unknown", "BLOCKER_FACTS_UNREADABLE", now, facts)
    facts["blockers_count"] = len(blockers)
    if blockers or status in _BLOCKED:
        return _result("waiting_dependency", "BLOCKED" if status in _BLOCKED else "BLOCKERS_RECORDED", now, facts)
    if sources is not None and not _all_sources_ok(sources):
        return _result("unknown", "REQUIRED_SOURCE_UNREADABLE", now, facts)
    if status in _NOT_STARTED:
        return _result("unknown", "NOT_STARTED", now, facts)
    attempt = dict((sources or {}).get("attempt") or {})
    if attempt.get("state") == "read" and attempt.get("status") != "running":
        # 记录显示 RUNNING，但权威当前 attempt 不在运行：两份事实冲突，不能说在干活。
        facts["attempt_status"] = attempt.get("status")
        return _result("unknown", "ATTEMPT_NOT_RUNNING", now, facts)
    alive = facts["heartbeat_age_seconds"] is not None and facts["heartbeat_age_seconds"] <= HEARTBEAT_STALE_AFTER_SECONDS
    if not alive:
        return _result("unknown", "HEARTBEAT_STALE_OR_MISSING", now, facts)
    # 有上下文时只认当前 attempt 的失败段（与可读性同一次读取）；旧纯投影沿用节点展示字段。
    streak = (dict((sources or {}).get("tool_failures") or {}).get("current_attempt_streak")
              if sources is not None else node.get("recent_tool_failure"))
    failures = _failure_count(streak)
    facts["consecutive_tool_failures"] = failures
    # 只有仍在进行的同码连续失败才算重试；历史上的连续失败次数证明不了现在还在重试。
    if failures >= RETRYING_MIN_FAILURES_COUNT and _failure_ongoing(streak):
        return _result("retrying", "REPEATED_TOOL_FAILURE", now, facts)
    age = facts["progress_age_seconds"]
    if age is None:
        # 活着但从没有过进展事件：没有证据说明在干活，也没有证据说明停滞。
        return _result("unknown", "NO_PROGRESS_EVENT_YET", now, facts)
    if age <= PROGRESS_STALE_AFTER_SECONDS:
        return _result("working", "RECENT_PROGRESS_EVENT", now, facts)
    return _result("no_progress", "PROGRESS_EVENTS_STALE", now, facts)


# LLM: 节点数超过上限时只分类前面的节点，其余明确标出未分类，不把“没分类”说成健康或停滞。
# 函数用途: 给一组节点逐个附上进展分类。
def classify_nodes(nodes: list[dict[str, Any]], *, now: float,
                   context: ProgressReadContext | None = None) -> list[dict[str, object]]:
    now = context.now if context is not None else now
    limit = DEFAULT_PROGRESS_CHECK_CHILD_LIMIT_COUNT if context is None else context.child_limit
    if context is not None and (context.config_error or limit <= 0):
        reason = context.config_error or "PROGRESS_CHILD_LIMIT_INVALID"
        return [{**_result("unknown", reason, now, {}), "coverage": "partial"} for _node in nodes]
    results = []
    for index, node in enumerate(nodes):
        if index >= limit:
            results.append({**_result("unknown", "NOT_CLASSIFIED_LIMIT", now, {}), "coverage": "partial"})
            continue
        run_id = str(node.get("run_id") or "")
        sources = None if context is None else dict(context.sources.get(run_id) or {})
        classified = classify_node_progress(node, now=now, sources=sources)
        git = dict(context.git.get(run_id) or {}) if context is not None else {}
        if git:
            classified = merge_git_observation(classified, git.get("observation") if git.get("association") == "exact"
                                               else None, git.get("previous"))
            classified["sources"] = {**dict(classified.get("sources") or {}),
                                     "git_association": {k: git.get(k) for k in ("association", "repo")}}
        results.append(classified)
    return results


# LLM: git 观察只能把已证实“活着却停滞”的 no_progress（RUNNING、心跳新鲜、没有未决申请/阻塞、进展事件过时）
#   提升为有据的 working，而且必须是 HEAD 或改动文件数相对上次成功观察真的变了。unknown（暂停、未开始、状态不明、
#   心跳缺失/过时、来源读不出）、等待、重试、终态一律原样保留：仓库有改动证明不了这个子代理还活着或在干活。
#   观察失败或未启用只记录在 sources.git 里，绝不把不可用当作有进展或没进展。
# 函数用途: 把一次可选 git 观察合并进节点分类结果。
def merge_git_observation(result: dict[str, object], observation: dict[str, object] | None,
                          previous: dict[str, object] | None) -> dict[str, object]:
    if not observation:
        return result
    merged = {**result, "sources": {**dict(result.get("sources") or {}), "git": dict(observation)}}
    if result.get("state") != "no_progress" or observation.get("error_code") or not previous or previous.get("error_code"):
        return merged
    changed = (observation.get("head") != previous.get("head")
               or observation.get("dirty_paths_count") != previous.get("dirty_paths_count"))
    if changed:
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


# LLM: 必需来源只接受 read / not_applicable；缺项、unreadable、partial 都不算可用。
# 函数用途: 判断必需来源是否全部可用。
def _all_sources_ok(sources: Mapping[str, Mapping[str, object]]) -> bool:
    return all(dict(sources.get(name) or {}).get("state") in _SOURCE_OK for name in REQUIRED_PROGRESS_SOURCES)


# LLM: 只认结构化 ongoing 严格为 True。
# 函数用途: 判断最近的连续失败段是否仍在进行。
def _failure_ongoing(value: object) -> bool:
    return isinstance(value, dict) and value.get("ongoing") is True


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
    "DEFAULT_PROGRESS_CHECK_CHILD_LIMIT_COUNT", "HEARTBEAT_STALE_AFTER_SECONDS", "PROGRESS_STALE_AFTER_SECONDS",
    "REQUIRED_PROGRESS_SOURCES", "SCHEMA_VERSION", "ProgressReadContext", "classify_node_progress", "classify_nodes",
    "merge_git_observation",
]
