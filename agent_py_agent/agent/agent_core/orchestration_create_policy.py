# LLM: Create-subagent policy helpers keep role decisions structured without turning prose into rules.
# 模块用途: 子代理创建前的 role 纠偏和 CreateRunParams 组装。

from __future__ import annotations

import json
from typing import Any

from ..subagents.role_contracts import normalize_subagent_role
from ..subagents.role_templates import role_template_id_for_role
from ..subagents.services.base import CreateRunParams
from .coordinator_seed_tools import explicit_root_allowed_tools
from .orchestration_create_constraints import (
    resolved_extra_write_roots,
    role_allows_direct_product_work,
)
from .orchestration_create_context import create_context_manifest, create_context_packs
from .orchestration_workflow_mode import tool_workflow_mode as _tool_workflow_mode
from .parameters import _positive_int, _string_list
from .runner_input_dependencies import params_output_refs
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
    context_manifest = create_context_manifest(raw_params)
    context_packs = create_context_packs(raw_params)
    parent_id, root_id, depth = _lineage_from_params_or_repair_contract(agent, raw_params, context_packs)
    if is_explicit_root:
        workflow_mode = "off"
        allowed_tools = explicit_root_allowed_tools(allowed_tools)
    elif _should_disable_generic_workflow_for_concrete_worker(raw_params, goal, role, workflow_mode):
        workflow_mode = "off"
    return CreateRunParams(
        goal=goal,
        thought=str(raw_params.get("thought") or "根据父代理派工执行，并保留可验收证据。").strip(),
        plan=_create_plan(raw_params),
        agent_name=_root_agent_name(raw_params, role),
        role=role,
        parent_id=parent_id,
        root_id=root_id,
        depth=depth,
        allowed_tools=allowed_tools,
        owner=str(raw_params.get("owner") or "").strip(),
        supervisor=str(raw_params.get("supervisor") or "parent").strip(),
        final_owner=str(raw_params.get("final_owner") or "").strip(),
        acceptance_checks=_string_list(raw_params.get("acceptance_checks")),
        extra_write_roots=resolved_extra_write_roots(agent, raw_params, goal),
        context_manifest=context_manifest,
        context_packs=context_packs,
        workflow_mode=workflow_mode,
        attributes=_create_attributes(raw_params),
    )


# LLM: repair create calls inherit failed-run lineage from machine repair contracts.
# 函数用途: 顶层 root 用 create_subagents 创建修复任务时，自动挂到失败 run 下，避免形成重复 root。
def _lineage_from_params_or_repair_contract(agent, raw_params: dict[str, object], context_packs: list[dict[str, object]]):
    parent_id = str(raw_params.get("parent_id") or "").strip()
    root_id = str(raw_params.get("root_id") or "").strip()
    depth = _positive_int(raw_params.get("depth"), default=0)
    if parent_id:
        return parent_id, root_id, depth
    failed_run_id = _single_repair_failed_run_id(context_packs)
    if not failed_run_id:
        return "", root_id, depth
    try:
        failed = agent.subagents.load(failed_run_id)
    except (AttributeError, FileNotFoundError, OSError, TypeError, ValueError):
        return "", root_id, depth
    return (
        failed_run_id,
        str(getattr(failed, "root_id", "") or getattr(failed, "id", "") or ""),
        int(getattr(failed, "depth", 0) or 0) + 1,
    )


# LLM: only explicit repair_contract.failed_run_ids may drive repair lineage.
# 函数用途: 从 context_packs 的结构化合同读取单一失败 run；不从 goal 文本猜父子关系。
def _single_repair_failed_run_id(context_packs: list[dict[str, object]]) -> str:
    for pack in context_packs:
        contract = pack.get("contract") if isinstance(pack, dict) else None
        if not _is_repair_contract(contract):
            continue
        failed_ids = _string_list(contract.get("failed_run_ids"))
        return failed_ids[0] if len(failed_ids) == 1 else ""
    return ""


# LLM: _is_repair_contract is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _is_repair_contract(contract: Any) -> bool:
    return isinstance(contract, dict) and str(contract.get("schema") or "") == "subagent_repair_contract.v1"


# LLM: _role_from_create_intent repairs structured-argument slips before workflow expansion.
# 函数用途: 只按 role、agent_name 和 tasks 这类结构化参数纠偏；不从 goal/用户 prompt 的自然语言猜角色。
def _role_from_create_intent(raw_params: dict[str, object], goal: str, agent) -> str:
    role = _structured_role_token(raw_params.get("role"))
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


# LLM: _structured_role_token normalizes role as a protocol field, not as display prose.
# 函数用途: role 只接受 ASCII 机器 token；带展示前缀的值只可通过其中模板 id 恢复，中文职责不参与判断。
def _structured_role_token(value: object) -> str:
    text = str(value or "worker").strip() or "worker"
    normalized = normalize_subagent_role(text)
    cleaned = normalized.strip().lower().replace("-", "_")
    if _is_ascii_role_token(cleaned):
        return cleaned
    template_role = role_template_id_for_role(cleaned, fallback="")
    return template_role or "worker"


# LLM: _is_ascii_role_token is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _is_ascii_role_token(value: str) -> bool:
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_")
    return bool(value) and all(ch in allowed for ch in value)


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
    return bool(params_output_refs(raw_params))


# LLM: _create_attributes persists create-time machine facts beside the human-facing goal.
# 函数用途: 把 output/input/QA/static/content 等结构化工具参数写入 task.attributes，运行期不再解析 goal。
def _create_attributes(raw_params: dict[str, object]) -> dict[str, object]:
    attrs = dict(raw_params.get("attributes") or {}) if isinstance(raw_params.get("attributes"), dict) else {}
    for key in _LIST_ATTRIBUTE_FIELDS:
        values = _string_list(raw_params.get(key))
        if values and key not in attrs:
            attrs[key] = values
    for key in _SCALAR_ATTRIBUTE_FIELDS:
        value = str(raw_params.get(key) or "").strip()
        if value and key not in attrs:
            attrs[key] = value
    for key in _MAPPING_ATTRIBUTE_FIELDS:
        value = raw_params.get(key)
        if isinstance(value, dict) and key not in attrs:
            attrs[key] = dict(value)
    for key in _BOOL_ATTRIBUTE_FIELDS:
        if key in raw_params and key not in attrs:
            attrs[key] = _bool_attribute(raw_params.get(key))
    return attrs


_LIST_ATTRIBUTE_FIELDS = (
    "artifact_refs",
    "forbidden_files",
    "input_files",
    "input_refs",
    "output_files",
    "output_refs",
    "qa_roles",
    "domain_scopes",
    "forbidden_child_scopes",
    "hierarchy_contracts",
    "capability_contracts",
    "required_content_lines",
    "required_dom_ids",
    "required_files",
    "required_qa_roles",
    "required_read_paths",
    "workflow_risk_tags",
)
_MAPPING_ATTRIBUTE_FIELDS = ("required_content_files",)
_BOOL_ATTRIBUTE_FIELDS = ("require_script",)
_SCALAR_ATTRIBUTE_FIELDS = (
    "preferred_workflow_template",
    "subagent_workflow_template",
    "workflow_task_type",
    "workflow_template_id",
)


# LLM: _bool_attribute is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _bool_attribute(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value == 1
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# LLM: _root_agent_name gives top-level spawned agents the same lineage naming contract as descendants.
# 函数用途: create_subagents 未传 agent_name 时，用“小傻妞-role”兜底。
def _root_agent_name(raw_params: dict[str, object], role: str) -> str:
    explicit = str(raw_params.get("agent_name") or "").strip().strip("-")
    if explicit:
        return explicit
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child"}:
        suffix = "worker"
    return f"小傻妞-{suffix}"


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


# LLM: _child_task_hint_plan turns Hermes-style nested tasks into readable coordinator steps.
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
