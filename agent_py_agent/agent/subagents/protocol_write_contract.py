
from __future__ import annotations

from ..model_visible_refs import (
    clean_path_contract_refs,
    current_model_ref,
    is_non_model_visible_locator_root,
)
from .models import SubAgentTask

_FILESYSTEM_WRITE_GRANT_TOOLS = {"write_file", "apply_patch"}


def build_write_contract(task: SubAgentTask) -> dict[str, object]:
    allowed_roots = _effective_allowed_write_roots(task)
    return {
        "internal_task_root": current_model_ref(_model_task_dir(task)),
        "product_write_roots": _product_write_roots(task, allowed_roots),
        "allowed_write_roots": allowed_roots,
        "forbidden_write_roots": _model_ref_list(_task_list(task, "forbidden_write_roots"), task=None),
        "locked_files": _model_ref_list(_task_list(task, "locked_files"), task=None),
    }


def _product_write_roots(task: SubAgentTask, allowed_roots: list[str]) -> list[str]:
    internal_roots = _internal_roots(task)
    task_root = _normalize_path(_model_task_dir(task))
    result: list[str] = []
    for root in allowed_roots:
        normalized = _normalize_path(root)
        if not _is_product_root(normalized, task_root, internal_roots):
            continue
        result.append(root)
    return result


def _internal_roots(task: SubAgentTask) -> set[str]:
    names = ("task_dir", "data_dir", "output_dir", "tests_dir", "reports_dir", "logs_dir", "scratch_dir")
    roots = {_normalize_path(_task_text(task, name)) for name in names}
    roots.add(_normalize_path(_task_text(task, "task_workspace_dir")))
    roots.add(_normalize_path(_task_text(task, "agent_run_workspace_dir")))
    return roots


def _is_product_root(normalized: str, task_root: str, internal_roots: set[str]) -> bool:
    if not normalized or normalized in internal_roots:
        return False
    return not (task_root and normalized.startswith(f"{task_root}/"))


def _normalize_path(value: str) -> str:
    return str(value or "").rstrip("/")


def _effective_allowed_write_roots(task: SubAgentTask) -> list[str]:
    roots = _model_ref_list(
        [
            _task_text(task, "task_workspace_dir"),
            _task_text(task, "agent_run_workspace_dir"),
            *_task_list(task, "allowed_write_roots"),
        ],
        task=task,
    )
    for grant in _task_list(task, "capability_grants"):
        if _grant_allows_filesystem_write(grant):
            roots = _merge_unique([*roots, *_model_ref_list(_grant_path_scope(grant), task=task)])
    return roots


def _grant_allows_filesystem_write(grant: object) -> bool:
    tools = {str(item or "").strip() for item in getattr(grant, "tools", []) or []}
    return bool(tools & _FILESYSTEM_WRITE_GRANT_TOOLS)


def _grant_path_scope(grant: object) -> list[str]:
    value = getattr(grant, "path_scope", [])
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, (list, tuple, set)) else []


def _merge_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _model_ref_list(values: list[object], *, task: SubAgentTask | None) -> list[str]:
    refs = clean_path_contract_refs(values)
    if task is None:
        return _merge_unique(refs)
    return _merge_unique([ref for ref in refs if not is_non_model_visible_locator_root(task, ref)])


def _model_task_dir(task: SubAgentTask) -> str:
    return _task_text(task, "agent_run_workspace_dir") or _task_text(task, "task_dir")


def _task_text(task: object, name: str) -> str:
    return str(getattr(task, name, "") or "")


def _task_list(task: object, name: str) -> list:
    value = getattr(task, name, [])
    return list(value) if isinstance(value, (list, tuple, set)) else []
