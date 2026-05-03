"""LLM: config validation and coercion helpers – normalize raw YAML data into safe values.

给人看的解释：
这个文件负责"校验和修正"用户手写的 YAML 配置。
用户容易写错类型或超范围，这里的函数逐项检查，不合法的回退到安全默认值并给出警告。
主配置数据类 AgentConfig 和加载函数仍在 config.py。
"""

from __future__ import annotations

import re

__all__ = [
    "_coerce_bool_config",
    "_coerce_choice_config",
    "_coerce_float_config",
    "_coerce_int_config",
    "normalize_agent_config",
    "normalize_subagent_workflow_config",
]

_INT_PATTERN = re.compile(r"-?[0-9]+")
_FLOAT_PATTERN = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def _coerce_bool_config(key: str, value: object, fallback: bool) -> tuple[bool, str | None]:
    """Coerce a raw config value to bool with a safe fallback."""
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
    """Coerce a raw config value to int with optional range checks."""
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
    """Coerce a raw config value to float with optional range checks."""
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
    """Coerce a raw config value to one of the allowed string choices."""
    if value is None:
        return fallback, None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in choices:
            return normalized, None
    return fallback, f"{key}: expected one of {list(choices)}, got {value!r}; using {fallback}"


# ---------------------------------------------------------------------------
# Per-domain normalize functions (broken out from the monolithic function)
# ---------------------------------------------------------------------------


def _normalize_model_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize model-related config fields."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # model_backend
    v, w = _coerce_choice_config(
        "model_backend", out.get("model_backend"), defaults.model_backend,
        ("echo", "anthropic_compatible", "openai_compatible"),
    )
    apply("model_backend", v, w)

    # request_timeout
    v, w = _coerce_int_config(
        "request_timeout", out.get("request_timeout"), defaults.request_timeout,
        min_val=1, max_val=600,
    )
    apply("request_timeout", v, w)

    # max_tokens
    v, w = _coerce_int_config(
        "max_tokens", out.get("max_tokens"), defaults.max_tokens, min_val=1,
    )
    apply("max_tokens", v, w)

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

    return out, warnings


def _normalize_gateway_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize gateway-related config fields."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # gateway_heartbeat_interval
    v, w = _coerce_int_config(
        "gateway_heartbeat_interval", out.get("gateway_heartbeat_interval"),
        defaults.gateway_heartbeat_interval, min_val=5,
    )
    apply("gateway_heartbeat_interval", v, w)

    # gateway_stale_seconds
    v, w = _coerce_int_config(
        "gateway_stale_seconds", out.get("gateway_stale_seconds"),
        defaults.gateway_stale_seconds, min_val=30,
    )
    apply("gateway_stale_seconds", v, w)

    # gateway_stop_timeout
    v, w = _coerce_int_config(
        "gateway_stop_timeout", out.get("gateway_stop_timeout"),
        defaults.gateway_stop_timeout, min_val=1,
    )
    apply("gateway_stop_timeout", v, w)

    # gateway_request_timeout
    v, w = _coerce_int_config(
        "gateway_request_timeout", out.get("gateway_request_timeout"),
        defaults.gateway_request_timeout, min_val=1,
    )
    apply("gateway_request_timeout", v, w)

    # gateway_request_poll_interval
    v, w = _coerce_int_config(
        "gateway_request_poll_interval", out.get("gateway_request_poll_interval"),
        defaults.gateway_request_poll_interval, min_val=1,
    )
    apply("gateway_request_poll_interval", v, w)

    # gateway_request_workers
    v, w = _coerce_int_config(
        "gateway_request_workers", out.get("gateway_request_workers"),
        defaults.gateway_request_workers, min_val=1,
    )
    apply("gateway_request_workers", v, w)

    # gateway_processing_timeout_seconds
    v, w = _coerce_int_config(
        "gateway_processing_timeout_seconds", out.get("gateway_processing_timeout_seconds"),
        defaults.gateway_processing_timeout_seconds, min_val=30,
    )
    apply("gateway_processing_timeout_seconds", v, w)

    # gateway_request_max_attempts
    v, w = _coerce_int_config(
        "gateway_request_max_attempts", out.get("gateway_request_max_attempts"),
        defaults.gateway_request_max_attempts, min_val=1,
    )
    apply("gateway_request_max_attempts", v, w)

    # gateway_port
    v, w = _coerce_int_config(
        "gateway_port", out.get("gateway_port"),
        defaults.gateway_port, min_val=0, max_val=65535,
    )
    apply("gateway_port", v, w)

    return out, warnings


def _normalize_daemon_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize daemon-related config fields."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # daemon_interval
    v, w = _coerce_int_config(
        "daemon_interval", out.get("daemon_interval"),
        defaults.daemon_interval, min_val=1,
    )
    apply("daemon_interval", v, w)

    # daemon_limit
    v, w = _coerce_int_config(
        "daemon_limit", out.get("daemon_limit"),
        defaults.daemon_limit, min_val=0,
    )
    apply("daemon_limit", v, w)

    # daemon_max_cycles
    v, w = _coerce_int_config(
        "daemon_max_cycles", out.get("daemon_max_cycles"),
        defaults.daemon_max_cycles, min_val=0,
    )
    apply("daemon_max_cycles", v, w)

    # daemon_max_cards
    v, w = _coerce_int_config(
        "daemon_max_cards", out.get("daemon_max_cards"),
        defaults.daemon_max_cards, min_val=0,
    )
    apply("daemon_max_cards", v, w)

    # daemon_apply
    v, w = _coerce_bool_config(
        "daemon_apply", out.get("daemon_apply"), defaults.daemon_apply,
    )
    apply("daemon_apply", v, w)

    # daemon_execute_runners
    v, w = _coerce_bool_config(
        "daemon_execute_runners", out.get("daemon_execute_runners"), defaults.daemon_execute_runners,
    )
    apply("daemon_execute_runners", v, w)

    return out, warnings


def _normalize_tool_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize tool-related config fields."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # max_tool_rounds
    v, w = _coerce_int_config(
        "max_tool_rounds", out.get("max_tool_rounds"),
        defaults.max_tool_rounds, min_val=1,
    )
    apply("max_tool_rounds", v, w)

    # tool_read_max_chars
    v, w = _coerce_int_config(
        "tool_read_max_chars", out.get("tool_read_max_chars"),
        defaults.tool_read_max_chars, min_val=100,
    )
    apply("tool_read_max_chars", v, w)

    # tool_http_timeout
    v, w = _coerce_int_config(
        "tool_http_timeout", out.get("tool_http_timeout"),
        defaults.tool_http_timeout, min_val=1,
    )
    apply("tool_http_timeout", v, w)

    # tool_shell_timeout
    v, w = _coerce_int_config(
        "tool_shell_timeout", out.get("tool_shell_timeout"),
        defaults.tool_shell_timeout, min_val=1,
    )
    apply("tool_shell_timeout", v, w)

    # stream_enabled
    v, w = _coerce_bool_config(
        "stream_enabled", out.get("stream_enabled"), defaults.stream_enabled,
    )
    apply("stream_enabled", v, w)

    return out, warnings


def _normalize_subagent_basic_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize basic subagent config fields (max_subagents, memory_top_k)."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # memory_top_k
    v, w = _coerce_int_config(
        "memory_top_k", out.get("memory_top_k"),
        defaults.memory_top_k, min_val=0,
    )
    apply("memory_top_k", v, w)

    # max_subagents
    v, w = _coerce_int_config(
        "max_subagents", out.get("max_subagents"),
        defaults.max_subagents, min_val=0,
    )
    apply("max_subagents", v, w)

    return out, warnings


def _normalize_adapter_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize adapter-related config fields (feishu, qq, etc.)."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # Feishu string fields pass through as-is
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
    apply("feishu_callback_port", v, w)

    # QQ fields (support env var override)
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

    return out, warnings


def _normalize_user_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize user-related config fields."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

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

    # auth_enabled
    v, w = _coerce_bool_config("auth_enabled", out.get("auth_enabled"), defaults.auth_enabled)
    apply("auth_enabled", v, w)

    # admin_user_id
    raw_admin = out.get("admin_user_id", defaults.admin_user_id)
    if isinstance(raw_admin, str) and raw_admin.strip():
        out["admin_user_id"] = raw_admin.strip()
    else:
        out["admin_user_id"] = defaults.admin_user_id

    return out, warnings


def _normalize_timeout_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize timeout-related config fields."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # lease_heartbeat_interval_seconds
    v, w = _coerce_int_config(
        "lease_heartbeat_interval_seconds", out.get("lease_heartbeat_interval_seconds"),
        defaults.lease_heartbeat_interval_seconds, min_val=10,
    )
    apply("lease_heartbeat_interval_seconds", v, w)

    # lease_stale_without_heartbeat_seconds
    v, w = _coerce_int_config(
        "lease_stale_without_heartbeat_seconds", out.get("lease_stale_without_heartbeat_seconds"),
        defaults.lease_stale_without_heartbeat_seconds, min_val=30,
    )
    apply("lease_stale_without_heartbeat_seconds", v, w)

    return out, warnings


def _normalize_subagent_advanced_fields(
    data: dict[str, object],
    defaults: object,
) -> tuple[dict[str, object], list[str]]:
    """Normalize advanced subagent config fields."""
    warnings: list[str] = []
    out = dict(data)

    def apply(key: str, coerced: object, warn: str | None) -> None:
        out[key] = coerced
        if warn:
            warnings.append(warn)

    # subagent_automation_level
    v, w = _coerce_int_config(
        "subagent_automation_level", out.get("subagent_automation_level"),
        defaults.subagent_automation_level, min_val=1, max_val=3,
    )
    apply("subagent_automation_level", v, w)

    # dynamic_timeout_safety_margin
    v, w = _coerce_float_config(
        "dynamic_timeout_safety_margin", out.get("dynamic_timeout_safety_margin"),
        defaults.dynamic_timeout_safety_margin, min_val=1.0, max_val=10.0,
    )
    apply("dynamic_timeout_safety_margin", v, w)

    # dynamic_timeout_min
    v, w = _coerce_int_config(
        "dynamic_timeout_min", out.get("dynamic_timeout_min"),
        defaults.dynamic_timeout_min, min_val=10,
    )
    apply("dynamic_timeout_min", v, w)

    # dynamic_timeout_max
    v, w = _coerce_int_config(
        "dynamic_timeout_max", out.get("dynamic_timeout_max"),
        defaults.dynamic_timeout_max, min_val=60,
    )
    apply("dynamic_timeout_max", v, w)

    # max_auto_split_depth
    v, w = _coerce_int_config(
        "max_auto_split_depth", out.get("max_auto_split_depth"),
        defaults.max_auto_split_depth, min_val=0,
    )
    apply("max_auto_split_depth", v, w)

    # max_auto_retry_attempts
    v, w = _coerce_int_config(
        "max_auto_retry_attempts", out.get("max_auto_retry_attempts"),
        defaults.max_auto_retry_attempts, min_val=1, max_val=10,
    )
    apply("max_auto_retry_attempts", v, w)

    return out, warnings


def normalize_agent_config(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    """LLM: validate and coerce all non-memory AgentConfig fields with safe fallbacks.

    Human version:
    用户手写 YAML 容易出错，这里逐项检查并回退到安全默认值。
    返回 (normalized_data, warnings)。
    """
    from .config import AgentConfig
    warnings: list[str] = []
    out: dict[str, object] = dict(data)
    defaults = AgentConfig()

    # Apply per-domain normalization in sequence
    out, w1 = _normalize_model_fields(out, defaults)
    warnings.extend(w1)

    out, w2 = _normalize_gateway_fields(out, defaults)
    warnings.extend(w2)

    out, w3 = _normalize_daemon_fields(out, defaults)
    warnings.extend(w3)

    out, w4 = _normalize_tool_fields(out, defaults)
    warnings.extend(w4)

    out, w5 = _normalize_subagent_basic_fields(out, defaults)
    warnings.extend(w5)

    out, w6 = _normalize_adapter_fields(out, defaults)
    warnings.extend(w6)

    out, w7 = _normalize_user_fields(out, defaults)
    warnings.extend(w7)

    out, w8 = _normalize_timeout_fields(out, defaults)
    warnings.extend(w8)

    out, w9 = _normalize_subagent_advanced_fields(out, defaults)
    warnings.extend(w9)

    return out, warnings


def normalize_subagent_workflow_config(config: object) -> list[dict[str, object]]:
    """LLM: validate and coerce subagent workflow config fields on an AgentConfig instance."""
    from .config import AgentConfig
    warnings: list[dict[str, object]] = []
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
    warnings: list[dict[str, object]],
    field_name: str,
    raw_value: object,
    fallback_value: object,
    reason: str,
) -> None:
    """Append a structured warning dict for a subagent workflow config field."""
    warnings.append(
        {
            "field_name": field_name,
            "raw_value": raw_value,
            "fallback_value": fallback_value,
            "reason": reason,
        }
    )