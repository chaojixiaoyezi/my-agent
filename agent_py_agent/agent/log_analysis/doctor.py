# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""本模块提供 LOG 轻量体检，不加载 storage/ML/worker 重依赖，只报告配置、路径和 feature gates。

新手说明:
doctor 像'体检命令'。用户想知道日志分析模块现在开没开、配置有没有问题、目录是否存在时，
就走这里。它故意不导入重型后端，避免只是看状态就启动 worker、加载 ML 或触发实际分析。
"""

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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 collect_doctor_status 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 collect doctor status 的候选结果，并按参数完成筛选、排序或数量限制。
def collect_doctor_status(
    config_path: str | Path | None = None,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """收集 LOG 模块的轻量状态快照，作为 CLI status/doctor 的结构化返回。

    新手说明:
    这个函数只回答'模块配置是什么、目录在哪里、功能门现在是什么状态'。
    它不会读取真实日志、不会跑检测器、不会启动子代理，也不会加载 ML 或集群后端。

    参数说明:
    config_path: 可选配置文件路径；不传时使用默认 `config/log_analysis_config.yaml`。
    workspace_root: 可选工作区根目录；用于把相对 data_dir 解析到指定工作区，测试时常传临时目录。

    返回说明:
    返回 dict，包含 module、state、enabled、capability_level、heavy_dependencies_loaded、
    config、paths 和 feature_gates。
    `heavy_dependencies_loaded` 固定为 False，表示这个体检路径保持轻量。

    失败处理:
    如果配置文件损坏或读取失败，函数不会崩掉 doctor，而是使用 LogAnalysisConfig 安全默认值，
    并把失败原因写进 config.warnings，方便用户修配置。"""

    root = _workspace_root(workspace_root)
    path = Path(config_path) if config_path is not None else default_log_analysis_config_path()
    config, config_warnings = _load_doctor_config(path)
    data_dir = resolve_log_analysis_data_dir(config.data_dir, workspace_root=root)

    return {
        "module": "log_analysis",
        "state": "enabled" if config.enabled else "disabled",
        "enabled": config.enabled,
        "capability_level": config.capability_level,
        "heavy_dependencies_loaded": False,
        "config": {
            "path": str(path),
            "exists": path.exists(),
            "warnings": config_warnings,
            "effective": _effective_config(config),
        },
        "paths": _runtime_paths(data_dir),
        "feature_gates": effective_feature_gates(config),
    }


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _workspace_root 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 workspace root 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def _workspace_root(workspace_root: str | Path | None) -> Path:
    return Path(workspace_root) if workspace_root is not None else Path(__file__).resolve().parents[2]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _load_doctor_config 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 load doctor config 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def _load_doctor_config(path: Path) -> tuple[LogAnalysisConfig, list[dict[str, Any]]]:
    try:
        config = load_log_analysis_config(path, missing_ok=True)
        return config, list(config.config_warnings)
    except Exception as exc:  # pragma: no cover - defensive guard for corrupted config files
        config = LogAnalysisConfig()
        warning = LogAnalysisConfigWarning(
            field_name="config_file",
            raw_value=str(path),
            fallback_value="safe defaults",
            reason=f"failed to load config: {type(exc).__name__}: {exc}",
        )
        return config, [warning.to_dict()]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _effective_config 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 effective config 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def _effective_config(config: LogAnalysisConfig) -> dict[str, Any]:
    return {
        "worker_enabled": config.worker_enabled,
        "security_prompt_enabled": config.security_prompt_enabled,
        "auto_dispatch_enabled": config.auto_dispatch_enabled,
        "ml_enabled": config.ml_enabled,
        "cluster_enabled": config.cluster_enabled,
        "response_execution_enabled": config.response_execution_enabled,
        "response_mode": config.response_mode,
        "local_store_backend": config.local_store_backend,
    }


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _runtime_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 runtime paths 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _runtime_paths(data_dir: Path) -> dict[str, dict[str, Any]]:
    paths = {"base": _path_status(data_dir)}
    for name in RUNTIME_DIRS:
        paths[name] = _path_status(data_dir / name)
    return paths


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _path_status 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 path status 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def _path_status(path: Path) -> dict[str, Any]:
    """把一个路径转成 doctor 可展示的 path/exists/is_dir 三元状态。

    新手说明:
    doctor 不需要读取目录内容，只要告诉用户这个路径存在吗、是不是目录。

    参数说明:
    path: 要检查的本地路径。

    返回说明:
    返回 dict，包含字符串 path、exists 布尔值、is_dir 布尔值。"""
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
