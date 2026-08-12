from __future__ import annotations

"""在规划和失败决策点主动召回正式 Lesson/HOT。"""

# LLM: 本模块只消费 LessonRepository/HotRuleRepository，并复用唯一 memory-context 投影；
# 禁止从 long-term 的 legacy kind=lesson_* 召回或向任意正式 Memory 落盘。
# 模块用途: 让 planner/runner 在关键决策点主动获得相关正式教训，同时不建立第二套记忆权威。

import re
from collections.abc import Mapping

from .common.value_parsing import dedupe_strings
from .memory_routing import RouteContextOptions, build_routed_memory_context
from .memory_routing.matcher import _chinese_ngrams
from .memory_store import (
    MemoryRecallScope,
    MemoryRecord,
    hot_memory_records,
    routed_lesson_records,
)
from .prompting_parts.memory_context import memory_context_text
from .runtime_errors import runtime_error_report
from .user_space.home_layout import runtime_route_root_and_index

_GOAL_MAX_CHARS = 80
_GOAL_NGRAM_MAX = 24
_STRUCTURED_SCOPE_FIELDS = (
    "company_id",
    "project_id",
    "task_class",
    "session_id",
    "temporary_scope_key",
    "memory_scope",
    "memory_scopes",
)


# LLM: 返回值只能含 active 正式 Lesson/HOT 的 MemoryRecord 投影；候选、Daily、Ops 和 legacy long-term lesson 不可进入。
# 函数用途: 查询关键决策点相关的正式教训，忽略可恢复的路由错误。
def push_relevant_memories(
    agent: object,
    trigger_type: str,
    context: Mapping[str, object],
    limit: int = 3,
) -> list[MemoryRecord]:
    memories, _load_errors = push_relevant_memories_report(
        agent,
        trigger_type,
        context,
        limit=limit,
    )
    return memories


# LLM: 路由或正式文件损坏时 fail closed 并返回结构化错误；决策主链不能因 Memory 故障崩溃。
# 函数用途: 查询正式教训并同时返回诊断信息。
def push_relevant_memories_report(
    agent: object,
    trigger_type: str,
    context: Mapping[str, object],
    limit: int = 3,
) -> tuple[list[MemoryRecord], list[dict[str, object]]]:
    try:
        bounded_limit = max(0, int(limit or 0))
        if bounded_limit <= 0:
            return [], []
        policy = getattr(agent, "owner_policy", None)
        if policy is not None and not bool(getattr(policy, "memory_enabled", True)):
            return [], []  # 总闸关闭:决策点召回短路(effective flag,对称 skill 空快照)
        query = _build_memory_query(trigger_type, context)
        routed = _route_formal_lessons(agent, query, bounded_limit)
        scope = MemoryRecallScope.from_runtime(
            task_id=str(context.get("task_id") or ""),
            task_attributes=_structured_scope_attributes(context),
        )
        memories = _formal_decision_memories(agent, routed, scope, bounded_limit)
        errors = [_routing_finding(item) for item in routed.findings if str(item).strip()]
        return memories, errors
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, UnicodeError) as exc:
        return [], [runtime_error_report(exc, context="memory_push.formal_recall")]


# LLM: Timeout/Failure 只是检索 reason，不创建独立存储或状态机。
# 函数用途: 召回一次超时决策可用的正式教训。
def push_timeout_memories(
    agent: object,
    task_id: str,
    goal: str,
    timeout_count: int = 0,
) -> list[MemoryRecord]:
    return push_relevant_memories(
        agent,
        "timeout",
        {
            "task_id": task_id,
            "goal": goal,
            "failure_type": "timeout",
            "timeout_count": max(0, int(timeout_count or 0)),
        },
        limit=3,
    )


# LLM: failure_type 只参与相关度查询，不能从自然语言推断 scope 或事实权威。
# 函数用途: 召回一次失败决策可用的正式教训。
def push_failure_memories(
    agent: object,
    task_id: str,
    goal: str,
    failure_type: str,
) -> list[MemoryRecord]:
    return push_relevant_memories(
        agent,
        "failure",
        {
            "task_id": task_id,
            "goal": goal,
            "failure_type": failure_type,
        },
        limit=3,
    )


# LLM: planner 只获得正式教训投影，不获得候选审核字段或 routing 原文块。
# 函数用途: 召回规划决策可用的正式教训。
def push_planning_memories(
    agent: object,
    task_id: str,
    goal: str,
) -> list[MemoryRecord]:
    return push_relevant_memories(
        agent,
        "planning",
        {"task_id": task_id, "goal": goal},
        limit=3,
    )


# LLM: 关键决策点与普通运行必须共用同一个安全投影函数，禁止恢复 [相关记忆提示] 等第二信封。
# 函数用途: 把正式教训记录渲染成唯一的非权威 memory-context 信封。
def format_memories_for_injection(memories: list[MemoryRecord]) -> str:
    return memory_context_text(memories) if memories else ""


# LLM: 路由根和索引只能来自当前 owner home；工作区 legacy INDEX 不得作为 fallback。
# 函数用途: 根据触发词和任务目标读取正式 lesson 路由小票。
def _route_formal_lessons(agent: object, query: str, limit: int):
    root, index_path = runtime_route_root_and_index(agent)
    config = getattr(agent, "config", None)
    auto_read_limit = min(
        limit,
        max(0, int(getattr(config, "memory_rule_auto_read_limit", limit) or 0)),
    )
    return build_routed_memory_context(
        root,
        query,
        options=RouteContextOptions(
            enabled=bool(getattr(config, "memory_rule_routing_enabled", True)),
            index_path=index_path,
            mode=str(getattr(config, "memory_rule_routing_mode", "soft") or "soft"),
            auto_read_limit=auto_read_limit,
            limit=max(limit, auto_read_limit),
        ),
    )


# LLM: HOT 与已成功路由的 Lesson 是这里仅有的两个来源；二者都继承正式 lesson 的 typed scope。
# 函数用途: 合并、按 entry_id 去重并限制关键决策点的正式教训数量。
def _formal_decision_memories(
    agent: object,
    routed: object,
    scope: MemoryRecallScope,
    limit: int,
) -> list[MemoryRecord]:
    lessons_repository = getattr(agent, "memory_lessons", None)
    hot_repository = getattr(agent, "memory_hot", None)
    if lessons_repository is None or hot_repository is None:
        raise RuntimeError("formal lesson repositories are unavailable")
    read_paths = [
        str(receipt.get("path") or "")
        for receipt in list(getattr(routed, "receipts", ()) or ())
        if isinstance(receipt, dict) and receipt.get("status") == "read"
    ]
    records = [
        *hot_memory_records(hot_repository, lessons_repository, scope=scope),
        *routed_lesson_records(
            lessons_repository,
            read_paths=read_paths,
            scope=scope,
            stale_days=float(
                getattr(getattr(agent, "config", None), "home_lesson_stale_caveat_days", 7.0)
                or 0.0
            ),
        ),
    ]
    result: list[MemoryRecord] = []
    seen: set[str] = set()
    for record in records:
        marker = str(record.entry_id or "")
        if not marker or marker in seen:
            continue
        seen.add(marker)
        result.append(record)
        if len(result) >= limit:
            break
    return result


# LLM: Scope 只从调用方 typed fields 复制；goal/failure 文本不参与 scope 推断。
# 函数用途: 构造 MemoryRecallScope 允许消费的结构化任务属性。
def _structured_scope_attributes(context: Mapping[str, object]) -> dict[str, object]:
    nested = context.get("task_attributes")
    result = dict(nested) if isinstance(nested, Mapping) else {}
    for field in _STRUCTURED_SCOPE_FIELDS:
        if field in context:
            result[field] = context[field]
    return result


# LLM: 查询词只影响相关度，不产生 subject_key、scope 或晋升动作。
# 函数用途: 把触发类型、结构化失败类别和短任务目标拼成 lesson 路由查询。
def _build_memory_query(trigger_type: str, context: Mapping[str, object]) -> str:
    parts = [str(trigger_type or "").strip()]
    failure_type = str(context.get("failure_type") or "").strip()
    if failure_type:
        parts.append(failure_type)
    goal = str(context.get("goal") or "").strip()[:_GOAL_MAX_CHARS]
    if goal:
        parts.append(goal)
        parts.extend(_goal_chinese_ngrams(goal))
    return " ".join(part for part in parts if part)


# LLM: 中文 n-gram 只改善路由召回，不以文本相似度合并候选或覆盖事实。
# 函数用途: 对中文连续串生成有界去重的路由查询片段。
def _goal_chinese_ngrams(goal: str) -> list[str]:
    grams: list[str] = []
    for run in re.findall(r"[一-鿿]+", goal):
        grams.extend(_chinese_ngrams(run))
    return dedupe_strings(grams)[:_GOAL_NGRAM_MAX]


# LLM: finding 只用于诊断，不进入 Prompt，也不能取得 Memory 内容权威。
# 函数用途: 将 routing finding 转成稳定结构化错误记录。
def _routing_finding(value: object) -> dict[str, object]:
    return {
        "code": "MEMORY_ROUTING_FINDING",
        "context": "memory_push.formal_recall",
        "message": str(value),
    }


__all__ = [
    "format_memories_for_injection",
    "push_failure_memories",
    "push_planning_memories",
    "push_relevant_memories",
    "push_relevant_memories_report",
    "push_timeout_memories",
]
