# LLM: Create-subagent policy helpers keep natural delegation structured without turning roles into restrictions.
# 模块用途: 子代理创建前的 role 纠偏和 CreateRunParams 组装。

from __future__ import annotations

import json

from ..subagents.services.base import CreateRunParams
from .coordinator_seed_tools import explicit_root_allowed_tools
from .orchestration_create_constraints import (
    goal_has_concrete_file_target,
    goal_has_single_concrete_file_target,
    resolved_extra_write_roots,
    role_allows_direct_product_work,
)
from .orchestration_create_context import create_context_manifest, create_context_packs
from .orchestration_root_contract import explicit_root_goal_with_user_contract
from .orchestration_workflow_mode import tool_workflow_mode as _tool_workflow_mode
from .parameters import _positive_int, _string_list
from .spawn_role_seed import is_explicit_root_role

_COORDINATOR_SEED_MARKERS = (
    "coordinator",
    "coordination",
    "root",
    "lead",
    "协调",
    "统筹",
    "小傻妞-root",
)
_CHILD_SPAWN_INTENT_MARKERS = (
    "schedule_child_subagents",
    "dispatch_subagents",
    "create child",
    "spawn child",
    "children must be created",
    "创建下一层",
    "继续创建",
    "派下一层",
    "派发下一层",
    "下级",
    "下层",
    "孩子",
    "子代理",
    "不能自己写最终产物",
    "不要写最终产物",
)
_HIERARCHY_CONTRACT_MARKERS = (
    "主代理 -> 小傻妞",
    "root -> 子",
    "depth=1",
    "小小傻妞",
    "child-coordinator",
    "child_coordinator",
)
_USER_STYLE_DELEGATION_MARKERS = (
    "找小小傻妞",
    "小小傻妞帮忙",
    "小傻妞再找小小傻妞",
    "小傻妞再派小小傻妞",
    "小傻妞派小小傻妞",
    "小傻妞如果需要",
    "子代理再派",
    "子代理派下一层",
    "下一层子代理",
    "下一级子代理",
)
_USER_STYLE_TOP_AGENT_MARKERS = (
    "小傻妞-",
    "派小傻妞",
    "让小傻妞",
    "不要你自己亲自写",
    "不要自己亲自写",
    "不要你亲自写",
    "小小傻妞",
)
_EXPLICIT_LOCAL_CHILD_SPAWN_MARKERS = (
    "schedule_child_subagents",
    "dispatch_subagents",
    "创建并调度",
    "创建至少",
    "调度至少",
    "至少2个孙代理",
    "至少 2 个孙代理",
    "至少两个孙代理",
    "至少2名孙代理",
    "至少 2 名孙代理",
    "孙代理",
    "小小傻妞",
)


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


# LLM: _role_from_create_intent repairs obvious structured-argument slips before workflow expansion.
# 函数用途: 当模型把“创建 root/coordinator 并继续派下一层”的任务误填为 worker 时，按目标意图纠偏。
def _role_from_create_intent(raw_params: dict[str, object], goal: str, agent) -> str:
    role = str(raw_params.get("role") or "worker").strip() or "worker"
    if _role_field_is_lineage_agent_name(role):
        return _role_from_lineage_agent_name(role)
    if is_explicit_root_role(role):
        return role
    if _json_task_items(raw_params.get("tasks")):
        return "coordinator"
    if _has_explicit_local_child_spawn_intent(raw_params, goal):
        return "coordinator"
    if _has_coordinator_seed_intent(raw_params, goal, agent):
        return "coordinator"
    if _has_user_style_delegation_intent(raw_params, goal, agent):
        return "coordinator"
    return role


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


# LLM: _has_coordinator_seed_intent detects local coordinator-plus-child intent.
# 函数用途: 只在参数自身明显表达“协调者/继续派工”时修 role。
def _has_coordinator_seed_intent(raw_params: dict[str, object], goal: str, agent) -> bool:
    local_text = _local_create_intent_text(raw_params, goal)
    if not _contains_any(local_text, _COORDINATOR_SEED_MARKERS):
        return False
    if _contains_any(local_text, _CHILD_SPAWN_INTENT_MARKERS):
        return True
    user_text = str(getattr(agent, "_current_user_prompt", "") or "")
    return _contains_any(local_text + "\n" + user_text, _HIERARCHY_CONTRACT_MARKERS)


# LLM: _has_user_style_delegation_intent catches natural "小傻妞找小小傻妞" prompts.
# 函数用途: 用户自然语言要求第一层继续派下一层时，把顶层 worker 纠成 coordinator。
def _has_user_style_delegation_intent(raw_params: dict[str, object], goal: str, agent) -> bool:
    user_text = str(getattr(agent, "_current_user_prompt", "") or "").lower()
    if not _contains_any(user_text, _USER_STYLE_DELEGATION_MARKERS):
        return False
    if goal_has_single_concrete_file_target(goal):
        return False
    local_text = _local_create_intent_text(raw_params, goal)
    explicit_count = _positive_int(raw_params.get("count"), default=1)
    return explicit_count == 1 and _contains_any(user_text + "\n" + local_text, _USER_STYLE_TOP_AGENT_MARKERS)


# LLM: _has_explicit_local_child_spawn_intent trusts a child's own goal over the role label.
# 函数用途: 子任务目标明确要求创建/调度孙代理时，即便 role 写成 worker，也按 coordinator 创建。
def _has_explicit_local_child_spawn_intent(raw_params: dict[str, object], goal: str) -> bool:
    local_text = _local_create_intent_text(raw_params, goal)
    return _contains_any(local_text, _EXPLICIT_LOCAL_CHILD_SPAWN_MARKERS)


# LLM: _local_create_intent_text extracts task intent only; tool grants are capability, not role intent.
# 函数用途: 收集 create_subagents 本次参数里的 goal、name、thought、plan 等任务语义字段。
def _local_create_intent_text(raw_params: dict[str, object], goal: str) -> str:
    parts = [
        goal,
        str(raw_params.get("agent_name") or ""),
        str(raw_params.get("thought") or ""),
        " ".join(_string_list(raw_params.get("plan"))),
    ]
    return "\n".join(part for part in parts if part).lower()


# LLM: _contains_any centralizes small multilingual marker checks.
# 函数用途: 检查文本中是否出现任一标记。
def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


# LLM: _role_field_is_lineage_agent_name catches display names leaked into structured role.
# 函数用途: 判断模型是否把“小傻妞-xxx”这类代理名字误填进 role 字段。
def _role_field_is_lineage_agent_name(role: str) -> bool:
    text = str(role or "").strip()
    return "小傻妞" in text


# LLM: _role_from_lineage_agent_name maps leaked display names back to role-template ids.
# 函数用途: 把带层级前缀的代理名字还原成标准 role。
def _role_from_lineage_agent_name(role: str) -> str:
    text = str(role or "").strip().lower()
    if "coordinator" in text or "协调" in text:
        return "coordinator"
    if "tester" in text or "测试" in text:
        return "tester"
    if "accept" in text or "验收" in text:
        return "acceptor"
    if "critic" in text or "bug" in text or "找茬" in text:
        return "bug_finder"
    return "worker"


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
