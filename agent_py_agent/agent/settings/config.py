
from __future__ import annotations

"""智能体配置加载工具。

这个模块干的事情不复杂，但很关键：
- 定义程序运行时到底有哪些配置项
- 从磁盘读取一个简化版 YAML 配置
- 把未知字段过滤掉，避免用户多写了配置就直接把程序搞崩

这里坚持只用标准库，目的是让项目在 Windows / Linux / macOS 上都能轻装运行。
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..path_access_policy import DEFAULT_DANGEROUS_PATH_ROOTS, DEFAULT_PATH_ACCESS_MODE
from .config_io import load_simple_yaml, parse_scalar
from .config_sources import (
    INTERNAL_RUNTIME_CONFIG_FIELDS,
    merge_agent_config_sources,
    public_config_keys,
)
from .defaults import (
    DEFAULT_COMMAND_ACCESS_MODE,
    DEFAULT_MODEL_MAX_TOKENS,
    DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS,
)
from .memory import normalize_agent_memory_config
from .normalize import (
    _coerce_bool_config,
    _coerce_choice_config,
    _coerce_float_config,
    _coerce_int_config,
    normalize_agent_config,
    normalize_subagent_workflow_config,
)

__all__ = [
    "AgentConfig",
    "INTERNAL_RUNTIME_CONFIG_FIELDS",
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


@dataclass
class _HomeProviderConfigFields:
    my_agent_home: str = ""
    my_agent_owner_provider: str = "local"
    my_agent_owner_kind: str = "main"
    my_agent_owner_id: str = "main"
    workspace_task_path_template: str = "tasks/{date}/{task_slug}"
    home_context_enabled: bool = True
    home_lesson_auto_read_limit: int = 3
    daily_memory_mirror_enabled: bool = True
    run_task_workspace_enabled: bool = True
    external_knowledge_index_file_name: str = "MY_AGENT_INDEX.md"
    external_knowledge_directory_roots: list[str] = field(default_factory=list)
    external_knowledge_api_sources: list[str] = field(default_factory=list)
    external_knowledge_database_sources: list[str] = field(default_factory=list)
    provider_space_default_max_storage_mb: int = 2048
    provider_space_max_download_file_mb: int = 200
    provider_space_trash_retention_days: int = 30
    provider_space_destructive_actions_use_trash: bool = True


@dataclass
class _ToolConfigFields:
    enable_tools: bool = True
    max_tool_rounds: int | None = None
    max_tool_calls_per_round: int | None = None
    tool_agent_budget_window_seconds: int | None = None
    tool_agent_budget_max_calls: int | None = None
    tool_artifact_read_budget_window_seconds: int = 600
    tool_artifact_read_budget_max_chars: int = 240_000
    tool_output_externalize_min_chars: int = 20_000
    tool_output_preview_chars: int = 4_000
    tool_payload_max_fields: int = 64
    tool_payload_max_field_name_chars: int = 128
    tool_payload_max_name_chars: int = 128
    tool_payload_parse_error_raw_chars: int = 1000
    tool_read_max_chars: int = 50_000
    tool_write_inline_max_chars: int = DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS
    tool_list_max_entries: int = 200
    tool_search_max_matches: int = 50
    tool_web_max_chars: int = 100_000
    tool_http_timeout: int = 30
    path_access_mode: str = DEFAULT_PATH_ACCESS_MODE
    path_dangerous_roots: list[str] = field(default_factory=lambda: list(DEFAULT_DANGEROUS_PATH_ROOTS))
    access_mode: str = DEFAULT_COMMAND_ACCESS_MODE
    tool_shell_timeout: int = 240
    tool_shell_output_max_chars: int = 12_000
    stream_enabled: bool = True
    tool_catalog_limit: int = 20
    tool_catalog_mode: str = "compact"
    tool_catalog_offset: int = 0
    tool_catalog_categories: list[str] = field(default_factory=list)
    # Default prompt catalog stays compact: examples and long parameter notes
    # remain available through list_tools or the recommended-tool details.
    tool_catalog_include_examples: bool = False
    tool_catalog_entry_max_chars: int = 700
    tool_catalog_show_truncated_notice: bool = True
    tool_detail_max_chars: int = 4000
    tool_retrieval_limit: int = 3
    tool_vector_search_enabled: bool = True


@dataclass
class _RuntimeBudgetConfigFields:
    contract_status_max_scan_files: int = 1000
    contract_status_max_report_bytes: int = 2_000_000
    contract_status_recent_findings_limit: int = 20
    skill_guard_max_files: int = 50
    skill_guard_max_size_kb: int = 1024
    small_real_acceptance_max_runtime_seconds: int = 900
    real_run_review_max_report_bytes: int = 5_000_000
    real_run_review_max_log_bytes: int = 1_000_000
    runner_auto_concurrency: int = 8
    conversation_thread_list_limit: int = 100
    conversation_pending_wake_limit: int = 100
    conversation_context_recent_limit: int = 20
    conversation_unhandled_observation_limit: int = 20
    background_pending_wake_prompt_limit: int = 20
    background_context_max_string_chars: int = 1200
    background_context_max_list_items: int = 20
    background_context_max_dict_items: int = 80
    background_context_max_depth: int = 6
    background_claim_ttl_seconds: int = 900
    background_claim_heartbeat_interval_seconds: int = 0
    subagent_watch_interval_seconds: int = 120
    background_main_agent_allowed_tools: list[str] = field(default_factory=list)


@dataclass
class AgentConfig(_HomeProviderConfigFields, _ToolConfigFields, _RuntimeBudgetConfigFields):

    agent_name: str = "myagent"
    system_prompt: str = "你是一个谨慎、可扩展、会记录记忆、会在必要时调用工具的 Python CLI 智能体。先理解任务，再给出结构化回答。"
    workspace_root: str | list[str] = ""
    auto_detect_work_on_startup: bool = True
    model_backend: str = "echo"
    memory_path: str = ""
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
    memory_compact_auto_trigger_percent: int = 70
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
    local_store_path: str = ""
    local_store_files_dir: str = ""
    local_store_events_path: str = ""
    local_store_fts_enabled: bool = True
    enable_self_learning: bool = False
    prompt_files: list[str] = field(default_factory=list)
    enable_subagents: bool = True
    subagent_mode: str = "trusted_local_hardening"
    max_subagents: int = 50
    subagent_board_limit: int = 5
    subagent_workspace: str = ""
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
    subagent_hierarchy_default_max_depth: int = 0
    subagent_hierarchy_recovery_max_nodes: int = 200
    subagent_hierarchy_max_children_per_tool_call: int = 0
    subagent_descendant_scan_limit: int = 128
    subagent_takeover_chain_max_depth: int = 0
    subagent_context_summary_inline_json_chars: int = 900
    subagent_context_summary_inline_text_chars: int = 500
    subagent_debug_trace_level: int = 0
    subagent_memory_retention_policy: str = "parent_review_or_cleanup"
    subagent_memory_delete_after_days: int = 0
    subagent_destroy_summary_required: bool = True
    result_check_execute_tests: bool = False
    result_check_timeout_seconds: int = 120
    closeout_for_all_task_nodes: bool = False
    dynamic_timeout_safety_margin: float = 2.0
    dynamic_timeout_min: int = 30
    dynamic_timeout_max: int = 600
    max_auto_split_depth: int = 2
    max_auto_retry_attempts: int = 3
    model_speed_profile_path: str = ""
    auto_bench_model_on_first_use: bool = True
    user_id: str = "admin"
    # 多租户鉴权配置
    auth_enabled: bool = True
    admin_user_id: str = "admin"
    scheduler_mode: str = "auto"
    runner_concurrency: str = "auto"
    runner_start_rate: str = "auto"
    runner_timeout_seconds: str = "off"
    runner_timeout_by_role: dict[str, object] = field(default_factory=dict)
    runner_failure_policy: str = "auto"
    gateway_workspace: str = ""
    gateway_heartbeat_interval: int = 5
    gateway_stale_seconds: int = 120
    gateway_stop_timeout: int = 20
    gateway_request_timeout: int = 300
    gateway_request_poll_interval: float = 0.2
    gateway_request_workers: int = 3
    gateway_processing_timeout_seconds: int = 900
    gateway_request_max_attempts: int = 2
    gateway_port: int = 8420
    gateway_worker_join_timeout_seconds: int = 2
    gateway_ready_timeout_seconds: int = 10
    gateway_service_command_timeout_seconds: int = 30
    gateway_service_stop_timeout_seconds: int = 90
    adapter_workspace: str = ""
    # 飞书适配器配置
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_callback_port: int = 8421
    # QQ 适配器配置
    qq_app_id: str = ""
    qq_app_secret: str = ""
    session_workspace: str = ""
    # 长期主代理会话账本目录。它保存 thread/message/task/policy 机器事实，
    # 不保存真实通道凭证，也不把用户任务变成内置 case。
    conversation_workspace: str = ""
    # 多代理协作控制面目录。这里保存 case/request/evidence/decision 的轻量账本，
    # 大日志、大文件、API 返回和截图只通过 evidence_refs 引用，避免把协作层变成业务模板。
    collaboration_workspace: str = ""
    # 协作请求自动唤醒 responder 的最大并发数。普通 dispatch 仍保持默认宽度；
    # 只有已有 collaboration request 等待多个代理响应时，才用这个上限减少串行等待。
    # 0 表示关闭自动放宽，完全按 dispatch/max_runners 原值执行。
    collaboration_auto_dispatch_max_runners: int = 8
    # 协作请求默认截止时间。模型没有显式传 deadline_at/deadline_seconds 时，
    # 系统会给 request 自动补一个相对 deadline，避免大规模协作无限等全员。
    # 0 表示不自动补截止时间，只使用模型或用户显式给出的 deadline。
    collaboration_default_deadline_seconds: int = 120
    notification_enabled: bool = True
    notification_store_path: str = ""
    notification_channel_timeout_seconds: int = 300
    concurrency_lock_enabled: bool = True
    task_lock_timeout_seconds: int = 30
    audit_enabled: bool = True
    audit_log_path: str = ""
    daemon_planner: bool = True
    daemon_mutate_state: bool = True
    daemon_start_runners: bool = True
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
    request_timeout: int = 240
    max_tokens: int = DEFAULT_MODEL_MAX_TOKENS
    model_context_window_tokens: int = 200_000
    temperature: str = "0.2"
    anthropic_version: str = "2023-06-01"
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
    config_path: str = ""
    config_sources: dict[str, dict[str, object]] = field(default_factory=dict)
    config_layers: list[dict[str, object]] = field(default_factory=list)

    def config_source_for(self, key: str) -> dict[str, object]:
        return dict(self.config_sources.get(key, {}))

    def config_source_snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "agent_config_sources.v1",
            "config_path": self.config_path,
            "layers": list(self.config_layers),
            "sources": dict(self.config_sources),
        }


def load_config(config_path: str | Path) -> AgentConfig:

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = load_simple_yaml(path)

    # 先做类型验证和默认值归一（在过滤未知 key 之前）
    normalized, config_warnings = normalize_agent_config(raw)

    # 过滤未知字段。运行时诊断字段只由 loader 写入，不能从 YAML 注入。
    allowed = _public_config_keys()
    raw_keys = set(raw)
    unknown_keys = [key for key in raw_keys if key not in allowed]
    for key in unknown_keys:
        config_warnings.append(f"unknown config key: {key!r}; ignored")
    resolved_path = str(path.expanduser().resolve())
    effective = merge_agent_config_sources(
        config_cls=AgentConfig,
        normalized=normalized,
        raw_keys=raw_keys,
        path=path,
    )
    config = AgentConfig(**effective.values)
    config.config_path = resolved_path
    config.config_warnings = config_warnings
    config.config_sources = effective.sources
    config.config_layers = list(effective.layers)

    normalize_agent_memory_config(config)
    normalize_subagent_workflow_config(config)

    apply_log_level(config)
    return config


def _public_config_keys() -> set[str]:
    return public_config_keys(AgentConfig)


def apply_log_level(config: AgentConfig) -> None:
    level_name = str(getattr(config, "log_level", "info") or "info").strip().lower()
    logging.getLogger("agent_py_agent").setLevel(_LOG_LEVELS.get(level_name, logging.INFO))
