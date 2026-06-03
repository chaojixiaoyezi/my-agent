
from __future__ import annotations

from dataclasses import dataclass, replace
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
    next_contract = _delivery_contract_with_workspace(getattr(params, "delivery_contract", None), result)
    agent._current_run_task_workspace = str(result.root)
    return replace(params, inject=next_inject, task_attributes=next_attrs, delivery_contract=next_contract)


def _should_create_workspace(agent, params) -> bool:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return False
    if str(getattr(params, "context_scope", "") or "").strip().lower() in {"task_local", "control_plane"}:
        return False
    auto_save = bool(getattr(agent.config, "auto_save_memory", True))
    do_save = auto_save if getattr(params, "save", None) is None else bool(getattr(params, "save", None))
    return bool(do_save and getattr(agent, "home_paths", None) is not None)


def _ensure_workspace_for_run(agent, params, user_prompt: str):
    existing = _existing_workspace_paths(getattr(params, "task_attributes", None))
    if existing is not None:
        existing.root.mkdir(parents=True, exist_ok=True)
        existing.output_dir.mkdir(parents=True, exist_ok=True)
        existing.work_dir.mkdir(parents=True, exist_ok=True)
        return existing
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


@dataclass(frozen=True)
class _ExistingWorkspacePaths:
    root: Path
    output_dir: Path
    work_dir: Path


def _existing_workspace_paths(attrs: object) -> _ExistingWorkspacePaths | None:
    if not isinstance(attrs, dict):
        return None
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    root_text = str(workspace.get("task_root") or "").strip()
    if not root_text:
        return None
    root = Path(root_text)
    output_dir = Path(str(workspace.get("output_dir") or root / "output"))
    work_dir = Path(str(workspace.get("work_dir") or root / "work"))
    return _ExistingWorkspacePaths(root=root, output_dir=output_dir, work_dir=work_dir)


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


def _delivery_contract_with_workspace(contract: object, paths):
    if not isinstance(contract, dict):
        return contract
    result = dict(contract)
    result["task_workspace"] = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        result["artifacts"] = [_artifact_with_default_output_root(item, paths) for item in artifacts]
    return result


def _artifact_with_default_output_root(item: object, paths) -> object:
    if not isinstance(item, dict):
        return item
    artifact = dict(item)
    if _artifact_declares_output_target(artifact):
        return artifact
    artifact["allowed_output_roots"] = [str(paths.output_dir)]
    return artifact


def _artifact_declares_output_target(artifact: dict) -> bool:
    if str(artifact.get("preferred_path") or artifact.get("path") or "").strip():
        return True
    for key in ("allowed_output_roots", "search_roots", "artifact_roots"):
        value = artifact.get(key)
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            return True
    return False


__all__ = ["attach_run_task_workspace_context", "write_run_task_workspace_if_needed"]
