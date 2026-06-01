# LLM: Run task workspace writer keeps finalization thin while preserving owner isolation.
# 模块用途: 保存型主代理 run 收尾时创建当前 owner 的任务工作区，并登记轻量索引。

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..user_space.run_workspace import EnsureRunWorkspaceRequest, ensure_run_workspace
from ._runtime_params import ArchiveRunParams
from .run_task_workspace_index import register_saved_run_task_ref


# LLM: write_run_task_workspace_if_needed creates one owner-scoped task workspace for saved root runs.
# 函数用途: 保存型 run 收尾时按 owner home 创建任务工作区，并避免 provider 用户再写 legacy workspace。
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


# LLM: attach_run_task_workspace_context makes the current output/work roots visible before the first model turn.
# 函数用途: 保存型主代理 run 开始时创建任务目录，并把 output/work 作为软运行状态注入 prompt。
def attach_run_task_workspace_context(agent, params, user_prompt: str):
    if not _should_create_workspace(agent, params):
        return params
    result = _ensure_workspace_for_run(agent, params, user_prompt)
    injection = _workspace_prompt_section(result)
    next_inject = _append_once(list(getattr(params, "inject", None) or []), injection)
    next_attrs = _task_attributes_with_workspace(getattr(params, "task_attributes", None), result)
    agent._current_run_task_workspace = str(result.root)
    return replace(params, inject=next_inject, task_attributes=next_attrs)


# LLM: _should_create_workspace keeps no-save and task-local runs from leaking persistent task folders.
# 函数用途: 判断本轮是否应创建 owner task workspace；保存关闭或隔离上下文时返回 False。
def _should_create_workspace(agent, params) -> bool:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return False
    if str(getattr(params, "context_scope", "") or "").strip().lower() in {"task_local", "control_plane"}:
        return False
    auto_save = bool(getattr(agent.config, "auto_save_memory", True))
    do_save = auto_save if getattr(params, "save", None) is None else bool(getattr(params, "save", None))
    return bool(do_save and getattr(agent, "home_paths", None) is not None)


# LLM: _ensure_workspace_for_run centralizes the run-start workspace request shape.
# 函数用途: 用当前 owner、run 和 task 标识创建或复用本轮任务目录。
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


# LLM: _workspace_prompt_section tells the model where this task's deliverables and scratch files belong.
# 函数用途: 渲染短提示，区分 output 交付区和 work 过程区，不强迫纯聊天任务落盘。
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


# LLM: _append_once prevents compact auto-continuation from duplicating the same workspace prompt.
# 函数用途: 只在注入列表中追加一次当前任务目录提示。
def _append_once(items: list[str], injection: str) -> list[str]:
    marker = "# Current Task Workspace"
    return items if any(marker in str(item) for item in items) else [*items, injection]


# LLM: _task_attributes_with_workspace makes the same task workspace refs available to machine readers.
# 函数用途: 把 task_root、output_dir 和 work_dir 写入 task_attributes.run_workspace。
def _task_attributes_with_workspace(attrs: object, paths) -> dict:
    result = dict(attrs) if isinstance(attrs, dict) else {}
    result["run_workspace"] = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    return result


__all__ = ["attach_run_task_workspace_context", "write_run_task_workspace_if_needed"]
