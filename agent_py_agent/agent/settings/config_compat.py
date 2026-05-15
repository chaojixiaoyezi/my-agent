# LLM: Hidden compatibility fields allow older config files without re-exposing removed knobs.
# 模块用途: 存放旧配置兼容字段名单，load_config 可忽略这些字段但不把它们作为新接口推荐。

from __future__ import annotations

HIDDEN_COMPAT_CONFIG_FIELDS = {
    "auto_bench_model_on_first_use",
    "dynamic_timeout_max",
    "dynamic_timeout_min",
    "dynamic_timeout_safety_margin",
    "max_auto_retry_attempts",
    "max_auto_split_depth",
    "model_speed_profile_path",
    "runner_concurrency",
    "runner_start_rate",
    "runner_timeout_by_role",
    "runner_timeout_seconds",
    "scheduler_mode",
    "subagent_allowed_tools",
    "subagent_automation_level",
    "subagent_board_limit",
    "subagent_builtin_workflows",
    "subagent_cli_default_limit",
    "subagent_context_summary_inline_json_chars",
    "subagent_context_summary_inline_text_chars",
    "subagent_descendant_scan_limit",
    "subagent_hierarchy_default_max_depth",
    "subagent_hierarchy_max_children_per_tool_call",
    "subagent_hierarchy_recovery_max_nodes",
    "subagent_probe_default_limit",
    "subagent_spawn_default_count",
    "subagent_takeover_chain_max_depth",
    "subagent_user_workflow_dirs",
    "subagent_workflow_mode",
    "subagent_workflow_review_rounds",
    "task_max_grandchildren",
    "task_max_subagents",
}
