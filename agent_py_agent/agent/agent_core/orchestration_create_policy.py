# LLM: Create-subagent policy helpers keep role decisions structured without turning prose into rules.
# 模块用途: 子代理创建前的 role 纠偏和 CreateRunParams 组装。

from __future__ import annotations

import json

from ..subagents.role_templates import role_template_id_for_role
from ..subagents.services.base import CreateRunParams
from .coordinator_seed_tools import explicit_root_allowed_tools
from .orchestration_create_constraints import (
    goal_has_concrete_file_target,
    resolved_extra_write_roots,
    role_allows_direct_product_work,
)
from .orchestration_create_context import create_context_manifest, create_context_packs
from .orchestration_root_contract import explicit_root_goal_with_user_contract
from .orchestration_workflow_mode import tool_workflow_mode as _tool_workflow_mode
from .parameters import _positive_int, _string_list
from .spawn_role_seed import is_explicit_root_role


# LLM: create_run_params turns model-facing create_subagents args into manager params.
# 函数用途: 生成 CreateRunParams；只做结构化归一和必要保真，不制造额外流程限制。
def create_run_params(
    agent,
    raw_params: dict[str, object],
    goal: str,
    allowed_tools: list[str] | None,
):
    workflow_mode = _tool_workflow_mode(raw_params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    role = _role_from_create_intent(raw_params, goal, agent)
    is_explicit_root = is_explicit_root_role(role)
    if is_explicit_root:
        workflow_mode = "off"
        allowed_tools = explicit_root_allowed_tools(allowed_tools)
        goal = explicit_root_goal_with_user_contract(agent, goal)
    elif _should_disable_generic_workflow_for_concrete_worker(raw_params, goal, role, workflow_mode):
        workflow_mode = "off"
    return CreateRunParams(
        goal=goal,
        thought=str(raw_params.get("thought") or "根据父代理派工执行，并保留可验收证据。").strip(),
        plan=_create_plan(raw_params),
        agent_name=_root_agent_name(raw_params, role),
        role=role,
        allowed_tools=allowed_tools,
        owner=str(raw_params.get("owner") or "").strip(),
        supervisor=str(raw_params.get("supervisor") or "parent").strip(),
        final_owner=str(raw_params.get("final_owner") or "").strip(),
        acceptance_checks=_string_list(raw_params.get("acceptance_checks")),
        extra_write_roots=resolved_extra_write_roots(agent, raw_params, goal),
        context_manifest=create_context_manifest(raw_params),
        context_packs=create_context_packs(raw_params),
        workflow_mode=workflow_mode,
    )


# LLM: _role_from_create_intent repairs structured-argument slips before workflow expansion.
# 函数用途: 只按 role、agent_name 和 tasks 这类结构化参数纠偏；不从 goal/用户 prompt 的自然语言猜角色。
def _role_from_create_intent(raw_params: dict[str, object], goal: str, agent) -> str:
    role = str(raw_params.get("role") or "worker").strip() or "worker"
    if _role_field_is_lineage_agent_name(role):
        return _role_from_lineage_agent_name(role)
    if is_explicit_root_role(role):
        return role
    if _json_task_items(raw_params.get("tasks")):
        return "coordinator"
    if role == "worker" and _has_child_dispatch_tool(raw_params) and not _role_identity_is_quality(raw_params):
        return "coordinator"
    return role


# LLM: _has_child_dispatch_tool treats explicit tool grants as role intent, not prose.
# 函数用途: 当 create_subagents 参数已经给出 schedule_child_subagents/dispatch_subagents 时，按带队节点创建。
def _has_child_dispatch_tool(raw_params: dict[str, object]) -> bool:
    if raw_params.get("_item_allowed_tools_explicit") is False:
        return False
    tools = {str(item or "").strip().lower() for item in _string_list(raw_params.get("allowed_tools"))}
    return bool({"schedule_child_subagents", "dispatch_subagents"}.intersection(tools))


# LLM: _role_identity_is_quality protects QA templates from being rewritten just because they can inspect boards.
# 函数用途: tester/bug_finder/acceptor 这类质量角色可拥有调度/看板工具，但角色身份不能被改成 coordinator。
def _role_identity_is_quality(raw_params: dict[str, object]) -> bool:
    identity = f"{raw_params.get('role') or ''} {raw_params.get('agent_name') or ''}"
    return role_template_id_for_role(identity, fallback="") in {"tester", "bug_finder", "acceptor"}


# LLM: _should_disable_generic_workflow_for_concrete_worker prevents simple deliverable workers from growing workflow children.
# 函数用途: 明确文件交付 worker 直接干活，QA/验收由父级按实际完成状态再派。
def _should_disable_generic_workflow_for_concrete_worker(
    raw_params: dict[str, object],
    goal: str,
    role: str,
    workflow_mode: str,
) -> bool:
    if workflow_mode != "auto":
        return False
    if not role_allows_direct_product_work(role):
        return False
    if _positive_int(raw_params.get("count"), default=1) <= 0:
        return False
    return goal_has_concrete_file_target(goal)


# LLM: _role_field_is_lineage_agent_name catches display names leaked into structured role.
# 函数用途: 判断模型是否把“小傻妞-xxx”这类代理名字误填进 role 字段。
def _role_field_is_lineage_agent_name(role: str) -> bool:
    text = str(role or "").strip()
    return "小傻妞" in text


# LLM: _role_from_lineage_agent_name maps leaked display names through template ids.
# 函数用途: 把带层级前缀的代理名字按模板 id 还原成标准 role；不按中文职责词猜。
def _role_from_lineage_agent_name(role: str) -> str:
    text = str(role or "").strip().lower().replace("-", "_")
    parts = [part for part in text.split("_") if part and part not in {"小傻妞", "小小傻妞", "agent", "subagent"}]
    template_role = role_template_id_for_role("_".join(parts), fallback="")
    return template_role or "worker"


# LLM: _root_agent_name gives top-level spawned agents the same lineage naming contract as descendants.
# 函数用途: create_subagents 未传 agent_name 时，用“小傻妞-role”兜底。
def _root_agent_name(raw_params: dict[str, object], role: str) -> str:
    explicit = str(raw_params.get("agent_name") or "").strip().strip("-")
    if explicit:
        return explicit
    role_name = _agent_name_from_role_field(raw_params)
    if role_name:
        return role_name
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child"}:
        suffix = "worker"
    return f"小傻妞-{suffix}"


# LLM: _agent_name_from_role_field preserves user-facing lineage names when the model used role wrongly.
# 函数用途: 如果 role 字段里其实是“小傻妞-xxx”显示名，就转存为 agent_name。
def _agent_name_from_role_field(raw_params: dict[str, object]) -> str:
    role_text = str(raw_params.get("role") or "").strip().strip("-")
    if not _role_field_is_lineage_agent_name(role_text):
        return ""
    return role_text.replace("_", "-")


# LLM: _create_plan prevents global batch plans from leaking into every item child.
# 函数用途: 优先用当前 item 自己的 plan；没有 plan 但有下级 tasks 时，把 tasks 变成协调者可读步骤。
def _create_plan(raw_params: dict[str, object]) -> list[str]:
    explicit = _string_list(raw_params.get("plan"))
    if explicit:
        return explicit
    task_hints = _child_task_hint_plan(raw_params.get("tasks"))
    if task_hints:
        return [
            "理解父级目标和可用资料",
            *task_hints,
            "汇总下级结果、证据 refs 和阻塞项",
            "等待父代理验收",
        ]
    return ["理解目标", "执行任务", "产出证据", "等待父代理验收"]


# LLM: _child_task_hint_plan turns 长期助手 nested tasks into readable coordinator steps.
# 函数用途: 把 item.tasks 的下级任务提示交给 coordinator，而不是丢在未使用参数里。
def _child_task_hint_plan(value: object) -> list[str]:
    items = _json_task_items(value)
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal") or "").strip()
        if not goal:
            continue
        role = str(item.get("role") or "worker").strip() or "worker"
        name = str(item.get("agent_name") or "").strip()
        suffix = f"（role={role}{', agent_name=' + name if name else ''}）"
        lines.append(f"按需创建/调度下级任务 {index}: {goal}{suffix}")
    return lines


# LLM: _json_task_items accepts the common nested tasks shapes produced by LLMs.
# 函数用途: 支持 list、单对象和 JSON 字符串形式的下级任务提示；解析失败时返回空列表。
def _json_task_items(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []
