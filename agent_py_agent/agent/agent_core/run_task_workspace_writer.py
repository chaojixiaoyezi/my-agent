# LLM: Run task workspace writer keeps finalization thin while preserving owner isolation.
# 模块用途: 保存型主代理 run 收尾时创建当前 owner 的任务工作区，并登记轻量索引。

from __future__ import annotations

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
    target_home = Path(owner_home) if owner_home and not _is_local_main_owner(home_paths) else Path(home_paths.root)
    result = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=target_home,
            template=str(getattr(agent.config, "workspace_task_path_template", "")),
            task_name=params.task_id or params.run_id or params.run_request_id or params.user_prompt,
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


# LLM: _is_local_main_owner keeps local CLI compatibility separate from provider owner isolation.
# 函数用途: 判断当前 owner 是否是本地默认主账号，只有该账号继续使用 legacy 任务工作区。
def _is_local_main_owner(home_paths) -> bool:
    return (
        str(getattr(home_paths, "owner_provider", "") or "local") == "local"
        and str(getattr(home_paths, "owner_kind", "") or "main") == "main"
        and str(getattr(home_paths, "owner_id", "") or "local/main") == "local/main"
    )


__all__ = ["write_run_task_workspace_if_needed"]
