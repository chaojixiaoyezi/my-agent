"""LLM: 本模块负责从 YAML 读取配置并逐字段校验、类型转换和范围限制，坏值写 warning 后回退安全默认值。

新手说明:
这里包含 load_log_analysis_config（从文件加载配置）、normalize_log_analysis_config（归一化原始配置）。
所有高风险能力在归一化后保持默认关闭，配置写错不会悄悄打开危险功能。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...settings.config import load_simple_yaml
from .config_coercers import (  # noqa: F401 — re-export for backward compatibility
    _INT_PATTERN,
    _LEVELS,
    _MISSING,
    _coerce_bool,
    _coerce_choice,
    _coerce_int,
    _coerce_path_string,
    _lookup,
    _warn,
)
from .config_model import (
    LogAnalysisConfig,
    LogAnalysisConfigWarning,
    default_log_analysis_config_path,
)


def load_log_analysis_config(
    config_path: str | Path | None = None,
    *,
    missing_ok: bool = True,
) -> LogAnalysisConfig:
    """LLM: 从 YAML 读取 LOG 配置并归一化成安全的 LogAnalysisConfig.

    新手说明:
    普通用户可能完全没启用日志分析，所以配置文件不存在时默认返回安全关闭状态。
    如果文件存在，函数会读取 YAML，再调用 normalize_log_analysis_config 校验每个字段。

    参数说明:
    config_path: 可选配置文件路径；不传时使用 default_log_analysis_config_path()。
    missing_ok: True 表示配置文件不存在时返回默认配置；False 表示不存在就抛 FileNotFoundError。

    返回说明:
    返回 LogAnalysisConfig。config.config_warnings 会包含坏值回退记录。

    异常说明:
    missing_ok=False 且文件不存在时抛 FileNotFoundError。YAML 解析错误会由 load_simple_yaml 抛出。
    """
    path = Path(config_path) if config_path is not None else default_log_analysis_config_path()
    if not path.exists():
        if not missing_ok:
            raise FileNotFoundError(f"log analysis config file does not exist: {path}")
        return LogAnalysisConfig()

    raw = load_simple_yaml(path)
    config, warnings = normalize_log_analysis_config(raw)
    config.config_warnings = [warning.to_dict() for warning in warnings]
    return config


def _coerce_bool_and_choice_fields(
    source: Mapping[str, Any] | object,
    defaults: LogAnalysisConfig,
    warnings: list[LogAnalysisConfigWarning],
) -> dict[str, Any]:
    """Coerce bool and choice (enum) LogAnalysisConfig fields."""
    return {
        "enabled": _coerce_bool("enabled", _lookup(source, "enabled"), default=defaults.enabled, warnings=warnings),
        "capability_level": _coerce_choice(
            "capability_level", _lookup(source, "capability_level"),
            default=defaults.capability_level, choices=_LEVELS, warnings=warnings, uppercase=True,
        ),
        "worker_enabled": _coerce_bool("worker_enabled", _lookup(source, "worker_enabled"), default=defaults.worker_enabled, warnings=warnings),
        "security_prompt_enabled": _coerce_bool("security_prompt_enabled", _lookup(source, "security_prompt_enabled"), default=defaults.security_prompt_enabled, warnings=warnings),
        "auto_dispatch_enabled": _coerce_bool("auto_dispatch_enabled", _lookup(source, "auto_dispatch_enabled"), default=defaults.auto_dispatch_enabled, warnings=warnings),
        "ml_enabled": _coerce_bool("ml_enabled", _lookup(source, "ml_enabled"), default=defaults.ml_enabled, warnings=warnings),
        "cluster_enabled": _coerce_bool("cluster_enabled", _lookup(source, "cluster_enabled"), default=defaults.cluster_enabled, warnings=warnings),
        "response_execution_enabled": _coerce_bool("response_execution_enabled", _lookup(source, "response_execution_enabled"), default=defaults.response_execution_enabled, warnings=warnings),
        "response_mode": _coerce_choice(
            "response_mode", _lookup(source, "response_mode"),
            default=defaults.response_mode, choices={"recommend", "dry_run", "execute"}, warnings=warnings,
        ),
        "local_store_backend": _coerce_choice(
            "local_store_backend", _lookup(source, "local_store_backend"),
            default=defaults.local_store_backend, choices={"jsonl", "sqlite", "duckdb", "parquet"}, warnings=warnings,
        ),
    }


def _coerce_path_and_int_fields(
    source: Mapping[str, Any] | object,
    defaults: LogAnalysisConfig,
    warnings: list[LogAnalysisConfigWarning],
) -> dict[str, Any]:
    """Coerce path and most integer LogAnalysisConfig fields."""
    return {
        "data_dir": _coerce_path_string("data_dir", _lookup(source, "data_dir"), default=defaults.data_dir, warnings=warnings),
        "config_version": _coerce_int("config_version", _lookup(source, "config_version"), default=defaults.config_version, min_value=1, max_value=100, warnings=warnings),
        "query_default_limit": _coerce_int("query_default_limit", _lookup(source, "query_default_limit"), default=defaults.query_default_limit, min_value=1, max_value=10000, warnings=warnings),
        "query_max_limit": _coerce_int("query_max_limit", _lookup(source, "query_max_limit"), default=defaults.query_max_limit, min_value=1, max_value=100000, warnings=warnings),
        "source_retention_days": _coerce_int("source_retention_days", _lookup(source, "source_retention_days"), default=defaults.source_retention_days, min_value=1, max_value=3650, warnings=warnings),
        "payload_preview_max_chars": _coerce_int("payload_preview_max_chars", _lookup(source, "payload_preview_max_chars"), default=defaults.payload_preview_max_chars, min_value=0, max_value=100000, warnings=warnings),
        "max_parallel_analyst_agents": _coerce_int("max_parallel_analyst_agents", _lookup(source, "max_parallel_analyst_agents"), default=defaults.max_parallel_analyst_agents, min_value=0, max_value=100, warnings=warnings),
        "dispatch_budget_per_hour": _coerce_int("dispatch_budget_per_hour", _lookup(source, "dispatch_budget_per_hour"), default=defaults.dispatch_budget_per_hour, min_value=0, max_value=10000, warnings=warnings),
        "case_merge_window_minutes": _coerce_int("case_merge_window_minutes", _lookup(source, "case_merge_window_minutes"), default=defaults.case_merge_window_minutes, min_value=1, max_value=10080, warnings=warnings),
        "detector_window_minutes": _coerce_int("detector_window_minutes", _lookup(source, "detector_window_minutes"), default=defaults.detector_window_minutes, min_value=1, max_value=1440, warnings=warnings),
    }


def normalize_log_analysis_config(
    values: Mapping[str, Any] | object | None = None,
) -> tuple[LogAnalysisConfig, list[LogAnalysisConfigWarning]]:
    """LLM: 把原始配置对象逐字段校验、类型转换、限制范围，并收集 warning.

    新手说明:
    YAML 读出来的值可能是字符串、数字、布尔值，也可能写错。这个函数统一检查：
    布尔值要像布尔值，整数要在范围内，枚举值要在允许集合里，路径不能是空字符串。

    参数说明:
    values: 原始配置。可以是 Mapping，也可以是带同名属性的对象；None 表示空配置。

    返回说明:
    返回 (config, warnings)。config 是最终生效配置，warnings 是 LogAnalysisConfigWarning 列表。

    重要边界:
    如果 query_default_limit 大于 query_max_limit，会回退 query_default_limit，避免默认查询超过最大上限。
    """
    source = values if values is not None else {}
    warnings: list[LogAnalysisConfigWarning] = []
    defaults = LogAnalysisConfig()

    fields: dict[str, Any] = {}
    fields.update(_coerce_bool_and_choice_fields(source, defaults, warnings))
    fields.update(_coerce_path_and_int_fields(source, defaults, warnings))
    config = LogAnalysisConfig(**fields)

    if config.query_default_limit > config.query_max_limit:
        _warn(
            warnings,
            "query_default_limit",
            config.query_default_limit,
            defaults.query_default_limit,
            "expected value <= query_max_limit",
        )
        config.query_default_limit = defaults.query_default_limit

    config.config_warnings = [warning.to_dict() for warning in warnings]
    return config, warnings