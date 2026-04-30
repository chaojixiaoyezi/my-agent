from __future__ import annotations

"""Safe configuration loader for the optional log analysis module."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any

from ..settings.config import load_simple_yaml


_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")
_LEVELS = {"L0", "L1", "L2", "L3", "L4", "L5"}


@dataclass(frozen=True)
class LogAnalysisConfigWarning:
    field_name: str
    raw_value: Any
    fallback_value: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LogAnalysisConfig:
    """Effective log analysis settings after validation.

    The module is default-off.  Values that could start workers, dispatch
    subagents, load ML/cluster backends, or execute response actions all stay
    false unless explicitly configured and accepted by feature gates.
    """

    enabled: bool = False
    capability_level: str = "L0"
    data_dir: str = "data/log_analysis"
    config_version: int = 1

    worker_enabled: bool = False
    security_prompt_enabled: bool = False
    auto_dispatch_enabled: bool = False
    ml_enabled: bool = False
    cluster_enabled: bool = False
    response_execution_enabled: bool = False

    response_mode: str = "recommend"
    local_store_backend: str = "jsonl"

    query_default_limit: int = 100
    query_max_limit: int = 1000
    source_retention_days: int = 30
    payload_preview_max_chars: int = 2048

    max_parallel_analyst_agents: int = 0
    dispatch_budget_per_hour: int = 0
    case_merge_window_minutes: int = 60
    detector_window_minutes: int = 15

    config_warnings: list[dict[str, Any]] = field(default_factory=list)


def default_log_analysis_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "log_analysis_config.yaml"


def load_log_analysis_config(
    config_path: str | Path | None = None,
    *,
    missing_ok: bool = True,
) -> LogAnalysisConfig:
    """Load and normalize log analysis config from a flat YAML file.

    Missing config returns safe defaults by default so ordinary commands can
    remain unaware of this optional module.
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


def normalize_log_analysis_config(
    values: Mapping[str, Any] | object | None = None,
) -> tuple[LogAnalysisConfig, list[LogAnalysisConfigWarning]]:
    source = values if values is not None else {}
    warnings: list[LogAnalysisConfigWarning] = []
    defaults = LogAnalysisConfig()

    config = LogAnalysisConfig(
        enabled=_coerce_bool("enabled", _lookup(source, "enabled"), default=defaults.enabled, warnings=warnings),
        capability_level=_coerce_choice(
            "capability_level",
            _lookup(source, "capability_level"),
            default=defaults.capability_level,
            choices=_LEVELS,
            warnings=warnings,
            uppercase=True,
        ),
        data_dir=_coerce_path_string(
            "data_dir",
            _lookup(source, "data_dir"),
            default=defaults.data_dir,
            warnings=warnings,
        ),
        config_version=_coerce_int(
            "config_version",
            _lookup(source, "config_version"),
            default=defaults.config_version,
            min_value=1,
            max_value=100,
            warnings=warnings,
        ),
        worker_enabled=_coerce_bool(
            "worker_enabled",
            _lookup(source, "worker_enabled"),
            default=defaults.worker_enabled,
            warnings=warnings,
        ),
        security_prompt_enabled=_coerce_bool(
            "security_prompt_enabled",
            _lookup(source, "security_prompt_enabled"),
            default=defaults.security_prompt_enabled,
            warnings=warnings,
        ),
        auto_dispatch_enabled=_coerce_bool(
            "auto_dispatch_enabled",
            _lookup(source, "auto_dispatch_enabled"),
            default=defaults.auto_dispatch_enabled,
            warnings=warnings,
        ),
        ml_enabled=_coerce_bool(
            "ml_enabled",
            _lookup(source, "ml_enabled"),
            default=defaults.ml_enabled,
            warnings=warnings,
        ),
        cluster_enabled=_coerce_bool(
            "cluster_enabled",
            _lookup(source, "cluster_enabled"),
            default=defaults.cluster_enabled,
            warnings=warnings,
        ),
        response_execution_enabled=_coerce_bool(
            "response_execution_enabled",
            _lookup(source, "response_execution_enabled"),
            default=defaults.response_execution_enabled,
            warnings=warnings,
        ),
        response_mode=_coerce_choice(
            "response_mode",
            _lookup(source, "response_mode"),
            default=defaults.response_mode,
            choices={"recommend", "dry_run", "execute"},
            warnings=warnings,
        ),
        local_store_backend=_coerce_choice(
            "local_store_backend",
            _lookup(source, "local_store_backend"),
            default=defaults.local_store_backend,
            choices={"jsonl", "sqlite", "duckdb", "parquet"},
            warnings=warnings,
        ),
        query_default_limit=_coerce_int(
            "query_default_limit",
            _lookup(source, "query_default_limit"),
            default=defaults.query_default_limit,
            min_value=1,
            max_value=10000,
            warnings=warnings,
        ),
        query_max_limit=_coerce_int(
            "query_max_limit",
            _lookup(source, "query_max_limit"),
            default=defaults.query_max_limit,
            min_value=1,
            max_value=100000,
            warnings=warnings,
        ),
        source_retention_days=_coerce_int(
            "source_retention_days",
            _lookup(source, "source_retention_days"),
            default=defaults.source_retention_days,
            min_value=1,
            max_value=3650,
            warnings=warnings,
        ),
        payload_preview_max_chars=_coerce_int(
            "payload_preview_max_chars",
            _lookup(source, "payload_preview_max_chars"),
            default=defaults.payload_preview_max_chars,
            min_value=0,
            max_value=100000,
            warnings=warnings,
        ),
        max_parallel_analyst_agents=_coerce_int(
            "max_parallel_analyst_agents",
            _lookup(source, "max_parallel_analyst_agents"),
            default=defaults.max_parallel_analyst_agents,
            min_value=0,
            max_value=100,
            warnings=warnings,
        ),
        dispatch_budget_per_hour=_coerce_int(
            "dispatch_budget_per_hour",
            _lookup(source, "dispatch_budget_per_hour"),
            default=defaults.dispatch_budget_per_hour,
            min_value=0,
            max_value=10000,
            warnings=warnings,
        ),
        case_merge_window_minutes=_coerce_int(
            "case_merge_window_minutes",
            _lookup(source, "case_merge_window_minutes"),
            default=defaults.case_merge_window_minutes,
            min_value=1,
            max_value=10080,
            warnings=warnings,
        ),
        detector_window_minutes=_coerce_int(
            "detector_window_minutes",
            _lookup(source, "detector_window_minutes"),
            default=defaults.detector_window_minutes,
            min_value=1,
            max_value=1440,
            warnings=warnings,
        ),
    )

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


def _lookup(source: Mapping[str, Any] | object, field_name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


def _warn(
    warnings: list[LogAnalysisConfigWarning],
    field_name: str,
    raw_value: Any,
    fallback_value: Any,
    reason: str,
) -> None:
    warnings.append(
        LogAnalysisConfigWarning(
            field_name=field_name,
            raw_value=raw_value,
            fallback_value=fallback_value,
            reason=reason,
        )
    )


def _coerce_bool(
    field_name: str,
    raw_value: Any,
    *,
    default: bool,
    warnings: list[LogAnalysisConfigWarning],
) -> bool:
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, int) and raw_value in {0, 1}:
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    _warn(warnings, field_name, raw_value, default, "expected a clear boolean value")
    return default


def _coerce_choice(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    choices: set[str],
    warnings: list[LogAnalysisConfigWarning],
    uppercase: bool = False,
) -> str:
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        normalized = normalized.upper() if uppercase else normalized.lower()
        if normalized in choices:
            return normalized
    _warn(warnings, field_name, raw_value, default, f"expected one of {sorted(choices)}")
    return default


def _coerce_int(
    field_name: str,
    raw_value: Any,
    *,
    default: int,
    min_value: int,
    max_value: int | None,
    warnings: list[LogAnalysisConfigWarning],
) -> int:
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        _warn(warnings, field_name, raw_value, default, "expected an integer, not a boolean")
        return default
    if isinstance(raw_value, int):
        number = raw_value
    elif isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        number = int(raw_value.strip())
    else:
        _warn(warnings, field_name, raw_value, default, "expected an integer")
        return default

    if number < min_value:
        _warn(warnings, field_name, raw_value, default, f"expected value >= {min_value}")
        return default
    if max_value is not None and number > max_value:
        _warn(warnings, field_name, raw_value, default, f"expected value <= {max_value}")
        return default
    return number


def _coerce_path_string(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    warnings: list[LogAnalysisConfigWarning],
) -> str:
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if normalized and "\x00" not in normalized and "\n" not in normalized and "\r" not in normalized:
            return normalized
    _warn(warnings, field_name, raw_value, default, "expected a non-empty path string")
    return default


__all__ = [
    "LogAnalysisConfig",
    "LogAnalysisConfigWarning",
    "default_log_analysis_config_path",
    "load_log_analysis_config",
    "normalize_log_analysis_config",
]
