from __future__ import annotations

"""Runtime config overlays referenced by subagent task identity."""

from pathlib import Path
from typing import Any

from ..config import AgentConfig, apply_log_level
from ..memory import normalize_agent_memory_config
from ..runtime_scope_config import load_runtime_config_layer, merge_runtime_config_layers


def apply_task_runtime_config_overlay(
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
    apply_log_level(config)
    return config


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


__all__ = ["apply_task_runtime_config_overlay"]
