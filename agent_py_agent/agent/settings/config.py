# LLM: 这是运行配置入口，解析规则和默认值变更会扩散到启动、工具和子代理。
# 模块用途: 主 AgentConfig 模型和轻量 YAML 配置加载。

from __future__ import annotations

"""智能体配置加载工具。

这个模块干的事情不复杂，但很关键：
- 定义程序运行时到底有哪些配置项
- 从磁盘读取一个简化版 YAML 配置
- 把未知字段过滤掉，避免用户多写了配置就直接把程序搞崩

这里坚持只用标准库，目的是让项目在 Windows / Linux / macOS 上都能轻装运行。
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_io import load_simple_yaml, parse_scalar
from .memory import normalize_agent_memory_config
from .normalize import (
    _coerce_bool_config,
    _coerce_choice_config,
    _coerce_float_config,
    _coerce_int_config,
    normalize_agent_config,
    normalize_subagent_workflow_config,
)
from .tool_config import DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS

__all__ = [
    "AgentConfig",
    "_coerce_bool_config",
    "_coerce_choice_config",
    "_coerce_float_config",
    "_coerce_int_config",
    "load_config",
    "load_simple_yaml",
    "parse_scalar",
    "apply_log_level",
    "normalize_agent_config",
    "normalize_subagent_workflow_config",
]

_INT_PATTERN = re.compile(r"-?[0-9]+")
_FLOAT_PATTERN = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


# LLM: AgentConfig 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 主运行配置对象，汇总模型、工具、gateway、子代理、通知和用户空间字段。
@dataclass
class AgentConfig:

    agent_name: str = "myagent"
    system_prompt: str = "你是一个谨慎、可扩展、会记录记忆、会在必要时调用工具的 Python CLI 智能体。先理解任务，再给出结构化回答。"
    workspace_root: str | list[str] = ""
    auto_detect_work_on_startup: bool = True
    model_backend: str = "echo"
    memory_path: str = "data/memory.jsonl"
    memory_top_k: int = 5
    auto_save_memory: bool = True
    memory_archive_level: int = 3
    memory_hook_enabled: bool = True
    memory_hook_archive_level: int = 3
    memory_hook_retention_days: int = 7
    memory_rule_routing_enabled: bool = True
    memory_rule_routing_mode: str = "soft"
    memory_rule_auto_read_limit: int = 3
    memory_rule_receipt_enabled: bool = True
    memory_resume_auto_context_enabled: bool = False
    memory_resume_auto_context_mode: str = "trigger"
    memory_resume_auto_context_limit: int = 5
    memory_compact_auto_allow_apply: bool = False
    memory_artifact_default_read_chars: int = 4000
    memory_archive_preview_level_0_chars: int = 2048
    memory_archive_preview_level_1_chars: int = 1024
    memory_archive_preview_level_2_chars: int = 512
    memory_archive_preview_level_3_chars: int = 160
    memory_archive_summary_chars: int = 96
    memory_archive_search_file_limit: int = 30
    memory_query_default_limit: int = 100
    memory_query_default_page_size: int = 100
    memory_query_content_preview_chars: int = 500
    memory_resume_archive_scan_limit: int = 0
    memory_resume_recommended_read_paths_limit: int = 20
    memory_doctor_recent_archive_file_limit: int = 5
    memory_config_warnings: list[dict[str, Any]] = field(default_factory=list)
    local_store_path: str = "data/local_store/local.db"
    local_store_files_dir: str = "data/local_store/files"
    local_store_events_path: str = "data/local_store/events.jsonl"
    local_store_fts_enabled: bool = True
    enable_self_learning: bool = False
    prompt_files: list[str] = field(default_factory=list)
    enable_subagents: bool = True
    max_subagents: int = 1000
    subagent_board_limit: int = 5
    subagent_workspace: str = "data/subagents"
    subagent_allowed_tools: list[str] = field(default_factory=list)
    subagent_role_template_dirs: list[str] = field(default_factory=list)
    subagent_workflow_mode: str = "auto"
    subagent_builtin_workflows: bool = True
    subagent_user_workflow_dirs: list[str] = field(default_factory=lambda: [".agent/workflows/user"])
    subagent_workflow_review_rounds: int = 1
    subagent_workflow_config_warnings: list[dict[str, Any]] = field(default_factory=list)
    task_max_subagents: int = 0
    task_max_grandchildren: int = 0
    subagent_spawn_default_count: int = 3
    subagent_cli_default_limit: int = 20
    subagent_probe_default_limit: int = 20
    subagent_hierarchy_default_max_depth: int = 2
    subagent_hierarchy_recovery_max_nodes: int = 200
    subagent_hierarchy_max_children_per_tool_call: int = 2
    subagent_descendant_scan_limit: int = 128
    subagent_context_summary_inline_json_chars: int = 900
    subagent_context_summary_inline_text_chars: int = 500
    subagent_automation_level: int = 2
    subagent_debug_trace_level: int = 0
    acceptance_execute_tests: bool = False
    acceptance_test_timeout_seconds: int = 120
    dynamic_timeout_safety_margin: float = 2.0
    dynamic_timeout_min: int = 30
    dynamic_timeout_max: int = 600
    max_auto_split_depth: int = 2
    max_auto_retry_attempts: int = 3
    model_speed_profile_path: str = "data/model_speed_profile.json"
    auto_bench_model_on_first_use: bool = True
    user_id: str = "admin"
    user_data_root: str = "data/users"
    # 多租户鉴权配置
    auth_enabled: bool = True
    admin_user_id: str = "admin"
    scheduler_mode: str = "auto"
    runner_concurrency: str = "auto"
    runner_start_rate: str = "auto"
    runner_timeout_seconds: str = "off"
    runner_failure_policy: str = "auto"
    gateway_workspace: str = "data/gateway"
    gateway_heartbeat_interval: int = 5
    gateway_stale_seconds: int = 120
    gateway_stop_timeout: int = 20
    gateway_request_timeout: int = 300
    gateway_request_poll_interval: int = 1
    gateway_request_workers: int = 1
    gateway_processing_timeout_seconds: int = 900
    gateway_request_max_attempts: int = 2
    gateway_port: int = 8420
    gateway_worker_join_timeout_seconds: int = 2
    gateway_ready_timeout_seconds: int = 10
    gateway_service_command_timeout_seconds: int = 30
    gateway_service_stop_timeout_seconds: int = 90
    adapter_workspace: str = "data/adapters/file"
    # 飞书适配器配置
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_callback_port: int = 8421
    # QQ 适配器配置
    qq_app_id: str = ""
    qq_app_secret: str = ""
    session_workspace: str = "data/sessions"
    notification_enabled: bool = True
    notification_store_path: str = "data/notifications"
    notification_channel_timeout_seconds: int = 300
    concurrency_lock_enabled: bool = True
    task_lock_timeout_seconds: int = 30
    audit_enabled: bool = True
    audit_log_path: str = "data/audit"
    daemon_planner: bool = True
    daemon_apply: bool = True
    daemon_execute_runners: bool = True
    daemon_interval: int = 30
    daemon_max_runners: str = "auto"
    daemon_limit: int = 0
    daemon_max_cycles: int = 0
    daemon_max_cards: int = 0
    daemon_probe: bool = True
    daemon_reviewer: str = "parent-daemon"
    daemon_runner_instruction: str = ""
    lease_heartbeat_interval_seconds: int = 60
    lease_stale_without_heartbeat_seconds: int = 300
    log_level: str = "info"
    extensions_dir: str = "extensions"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "AGENT_API_KEY"
    model_name: str = "gpt-4o-mini"
    request_timeout: int = 60
    max_tokens: int = 1024
    temperature: str = "0.2"
    anthropic_version: str = "2023-06-01"
    enable_tools: bool = True
    max_tool_rounds: int = 0
    tool_agent_budget_window_seconds: int = 600
    tool_agent_budget_max_calls: int = 50
    tool_artifact_read_budget_window_seconds: int = 600
    tool_artifact_read_budget_max_chars: int = 240_000
    tool_read_max_chars: int = 50_000
    tool_write_inline_max_chars: int = DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS
    tool_list_max_entries: int = 200
    tool_search_max_matches: int = 50
    tool_web_max_chars: int = 100_000
    tool_http_timeout: int = 30
    tool_shell_timeout: int = 240
    stream_enabled: bool = True
    tool_catalog_limit: int = 20
    tool_catalog_mode: str = "compact"
    tool_catalog_offset: int = 0
    tool_catalog_categories: list[str] = field(default_factory=list)
    tool_catalog_include_examples: bool = True
    tool_catalog_entry_max_chars: int = 1200
    tool_catalog_show_truncated_notice: bool = True
    tool_detail_max_chars: int = 4000
    tool_retrieval_limit: int = 3
    tool_vector_search_enabled: bool = True
    chat_history_max_turns: int = 20
    chat_history_assistant_preview_chars: int = 500
    chat_transcript_max_chars: int = 500_000
    chat_collapse_preview_lines: int = 12
    chat_collapse_preview_chars: int = 900
    chat_context_window_chars: int = 200_000
    chat_transcript_scroll_lines: int = 10
    cli_status_limit: int = 5
    cli_timeline_limit: int = 20
    cli_memory_list_limit: int = 20
    cli_memory_search_limit: int = 5
    cli_chat_memory_limit: int = 5
    cli_memory_archive_limit: int = 20
    cli_memory_route_limit: int = 5
    cli_local_search_limit: int = 5
    cli_local_search_preview_chars: int = 500
    cli_local_doctor_limit: int = 20
    cli_task_list_limit: int = 50
    cli_notification_limit: int = 20
    cli_audit_limit: int = 100
    cli_audit_cleanup_days: int = 90
    # Dispatch 闭环保证配置
    dispatch_max_consecutive_rounds: int = 20
    dispatch_active_interval: int = 5
    dispatch_idle_interval: int = 30
    dispatch_default_max_runners: int = 1
    dispatch_default_limit: int = 20
    dispatch_default_watch_interval: float = 30.0
    dispatch_pending_runner_scan_limit: int = 999
    # Watchdog 配置
    watchdog_enabled: bool = False
    watchdog_interval: int = 60
    watchdog_max_restarts: int = 3
    watchdog_restart_delay: int = 10
    config_warnings: list[str] = field(default_factory=list)


# LLM: load_config 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 读取 load_config 数据并转换成内部对象。
def load_config(config_path: str | Path) -> AgentConfig:

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = load_simple_yaml(path)

    # 先做类型验证和回退（在过滤未知 key 之前）
    normalized, config_warnings = normalize_agent_config(raw)

    # 过滤未知字段
    allowed = set(AgentConfig.__dataclass_fields__.keys())
    unknown_keys = [key for key in normalized if key not in allowed]
    for key in unknown_keys:
        config_warnings.append(f"unknown config key: {key!r}; ignored")
    clean = {key: value for key, value in normalized.items() if key in allowed}
    config = AgentConfig(**clean)
    config.config_warnings = config_warnings

    normalize_agent_memory_config(config)
    normalize_subagent_workflow_config(config)

    # 优先从环境变量读取密钥。
    # 大白话解释：仓库里只留"去哪里拿 key"的说明，不再把真 key 写进代码仓库。
    env_name = str(config.api_key_env).strip()
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            config.api_key = env_value

    apply_log_level(config)
    return config


# LLM: apply_log_level makes the user-facing log_level config affect package loggers without touching global handlers.
# 函数用途: 根据 log_level 调整 agent_py_agent 包日志级别；改配置后重新加载配置即可生效。
def apply_log_level(config: AgentConfig) -> None:
    level_name = str(getattr(config, "log_level", "info") or "info").strip().lower()
    logging.getLogger("agent_py_agent").setLevel(_LOG_LEVELS.get(level_name, logging.INFO))
