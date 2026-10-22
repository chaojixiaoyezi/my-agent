
# LLM: 守卫配置按stat在进程内最多缓存64项原始字典；原路径、OSError与覆盖层语义保持，不写配置或缓存调用方policy。
# 模块用途: 提供共享守卫策略及其来源，每次返回隔离值，配置改变立即重新解析。
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_io import load_simple_yaml

DEFAULT_RUNTIME_GUARD_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "runtime_guard_config.yaml"
_RUNTIME_GUARD_RAW_CACHE: OrderedDict[tuple[str, int, int, int, int, int], dict[str, Any]] = OrderedDict()
_RUNTIME_GUARD_RAW_CACHE_LOCK = threading.RLock()
_RUNTIME_GUARD_RAW_CACHE_MAX_SIZE = 64


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
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true"}:
            return True
        if normalized in {"0", "false"}:
            return False
        return default

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


# LLM: 只复用同版本原始配置，overrides/时间/来源每次新建；OSError保持空原始策略，不拿旧成功内容兜底。
# 函数用途: 读取共享守卫配置并合成本次独立策略，调用方覆盖不会污染其它请求。
def runtime_guard_policy(
    path: Path | str | None = None,
    *,
    overrides: dict[str, object] | None = None,
) -> RuntimeGuardPolicy:
    config_path = _config_path(path)
    try:
        data = _runtime_guard_raw(config_path)
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


# LLM: stat失败沿原OSError向上传递；RLock覆盖冷解析，失败不入LRU，存浅字典但输出深复制避免嵌套共享。
# 函数用途: 复用未变化的守卫文件正文，保留64项上限与即时磁盘失效。
def _runtime_guard_raw(config_path: Path) -> dict[str, Any]:
    st = config_path.stat()
    key = (str(config_path), int(st.st_dev), int(st.st_ino), int(st.st_size),
           int(st.st_mtime_ns), int(st.st_ctime_ns))
    with _RUNTIME_GUARD_RAW_CACHE_LOCK:
        if key in _RUNTIME_GUARD_RAW_CACHE:
            _RUNTIME_GUARD_RAW_CACHE.move_to_end(key)
            return deepcopy(_RUNTIME_GUARD_RAW_CACHE[key])
        loaded = load_simple_yaml(config_path)
        if not isinstance(loaded, dict):
            return loaded
        _RUNTIME_GUARD_RAW_CACHE[key] = dict(loaded)
        if len(_RUNTIME_GUARD_RAW_CACHE) > _RUNTIME_GUARD_RAW_CACHE_MAX_SIZE:
            _RUNTIME_GUARD_RAW_CACHE.popitem(last=False)
        return deepcopy(loaded)


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
