
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..user_space.run_workspace import EnsureRunWorkspaceRequest, ensure_run_workspace
from ._runtime_params import ArchiveRunParams
from .run_task_workspace_index import register_saved_run_task_ref


def write_run_task_workspace_if_needed(agent, params: ArchiveRunParams) -> str:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return ""
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return ""
    owner_home = getattr(home_paths, "owner_home_dir", None)
    target_home = Path(owner_home) if owner_home else Path(home_paths.root)
    result = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=target_home,
            template=str(getattr(agent.config, "workspace_task_path_template", "")),
            task_name=params.task_id or params.user_prompt,
            user_prompt=params.user_prompt,
            request_id=params.run_request_id,
            run_id=params.run_id,
            task_id=params.task_id,
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            owner_home=str(getattr(home_paths, "owner_home_dir", "") or ""),
            source=params.source,
        )
    )
    register_saved_run_task_ref(agent, result, params)
    return str(result.root)


def attach_run_task_workspace_context(agent, params, user_prompt: str):
    if not _should_create_workspace(agent, params):
        return params
    result = _ensure_workspace_for_run(agent, params, user_prompt)
    injection = _workspace_prompt_section(result)
    next_inject = _append_once(list(getattr(params, "inject", None) or []), injection)
    next_attrs = _task_attributes_with_workspace(getattr(params, "task_attributes", None), result)
    agent._current_run_task_workspace = str(result.root)
    return replace(params, inject=next_inject, task_attributes=next_attrs)


def _should_create_workspace(agent, params) -> bool:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return False
    if str(getattr(params, "context_scope", "") or "").strip().lower() in {"task_local", "control_plane"}:
        return False
    auto_save = bool(getattr(agent.config, "auto_save_memory", True))
    do_save = auto_save if getattr(params, "save", None) is None else bool(getattr(params, "save", None))
    return bool(do_save and getattr(agent, "home_paths", None) is not None)


def _ensure_workspace_for_run(agent, params, user_prompt: str):
    home_paths = agent.home_paths
    owner_home = getattr(home_paths, "owner_home_dir", None)
    target_home = Path(owner_home) if owner_home else Path(home_paths.root)
    return ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=target_home,
            template=str(getattr(agent.config, "workspace_task_path_template", "")),
            task_name=getattr(params, "task_id", "") or user_prompt,
            user_prompt=user_prompt,
            request_id=str(getattr(params, "request_id", "") or ""),
            run_id=str(getattr(params, "run_id", "") or ""),
            task_id=str(getattr(params, "task_id", "") or ""),
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            owner_home=str(getattr(home_paths, "owner_home_dir", "") or ""),
            source=str(getattr(params, "source", "") or "run"),
        )
    )


def _workspace_prompt_section(paths) -> str:
    return "\n".join(
        [
            "# Current Task Workspace",
            "- 本轮任务已有独立任务目录；如果需要写交付物，请优先写到 output_dir。",
            "- work_dir 用于草稿、日志、中间材料和过程文件；不要把参考源码目录当成交付目录。",
            "- 如果用户只要求聊天回答、不需要文件，可以正常直接回答，不必强行落盘。",
            f"- task_root: {paths.root}",
            f"- output_dir: {paths.output_dir}",
            f"- work_dir: {paths.work_dir}",
        ]
    )


def _append_once(items: list[str], injection: str) -> list[str]:
    marker = "# Current Task Workspace"
    return items if any(marker in str(item) for item in items) else [*items, injection]


def _task_attributes_with_workspace(attrs: object, paths) -> dict:
    result = dict(attrs) if isinstance(attrs, dict) else {}
    result["run_workspace"] = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    return result


__all__ = ["attach_run_task_workspace_context", "write_run_task_workspace_if_needed"]
