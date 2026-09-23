# LLM: 原 task overlay 只有一份路径、合并和规范化算法；只读候选投影不应用进程日志配置，正式 worker 仍沿原入口应用。
# 模块用途: 读取子代理原配置覆盖并生成最终配置，把预览与日志级别应用副作用分开。
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import AgentConfig, apply_log_level
from ..memory import normalize_agent_memory_config
from ..runtime_scope_config import load_runtime_config_layer, merge_runtime_config_layers


# LLM: worker 的原调用入口复用只读投影；只有实际存在覆盖时应用原日志级别，不改变原无覆盖对象身份。
# 函数用途: 给正式子代理应用最终配置和进程日志设置，容量预览须调用 project 入口。
def apply_task_runtime_config_overlay(
    base_config: AgentConfig,
    task: Any,
    *,
    workspace_root: str | Path,
) -> AgentConfig:
    config = project_task_runtime_config_overlay(base_config, task, workspace_root=workspace_root)
    if config is not base_config:
        apply_log_level(config)
    return config


# LLM: 只读原 overlay 文件并复用原优先级/规范化；不写文件、不改 base_config 或日志，不构造任务、后端或线程。
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
    config = AgentConfig(**effective.values)
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
