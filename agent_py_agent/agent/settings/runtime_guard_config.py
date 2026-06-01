# LLM: Runtime guard config is the low-level reader for runtime reminder/rework knobs.
# 模块用途: 在 settings 层统一读取运行门配置，避免 tooling 和 agent_core 互相导入造成循环依赖。

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_io import load_simple_yaml

DEFAULT_RUNTIME_GUARD_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "runtime_guard_config.yaml"


@dataclass(frozen=True)
class RuntimeGuardPolicy:
    schema_version: str = "runtime_guard_policy.v1"
    source_path: str = ""
    loaded_at: float = 0.0
    values: dict[str, object] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def int_value(self, key: str, default: int = 0) -> int:
        try:
            return max(0, int(self.values.get(key, default)))
        except (TypeError, ValueError):
            return max(0, int(default))

    def bool_value(self, key: str, default: bool = False) -> bool:
        value = self.values.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def float_tuple_value(self, key: str, default: tuple[float, ...]) -> tuple[float, ...]:
        value = self.values.get(key, default)
        if not isinstance(value, list | tuple):
            return default
        parsed = _float_tuple(value)
        return parsed or default

    def snapshot(self, *, task_id: str = "", run_id: str = "") -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": task_id,
            "run_id": run_id,
            "source_path": self.source_path,
            "loaded_at": self.loaded_at,
            "values": dict(self.values),
            "sources": dict(self.sources),
        }


# LLM: runtime_guard_data is dynamic so config edits and monkeypatched paths take effect immediately.
# 函数用途: 每次读取当前 DEFAULT_RUNTIME_GUARD_CONFIG_PATH，确保改 YAML 后不会被代码默认值遮住。
def runtime_guard_data(path: Path | str | None = None) -> dict[str, object]:
    return runtime_guard_policy(path).values


def runtime_guard_policy(path: Path | str | None = None, *, overrides: dict[str, object] | None = None) -> RuntimeGuardPolicy:
    config_path = _config_path(path)
    try:
        data = load_simple_yaml(config_path)
    except OSError:
        data = {}
    values = data if isinstance(data, dict) else {}
    merged = {**values, **(overrides or {})}
    sources = {key: str(config_path) for key in values}
    sources.update(dict.fromkeys(overrides or {}, "runtime_override"))
    return RuntimeGuardPolicy(
        source_path=str(config_path),
        loaded_at=time.time(),
        values=merged,
        sources=sources,
    )


# LLM: runtime_guard_int is the shared integer reader for runtime guard knobs.
# 函数用途: 从 runtime_guard_config.yaml 读取非负整数，缺失或非法时使用调用方默认值。
def runtime_guard_int(key: str, default: int = 0, *, path: Path | str | None = None) -> int:
    return runtime_guard_policy(path).int_value(key, default)


# LLM: runtime_guard_bool is the shared boolean reader for runtime guard switches.
# 函数用途: 从 runtime_guard_config.yaml 读取布尔开关，支持 true/yes/on/1 这类配置写法。
def runtime_guard_bool(key: str, default: bool = False, *, path: Path | str | None = None) -> bool:
    return runtime_guard_policy(path).bool_value(key, default)


# LLM: runtime_guard_float_tuple keeps numeric sequence config parsing in one place.
# 函数用途: 从 runtime_guard_config.yaml 读取浮点数列表，非法条目自动跳过并回退默认序列。
def runtime_guard_float_tuple(
    key: str,
    default: tuple[float, ...],
    *,
    path: Path | str | None = None,
) -> tuple[float, ...]:
    return runtime_guard_policy(path).float_tuple_value(key, default)


def _config_path(path: Path | str | None = None) -> Path:
    return Path(path).expanduser() if path else DEFAULT_RUNTIME_GUARD_CONFIG_PATH


def _float_tuple(value: list | tuple) -> tuple[float, ...]:
    parsed: list[float] = []
    for item in value:
        try:
            parsed.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(parsed)


__all__ = [
    "DEFAULT_RUNTIME_GUARD_CONFIG_PATH",
    "RuntimeGuardPolicy",
    "runtime_guard_bool",
    "runtime_guard_data",
    "runtime_guard_float_tuple",
    "runtime_guard_int",
    "runtime_guard_policy",
]
