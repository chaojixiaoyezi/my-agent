# LLM: Create-subagent constraint helpers protect user intent without limiting normal work.
# 模块用途: 检查派工目标是否丢路径、抢同一产物、削弱用户硬约束。

from __future__ import annotations

import re
from pathlib import Path

from .orchestration_create_target_roots import (
    agent_workspace_roots,
    context_target_write_roots,
    is_relative_to,
    normalized_write_root,
    structured_output_write_roots,
    structured_task_output_write_roots,
)
from .parameters import _string_list
from .runner_ref_fields import params_output_refs

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


# LLM: merged_extra_write_roots combines explicit structured roots only.
# 函数用途: 汇总本次子任务允许写入的产物目录，保持顺序并去重。
def merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    del goal
    for item in _string_list(params.get("extra_write_roots")):
        text = normalized_write_root(item)
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: resolved_extra_write_roots resolves write roots from explicit fields, refs, and real workspace defaults.
# 函数用途: 合并显式写入根、结构化 target/read refs 和安全 workspace_root 默认值，避免用户必须填写底层 extra_write_roots。
def resolved_extra_write_roots(agent: object, params: dict[str, object], goal: str) -> list[str]:
    explicit = merged_extra_write_roots(params, goal)
    if explicit:
        return explicit
    target_roots = []
    if _has_structured_write_intent(params, goal):
        target_roots.extend(structured_output_write_roots(agent, params))
        if _has_repair_write_intent(params):
            target_roots.extend(context_target_write_roots(agent, params))
        target_roots.extend(structured_task_output_write_roots(agent, params))
    if target_roots:
        return _unique_roots(target_roots)
    default_root = _default_workspace_product_root(agent, params, goal)
    return [default_root] if default_root else []


# LLM: explicit_root_missing_write_root_error prevents product paths from drifting into agent workspaces.
# 函数用途: 显式 root/coordinator 要交付文件但没带产物写入根时拒绝创建，要求模型带 extra_write_roots 重试。
def explicit_root_missing_write_root_error(agent: object, params: dict[str, object], goal: str) -> str:
    if resolved_extra_write_roots(agent, params, goal):
        return ""
    if not _structured_output_refs(params):
        return ""
    role = str(params.get("role") or "worker").strip().casefold().replace("-", "_")
    if not (
        _manager_has_real_workspace(agent)
        or _structured_output_refs(params)
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


# LLM: delegation_constraint_conflict_error keeps child params from weakening machine constraints.
# 函数用途: 主代理派工时如果结构化 constraint 参数被 child 反向 override，直接要求重写参数。
def delegation_constraint_conflict_error(params: dict[str, object]) -> str:
    conflicts = sorted(_structured_parent_constraints(params) & _structured_child_relaxations(params))
    if not conflicts:
        return ""
    conflicts_text = ", ".join(conflicts)
    return (
        "delegation_constraint_conflict: 派工目标不能削弱或反向改写父级结构化约束。"
        f"冲突约束: {conflicts_text}。"
        "请重新调用 create_subagents：保留 delegation_constraints，删除冲突的 constraint_overrides。"
    )


# LLM: _structured_output_refs reads only machine deliverable refs from tool parameters.
# 函数用途: 返回 output_files/output_refs/artifact_refs 中的产物路径；普通句子里的文件名不算机器事实。
def _structured_output_refs(params: dict[str, object]) -> list[str]:
    return params_output_refs(params)


# LLM: _has_structured_write_intent enables target roots from output refs or repair contracts only.
# 函数用途: 有 output_files 或 repair_contract 时才把 required_read_paths 中的产物文件当作写入目标。
def _has_structured_write_intent(params: dict[str, object], goal: str) -> bool:
    if _structured_output_refs(params):
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


# LLM: _has_repair_write_intent gates required_read_paths-derived write roots to repair flows.
# 函数用途: 普通输出任务只按 output_files 授权写根；修复任务才可把目标读路径作为待修产物根。
def _has_repair_write_intent(params: dict[str, object]) -> bool:
    if isinstance(params.get("repair_contract"), dict):
        return True
    role = str(params.get("role") or "").casefold().replace("-", "_")
    if "repair" in role:
        return True
    packs = params.get("context_packs")
    if not isinstance(packs, list):
        return False
    return any(isinstance(pack, dict) and pack.get("kind") == "repair_contract" for pack in packs)


# LLM: _default_workspace_product_root refuses mocks/internal subagent dirs and only returns a real workspace path.
# 函数用途: 从 agent.subagents.workspace_root 取当前任务工作区；如果只是测试 MagicMock 或内部 subagents 目录则不自动授权。
def _default_workspace_product_root(agent: object, params: dict[str, object], goal: str) -> str:
    if not _structured_output_refs(params):
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


def _unique_roots(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# LLM: _structured_parent_constraints reads only create_subagents protocol params.
# 函数用途: 从 delegation_constraints/required_constraints/hard_constraints 参数读取父级硬约束。
def _structured_parent_constraints(params: dict[str, object]) -> set[str]:
    return _structured_tokens(params, _PARENT_CONSTRAINT_FIELDS, _CONSTRAINT_ALIAS_MAP)


# LLM: _structured_child_relaxations reads only create_subagents protocol params.
# 函数用途: 从 constraint_overrides/constraint_relaxations/allowed_relaxations 参数读取 child 的放宽声明。
def _structured_child_relaxations(params: dict[str, object]) -> set[str]:
    return _structured_tokens(params, _CHILD_RELAXATION_FIELDS, _RELAXATION_ALIAS_MAP)


# LLM: _structured_tokens parses shallow machine params without natural-language intent inference.
# 函数用途: 解析明确参数值；普通 goal/prompt 句子不产生任何硬约束。
def _structured_tokens(params: dict[str, object], fields: set[str], aliases: dict[str, str]) -> set[str]:
    found: set[str] = set()
    for field in fields:
        found.update(_token_items(params.get(field), aliases))
    attrs = params.get("attributes")
    if isinstance(attrs, dict):
        for field in fields:
            found.update(_token_items(attrs.get(field), aliases))
    return found


# LLM: _token_items normalizes machine enum values from one field value.
# 函数用途: 兼容逗号、顿号、竖线和 bullet 形式，但不把普通自然语言分词当 token。
def _token_items(value: object, aliases: dict[str, str]) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {item for raw in value for item in _token_items(raw, aliases)}
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
