
from __future__ import annotations

import re
from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..runner.ref_fields import params_output_refs
from .create_target_roots import (
    agent_workspace_roots,
    context_target_write_roots,
    is_relative_to,
    normalized_write_root,
    structured_output_write_roots,
    structured_task_output_write_roots,
)

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
    "bug_finder",
    "coordinator",
    "critic",
    "qa",
    "reviewer",
    "root",
    "tester",
    "verifier",
}


def role_allows_direct_product_work(role: str) -> bool:
    normalized = str(role or "worker").strip().lower().replace("-", "_")
    if not normalized:
        return True
    if normalized in _NON_WORKER_ROLES:
        return False
    return not any(part in normalized for part in _NON_WORKER_ROLES)


def merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    del goal
    for item in string_list(params.get("extra_write_roots"), TOOL_TEXT_LIST_OPTIONS):
        text = normalized_write_root(item)
        if text and text not in roots:
            roots.append(text)
    return roots


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


def explicit_root_missing_write_root_error(agent: object, params: dict[str, object], goal: str) -> str:
    del agent, params, goal
    return ""


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


def _structured_output_refs(params: dict[str, object]) -> list[str]:
    return params_output_refs(params)


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


def _structured_parent_constraints(params: dict[str, object]) -> set[str]:
    return _structured_tokens(params, _PARENT_CONSTRAINT_FIELDS, _CONSTRAINT_ALIAS_MAP)


def _structured_child_relaxations(params: dict[str, object]) -> set[str]:
    return _structured_tokens(params, _CHILD_RELAXATION_FIELDS, _RELAXATION_ALIAS_MAP)


def _structured_tokens(params: dict[str, object], fields: set[str], aliases: dict[str, str]) -> set[str]:
    found: set[str] = set()
    for field in fields:
        found.update(_token_items(params.get(field), aliases))
    attrs = params.get("attributes")
    if isinstance(attrs, dict):
        for field in fields:
            found.update(_token_items(attrs.get(field), aliases))
    return found


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
