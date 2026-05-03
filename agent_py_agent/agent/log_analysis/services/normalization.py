"""Field-group normalization for log analysis config.

Each function normalizes a logical group of related fields and accumulates
warnings when values fall back to safe defaults.
"""

from __future__ import annotations

from typing import Any

from ..config import LogAnalysisConfig, LogAnalysisConfigWarning
from . import coercion

_LEVELS = {"L0", "L1", "L2", "L3", "L4", "L5"}


def normalize_core_bool_fields(
    source: dict[str, Any],
    defaults: LogAnalysisConfig,
    warnings: list[LogAnalysisConfigWarning],
) -> dict[str, bool]:
    """Normalize the core boolean capability flags."""
    return {
        "enabled": coercion.coerce_bool(
            "enabled", coercion.lookup(source, "enabled"),
            default=defaults.enabled, warnings=warnings
        ),
        "worker_enabled": coercion.coerce_bool(
            "worker_enabled", coercion.lookup(source, "worker_enabled"),
            default=defaults.worker_enabled, warnings=warnings
        ),
        "security_prompt_enabled": coercion.coerce_bool(
            "security_prompt_enabled", coercion.lookup(source, "security_prompt_enabled"),
            default=defaults.security_prompt_enabled, warnings=warnings
        ),
        "auto_dispatch_enabled": coercion.coerce_bool(
            "auto_dispatch_enabled", coercion.lookup(source, "auto_dispatch_enabled"),
            default=defaults.auto_dispatch_enabled, warnings=warnings
        ),
        "ml_enabled": coercion.coerce_bool(
            "ml_enabled", coercion.lookup(source, "ml_enabled"),
            default=defaults.ml_enabled, warnings=warnings
        ),
        "cluster_enabled": coercion.coerce_bool(
            "cluster_enabled", coercion.lookup(source, "cluster_enabled"),
            default=defaults.cluster_enabled, warnings=warnings
        ),
        "response_execution_enabled": coercion.coerce_bool(
            "response_execution_enabled", coercion.lookup(source, "response_execution_enabled"),
            default=defaults.response_execution_enabled, warnings=warnings
        ),
    }


def normalize_choice_fields(
    source: dict[str, Any],
    defaults: LogAnalysisConfig,
    warnings: list[LogAnalysisConfigWarning],
) -> dict[str, str]:
    """Normalize choice/select fields (capability_level, response_mode, local_store_backend)."""
    return {
        "capability_level": coercion.coerce_choice(
            "capability_level", coercion.lookup(source, "capability_level"),
            default=defaults.capability_level, choices=_LEVELS,
            warnings=warnings, uppercase=True,
        ),
        "response_mode": coercion.coerce_choice(
            "response_mode", coercion.lookup(source, "response_mode"),
            default=defaults.response_mode,
            choices={"recommend", "dry_run", "execute"},
            warnings=warnings,
        ),
        "local_store_backend": coercion.coerce_choice(
            "local_store_backend", coercion.lookup(source, "local_store_backend"),
            default=defaults.local_store_backend,
            choices={"jsonl", "sqlite", "duckdb", "parquet"},
            warnings=warnings,
        ),
    }


def normalize_path_and_version_fields(
    source: dict[str, Any],
    defaults: LogAnalysisConfig,
    warnings: list[LogAnalysisConfigWarning],
) -> dict[str, Any]:
    """Normalize data_dir and config_version fields."""
    return {
        "data_dir": coercion.coerce_path_string(
            "data_dir", coercion.lookup(source, "data_dir"),
            default=defaults.data_dir, warnings=warnings,
        ),
        "config_version": coercion.coerce_int(
            "config_version", coercion.lookup(source, "config_version"),
            default=defaults.config_version,
            min_value=1, max_value=100,
            warnings=warnings,
        ),
    }


def normalize_query_limit_fields(
    source: dict[str, Any],
    defaults: LogAnalysisConfig,
    warnings: list[LogAnalysisConfigWarning],
) -> dict[str, int]:
    """Normalize query_default_limit and query_max_limit fields."""
    return {
        "query_default_limit": coercion.coerce_int(
            "query_default_limit", coercion.lookup(source, "query_default_limit"),
            default=defaults.query_default_limit,
            min_value=1, max_value=10000,
            warnings=warnings,
        ),
        "query_max_limit": coercion.coerce_int(
            "query_max_limit", coercion.lookup(source, "query_max_limit"),
            default=defaults.query_max_limit,
            min_value=1, max_value=100000,
            warnings=warnings,
        ),
    }


def normalize_retention_and_preview_fields(
    source: dict[str, Any],
    defaults: LogAnalysisConfig,
    warnings: list[LogAnalysisConfigWarning],
) -> dict[str, int]:
    """Normalize source_retention_days and payload_preview_max_chars fields."""
    return {
        "source_retention_days": coercion.coerce_int(
            "source_retention_days", coercion.lookup(source, "source_retention_days"),
            default=defaults.source_retention_days,
            min_value=1, max_value=3650,
            warnings=warnings,
        ),
        "payload_preview_max_chars": coercion.coerce_int(
            "payload_preview_max_chars", coercion.lookup(source, "payload_preview_max_chars"),
            default=defaults.payload_preview_max_chars,
            min_value=0, max_value=100000,
            warnings=warnings,
        ),
    }


def normalize_dispatch_and_window_fields(
    source: dict[str, Any],
    defaults: LogAnalysisConfig,
    warnings: list[LogAnalysisConfigWarning],
) -> dict[str, int]:
    """Normalize dispatch and time window fields."""
    return {
        "max_parallel_analyst_agents": coercion.coerce_int(
            "max_parallel_analyst_agents", coercion.lookup(source, "max_parallel_analyst_agents"),
            default=defaults.max_parallel_analyst_agents,
            min_value=0, max_value=100,
            warnings=warnings,
        ),
        "dispatch_budget_per_hour": coercion.coerce_int(
            "dispatch_budget_per_hour", coercion.lookup(source, "dispatch_budget_per_hour"),
            default=defaults.dispatch_budget_per_hour,
            min_value=0, max_value=10000,
            warnings=warnings,
        ),
        "case_merge_window_minutes": coercion.coerce_int(
            "case_merge_window_minutes", coercion.lookup(source, "case_merge_window_minutes"),
            default=defaults.case_merge_window_minutes,
            min_value=1, max_value=10080,
            warnings=warnings,
        ),
        "detector_window_minutes": coercion.coerce_int(
            "detector_window_minutes", coercion.lookup(source, "detector_window_minutes"),
            default=defaults.detector_window_minutes,
            min_value=1, max_value=1440,
            warnings=warnings,
        ),
    }


def normalize_all(
    values: dict[str, Any] | object | None,
) -> tuple[LogAnalysisConfig, list[LogAnalysisConfigWarning]]:
    """Normalize all config fields from a raw values mapping.

    Returns (config, warnings) where warnings contain entries for any field
    that fell back to its default due to invalid input.
    If query_default_limit exceeds query_max_limit, query_default_limit is
    also corrected to the default.
    """
    source = values if values is not None else {}
    warnings: list[LogAnalysisConfigWarning] = []
    defaults = LogAnalysisConfig()

    core_fields = normalize_core_bool_fields(source, defaults, warnings)
    choice_fields = normalize_choice_fields(source, defaults, warnings)
    path_version_fields = normalize_path_and_version_fields(source, defaults, warnings)
    query_limit_fields = normalize_query_limit_fields(source, defaults, warnings)
    retention_preview_fields = normalize_retention_and_preview_fields(source, defaults, warnings)
    dispatch_window_fields = normalize_dispatch_and_window_fields(source, defaults, warnings)

    normalized = {
        **core_fields,
        **choice_fields,
        **path_version_fields,
        **query_limit_fields,
        **retention_preview_fields,
        **dispatch_window_fields,
    }

    config = LogAnalysisConfig(**normalized)

    if config.query_default_limit > config.query_max_limit:
        coercion.append_warning(
            warnings,
            "query_default_limit",
            config.query_default_limit,
            defaults.query_default_limit,
            "expected value <= query_max_limit",
        )
        config.query_default_limit = defaults.query_default_limit

    return config, warnings