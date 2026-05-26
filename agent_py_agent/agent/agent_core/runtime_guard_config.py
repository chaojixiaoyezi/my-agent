# LLM: Runtime guard config owns the shared default YAML path for all runtime reminder/rework knobs.
# 模块用途: 统一运行门、提醒和返工预算的默认配置文件路径，避免同类配置分散在多个 YAML 中。

from __future__ import annotations

from pathlib import Path

DEFAULT_RUNTIME_GUARD_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "runtime_guard_config.yaml"

__all__ = ["DEFAULT_RUNTIME_GUARD_CONFIG_PATH"]
