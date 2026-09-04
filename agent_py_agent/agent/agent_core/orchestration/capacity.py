"""主代理与递归代理共用的子代理容量事实和原子拒绝回执。"""

from __future__ import annotations

import json

from ...settings.defaults import default_config_int
from ...tooling.models import ToolHandlerOutcome

_DEFAULT_MAX_SUBAGENTS = default_config_int("max_subagents")


# LLM: Capacity storage failures are distinct from user-requested over-capacity
# and must fail closed before any child record is materialized.
# 类用途: 标记权威子代理占用量无法读取，防止根代理或递归代理按零占用继续创建。
class SubagentCapacityStateError(RuntimeError):
    """Canonical live subagent usage could not be read safely."""


# LLM: Root and descendant creation must call this inside the same owner-local
# creation transaction so the durable count and subsequent writes are atomic.
# 函数用途: 统一读取当前可用子代理容量；账本异常时返回整批未启动的结构化错误。
def checked_creation_capacity(
    agent: object,
) -> tuple[int, dict[str, int]] | ToolHandlerOutcome:
    try:
        return available_creation_slots(agent)
    except SubagentCapacityStateError:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            "当前无法读取权威子代理容量状态；本批没有创建任何子代理。",
            error_code="SUBAGENT_CAPACITY_UNAVAILABLE",
            effect_outcome="not_started",
        )


# LLM: ``max_subagents`` is root-session scoped like 会话运行时 AgentControl;
# explicit owner-policy caps remain owner-wide and count every non-terminal run.
# All hierarchy levels must consume this one calculation rather than a per-parent shortcut.
# 函数用途: 按当前根任务容量、全局管理员配额和单次上限计算真正可用的创建槽位。
def available_creation_slots(agent: object) -> tuple[int, dict[str, int]]:
    """Return the strictest remaining session/owner/task/per-call capacity."""

    session_cap = _configured_max_subagents(agent)
    owner_policy_cap = _positive_limit(
        getattr(getattr(agent, "owner_policy", None), "max_subagents", 0)
    )
    per_call_cap = _positive_limit(
        getattr(
            getattr(agent, "config", None),
            "subagent_hierarchy_max_children_per_tool_call",
            0,
        )
    )
    task_cap = _positive_limit(
        getattr(getattr(agent, "config", None), "task_max_subagents", 0)
    )
    owner_active, task_active = _active_subagent_counts(agent)
    has_root_scope = bool(_current_root_task_id(agent))
    session_active = task_active if has_root_scope else owner_active
    candidates = [session_cap - session_active] if session_cap else []
    if owner_policy_cap:
        candidates.append(owner_policy_cap - owner_active)
    active_agent_cap = _positive_limit(
        getattr(getattr(agent, "owner_policy", None), "max_active_agents", 0)
    )
    if active_agent_cap:
        candidates.append(active_agent_cap - 1 - owner_active)
    if per_call_cap:
        candidates.append(per_call_cap)
    if task_cap:
        candidates.append(task_cap - task_active)
    slots = max(0, min(candidates)) if candidates else _DEFAULT_MAX_SUBAGENTS
    return slots, {
        "session_cap": session_cap,
        "session_active": session_active,
        "owner_cap": owner_policy_cap,
        "owner_active": owner_active,
        "active_agent_cap": active_agent_cap,
        "task_cap": task_cap,
        "task_active": task_active,
        "per_call_cap": per_call_cap,
    }


# LLM: This structured error is shared by root and nested create paths; callers
# must reject the whole batch and never truncate it to the remaining slots.
# 函数用途: 生成子代理数量超限时的统一整批拒绝结果，告诉模型当前真实可用槽位。
def subagent_quota_result(
    requested: int,
    available: int,
    limits: dict[str, int],
) -> ToolHandlerOutcome:
    payload = {
        "ok": False,
        "error_code": "SUBAGENT_CAPACITY_EXCEEDED",
        "error": "本次请求的子代理数量超过当前可用容量；没有创建任何部分批次。",
        "requested": requested,
        "available": available,
        "limits": limits,
        "how_to_fix": "减少 items 数量后重试；已有子代理结束后容量会自动释放。",
    }
    return ToolHandlerOutcome(
        "create_subagents",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="SUBAGENT_CAPACITY_EXCEEDED",
        effect_outcome="not_started",
    )


# LLM: Config parsing stays here so root and recursive calls cannot drift onto
# different defaults when a lightweight test or older caller omits the field.
# 函数用途: 读取会话树的子代理上限，非法值回到正式配置默认值。
def _configured_max_subagents(agent: object) -> int:
    raw_value = getattr(
        getattr(agent, "config", None),
        "max_subagents",
        _DEFAULT_MAX_SUBAGENTS,
    )
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SUBAGENTS


# LLM: Count canonical non-terminal runs globally for the owner and exactly for
# the current conversation task; projections and model prose are not capacity facts.
# 函数用途: 统计当前 owner 与当前根任务已占用的子代理数。
def _active_subagent_counts(agent: object) -> tuple[int, int]:
    try:
        from ...subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

        runs = list(agent.subagents.list_runs())
        active = [
            run
            for run in runs
            if str(getattr(run, "id", "") or "").strip()
            and not task_status_in(
                getattr(run, "status", ""),
                SUBAGENT_ENDED_STATUSES,
            )
        ]
    except Exception as exc:
        raise SubagentCapacityStateError("subagent registry is unavailable") from exc
    task_id = _current_root_task_id(agent)
    if not task_id:
        return len(active), 0
    try:
        related_ids = set(agent.subagent_run_ids_for_request(task_id))
    except Exception as exc:
        raise SubagentCapacityStateError("task subagent lineage is unavailable") from exc
    return len(active), sum(
        str(getattr(run, "id", "") or "").strip() in related_ids for run in active
    )


# LLM: Conversation task identity is the session-tree capacity key; request/run
# IDs are bounded compatibility fallbacks, never inferred from task descriptions.
# 函数用途: 从当前结构化运行参数解析根任务身份。
def _current_root_task_id(agent: object) -> str:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if isinstance(attrs, dict):
        task_id = str(attrs.get("conversation_task_id") or "").strip()
        if task_id:
            return task_id
    return str(
        getattr(current, "task_id", "")
        or getattr(current, "request_id", "")
        or getattr(current, "run_id", "")
        or ""
    ).strip()


# LLM: Zero means this layer adds no limit; invalid values never become
# accidental negative capacity.
# 函数用途: 把配置容量收紧为非负整数。
def _positive_limit(value: object) -> int:
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "SubagentCapacityStateError",
    "available_creation_slots",
    "checked_creation_capacity",
    "subagent_quota_result",
]
