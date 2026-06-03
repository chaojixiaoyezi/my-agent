
from __future__ import annotations


def source_refs() -> dict[str, list[str]]:
    return {
        "goal": ["task.goal"],
        "thought": ["task.thought"],
        "plan": ["task.plan"],
        "acceptance_checks": ["task.acceptance_checks"],
        "permissions": ["task.allowed_tools", "task.allowed_skills", "task.capability_grants"],
        "constraints": ["task.allowed_write_roots", "task.forbidden_write_roots", "task.locked_files"],
        "workspace_refs": ["task.task_dir", "task.task_workspace_dir", "task.agent_run_workspace_dir"],
        "output_contract": [
            "task.output_json",
            "task.runner_result_json",
            "task.agent_run_final_report_md",
            "task.goal",
            "task.thought",
            "task.acceptance_checks",
        ],
        "lineage": ["task.root_id", "task.parent_id", "task.depth", "task.inheritance_manifest_json"],
        "context_packs": ["task.context_packs"],
    }
