from __future__ import annotations

"""智能体配置加载工具。

这个模块干的事情不复杂，但很关键：
- 定义程序运行时到底有哪些配置项
- 从磁盘读取一个简化版 YAML 配置
- 把未知字段过滤掉，避免用户多写了配置就直接把程序搞崩

这里坚持只用标准库，目的是让项目在 Windows / Linux / macOS 上都能轻装运行。
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any

from .memory import normalize_agent_memory_config

_INT_PATTERN = re.compile(r"-?[0-9]+")
_FLOAT_PATTERN = re.compile(r"-?[0-9]+(\.[0-9]+)?")


@dataclass
class AgentConfig:
    """运行时配置总表。

    你可以把它理解成“智能体启动前的总开关面板”。
    大到模型后端，小到工具返回长度限制，都从这里统一进来。
    """

    agent_name: str = "SimplePythonAgent"
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
    """解析一个简单标量值。

    这里支持的类型很克制：
    - `true/false`
    - 整数
    - 其他内容按普通字符串处理

    这样做的好处是规则简单，坏处是 YAML 能力有限。
    对这个小项目来说，够用比花哨更重要。
    """

    value = value.strip().strip('"').strip("'")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _coerce_bool_config(key: str, value: object, fallback: bool) -> tuple[bool, str | None]:
    if value is None:
        return fallback, None
    if isinstance(value, bool):
        return value, None
    if isinstance(value, int) and value in {0, 1}:
        return bool(value), None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True, None
        if normalized in {"false", "no", "off", "0"}:
            return False, None
    return fallback, f"{key}: expected a clear boolean, got {value!r}; using {fallback}"


def _coerce_int_config(
    key: str, value: object, fallback: int, *, min_val: int | None = None, max_val: int | None = None
) -> tuple[int, str | None]:
    if value is None:
        return fallback, None
    if isinstance(value, bool):
        return fallback, f"{key}: expected an integer, got boolean; using {fallback}"
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _INT_PATTERN.fullmatch(value.strip()):
        number = int(value.strip())
    elif isinstance(value, float) and value == int(value):
        number = int(value)
    else:
        return fallback, f"{key}: expected an integer, got {value!r}; using {fallback}"
    if min_val is not None and number < min_val:
        return fallback, f"{key}: expected >= {min_val}, got {number}; using {fallback}"
    if max_val is not None and number > max_val:
        return fallback, f"{key}: expected <= {max_val}, got {number}; using {fallback}"
    return number, None


def _coerce_float_config(
    key: str, value: object, fallback: float, *, min_val: float | None = None, max_val: float | None = None
) -> tuple[float, str | None]:
    if value is None:
        return fallback, None
    if isinstance(value, bool):
        return fallback, f"{key}: expected a float, got boolean; using {fallback}"
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if _FLOAT_PATTERN.fullmatch(stripped):
            number = float(stripped)
        else:
            return fallback, f"{key}: expected a float, got {value!r}; using {fallback}"
    else:
        return fallback, f"{key}: expected a float, got {value!r}; using {fallback}"
    if min_val is not None and number < min_val:
        return fallback, f"{key}: expected >= {min_val}, got {number}; using {fallback}"
    if max_val is not None and number > max_val:
        return fallback, f"{key}: expected <= {max_val}, got {number}; using {fallback}"
    return number, None


def _coerce_choice_config(
    key: str, value: object, fallback: str, choices: tuple[str, ...]
) -> tuple[str, str | None]:
    if value is None:
        return fallback, None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in choices:
            return normalized, None
    return fallback, f"{key}: expected one of {list(choices)}, got {value!r}; using {fallback}"


def normalize_agent_config(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    """LLM: validate and coerce all non-memory AgentConfig fields with safe fallbacks.

    Human version:
    用户手写 YAML 容易出错，这里逐项检查并回退到安全默认值。
    返回 (normalized_data, warnings)。
    """
    warnings: list[str] = []
    out: dict[str, object] = dict(data)
    defaults = AgentConfig()

    def _apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # model_backend
    v, w = _coerce_choice_config(
        "model_backend", out.get("model_backend"), defaults.model_backend,
        ("echo", "anthropic_compatible", "openai_compatible"),
    )
    _apply("model_backend", v, w)

    # request_timeout
    v, w = _coerce_int_config(
        "request_timeout", out.get("request_timeout"), defaults.request_timeout,
        min_val=1, max_val=600,
    )
    _apply("request_timeout", v, w)

    # max_tokens
    v, w = _coerce_int_config(
        "max_tokens", out.get("max_tokens"), defaults.max_tokens, min_val=1,
    )
    _apply("max_tokens", v, w)

    # temperature (stored as str in AgentConfig, but validate as float)
    raw_temp = out.get("temperature", defaults.temperature)
    if isinstance(raw_temp, str):
        try:
            temp_val = float(raw_temp.strip())
            if 0.0 <= temp_val <= 2.0:
                out["temperature"] = raw_temp.strip()
            else:
                warnings.append(f"temperature: expected 0.0-2.0, got {temp_val}; using {defaults.temperature}")
                out["temperature"] = defaults.temperature
        except ValueError:
            warnings.append(f"temperature: expected a float string, got {raw_temp!r}; using {defaults.temperature}")
            out["temperature"] = defaults.temperature
    elif isinstance(raw_temp, (int, float)):
        temp_val = float(raw_temp)
        if 0.0 <= temp_val <= 2.0:
            out["temperature"] = str(temp_val)
        else:
            warnings.append(f"temperature: expected 0.0-2.0, got {temp_val}; using {defaults.temperature}")
            out["temperature"] = defaults.temperature

    # gateway_heartbeat_interval
    v, w = _coerce_int_config(
        "gateway_heartbeat_interval", out.get("gateway_heartbeat_interval"),
        defaults.gateway_heartbeat_interval, min_val=5,
    )
    _apply("gateway_heartbeat_interval", v, w)

    # gateway_stale_seconds
    v, w = _coerce_int_config(
        "gateway_stale_seconds", out.get("gateway_stale_seconds"),
        defaults.gateway_stale_seconds, min_val=30,
    )
    _apply("gateway_stale_seconds", v, w)

    # gateway_stop_timeout
    v, w = _coerce_int_config(
        "gateway_stop_timeout", out.get("gateway_stop_timeout"),
        defaults.gateway_stop_timeout, min_val=1,
    )
    _apply("gateway_stop_timeout", v, w)

    # gateway_request_timeout
    v, w = _coerce_int_config(
        "gateway_request_timeout", out.get("gateway_request_timeout"),
        defaults.gateway_request_timeout, min_val=1,
    )
    _apply("gateway_request_timeout", v, w)

    # gateway_request_poll_interval
    v, w = _coerce_int_config(
        "gateway_request_poll_interval", out.get("gateway_request_poll_interval"),
        defaults.gateway_request_poll_interval, min_val=1,
    )
    _apply("gateway_request_poll_interval", v, w)

    # gateway_request_workers
    v, w = _coerce_int_config(
        "gateway_request_workers", out.get("gateway_request_workers"),
        defaults.gateway_request_workers, min_val=1,
    )
    _apply("gateway_request_workers", v, w)

    # gateway_processing_timeout_seconds
    v, w = _coerce_int_config(
        "gateway_processing_timeout_seconds", out.get("gateway_processing_timeout_seconds"),
        defaults.gateway_processing_timeout_seconds, min_val=30,
    )
    _apply("gateway_processing_timeout_seconds", v, w)

    # gateway_request_max_attempts
    v, w = _coerce_int_config(
        "gateway_request_max_attempts", out.get("gateway_request_max_attempts"),
        defaults.gateway_request_max_attempts, min_val=1,
    )
    _apply("gateway_request_max_attempts", v, w)

    # gateway_port
    v, w = _coerce_int_config(
        "gateway_port", out.get("gateway_port"),
        defaults.gateway_port, min_val=0, max_val=65535,
    )
    _apply("gateway_port", v, w)

    # 飞书配置（字符串，直接透传）
    for key in ("feishu_app_id", "feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key"):
        val = out.get(key, defaults.feishu_app_id if key == "feishu_app_id" else "")
        if isinstance(val, str):
            out[key] = val
        else:
            out[key] = ""

    # feishu_callback_port
    v, w = _coerce_int_config(
        "feishu_callback_port", out.get("feishu_callback_port"),
        defaults.feishu_callback_port, min_val=1024, max_val=65535,
    )
    _apply("feishu_callback_port", v, w)

    # QQ 配置（字符串，直接透传，支持环境变量覆盖）
    import os as _os
    for key in ("qq_app_id", "qq_app_secret"):
        env_key = key.upper()
        env_val = _os.environ.get(env_key, "")
        if env_val:
            out[key] = env_val
        else:
            val = out.get(key, defaults.qq_app_id if key == "qq_app_id" else "")
            if isinstance(val, str):
                out[key] = val
            else:
                out[key] = ""

    # user_id - validate non-empty string
    raw_user_id = out.get("user_id", defaults.user_id)
    if isinstance(raw_user_id, str) and raw_user_id.strip():
        out["user_id"] = raw_user_id.strip()
    else:
        out["user_id"] = defaults.user_id
        warnings.append(f"user_id: expected a non-empty string, got {raw_user_id!r}; using default")

    # user_data_root - validate non-empty string
    raw_user_data_root = out.get("user_data_root", defaults.user_data_root)
    if isinstance(raw_user_data_root, str) and raw_user_data_root.strip():
        out["user_data_root"] = raw_user_data_root.strip()
    else:
        out["user_data_root"] = defaults.user_data_root
        warnings.append(f"user_data_root: expected a non-empty string, got {raw_user_data_root!r}; using default")

    # daemon_interval
    v, w = _coerce_int_config(
        "daemon_interval", out.get("daemon_interval"),
        defaults.daemon_interval, min_val=1,
    )
    _apply("daemon_interval", v, w)

    # daemon_limit
    v, w = _coerce_int_config(
        "daemon_limit", out.get("daemon_limit"),
        defaults.daemon_limit, min_val=0,
    )
    _apply("daemon_limit", v, w)

    # daemon_max_cycles
    v, w = _coerce_int_config(
        "daemon_max_cycles", out.get("daemon_max_cycles"),
        defaults.daemon_max_cycles, min_val=0,
    )
    _apply("daemon_max_cycles", v, w)

    # daemon_max_cards
    v, w = _coerce_int_config(
        "daemon_max_cards", out.get("daemon_max_cards"),
        defaults.daemon_max_cards, min_val=0,
    )
    _apply("daemon_max_cards", v, w)

    # daemon_apply
    v, w = _coerce_bool_config(
        "daemon_apply", out.get("daemon_apply"), defaults.daemon_apply,
    )
    _apply("daemon_apply", v, w)

    # daemon_execute_runners
    v, w = _coerce_bool_config(
        "daemon_execute_runners", out.get("daemon_execute_runners"), defaults.daemon_execute_runners,
    )
    _apply("daemon_execute_runners", v, w)

    # lease_heartbeat_interval_seconds
    v, w = _coerce_int_config(
        "lease_heartbeat_interval_seconds", out.get("lease_heartbeat_interval_seconds"),
        defaults.lease_heartbeat_interval_seconds, min_val=10,
    )
    _apply("lease_heartbeat_interval_seconds", v, w)

    # lease_stale_without_heartbeat_seconds
    v, w = _coerce_int_config(
        "lease_stale_without_heartbeat_seconds", out.get("lease_stale_without_heartbeat_seconds"),
        defaults.lease_stale_without_heartbeat_seconds, min_val=30,
    )
    _apply("lease_stale_without_heartbeat_seconds", v, w)

    # max_tool_rounds
    v, w = _coerce_int_config(
        "max_tool_rounds", out.get("max_tool_rounds"),
        defaults.max_tool_rounds, min_val=1,
    )
    _apply("max_tool_rounds", v, w)

    # tool_read_max_chars
    v, w = _coerce_int_config(
        "tool_read_max_chars", out.get("tool_read_max_chars"),
        defaults.tool_read_max_chars, min_val=100,
    )
    _apply("tool_read_max_chars", v, w)

    # tool_http_timeout
    v, w = _coerce_int_config(
        "tool_http_timeout", out.get("tool_http_timeout"),
        defaults.tool_http_timeout, min_val=1,
    )
    _apply("tool_http_timeout", v, w)

    # tool_shell_timeout
    v, w = _coerce_int_config(
        "tool_shell_timeout", out.get("tool_shell_timeout"),
        defaults.tool_shell_timeout, min_val=1,
    )
    _apply("tool_shell_timeout", v, w)

    # stream_enabled
    v, w = _coerce_bool_config(
        "stream_enabled", out.get("stream_enabled"), defaults.stream_enabled,
    )
    _apply("stream_enabled", v, w)

    # memory_top_k
    v, w = _coerce_int_config(
        "memory_top_k", out.get("memory_top_k"),
        defaults.memory_top_k, min_val=0,
    )
    _apply("memory_top_k", v, w)

    # max_subagents
    v, w = _coerce_int_config(
        "max_subagents", out.get("max_subagents"),
        defaults.max_subagents, min_val=0,
    )
    _apply("max_subagents", v, w)

    # subagent_automation_level
    v, w = _coerce_int_config(
        "subagent_automation_level", out.get("subagent_automation_level"),
        defaults.subagent_automation_level, min_val=1, max_val=3,
    )
    _apply("subagent_automation_level", v, w)

    # dynamic_timeout_safety_margin
    v, w = _coerce_float_config(
        "dynamic_timeout_safety_margin", out.get("dynamic_timeout_safety_margin"),
        defaults.dynamic_timeout_safety_margin, min_val=1.0, max_val=10.0,
    )
    _apply("dynamic_timeout_safety_margin", v, w)

    # dynamic_timeout_min
    v, w = _coerce_int_config(
        "dynamic_timeout_min", out.get("dynamic_timeout_min"),
        defaults.dynamic_timeout_min, min_val=10,
    )
    _apply("dynamic_timeout_min", v, w)

    # dynamic_timeout_max
    v, w = _coerce_int_config(
        "dynamic_timeout_max", out.get("dynamic_timeout_max"),
        defaults.dynamic_timeout_max, min_val=60,
    )
    _apply("dynamic_timeout_max", v, w)

    # max_auto_split_depth
    v, w = _coerce_int_config(
        "max_auto_split_depth", out.get("max_auto_split_depth"),
        defaults.max_auto_split_depth, min_val=0,
    )
    _apply("max_auto_split_depth", v, w)

    # max_auto_retry_attempts
    v, w = _coerce_int_config(
        "max_auto_retry_attempts", out.get("max_auto_retry_attempts"),
        defaults.max_auto_retry_attempts, min_val=1, max_val=10,
    )
    _apply("max_auto_retry_attempts", v, w)

    # auto_bench_model_on_first_use
    v, w = _coerce_bool_config(
        "auto_bench_model_on_first_use", out.get("auto_bench_model_on_first_use"),
        defaults.auto_bench_model_on_first_use,
    )
    _apply("auto_bench_model_on_first_use", v, w)

    # model_speed_profile_path - just validate non-empty string
    raw_path = out.get("model_speed_profile_path", defaults.model_speed_profile_path)
    if isinstance(raw_path, str) and raw_path.strip():
        out["model_speed_profile_path"] = raw_path.strip()
    else:
        out["model_speed_profile_path"] = defaults.model_speed_profile_path
        warnings.append(f"model_speed_profile_path: expected a non-empty string, got {raw_path!r}; using default")

    # auth_enabled
    v, w = _coerce_bool_config("auth_enabled", out.get("auth_enabled"), defaults.auth_enabled)
    _apply("auth_enabled", v, w)

    # admin_user_id
    raw_admin = out.get("admin_user_id", defaults.admin_user_id)
    if isinstance(raw_admin, str) and raw_admin.strip():
        out["admin_user_id"] = raw_admin.strip()
    else:
        out["admin_user_id"] = defaults.admin_user_id

    return out, warnings


def load_simple_yaml(path: Path) -> dict[str, Any]:
    """读取一个极简 YAML 子集。

    这不是完整 YAML 解析器，只支持当前项目要用到的结构：
    - `key: value`
    - `key:` 后面接列表项

    大白话说，就是“够项目自己吃，不追求兼容所有 YAML 花样”。
    """

    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
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
    """从磁盘加载配置，并过滤未知字段。

    这样用户即使先写了某些未来配置项，旧版本程序也不会立刻炸掉。
    """

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
    # 大白话解释：仓库里只留“去哪里拿 key”的说明，不再把真 key 写进代码仓库。
    env_name = str(config.api_key_env).strip()
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            config.api_key = env_value

    return config


def normalize_subagent_workflow_config(config: AgentConfig) -> list[dict[str, Any]]:
    """Normalize Subagent Workflow config values after YAML loading."""

    warnings: list[dict[str, Any]] = []
    defaults = AgentConfig()

    raw_mode = config.subagent_workflow_mode
    if isinstance(raw_mode, str) and raw_mode.strip().lower() in {"auto", "manual", "off"}:
        config.subagent_workflow_mode = raw_mode.strip().lower()
    else:
        config.subagent_workflow_mode = defaults.subagent_workflow_mode
        _add_subagent_workflow_warning(
            warnings,
            "subagent_workflow_mode",
            raw_mode,
            defaults.subagent_workflow_mode,
            "expected one of ['auto', 'manual', 'off']",
        )

    raw_builtin = config.subagent_builtin_workflows
    if isinstance(raw_builtin, bool):
        config.subagent_builtin_workflows = raw_builtin
    else:
        config.subagent_builtin_workflows = defaults.subagent_builtin_workflows
        _add_subagent_workflow_warning(
            warnings,
            "subagent_builtin_workflows",
            raw_builtin,
            defaults.subagent_builtin_workflows,
            "expected a boolean value",
        )

    raw_dirs = config.subagent_user_workflow_dirs
    if (
        isinstance(raw_dirs, list)
        and all(isinstance(item, str) and item.strip() for item in raw_dirs)
    ):
        config.subagent_user_workflow_dirs = [item.strip() for item in raw_dirs]
    else:
        config.subagent_user_workflow_dirs = list(defaults.subagent_user_workflow_dirs)
        _add_subagent_workflow_warning(
            warnings,
            "subagent_user_workflow_dirs",
            raw_dirs,
            list(defaults.subagent_user_workflow_dirs),
            "expected a list of non-empty strings",
        )

    raw_review_rounds = config.subagent_workflow_review_rounds
    if isinstance(raw_review_rounds, bool):
        review_rounds: int | None = None
    elif isinstance(raw_review_rounds, int):
        review_rounds = raw_review_rounds
    elif isinstance(raw_review_rounds, str) and raw_review_rounds.strip().isdigit():
        review_rounds = int(raw_review_rounds.strip())
    else:
        review_rounds = None

    if review_rounds is not None and 0 <= review_rounds <= 5:
        config.subagent_workflow_review_rounds = review_rounds
    else:
        config.subagent_workflow_review_rounds = defaults.subagent_workflow_review_rounds
        _add_subagent_workflow_warning(
            warnings,
            "subagent_workflow_review_rounds",
            raw_review_rounds,
            defaults.subagent_workflow_review_rounds,
            "expected an integer between 0 and 5",
        )

    config.subagent_workflow_config_warnings = warnings
    return warnings


def _add_subagent_workflow_warning(
    warnings: list[dict[str, Any]],
    field_name: str,
    raw_value: Any,
    fallback_value: Any,
    reason: str,
) -> None:
    warnings.append(
        {
            "field_name": field_name,
            "raw_value": raw_value,
            "fallback_value": fallback_value,
            "reason": reason,
        }
    )
