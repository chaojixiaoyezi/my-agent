# LLM: Context bundle source refs stay separate from bundle assembly to keep the main file small.
# 模块用途: 维护 context bundle 字段来源说明，方便调试和后续 schema 扩展。

from __future__ import annotations


# LLM: source_refs makes every major bundle field traceable to existing task facts.
# 函数用途: 给大模型和调试人员标明关键字段从哪里来，后续可扩展到 ledger/compact/task refs。
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
