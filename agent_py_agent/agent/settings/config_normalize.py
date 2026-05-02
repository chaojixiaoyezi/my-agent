"""LLM: config validation and coercion helpers – normalize raw YAML data into safe AgentConfig values.

给人看的解释：
这个文件负责"校验和修正"用户手写的 YAML 配置。
用户容易写错类型或超范围，这里的函数逐项检查，不合法的回退到安全默认值并给出警告。
主配置数据类 AgentConfig 和加载函数仍在 config.py。
"""

from __future__ import annotations

import re
from typing import Any

_INT_PATTERN = re.compile(r"-?[0-9]+")
_FLOAT_PATTERN = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def _coerce_bool_config(key: str, value: object, fallback: bool) -> tuple[bool, str | None]:
    """LLM: coerce a raw config value to bool with a safe fallback.

    新手说明:
    把用户写的配置值转成布尔值。
    支持 Python 的 True/False、整数 0/1、字符串 "true"/"yes"/"on"/"1" 等。
    转不了就回退到 fallback 并返回一条警告。
    """
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
    """LLM: coerce a raw config value to int with optional range checks.

    新手说明:
    把用户写的配置值转成整数。
    支持字符串 "30"、整数、以及值等于整数的浮点数（如 30.0）。
    可以指定最小值和最大值，超出范围就回退到 fallback 并返回警告。
    """
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
    """LLM: coerce a raw config value to float with optional range checks.

    新手说明:
    把用户写的配置值转成浮点数。
    支持整数自动转浮点、字符串 "0.7" 等。
    可以指定最小值和最大值，超出范围就回退到 fallback 并返回警告。
    """
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
    """LLM: coerce a raw config value to one of the allowed string choices.

    新手说明:
    把用户写的配置值和一个允许值列表对比。
    匹配就采用，不匹配就回退到 fallback 并返回警告。
    """
    if value is None:
        return fallback, None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in choices:
            return normalized, None
    return fallback, f"{key}: expected one of {list(choices)}, got {value!r}; using {fallback}"


def normalize_agent_config(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    """LLM: validate and coerce all non-memory AgentConfig fields with safe fallbacks.

    新手说明:
    用户手写 YAML 容易出错，这里逐项检查并回退到安全默认值。
    返回 (normalized_data, warnings)。
    """
    warnings: list[str] = []
    out: dict[str, object] = dict(data)
    from .config import AgentConfig
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

    return out, warnings


def normalize_subagent_workflow_config(config: AgentConfig) -> list[dict[str, Any]]:
    """LLM: validate and coerce subagent workflow config fields on an AgentConfig instance.

    新手说明:
    检查 subagent_workflow_mode、subagent_builtin_workflows、subagent_user_workflow_dirs
    和 subagent_workflow_review_rounds 是否合法。
    不合法的值回退到默认值，并在 config.subagent_workflow_config_warnings 里记录原因。
    """
    from .config import AgentConfig
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
    """LLM: append a structured warning dict for a subagent workflow config field.

    新手说明:
    当某个 subagent workflow 配置项值不合法时，把字段名、原始值、回退值和原因
    打包成字典追加到 warnings 列表里，方便上层统一收集和展示。
    """
    warnings.append(
        {
            "field_name": field_name,
            "raw_value": raw_value,
            "fallback_value": fallback_value,
            "reason": reason,
        }
    )
