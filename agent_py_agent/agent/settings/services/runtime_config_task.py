# LLM: task overlay只有一份路径/合并/规范化算法；正式worker和预览都不修改进程日志级别，脱敏安装保持幂等。
# 模块用途: 投影子代理配置并保留日志脱敏，不让单任务覆盖污染其他owner的logger。
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ...common.log_redaction import install_log_redaction
from ..config import AgentConfig
from ..memory import normalize_agent_memory_config
from ..runtime_scope_config import load_runtime_config_layer, merge_runtime_config_layers
from ..secret_ref import freeze_managed_secret_environment


# LLM: worker复用原投影；覆盖只保存在返回配置，不改变进程logger，仍安装原幂等脱敏器并保留无覆盖对象身份。
# 函数用途: 应用子代理任务配置但不改共享日志级别，容量预览继续走project入口。
def apply_task_runtime_config_overlay(
    base_config: AgentConfig,
    task: Any,
    *,
    workspace_root: str | Path,
) -> AgentConfig:
    config = project_task_runtime_config_overlay(base_config, task, workspace_root=workspace_root)
    if config is not base_config:
        install_log_redaction()
    return config


# LLM: 只读原 overlay 并复用优先级；克隆保留宿主来源上界，任务文件的内部字段伪造无效，不改变原无覆盖对象。
# 函数用途: 为正式 worker 和创建前候选共同投影最终配置，保留模型来源与覆盖警告。
def project_task_runtime_config_overlay(
    base_config: AgentConfig,
    task: Any,
    *,
    workspace_root: str | Path,
) -> AgentConfig:
    identity = getattr(task, "runtime_identity", None)
    overlay_ref = str(getattr(identity, "config_overlay_ref", "") or "").strip()
    if not overlay_ref:
        return base_config
    scope = str(getattr(identity, "config_scope", "") or "run").strip() or "run"
    path = _resolve_overlay_ref(overlay_ref, workspace_root=workspace_root, task=task)
    layer = load_runtime_config_layer(path, scope=scope, config_cls=type(base_config))
    effective = merge_runtime_config_layers(base_config, [layer], config_cls=type(base_config))
    config = freeze_managed_secret_environment(replace(freeze_managed_secret_environment(base_config), **effective.values))
    config.config_path = base_config.config_path
    config.config_sources = effective.sources
    config.config_layers = list(effective.layers)
    config.config_warnings = [
        *list(getattr(base_config, "config_warnings", []) or []),
        *[f"{layer.source}: {warning}" for warning in layer.warnings],
    ]
    normalize_agent_memory_config(config)
    return config


# LLM: overlay 只按原工作区优先与任务目录候补规则读取；投影和正式应用不能维护不同路径来源。
# 函数用途: 找到已有配置覆盖文件，未找到时保留原工作区地址，不创建目录或文件。
def _resolve_overlay_ref(overlay_ref: str, *, workspace_root: str | Path, task: Any) -> Path:
    raw = Path(overlay_ref).expanduser()
    if raw.is_absolute():
        return raw
    workspace_path = Path(workspace_root) / raw
    if workspace_path.exists():
        return workspace_path
    task_dir = str(getattr(task, "task_dir", "") or "").strip()
    if task_dir:
        task_path = Path(task_dir) / raw
        if task_path.exists():
            return task_path
    return workspace_path


__all__ = ["apply_task_runtime_config_overlay", "project_task_runtime_config_overlay"]
