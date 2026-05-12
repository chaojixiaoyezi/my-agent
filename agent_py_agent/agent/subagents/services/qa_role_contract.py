# LLM: QA role contract helpers are shared by scheduling and acceptance gates.
# 模块用途: 统一识别用户明确点名的 tester、bug_finder、acceptor 角色，避免调度和验收各自维护一套规则。

from __future__ import annotations

from ..role_contracts import normalize_subagent_role

QA_ROLE_MARKERS = {
    "tester": ("tester", "测试子代理", "测试代理", "测试角色", "测试员"),
    "bug_finder": (
        "bug_finder",
        "bug-finder",
        "bug finder",
        "bugfinder",
        "找错子代理",
        "找茬子代理",
        "找错代理",
        "找茬代理",
        "找错角色",
        "找茬角色",
        "找错",
        "找茬",
    ),
    "acceptor": ("acceptor", "验收子代理", "验收代理", "验收角色", "验收员", "由acceptor", "由 acceptor"),
}
QA_ROLE_ORDER = ("tester", "bug_finder", "acceptor")


# LLM: qa_roles_required_by_task extracts requested QA roles from stable parent contracts only.
# 函数用途: 从 goal/acceptance_checks 判断父任务是否要求真实 QA 子代理；leaf 节点不会继续继承这个责任。
def qa_roles_required_by_task(task) -> list[str]:
    if qa_role_task_is_leaf(task):
        return []
    return qa_roles_from_text(qa_role_contract_text(task))


# LLM: qa_role_contract_text avoids trusting output summaries when determining role requirements.
# 函数用途: 只合并任务目标和验收条件，用来判断“要求过什么”，不读取模型自称完成的输出。
def qa_role_contract_text(task) -> str:
    values = [str(getattr(task, "goal", "") or "")]
    values.extend(str(item) for item in getattr(task, "acceptance_checks", []) or [])
    return " ".join(values).lower()


# LLM: qa_roles_from_text returns roles in deterministic dependency order for scheduling and reports.
# 函数用途: 从任意文本中提取被点名的 QA 角色，按 tester、bug_finder、acceptor 固定顺序返回。
def qa_roles_from_text(text: str) -> list[str]:
    return [role for role in QA_ROLE_ORDER if qa_role_marker_present(text, role)]


# LLM: qa_role_identity_roles reads actual role identity fields, not inherited goal prose.
# 函数用途: 从 role/agent_name 判断一个已存在或候选 run 是否真正属于 QA 角色。
def qa_role_identity_roles(*, role: str, agent_name: str = "") -> set[str]:
    roles: set[str] = set()
    normalized = normalize_subagent_role(role)
    if normalized in QA_ROLE_ORDER:
        roles.add(normalized)
    label = str(agent_name or "").lower()
    roles.update(item for item in QA_ROLE_ORDER if qa_role_marker_present(label, item))
    return roles


# LLM: qa_role_task_is_leaf mirrors hierarchy semantics so leaves are not punished for inherited wording.
# 函数用途: 判断任务是否已经是执行叶子，leaf 不负责继续创建 tester/bug_finder/acceptor 下级。
def qa_role_task_is_leaf(task) -> bool:
    role = str(getattr(task, "role", "") or "").lower()
    agent_name = str(getattr(task, "agent_name", "") or "").lower()
    return role == "leaf_worker" or "leaf" in agent_name


# LLM: qa_role_marker_present keeps explicit bilingual marker matching centralized and conservative.
# 函数用途: 检查文本是否点名某个 QA 角色，避免普通“验收条件”被误当成 acceptor 角色要求。
def qa_role_marker_present(text: str, role: str) -> bool:
    markers = QA_ROLE_MARKERS.get(role, ())
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in markers)
