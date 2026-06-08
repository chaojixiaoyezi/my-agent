
from __future__ import annotations

from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...subagents.role_templates import role_template_snapshot_for_role
from ..runner.ref_fields import params_output_refs
from .create_target_roots import (
    agent_workspace_roots,
    context_target_write_roots,
    is_relative_to,
    normalized_write_root,
    structured_output_write_roots,
    structured_task_output_write_roots,
)


def role_allows_direct_product_work(role: str, role_template_dirs: object = None) -> bool:
    normalized = str(role or "worker").strip().lower().replace("-", "_")
    if not normalized:
        return True
    snapshot = role_template_snapshot_for_role(normalized, role_template_dirs)
    if not snapshot:
        return True
    if bool(snapshot.get("can_spawn_children")):
        return False
    if bool(snapshot.get("depends_on_outputs")) or bool(snapshot.get("can_run_tests")):
        return False
    return bool(snapshot.get("can_write"))


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
        target_roots.extend(_current_task_output_write_roots(agent, params))
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


def _structured_output_refs(params: dict[str, object]) -> list[str]:
    return params_output_refs(params)


def _has_structured_write_intent(params: dict[str, object], goal: str) -> bool:
    if _structured_output_refs(params):
        return True
    if isinstance(params.get("repair_contract"), dict):
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
    if not any(is_relative_to(root, item) for item in roots):
        return ""
    return str(root) if _has_output_ref_inside_workspace(params, root, roots) else ""


def _has_output_ref_inside_workspace(params: dict[str, object], root: Path, roots: list[Path]) -> bool:
    for ref in params_output_refs(params):
        path = _output_ref_path(ref, root)
        if path is not None and any(is_relative_to(path, workspace_root) for workspace_root in roots):
            return True
    return False


def _current_task_output_write_roots(agent: object, params: dict[str, object]) -> list[str]:
    task_root = _current_task_root(agent)
    if not task_root:
        return []
    task_output = (Path(task_root).expanduser() / "output").resolve(strict=False)
    roots: list[str] = []
    for ref in params_output_refs(params):
        path = _output_ref_path(ref, task_output)
        if path is not None and is_relative_to(path, task_output):
            text = str(path.parent)
            if text not in roots:
                roots.append(text)
    return roots


def _current_task_root(agent: object) -> str:
    raw = getattr(agent, "_current_run_task_workspace", "")
    if not isinstance(raw, str | Path):
        return ""
    return str(raw).strip()


def _output_ref_path(ref: str, root: Path) -> Path | None:
    text = str(ref or "").strip()
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
    except OSError:
        return None
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=False)


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
