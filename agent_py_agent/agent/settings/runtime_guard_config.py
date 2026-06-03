
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


def runtime_guard_data(
    path: Path | str | None = None,
    *,
    policy: RuntimeGuardPolicy | None = None,
) -> dict[str, object]:
    return _effective_policy(path=path, policy=policy).values


def runtime_guard_policy(
    path: Path | str | None = None,
    *,
    overrides: dict[str, object] | None = None,
) -> RuntimeGuardPolicy:
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


def runtime_guard_int(
    key: str,
    default: int = 0,
    *,
    path: Path | str | None = None,
    policy: RuntimeGuardPolicy | None = None,
) -> int:
    return _effective_policy(path=path, policy=policy).int_value(key, default)


def runtime_guard_bool(
    key: str,
    default: bool = False,
    *,
    path: Path | str | None = None,
    policy: RuntimeGuardPolicy | None = None,
) -> bool:
    return _effective_policy(path=path, policy=policy).bool_value(key, default)


def runtime_guard_float_tuple(
    key: str,
    default: tuple[float, ...],
    *,
    path: Path | str | None = None,
    policy: RuntimeGuardPolicy | None = None,
) -> tuple[float, ...]:
    return _effective_policy(path=path, policy=policy).float_tuple_value(key, default)


def _effective_policy(
    *,
    path: Path | str | None = None,
    policy: RuntimeGuardPolicy | None = None,
) -> RuntimeGuardPolicy:
    if policy is not None:
        return policy
    return runtime_guard_policy(path)


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
