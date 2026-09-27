# LLM: 工具与会话类整数配置的范围表（键, 最小, 最大）；改动须同步 AgentConfig 默认值与随包 YAML。
# 模块用途: 列出工具、会话历史与 CLI 列表类整数配置的合法范围，供规范化统一校验。
from __future__ import annotations

TOOL_INT_FIELDS = (
    ("max_tool_rounds", 0, None),
    ("max_parallel_tool_calls", 0, None),
    ("tool_agent_budget_max_calls", 0, None),
    ("tool_artifact_read_budget_max_chars", 0, None),
    ("tool_output_externalize_min_chars", 0, None),
    ("tool_output_preview_chars", 0, None),
    ("tool_read_max_chars", 100, None),
    ("tool_write_inline_max_chars", 100, 100_000),
    ("tool_web_max_chars", 0, None),
    ("tool_http_timeout", 1, None),
    ("tool_shell_timeout", 1, None),
    ("tool_shell_output_max_chars", 100, None),
    ("tool_catalog_limit", 0, None),
    ("tool_catalog_offset", 0, None),
    ("tool_catalog_entry_max_chars", 0, None),
    ("tool_detail_max_chars", 0, None),
    ("chat_history_max_turns", 1, None),
    ("conversation_history_max_turns", 1, None),
    ("conversation_history_max_chars", 1000, None),
    ("conversation_terminal_tool_fold_max_chars", 1000, 48_000),
    ("compact_landmark_max_tokens", 0, 200_000),
    ("conversation_terminal_tool_hot_tail_seconds", 0, 86_400),
    ("cli_audit_cleanup_days", 0, None),
)
