# LLM: Create-subagent constraint helpers protect user intent without limiting normal work.
# 模块用途: 检查派工目标是否丢路径、抢同一产物、削弱用户硬约束。

from __future__ import annotations

import re
from pathlib import Path

from ..subagents.services.base import _extract_write_dirs
from .orchestration_create_target_roots import (
    agent_workspace_roots,
    context_target_write_roots,
    is_relative_to,
    normalized_write_root,
)
from .parameters import _string_list
from .runner_input_dependencies import goal_output_refs

_CONCRETE_FILE_TARGET_RE = re.compile(
    r"[\w.-]+\.(?:html|css|js|mjs|cjs|ts|tsx|jsx|py|md|json|yaml|yml|txt|csv|xlsx|xls|pdf|vue|svelte)\b",
    re.IGNORECASE,
)
_STRUCTURED_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_\-.]*)\s*:\s*(.*)$")
_PARENT_CONSTRAINT_FIELDS = {"delegation_constraints", "required_constraints", "hard_constraints"}
_CHILD_RELAXATION_FIELDS = {"constraint_overrides", "constraint_relaxations", "allowed_relaxations"}
_CONSTRAINT_ALIAS_MAP = {
    "working_buttons": "working_buttons",
    "no_dead_buttons": "working_buttons",
    "no_broken_buttons": "working_buttons",
    "verified_images": "verified_images",
    "verified_images_only": "verified_images",
    "no_broken_images": "verified_images",
    "no_comments": "no_comments",
    "comment_free": "no_comments",
}
_RELAXATION_ALIAS_MAP = {
    "allow_dead_buttons": "working_buttons",
    "dead_buttons_allowed": "working_buttons",
    "allow_hash_buttons": "working_buttons",
    "allow_unverified_remote_images": "verified_images",
    "allow_remote_images": "verified_images",
    "allow_broken_images": "verified_images",
    "allow_comments": "no_comments",
    "comments_allowed": "no_comments",
}
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
        text = normalized_write_root(item)
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: resolved_extra_write_roots gives vague "目标目录" tasks the current task workspace as a concrete root.
# 函数用途: 合并显式写入根、goal 中路径和安全的 workspace_root 默认值，避免用户必须填写底层 extra_write_roots。
def resolved_extra_write_roots(agent: object, params: dict[str, object], goal: str) -> list[str]:
    explicit = merged_extra_write_roots(params, goal)
    if explicit:
        return explicit
    target_roots = context_target_write_roots(agent, params) if _has_structured_write_intent(params, goal) else []
    if target_roots:
        return target_roots
    default_root = _default_workspace_product_root(agent, params, goal)
    return [default_root] if default_root else []


# LLM: explicit_root_missing_write_root_error prevents product paths from drifting into agent workspaces.
# 函数用途: 显式 root/coordinator 要交付文件但没带产物写入根时拒绝创建，要求模型带 extra_write_roots 重试。
def explicit_root_missing_write_root_error(agent: object, params: dict[str, object], goal: str) -> str:
    if resolved_extra_write_roots(agent, params, goal):
        return ""
    if not _goal_has_product_write_intent(goal):
        return ""
    role = str(params.get("role") or "worker").strip().casefold().replace("-", "_")
    if not (
        _manager_has_real_workspace(agent)
        or _goal_has_structured_product_contract(goal)
        or not role_allows_direct_product_work(role)
    ):
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


# LLM: delegation_constraint_conflict_error keeps child goals from weakening machine constraints.
# 函数用途: 主代理派工时如果结构化 constraint 字段被 child 反向 override，直接要求重写目标。
def delegation_constraint_conflict_error(agent, goal: str) -> str:
    user_text = str(getattr(agent, "_current_user_prompt", "") or "")
    goal_text = str(goal or "")
    conflicts = sorted(
        _structured_parent_constraints(user_text) & _structured_child_relaxations(goal_text)
    )
    if not conflicts:
        return ""
    conflicts_text = ", ".join(conflicts)
    return (
        "delegation_constraint_conflict: 派工目标不能削弱或反向改写父级结构化约束。"
        f"冲突约束: {conflicts_text}。"
        "请重新调用 create_subagents：保留 delegation_constraints，删除冲突的 constraint_overrides。"
    )


# LLM: _goal_needs_product_write_root detects concrete deliverable tasks without parsing prose too broadly.
# 函数用途: 判断目标是否像文件/网站交付任务；只用于缺写入根时的保守拦截。
def _goal_needs_product_write_root(goal: str) -> bool:
    return bool(goal_output_refs(goal))


# LLM: _goal_has_product_write_intent is broader than missing-root rejection and still local to product tasks.
# 函数用途: 判断目标是否像真实交付物写入任务，用于决定是否可采用 workspace_root 默认写入根。
def _goal_has_product_write_intent(goal: str) -> bool:
    return _goal_needs_product_write_root(goal)


# LLM: _goal_has_structured_product_contract distinguishes protocol refs from casual prose refs.
# 函数用途: 识别 output_files/output_refs/artifact_refs 这类机器字段；没有 workspace 时也必须要求真实写入根。
def _goal_has_structured_product_contract(goal: str) -> bool:
    return bool(re.search(r"^\s*(?:[-*]\s*)?(?:output_refs|output_files|artifact_refs)\s*[:=]", str(goal or ""), re.IGNORECASE | re.MULTILINE))


# LLM: _has_structured_write_intent enables target roots from output refs or repair contracts only.
# 函数用途: 有 output_files 或 repair_contract 时才把 required_read_paths 中的产物文件当作写入目标。
def _has_structured_write_intent(params: dict[str, object], goal: str) -> bool:
    if _goal_has_product_write_intent(goal):
        return True
    if isinstance(params.get("repair_contract"), dict):
        return True
    role = str(params.get("role") or "").casefold().replace("-", "_")
    if "repair" in role:
        return True
    packs = params.get("context_packs")
    if not isinstance(packs, list):
        return False
    return any(
        isinstance(pack, dict) and (pack.get("kind") == "repair_contract" or isinstance(pack.get("contract"), dict))
        for pack in packs
    )


# LLM: _default_workspace_product_root refuses mocks/internal subagent dirs and only returns a real workspace path.
# 函数用途: 从 agent.subagents.workspace_root 取当前任务工作区；如果只是测试 MagicMock 或内部 subagents 目录则不自动授权。
def _default_workspace_product_root(agent: object, params: dict[str, object], goal: str) -> str:
    if not _goal_has_product_write_intent(goal):
        return ""
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw, str | Path):
        return ""
    root = Path(raw).expanduser().resolve(strict=False)
    workspace = getattr(getattr(agent, "subagents", None), "workspace", None)
    if isinstance(workspace, str | Path) and root == Path(workspace).expanduser().resolve(strict=False):
        return ""
    roots = agent_workspace_roots(agent, root)
    return str(root) if any(is_relative_to(root, item) for item in roots) else ""


# LLM: _manager_has_real_workspace distinguishes real managers from loose mocks and old adapters.
# 函数用途: 只有存在可解析 workspace_root 时，缺写入根才值得阻断；普通 mock/旧 adapter 保持兼容创建。
def _manager_has_real_workspace(agent: object) -> bool:
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    return isinstance(raw, str | Path)


# LLM: _structured_parent_constraints reads only protocol fields from root prompt/context.
# 函数用途: 从 delegation_constraints/required_constraints/hard_constraints 字段读取父级硬约束。
def _structured_parent_constraints(text: str) -> set[str]:
    return _structured_tokens(text, _PARENT_CONSTRAINT_FIELDS, _CONSTRAINT_ALIAS_MAP)


# LLM: _structured_child_relaxations reads only protocol fields from child goal/context.
# 函数用途: 从 constraint_overrides/constraint_relaxations/allowed_relaxations 字段读取 child 的放宽声明。
def _structured_child_relaxations(text: str) -> set[str]:
    return _structured_tokens(text, _CHILD_RELAXATION_FIELDS, _RELAXATION_ALIAS_MAP)


# LLM: _structured_tokens parses shallow machine fields without natural-language intent inference.
# 函数用途: 解析 `field: token, token` 形式；普通句子不产生任何硬约束。
def _structured_tokens(text: str, fields: set[str], aliases: dict[str, str]) -> set[str]:
    found: set[str] = set()
    active = False
    for line in str(text or "").splitlines():
        active, values = _structured_token_line(line, active=active, fields=fields, aliases=aliases)
        found.update(values)
    return found


# LLM: _structured_token_line keeps multiline protocol parsing shallow for code-size guards.
# 函数用途: 解析一行结构化 token 字段，返回下一行是否仍处于 active 字段和本行 token 集合。
def _structured_token_line(
    line: str,
    *,
    active: bool,
    fields: set[str],
    aliases: dict[str, str],
) -> tuple[bool, set[str]]:
    match = _STRUCTURED_FIELD_RE.match(line)
    if match:
        field = match.group(1).strip().casefold().replace("-", "_")
        is_active = field in fields
        return is_active, _token_items(match.group(2), aliases) if is_active else set()
    if active and _is_list_continuation(line):
        return active, _token_items(line, aliases)
    return False, set()


# LLM: _token_items normalizes machine enum values from one field value.
# 函数用途: 兼容逗号、顿号、竖线和 bullet 形式，但不把普通自然语言分词当 token。
def _token_items(value: str, aliases: dict[str, str]) -> set[str]:
    cleaned = str(value or "").strip().strip("[]")
    for prefix in ("-", "*"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[1:].strip()
    tokens = re.split(r"[,，、|]+", cleaned)
    return {
        mapped
        for token in tokens
        if (mapped := aliases.get(token.strip().strip("'\"`").casefold().replace("-", "_")))
    }


# LLM: _is_list_continuation keeps multiline protocol fields readable.
# 函数用途: 判断当前行是否是结构化字段下面的 bullet continuation。
def _is_list_continuation(line: str) -> bool:
    stripped = str(line or "").strip()
    return stripped.startswith(("-", "*"))
