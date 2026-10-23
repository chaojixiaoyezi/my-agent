# LLM: Goal 的最近变更事实与原目标同次持久化；它不是第二个状态机，完成声明也不等于独立验收通过。
# 模块用途: 为目标状态记录来源、版本及执行身份，输出不包含目标正文、路径或原始错误。
from __future__ import annotations

from dataclasses import replace

# 插件来源只能由已授权宿主适配器传入，不能伪装成人类直接控制。
_SOURCES = frozenset({"model_tool", "user_control", "plugin_control", "budget", "no_progress_fuse", "runtime", "unknown"})
_IDENTITIES = ("request_id", "run_id", "attempt_id", "tool_call_id")


# LLM: 纯计费运算不推进内容 revision；晚到用量不改已完成来源，达到预算才记新的预算状态事件。
# 函数用途: 将已核验的 token/时间增量应用到 Goal，写盘和锁仍归原 GoalStore。
def goal_with_accounted_usage(current, *, token_delta: int, time_delta: int, now: float):
    tokens = current.tokens_used + token_delta
    elapsed = current.time_used_seconds + time_delta
    limited = ((current.token_budget is not None and tokens >= current.token_budget)
               or (current.duration_seconds is not None and elapsed >= current.duration_seconds))
    status = "budget_limited" if current.status == "active" and limited else current.status
    transition = (goal_transition_event(current, status=status, revision=current.revision, now=now, source="budget")
                  if status != current.status else current.transition)
    return replace(current, tokens_used=tokens, time_used_seconds=elapsed, status=status, updated_at=now,
                   transition=transition)


# LLM: source 与身份仅接受内部调用方给出的结构化上下文；目标/状态/版本/时间来自已赢 CAS 的记录。
# 函数用途: 在同次 Goal 写入中生成可审计来源，不从 objective 或模型回复推断完成原因。
def goal_transition_event(current, *, status: str, revision: int, now: float, source: str,
                          context: dict | None = None, objective_changed: bool = False) -> dict:
    facts = context if isinstance(context, dict) else {}
    return {
        "schema": "goal_transition.v1",
        "source": source if source in _SOURCES else "unknown",
        "goal_id": current.goal_id,
        "task_id": current.task_id,
        "previous_status": current.status,
        "status": status,
        "previous_revision": current.revision,
        "revision": revision,
        "at": now,
        "objective_changed": objective_changed,
        **{key: facts[key] for key in _IDENTITIES if isinstance(facts.get(key), str) and facts[key]},
        "verification": "not_evaluated",
        "verification_refs": _verification_refs(facts.get("verification_refs")),
        "progress": _progress_metadata(facts.get("progress")),
    }


# LLM: 只读已经按 owner/thread/task 选出的验证事件；通过或完整 scope 都不提升为 Goal 语义验收。
# 函数用途: 保留验证编号、结果及过期事实，排除命令、输出与工作区路径。
def _verification_refs(value: object) -> list[dict]:
    rows = value if isinstance(value, (tuple, list)) else ()
    return [{"event_id": row["event_id"],
             "status": row.get("status") if row.get("status") in {"passed", "failed", "stale", "unknown"} else "unknown",
             "scope": row.get("scope") if row.get("scope") in {"full", "targeted", "unknown"} else "unknown"}
            for row in rows if isinstance(row, dict) and type(row.get("event_id")) is int and row["event_id"] > 0]


# LLM: 历史字段缺失保持未知；只暴露 schema 定义的状态字段，额外字段和私有 metadata 不随读取扩散。
# 函数用途: 从保存的变更记录生成纯元数据视图。
def public_goal_transition(value: object) -> dict:
    if not isinstance(value, dict) or value.get("schema") != "goal_transition.v1":
        return {}
    keys = ("schema", "source", "goal_id", "task_id", "previous_status", "status", "previous_revision", "revision",
            "at", "objective_changed", *_IDENTITIES)
    result = {key: value[key] for key in keys if key in value}
    result.update(verification="not_evaluated", verification_refs=_verification_refs(value.get("verification_refs")))
    result["progress"] = _progress_metadata(value.get("progress"))
    return result


# LLM: 进度只保留机器计数与编号；模型自报全部 done 仍不提升为业务验收，禁止扩散标题、结果正文及错误。
# 函数用途: 为完成事件冻结当时清单的无正文快照。
def _progress_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        value = {"state": "unavailable"}
    result = {key: value[key] for key in ("ledger_id", "state", "revision", "open_count", "ids_truncated") if key in value}
    counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
    result["counts"] = {key: item for key, item in counts.items()
                        if key in {"total", "pending", "in_progress", "done", "skipped", "blocked", "unknown"} and type(item) is int}
    result["open_ids"] = [key for key in (value.get("open_ids") or []) if isinstance(key, str)][:64]
    result["verification"] = "not_evaluated"
    return result
