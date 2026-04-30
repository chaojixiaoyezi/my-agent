from __future__ import annotations

"""Lightweight doctor/status checks for log analysis."""

from pathlib import Path
from typing import Any

from .capabilities import effective_feature_gates
from .config import (
    LogAnalysisConfig,
    LogAnalysisConfigWarning,
    default_log_analysis_config_path,
    load_log_analysis_config,
    resolve_log_analysis_data_dir,
)


RUNTIME_DIRS = [
    "sources",
    "checkpoints",
    "manifests",
    "spool",
    "normalized",
    "parquet",
    "duckdb",
    "rollups",
    "detections",
    "evidence",
    "cases",
    "reports",
    "dead_letter",
    "ml",
]


def collect_doctor_status(
    config_path: str | Path | None = None,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return structured status without importing storage, ML, or worker backends."""

    root = Path(workspace_root) if workspace_root is not None else Path(__file__).resolve().parents[2]
    path = Path(config_path) if config_path is not None else default_log_analysis_config_path()
    config_exists = path.exists()
    load_warning: LogAnalysisConfigWarning | None = None

    try:
        config = load_log_analysis_config(path, missing_ok=True)
    except Exception as exc:  # pragma: no cover - defensive guard for corrupted config files
        config = LogAnalysisConfig()
        load_warning = LogAnalysisConfigWarning(
            field_name="config_file",
            raw_value=str(path),
            fallback_value="safe defaults",
            reason=f"failed to load config: {type(exc).__name__}: {exc}",
        )

    config_warnings = list(config.config_warnings)
    if load_warning is not None:
        config_warnings.append(load_warning.to_dict())

    data_dir = resolve_log_analysis_data_dir(config.data_dir, workspace_root=root)
    paths = {"base": _path_status(data_dir)}
    for name in RUNTIME_DIRS:
        paths[name] = _path_status(data_dir / name)

    return {
        "module": "log_analysis",
        "state": "enabled" if config.enabled else "disabled",
        "enabled": config.enabled,
        "capability_level": config.capability_level,
        "heavy_dependencies_loaded": False,
        "config": {
            "path": str(path),
            "exists": config_exists,
            "warnings": config_warnings,
            "effective": {
                "worker_enabled": config.worker_enabled,
                "security_prompt_enabled": config.security_prompt_enabled,
                "auto_dispatch_enabled": config.auto_dispatch_enabled,
                "ml_enabled": config.ml_enabled,
                "cluster_enabled": config.cluster_enabled,
                "response_execution_enabled": config.response_execution_enabled,
                "response_mode": config.response_mode,
                "local_store_backend": config.local_store_backend,
            },
        },
        "paths": paths,
        "feature_gates": effective_feature_gates(config),
    }


def _path_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
    }


doctor_status = collect_doctor_status
get_log_analysis_status = collect_doctor_status


__all__ = [
    "RUNTIME_DIRS",
    "collect_doctor_status",
    "doctor_status",
    "get_log_analysis_status",
]
