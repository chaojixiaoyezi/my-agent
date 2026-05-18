# LLM: QA role contracts read persisted attributes, not goal or acceptance prose.
# 模块用途: 统一读取 task.attributes 里的 required_qa_roles / qa_roles，并按实际 role identity 判断已有 QA 覆盖。

from __future__ import annotations

import re

from ..role_contracts import normalize_subagent_role
from ..role_templates import role_template_id_for_role

QA_ROLE_ORDER = ("tester", "bug_finder", "acceptor")
_QA_ROLE_FIELDS = frozenset({"required_qa_roles", "qa_roles"})


# LLM: qa_roles_required_by_task extracts requested QA roles from task attributes only.
# 函数用途: 父任务必须在 attributes.required_qa_roles / qa_roles 声明，才会形成代码层 QA 义务。
def qa_roles_required_by_task(task) -> list[str]:
    if qa_role_task_is_leaf(task):
        return []
    return qa_roles_from_attributes(_task_attributes(task))


# LLM: qa_role_contract_text renders attribute-backed role ids for legacy display callers only.
# 函数用途: 兼容旧调用方；内容来自 attributes，不再拼接 goal 或 acceptance_checks。
def qa_role_contract_text(task) -> str:
    roles = qa_roles_from_attributes(_task_attributes(task))
    return "required_qa_roles: " + ", ".join(roles) if roles else ""

# LLM: qa_roles_from_attributes is the runtime contract reader for QA obligations.
# 函数用途: 从 task.attributes 的 QA 字段读取角色 id，支持字符串、列表和嵌套列表格式。
def qa_roles_from_attributes(attributes: dict[str, object]) -> list[str]:
    requested: set[str] = set()
    for field in _QA_ROLE_FIELDS:
        requested.update(_role_items_from_object(attributes.get(field)))
    return [role for role in QA_ROLE_ORDER if role in requested]


# LLM: _task_attributes normalizes persisted task attributes without looking at task prose.
# 函数用途: 安全取得 task.attributes；缺失或非 dict 时返回空字典。
def _task_attributes(task) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    return attrs if isinstance(attrs, dict) else {}


# LLM: qa_role_identity_roles reads actual role identity fields, not inherited goal prose.
# 函数用途: 从 persisted role/agent_name 判断一个已存在或候选 run 是否真正属于 QA 角色。
def qa_role_identity_roles(*, role: str, agent_name: str = "") -> set[str]:
    roles: set[str] = set()
    for value in (role, agent_name):
        normalized = _role_template_identity(value)
        if normalized in QA_ROLE_ORDER:
            roles.add(normalized)
    return roles


# LLM: qa_role_task_is_leaf treats terminal workers/reviewers as not responsible for spawning QA copies.
# 函数用途: 判断任务是否已经是执行叶子或 QA reviewer；这类节点不负责继续创建 tester/bug_finder/acceptor 下级。
def qa_role_task_is_leaf(task) -> bool:
    role = str(getattr(task, "role", "") or "").lower()
    agent_name = str(getattr(task, "agent_name", "") or "").lower()
    if qa_role_identity_roles(role=role, agent_name=agent_name):
        return True
    return role == "leaf_worker" or "leaf" in agent_name


# LLM: _role_template_identity maps explicit role/name tokens through the active template catalog.
# 函数用途: 允许 `小傻妞-tester-1` 这类结构化模板 id 命名被识别，但不匹配中文自然语言职责词。
def _role_template_identity(value: object) -> str:
    normalized = normalize_subagent_role(str(value or ""))
    if normalized in QA_ROLE_ORDER:
        return normalized
    return role_template_id_for_role(normalized, fallback="")


# LLM: _role_items tokenizes one structured QA role value.
# 函数用途: 支持逗号、顿号、竖线和空白分隔的 QA role id 列表。
def _role_items(value: object) -> set[str]:
    roles: set[str] = set()
    for raw in re.split(r"[\s,，、|/]+", str(value or "")):
        item = _role_template_identity(raw)
        if item in QA_ROLE_ORDER:
            roles.add(item)
    return roles


# LLM: _role_items_from_object accepts config-like role values while keeping role ids explicit.
# 函数用途: 支持 required_qa_roles 写成字符串、列表或元组，最终只保留已知 QA role id。
def _role_items_from_object(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        roles: set[str] = set()
        for item in value:
            roles.update(_role_items_from_object(item))
        return roles
    return _role_items(value)
