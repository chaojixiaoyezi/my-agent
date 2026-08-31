
from __future__ import annotations

from pathlib import Path

from ...user_space.runtime_paths import runtime_owner_root


def runtime_scope_root(
    agent: object,
    *,
    context_scope: str = "",
    task_attributes: object = None,
) -> Path:
    """Resolve the persistence root for one runtime scope.

    A task-local subagent owns its compact/archive files inside its existing
    agent-run workspace.  This does not grant a new write root and does not
    write into the owner's long-term memory.
    """

    params = getattr(agent, "_current_run_params", None)
    scope = str(context_scope or getattr(params, "context_scope", "") or "").strip().lower()
    attrs = task_attributes
    if not isinstance(attrs, dict):
        candidate = getattr(params, "task_attributes", None)
        attrs = candidate if isinstance(candidate, dict) else {}
    if scope == "task_local":
        workspace = _task_local_workspace(agent, attrs, params)
        if not workspace:
            raise RuntimeError(
                "task_local runtime requires an existing agent_run_workspace_dir; "
                "refusing to fall back to owner storage"
            )
        return Path(workspace).expanduser().resolve(strict=False)
    return runtime_owner_root(agent)


def _task_local_workspace(agent: object, attrs: dict[str, object], params: object) -> str:
    explicit = str(attrs.get("agent_run_workspace_dir") or "").strip()
    if explicit:
        return explicit
    run_id = str(getattr(params, "run_id", "") or "").strip()
    manager = getattr(agent, "subagents", None)
    if not run_id or manager is None or not hasattr(manager, "load"):
        return ""
    try:
        task = manager.load(run_id)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return ""
    return str(getattr(task, "agent_run_workspace_dir", "") or "").strip()


def runtime_archive_roots(
    agent: object,
    *,
    context_scope: str = "",
    task_attributes: object = None,
) -> tuple[Path, ...]:
    return (
        runtime_scope_root(
            agent,
            context_scope=context_scope,
            task_attributes=task_attributes,
        ),
    )


__all__ = ["runtime_archive_roots", "runtime_owner_root", "runtime_scope_root"]
