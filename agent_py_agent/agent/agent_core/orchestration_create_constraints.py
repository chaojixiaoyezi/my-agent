# LLM: Create-subagent constraint helpers protect user intent without limiting normal work.
# 模块用途: 检查派工目标是否丢路径、抢同一产物、削弱用户硬约束。

from __future__ import annotations

import re

from ..subagents.services.base import _extract_write_dirs
from .orchestration_negation_markers import contains_unnegated_marker
from .parameters import _string_list
from .spawn_role_seed import is_explicit_root_role

_VAGUE_PRODUCT_TARGET_WORDS = (
    "目标目录",
    "同一目录",
    "当前目录",
    "任务目录",
    "产物目录",
    "输出目录",
    "build 目录",
    "build目录",
    "deliverables 目录",
    "deliverables目录",
    "target directory",
    "same directory",
    "current directory",
    "task directory",
    "output directory",
)
_CONCRETE_FILE_TARGET_RE = re.compile(
    r"[\w.-]+\.(?:html|css|js|mjs|cjs|ts|tsx|jsx|py|md|json|yaml|yml|txt|csv|vue|svelte)\b",
    re.IGNORECASE,
)
_NON_WORKER_ROLES = {
    "acceptor",
    "bug_finder",
    "coordinator",
    "critic",
    "qa",
    "reviewer",
    "root",
    "tester",
    "verifier",
}


# LLM: role_allows_direct_product_work separates worker-like roles from quality/coordinator roles.
# 函数用途: 判断当前 role 是否是会直接产出业务文件的普通 worker 类角色。
def role_allows_direct_product_work(role: str) -> bool:
    normalized = str(role or "worker").strip().lower().replace("-", "_")
    if not normalized:
        return True
    if normalized in _NON_WORKER_ROLES:
        return False
    return not any(part in normalized for part in _NON_WORKER_ROLES)


# LLM: goal_has_concrete_file_target treats named files as direct deliverables.
# 函数用途: 识别 index.html、report.md 这类明确文件目标。
def goal_has_concrete_file_target(goal: str) -> bool:
    return bool(_CONCRETE_FILE_TARGET_RE.search(str(goal or "")))


# LLM: goal_has_single_concrete_file_target prevents broad delegation hints from changing one-file workers.
# 函数用途: 判断 create 调用是否只交付一个明确文件。
def goal_has_single_concrete_file_target(goal: str) -> bool:
    return len(set(_CONCRETE_FILE_TARGET_RE.findall(str(goal or "")))) == 1


# LLM: merged_extra_write_roots combines explicit roots and roots parsed from goals.
# 函数用途: 汇总本次子任务允许写入的产物目录，保持顺序并去重。
def merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    for item in [*_string_list(params.get("extra_write_roots")), *_extract_write_dirs(goal)]:
        text = str(item or "").strip()
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: explicit_root_missing_write_root_error prevents product paths from drifting into agent workspaces.
# 函数用途: 显式 root/coordinator 要交付文件但没带产物写入根时拒绝创建，要求模型带 extra_write_roots 重试。
def explicit_root_missing_write_root_error(params: dict[str, object], goal: str) -> str:
    role = str(params.get("role") or "worker").strip()
    if merged_extra_write_roots(params, goal):
        return ""
    if not _goal_needs_product_write_root(goal):
        return ""
    if not is_explicit_root_role(role) and not _goal_has_vague_product_target(goal):
        return ""
    return (
        "要交付文件或网站时，必须提供真实产物写入根，"
        "否则下级会误把 agent-run workspace 当成 build 目录。"
        "请重新调用 create_subagents，并在顶层传入 extra_write_roots，"
        "例如 extra_write_roots=[\"/Users/.../deliverables/.../build\"]；"
        "不要只在 goal 里写“目标目录”“同一目录”或“build 目录”。"
    )


# LLM: ambiguous_repeated_product_goal_error rejects cloned workers for one concrete deliverable target.
# 函数用途: 防止 count>1 复制同一组明确文件目标，导致多个 worker 抢同一批产物。
def ambiguous_repeated_product_goal_error(goal: str, count: int, role: str) -> str:
    if count <= 1 or not role_allows_direct_product_work(role):
        return ""
    file_targets = sorted(set(_CONCRETE_FILE_TARGET_RE.findall(str(goal or ""))))
    if not file_targets:
        return ""
    files_text = ", ".join(file_targets[:6])
    return (
        "ambiguous_repeated_product_goal: 不要用 count 复制同一个带具体文件名的交付任务。"
        f"本次 goal 提到了 {files_text}，count={count} 会让多个 worker 抢同一批文件。"
        "请改成二选一：1) 创建 count=1 的 coordinator，让它按文件继续拆给下一层；"
        "2) 多次调用 create_subagents，每次只给一个 worker 一个明确文件目标。"
    )


# LLM: delegation_constraint_conflict_error keeps child goals from weakening explicit user constraints.
# 函数用途: 主代理派工时如果把“不要失灵/不要失效/不要注释”反向改写，直接要求重写目标。
def delegation_constraint_conflict_error(agent, goal: str) -> str:
    user_text = str(getattr(agent, "_current_user_prompt", "") or "")
    goal_text = str(goal or "")
    conflicts: list[str] = []
    if _user_requires_working_buttons(user_text) and _goal_allows_dead_buttons(goal_text):
        conflicts.append("用户要求不要有失灵按钮，但子任务目标允许按钮指向 #。")
    if _user_requires_no_broken_images(user_text) and _goal_requires_unverified_remote_images(goal_text):
        conflicts.append("用户要求不要出现失效图片链接，但子任务目标要求使用未验证的远程图片 URL。")
    if _user_requires_no_comments(user_text) and _goal_requests_comments(goal_text):
        conflicts.append("用户要求不要注释，但子任务目标要求写注释。")
    if not conflicts:
        return ""
    return (
        "delegation_constraint_conflict: 派工目标不能削弱或反向改写用户原始约束。"
        + " ".join(conflicts)
        + "请重新调用 create_subagents：保留用户约束原文，删除冲突要求；"
        "图片可用 CSS/本地/内联视觉替代，按钮必须执行真实交互或跳到页面内真实锚点。"
    )


# LLM: _goal_needs_product_write_root detects concrete deliverable tasks without parsing prose too broadly.
# 函数用途: 判断目标是否像文件/网站交付任务；只用于缺写入根时的保守拦截。
def _goal_needs_product_write_root(goal: str) -> bool:
    lowered = goal.lower()
    if not any(word in lowered for word in ("交付", "deliver", "build", "网站", "demo", "文件")):
        return False
    return any(suffix in lowered for suffix in (".html", ".css", ".js", ".py", ".md", ".json", ".txt"))


# LLM: _goal_has_vague_product_target blocks path drift before a worker silently writes into task_dir.
# 函数用途: 识别“目标目录/任务目录”等模糊产物位置。
def _goal_has_vague_product_target(goal: str) -> bool:
    lowered = goal.lower()
    return any(word in lowered for word in _VAGUE_PRODUCT_TARGET_WORDS)


# LLM: _user_requires_working_buttons detects the natural-language no-dead-buttons contract.
# 函数用途: 识别用户不希望 href=#、空按钮或假交互的约束。
def _user_requires_working_buttons(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("不要有失灵按钮", "不要失灵按钮", "no broken buttons", "no dead buttons"))


# LLM: _goal_allows_dead_buttons catches common weakening phrases models add during delegation.
# 函数用途: 判断派工目标是否允许 # 空链接或假按钮。
def _goal_allows_dead_buttons(text: str) -> bool:
    lowered = text.lower()
    return contains_unnegated_marker(
        lowered,
        ('href="#"', "指向 #", "指向#", "可指向 #", "#锚点", "# 锚点", "空锚点", "hash anchor", "can point to #"),
    )


# LLM: _user_requires_no_broken_images detects image reliability constraints in plain language.
# 函数用途: 识别用户要求图片不要失效、不要坏链的约束。
def _user_requires_no_broken_images(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("不要出现失效图片", "不要有失效图片", "no broken image"))


# LLM: _goal_requires_unverified_remote_images treats remote image mandates as risky unless the user asked for them.
# 函数用途: 子任务目标主动要求远程图片 URL 时，如果用户要求不失效图片，就拒绝这类弱化约束。
def _goal_requires_unverified_remote_images(text: str) -> bool:
    lowered = text.lower()
    return contains_unnegated_marker(lowered, ("unsplash", "images.unsplash", "图片 url", "image url", "http"))


# LLM: _user_requires_no_comments detects simple no-comment deliverable requests.
# 函数用途: 识别用户明确不要注释的交付约束。
def _user_requires_no_comments(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "不要注释",
            "不要有注释",
            "不要写注释",
            "不写注释",
            "禁止注释",
            "no comments",
            "without comments",
            "do not write comments",
        )
    )


# LLM: _goal_requests_comments catches delegated tasks that reintroduce comments.
# 函数用途: 判断派工目标是否要求代码注释或注释说明。
def _goal_requests_comments(text: str) -> bool:
    lowered = text.lower()
    return contains_unnegated_marker(lowered, ("有注释", "写注释", "代码注释", "with comments"))
