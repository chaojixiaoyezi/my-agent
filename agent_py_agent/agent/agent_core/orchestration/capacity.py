"""主代理与递归代理共用的子代理容量事实和原子拒绝回执。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...settings.defaults import default_config_int
from ...tooling.models import ToolHandlerOutcome

if TYPE_CHECKING:
    from .delegation_constraints import StructuredDelegationConstraints

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
        slots, limits = available_creation_slots(agent)
    except SubagentCapacityStateError as exc:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            f"当前无法读取权威子代理容量状态（{exc}）；本批没有创建任何子代理。"
            "请核实容量账本可读、父代理记录完整后再重试，不要原样盲目重试。",
            error_code="SUBAGENT_CAPACITY_UNAVAILABLE",
            effect_outcome="not_started",
        )
    if limits.get("descendants_forbidden"):
        return subagent_quota_result(1, 0, limits)
    return slots, limits


# LLM: ``max_subagents`` is root-session scoped like 会话运行时 AgentControl;
# explicit owner-policy caps remain owner-wide and count every non-terminal run.
# All hierarchy levels must consume this one calculation rather than a per-parent shortcut.
# 函数用途: 按当前根任务容量、全局管理员配额、累计上限和单次上限计算真正可用的创建槽位。
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

    parent_constraints = parent_delegation_constraints(agent)
    cumulative_cap: int | None = None
    existing_cumulative = 0
    remaining_cumulative: int | None = None

    if parent_constraints is not None:
        if not parent_constraints.allow_descendants:
            candidates.append(0)
        if parent_constraints.max_cumulative_children is not None:
            cumulative_cap = max(0, int(parent_constraints.max_cumulative_children))
            parent_id = _resolve_parent_run_id(agent)
            existing_cumulative = cumulative_created_child_count(agent, parent_id)
            remaining_cumulative = max(0, cumulative_cap - existing_cumulative)
            candidates.append(remaining_cumulative)

    slots = max(0, min(candidates)) if candidates else _DEFAULT_MAX_SUBAGENTS
    limits: dict[str, int] = {
        "session_cap": session_cap,
        "session_active": session_active,
        "owner_cap": owner_policy_cap,
        "owner_active": owner_active,
        "active_agent_cap": active_agent_cap,
        "task_cap": task_cap,
        "task_active": task_active,
        "per_call_cap": per_call_cap,
    }
    if parent_constraints is not None:
        if not parent_constraints.allow_descendants:
            limits["descendants_forbidden"] = 1
            limits["allow_descendants"] = 0
        if cumulative_cap is not None:
            limits["cumulative_cap"] = cumulative_cap
            limits["existing_cumulative"] = existing_cumulative
            limits["remaining_cumulative"] = remaining_cumulative if remaining_cumulative is not None else 0

    return slots, limits


# LLM: 提取当前代理名下生效的结构化委托约束。
# 函数用途: 返回当前代理的委托约束对象或 None。
def parent_delegation_constraints(agent: object) -> StructuredDelegationConstraints | None:
    from .delegation_constraints import extract_delegation_constraints

    return extract_delegation_constraints(None, agent=agent)


# LLM: 解析当前代理的父级标识，供累计创建量对账。
# 函数用途: 获取当前 subagent run id 或根任务 id。
def _resolve_parent_run_id(agent: object) -> str:
    from ...runtime_context import current_subagent_run_id

    scoped_id = current_subagent_run_id(agent)
    if scoped_id:
        return scoped_id
    return _current_root_task_id(agent)


# LLM: 权威累计创建子代理数从 delegations 表直读并在 creation_guard 事务内与 SubAgentTask.child_ids 对账；已终结/取消的子代理永不释放配额。
# 函数用途: 统计当前父代理历史已创建的累计子代理总数。
def cumulative_created_child_count(agent: object, parent_run_id: str | None = None) -> int:
    resolved_parent_id = str(
        parent_run_id or _resolve_parent_run_id(agent) or ""
    ).strip()
    if not resolved_parent_id:
        return 0

    db_count = 0
    runtime_db = getattr(agent, "runtime_db", None) or getattr(
        getattr(agent, "subagents", None), "runtime_db", None
    )
    if runtime_db is not None:
        try:
            with runtime_db._runtime_connection() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) FROM delegations d
                    LEFT JOIN agent_runs p ON p.agent_run_id = d.parent_agent_run_id
                    WHERE d.parent_agent_run_id = ? OR p.run_id = ?""",
                    (resolved_parent_id, resolved_parent_id),
                ).fetchone()
                if row and row[0] is not None:
                    db_count = int(row[0])
        except Exception as exc:
            # 累计计数是权限上界；读取失败不能伪装成零历史释放额度。
            raise SubagentCapacityStateError(
                f"cumulative.delegations 读取失败: {type(exc).__name__}"
            ) from exc

    lineage_count = 0
    manager = getattr(agent, "subagents", None)
    if manager is not None:
        try:
            parent_task = manager.load(resolved_parent_id)
            if parent_task is not None:
                child_ids = getattr(parent_task, "child_ids", None)
                if isinstance(child_ids, (list, tuple)):
                    lineage_count = len(child_ids)
        except Exception as exc:
            raise SubagentCapacityStateError(
                f"cumulative.parent.child_ids 读取失败: {type(exc).__name__}"
            ) from exc

    return max(db_count, lineage_count)


# LLM: This structured error is shared by root and nested create paths; callers
# must reject the whole batch and never truncate it to the remaining slots.
# 函数用途: 生成子代理数量超限时的统一整批拒绝结果，告诉模型当前真实可用槽位。
def subagent_quota_result(
    requested: int,
    available: int,
    limits: dict[str, int],
) -> ToolHandlerOutcome:
    if limits.get("descendants_forbidden"):
        payload = {
            "ok": False,
            "error_code": "SUBAGENT_DESCENDANTS_FORBIDDEN",
            "error": "当前代理策略禁止创建后代子代理（allow_descendants=False）。本批没有创建任何子代理。",
            "requested": requested,
            "available": 0,
            "limits": limits,
            "how_to_fix": "不要为此代理创建子代理；直接在当前代理内完成任务。",
        }
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            json.dumps(payload, ensure_ascii=False, indent=2),
            error_code="SUBAGENT_DESCENDANTS_FORBIDDEN",
            effect_outcome="not_started",
        )

    if limits.get("cumulative_cap") is not None:
        cum_cap = limits["cumulative_cap"]
        existing = limits.get("existing_cumulative", 0)
        remaining = limits.get("remaining_cumulative", 0)
        if existing + requested > cum_cap or remaining < requested:
            payload = {
                "ok": False,
                "error_code": "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED",
                "error": "本次请求的子代理数量超过累计创建上限；已结束、取消或失败的子代理仍计入累计配额；没有创建任何部分批次。",
                "requested": requested,
                "available": available,
                "limits": limits,
                "how_to_fix": "减少 items 数量或复用已有的同名子代理；已结束或取消的子代理不释放累计配额。",
            }
            return ToolHandlerOutcome(
                "create_subagents",
                False,
                json.dumps(payload, ensure_ascii=False, indent=2),
                error_code="SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED",
                effect_outcome="not_started",
            )

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


# LLM: 规范写者同步维护带 pending 栅栏的索引；容量热查询不列举终态历史，partial 必须拒绝。
# 函数用途: 统计当前 owner 与当前根任务已占用的子代理数。
def _active_subagent_counts(agent: object) -> tuple[int, int]:
    try:
        from ...subagents.manager import creation_capacity_counts

        return creation_capacity_counts(agent, _current_root_task_id(agent))
    except Exception as exc:
        raise SubagentCapacityStateError("subagent registry is unavailable") from exc


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
    "cumulative_created_child_count",
    "parent_delegation_constraints",
    "subagent_quota_result",
]
