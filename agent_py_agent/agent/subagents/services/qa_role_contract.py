# LLM: QA role contracts read structured role fields, not prose keywords.
# 模块用途: 统一读取 required_qa_roles / qa_roles 机器字段，并按实际 role identity 判断已有 QA 覆盖。

from __future__ import annotations

import re

from ..role_contracts import normalize_subagent_role
from ..role_templates import role_template_id_for_role

QA_ROLE_ORDER = ("tester", "bug_finder", "acceptor")
_QA_ROLE_FIELDS = frozenset({"required_qa_roles", "qa_roles"})
_QA_ROLE_FIELD_RE = re.compile(r"^\s*(?:[-*]\s*)?(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<tail>.*)$")
_INHERITED_CONTRACT_MARKERS = (
    "继承父级目标/边界",
    "父级层级/协作约束",
    "父级能力/工具/安全约束",
    "相关父级片段",
    "父级摘要",
)


# LLM: qa_roles_required_by_task extracts requested QA roles from structured parent contracts only.
# 函数用途: 父任务必须显式写 `required_qa_roles: tester, acceptor` 才会形成代码层 QA 义务。
def qa_roles_required_by_task(task) -> list[str]:
    if qa_role_task_is_leaf(task):
        return []
    return qa_roles_from_text(qa_role_contract_text(task))


# LLM: qa_role_contract_text avoids trusting output summaries when determining role requirements.
# 函数用途: 只合并任务目标和验收条件，用来读取 required_qa_roles/qa_roles 字段，不读取模型自称完成的输出。
def qa_role_contract_text(task) -> str:
    values = [_direct_role_contract_text(str(getattr(task, "goal", "") or ""))]
    values.extend(_direct_role_contract_text(str(item)) for item in getattr(task, "acceptance_checks", []) or [])
    return "\n".join(values)


# LLM: _direct_role_contract_text limits QA obligations to the current node's own goal.
# 函数用途: 继承块里的 QA 字段是父级背景，不应让每个中间 coordinator 都背同一套 QA 硬义务。
def _direct_role_contract_text(text: str) -> str:
    value = str(text or "")
    positions = [index for marker in _INHERITED_CONTRACT_MARKERS if (index := value.find(marker)) >= 0]
    if not positions:
        return value
    return value[: min(positions)]


# LLM: qa_roles_from_text reads only required_qa_roles/qa_roles protocol fields.
# 函数用途: 从机器字段中提取 tester、bug_finder、acceptor，普通“测试/验收/找茬”自然语言不参与。
def qa_roles_from_text(text: str) -> list[str]:
    requested: set[str] = set()
    active = False
    for raw in str(text or "").splitlines():
        active, roles = _qa_role_contract_line(raw, active=active)
        requested.update(roles)
    requested.update(_inline_role_id_mentions(text))
    return [role for role in QA_ROLE_ORDER if role in requested]


# LLM: _qa_role_contract_line parses one structured QA role field line.
# 函数用途: 返回 active 状态和本行 QA roles，避免 qa_roles_from_text 继续加深。
def _qa_role_contract_line(raw: str, *, active: bool) -> tuple[bool, set[str]]:
    line = raw.strip()
    match = _QA_ROLE_FIELD_RE.match(line)
    if match:
        is_active = match.group("field").strip().lower() in _QA_ROLE_FIELDS
        return is_active, set(_role_items(match.group("tail"))) if is_active else set()
    if active and line.startswith(("-", "*")):
        return active, set(_role_items(line.lstrip("-* ")))
    return False, set()


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


# LLM: qa_role_marker_present is kept for callers but now checks structured field content only.
# 函数用途: 兼容旧调用方；判断某段文本的 QA 机器字段是否包含指定角色。
def qa_role_marker_present(text: str, role: str) -> bool:
    return role in qa_roles_from_text(text)


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


# LLM: _inline_role_id_mentions accepts exact QA role ids in compact contracts.
# 函数用途: 支持 `tester / bug_finder / acceptor` 这类机器 role id 列表；不解析“测试/验收”等自然语言。
def _inline_role_id_mentions(text: object) -> set[str]:
    compact = str(text or "").lower().replace("-", "_")
    return {role for role in QA_ROLE_ORDER if re.search(rf"(?<![a-z0-9_]){re.escape(role)}(?![a-z0-9_])", compact)}
