# LLM: Runtime guard config is the low-level reader for runtime reminder/rework knobs.
# 模块用途: 在 settings 层统一读取运行门配置，避免 tooling 和 agent_core 互相导入造成循环依赖。

from __future__ import annotations

from pathlib import Path

from .config_io import load_simple_yaml

DEFAULT_RUNTIME_GUARD_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "runtime_guard_config.yaml"


# LLM: runtime_guard_data is dynamic so config edits and monkeypatched paths take effect immediately.
# 函数用途: 每次读取当前 DEFAULT_RUNTIME_GUARD_CONFIG_PATH，确保改 YAML 后不会被代码默认值遮住。
def runtime_guard_data(path: Path | str | None = None) -> dict[str, object]:
    config_path = Path(path).expanduser() if path else DEFAULT_RUNTIME_GUARD_CONFIG_PATH
    try:
        data = load_simple_yaml(config_path)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


# LLM: runtime_guard_int is the shared integer reader for runtime guard knobs.
# 函数用途: 从 runtime_guard_config.yaml 读取非负整数，缺失或非法时使用调用方默认值。
def runtime_guard_int(key: str, default: int = 0, *, path: Path | str | None = None) -> int:
    try:
        return max(0, int(runtime_guard_data(path).get(key, default)))
    except (TypeError, ValueError):
        return max(0, int(default))


# LLM: runtime_guard_bool is the shared boolean reader for runtime guard switches.
# 函数用途: 从 runtime_guard_config.yaml 读取布尔开关，支持 true/yes/on/1 这类配置写法。
def runtime_guard_bool(key: str, default: bool = False, *, path: Path | str | None = None) -> bool:
    value = runtime_guard_data(path).get(key, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# LLM: runtime_guard_float_tuple keeps numeric sequence config parsing in one place.
# 函数用途: 从 runtime_guard_config.yaml 读取浮点数列表，非法条目自动跳过并回退默认序列。
def runtime_guard_float_tuple(
    key: str,
    default: tuple[float, ...],
    *,
    path: Path | str | None = None,
) -> tuple[float, ...]:
    value = runtime_guard_data(path).get(key, default)
    if not isinstance(value, list | tuple):
        return default
    parsed: list[float] = []
    for item in value:
        try:
            parsed.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(parsed) or default


__all__ = [
    "DEFAULT_RUNTIME_GUARD_CONFIG_PATH",
    "runtime_guard_bool",
    "runtime_guard_data",
    "runtime_guard_float_tuple",
    "runtime_guard_int",
]
