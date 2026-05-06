from __future__ import annotations

"""LLM: bridge legacy subagent work orders into runtime memory task workspaces."""

from pathlib import Path

from ...memory_archive.task_workspace import ensure_subagent_task_workspace
from ..models import SubAgentTask


def sync_task_workspace_fields(workspace: str | Path, task: SubAgentTask) -> None:
    """Create/update the task workspace adapter and copy its paths onto the task."""

    task_workspace_paths = ensure_subagent_task_workspace(workspace, task)
    task.task_workspace_dir = str(task_workspace_paths.root)
    task.task_workspace_task_yaml = str(task_workspace_paths.task_yaml)
    task.task_workspace_state_json = str(task_workspace_paths.state_json)
    task.task_workspace_timeline_jsonl = str(task_workspace_paths.timeline_jsonl)
    task.task_workspace_summary_file = str(task_workspace_paths.current_summary)
    task.task_workspace_shared_dir = str(task_workspace_paths.shared_dir)
    task.task_workspace_artifacts_dir = str(task_workspace_paths.artifacts_dir)
    task.task_workspace_agents_dir = str(task_workspace_paths.agents_dir)
    task.agent_run_workspace_dir = str(task_workspace_paths.agent_adapter_dir)
    task.legacy_run_ref_json = str(task_workspace_paths.legacy_run_ref_json)
