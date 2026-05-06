from __future__ import annotations

"""智能体配置加载工具。

这个模块干的事情不复杂，但很关键：
- 定义程序运行时到底有哪些配置项
- 从磁盘读取一个简化版 YAML 配置
- 把未知字段过滤掉，避免用户多写了配置就直接把程序搞崩

这里坚持只用标准库，目的是让项目在 Windows / Linux / macOS 上都能轻装运行。
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    "_coerce_bool_config",
    "_coerce_choice_config",
    "_coerce_float_config",
    "_coerce_int_config",
    "load_config",
    "load_simple_yaml",
    "parse_scalar",
    "normalize_agent_config",
    "normalize_subagent_workflow_config",
]

_INT_PATTERN = re.compile(r"-?[0-9]+")
_FLOAT_PATTERN = re.compile(r"-?[0-9]+(\.[0-9]+)?")


@dataclass
class AgentConfig:

    agent_name: str = "myagent"
    system_prompt: str = "你是一个谨慎、可扩展、会记录记忆、会在必要时调用工具的 Python CLI 智能体。先理解任务，再给出结构化回答。"
    workspace_root: str = ""
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
    memory_config_warnings: list[dict[str, Any]] = field(default_factory=list)
    local_store_path: str = "data/local_store/local.db"
    local_store_files_dir: str = "data/local_store/files"
    local_store_events_path: str = "data/local_store/events.jsonl"
    local_store_fts_enabled: bool = True
    enable_self_learning: bool = False
    prompt_files: list[str] = field(default_factory=list)
    enable_subagents: bool = True
    max_subagents: int = 5
    subagent_board_limit: int = 5
    subagent_workspace: str = "data/subagents"
    subagent_workflow_mode: str = "auto"
    subagent_builtin_workflows: bool = True
    subagent_user_workflow_dirs: list[str] = field(default_factory=lambda: [".agent/workflows/user"])
    subagent_workflow_review_rounds: int = 1
    subagent_workflow_config_warnings: list[dict[str, Any]] = field(default_factory=list)
    task_max_subagents: int = 0
    task_max_grandchildren: int = 0
    subagent_automation_level: int = 2
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
    runner_timeout_seconds: str = "auto"
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
    audit_enabled: bool = True
    audit_log_path: str = "data/audit/audit.jsonl"
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
    max_tool_rounds: int = 5
    tool_read_max_chars: int = 6000
    tool_list_max_entries: int = 200
    tool_search_max_matches: int = 50
    tool_web_max_chars: int = 12000
    tool_http_timeout: int = 30
    tool_shell_timeout: int = 30
    stream_enabled: bool = True
    tool_catalog_limit: int = 20
    tool_retrieval_limit: int = 3
    tool_vector_search_enabled: bool = False
    # Dispatch 闭环保证配置
    dispatch_max_consecutive_rounds: int = 20
    dispatch_active_interval: int = 5
    dispatch_idle_interval: int = 30
    # Watchdog 配置
    watchdog_enabled: bool = False
    watchdog_interval: int = 60
    watchdog_max_restarts: int = 3
    watchdog_restart_delay: int = 10
    config_warnings: list[str] = field(default_factory=list)


def parse_scalar(value: str) -> Any:

    value = value.strip().strip('"').strip("'")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def load_simple_yaml(path: Path) -> dict[str, Any]:

    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, []).append(parse_scalar(line[4:]))
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value == "":
                data[key] = []
                current_key = key
            else:
                data[key] = parse_scalar(value)
                current_key = None
    return data


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

    return config
