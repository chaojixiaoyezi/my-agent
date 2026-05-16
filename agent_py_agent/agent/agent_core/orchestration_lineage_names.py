# LLM: create_subagents lineage naming helpers keep orchestration_tools thin and deterministic.
# 模块用途: 统一顶层小傻妞默认命名，把“层级/角色/编号”合同从工具入口拆出来。

from __future__ import annotations

from ..subagents.services.base import CreateRunParams


# LLM: indexed_count_params makes count fanout replayable without merging sibling children.
# 函数用途: count 批量或默认名创建时补稳定序号，并让重复 create 能按同一合同复用对应 run。
def indexed_count_params(run_params: CreateRunParams, *, index: int, count: int) -> CreateRunParams:
    task_goal = f"{run_params.goal} / 子任务{index}" if count > 1 else run_params.goal
    task_name = indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=count > 1,
    )
    return CreateRunParams(**{**run_params.__dict__, "goal": task_goal, "agent_name": task_name})


# LLM: indexed_item_params gives system-generated item names the same stable lineage id contract.
# 函数用途: items[] 派工时为默认名补编号；显式语义名只有批量时才追加编号，避免破坏旧引用。
def indexed_item_params(run_params: CreateRunParams, *, index: int, total: int) -> CreateRunParams:
    task_name = indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=total > 1,
    )
    return CreateRunParams(**{**run_params.__dict__, "agent_name": task_name})


# LLM: indexed_agent_name keeps sibling display names deterministic without trusting model prose.
# 函数用途: 自动生成名统一变为 小傻妞-role-index；批量显式名也补 index，避免看板和幂等合并错人。
def indexed_agent_name(agent_name: str, *, role: str, index: int, require_index: bool = False) -> str:
    name = str(agent_name or "").strip()
    if needs_system_lineage_name(name):
        return f"小傻妞-{role_suffix(role)}-{index}"
    if require_index and not has_trailing_identifier(name):
        return f"{name}-{index}"
    return name


# LLM: needs_system_lineage_name detects names produced by fallback policy rather than user semantics.
# 函数用途: 判断是否需要由系统补全 role/index，覆盖空名、小傻妞、worker/general 等默认名。
def needs_system_lineage_name(agent_name: str) -> bool:
    text = str(agent_name or "").strip().strip("-")
    return text in {
        "",
        "general",
        "worker",
        "subagent",
        "agent",
        "小傻妞",
        "小傻妞-general",
        "小傻妞-worker",
        "小傻妞-subagent",
        "小傻妞-agent",
    } or text.endswith("傻妞")


# LLM: role_suffix keeps role text stable and readable inside generated 小傻妞 names.
# 函数用途: 把 role 归一成名字片段；general/child 这类泛称统一视作 worker。
def role_suffix(role: str) -> str:
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child", "subagent", "agent"}:
        return "worker"
    return suffix


# LLM: has_trailing_identifier prevents replay from appending -1 forever.
# 函数用途: 判断显式名字是否已经带数字编号；已有编号时不重复追加。
def has_trailing_identifier(agent_name: str) -> bool:
    tail = str(agent_name or "").strip().rsplit("-", 1)[-1]
    return tail.isdigit()
