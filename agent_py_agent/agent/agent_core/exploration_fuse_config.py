
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..settings.config_io import load_simple_yaml
from ..settings.runtime_guard_config import DEFAULT_RUNTIME_GUARD_CONFIG_PATH

DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD = 50
DEFAULT_UNLIMITED_HINT_ROUNDS = (50, 150, 250)
DEFAULT_EXPLORATION_FUSE_CONFIG_PATH = DEFAULT_RUNTIME_GUARD_CONFIG_PATH
EXPLORATION_FUSE_CONFIG_ENV = "MY_AGENT_EXPLORATION_FUSE_CONFIG"
_RATIO_HINT_NUMERATORS = (1, 2, 4)
_RATIO_HINT_DENOMINATOR = 5


@dataclass(frozen=True)
class ExplorationFuseConfig:
    """Config for exploration-only tool loop hints.

    round_threshold:
        连续只读/搜索/抓取多少轮内按比例给软提醒。默认 50。
        不阻断任务；0 表示只在 50、150、250 轮做固定软提醒。
    unlimited_hint_rounds:
        round_threshold=0 时使用的固定提示轮次。
    """

    round_threshold: int = DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD
    unlimited_hint_rounds: tuple[int, ...] = DEFAULT_UNLIMITED_HINT_ROUNDS


def exploration_fuse_config(agent: object) -> ExplorationFuseConfig:
    override = getattr(agent, "_exploration_fuse_config", None)
    if isinstance(override, ExplorationFuseConfig):
        return override
    return load_exploration_fuse_config()


def load_exploration_fuse_config(path: Path | str | None = None) -> ExplorationFuseConfig:
    config_path = _resolve_config_path(path)
    data = _read_config_data(config_path)
    return ExplorationFuseConfig(
        round_threshold=_non_negative_int(data.get("round_threshold"), DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD),
        unlimited_hint_rounds=_positive_int_tuple(
            data.get("unlimited_hint_rounds"),
            DEFAULT_UNLIMITED_HINT_ROUNDS,
        ),
    )


def exploration_fuse_hint_rounds(config: ExplorationFuseConfig) -> tuple[int, ...]:
    if config.round_threshold <= 0:
        return tuple(item for item in config.unlimited_hint_rounds if item > 0)
    hints = {
        max(1, (config.round_threshold * numerator) // _RATIO_HINT_DENOMINATOR)
        for numerator in _RATIO_HINT_NUMERATORS
    }
    return tuple(sorted(item for item in hints if item < config.round_threshold))


def exploration_fuse_used_percent(config: ExplorationFuseConfig, count: int) -> int:
    if config.round_threshold <= 0:
        return 0
    for numerator in _RATIO_HINT_NUMERATORS:
        if count == max(1, (config.round_threshold * numerator) // _RATIO_HINT_DENOMINATOR):
            return (numerator * 100) // _RATIO_HINT_DENOMINATOR
    return 0


def _non_negative_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _positive_int_tuple(value: object, default: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(value, list | tuple):
        return default
    parsed: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            parsed.append(number)
    return tuple(parsed) or default


def _resolve_config_path(path: Path | str | None) -> Path:
    if path:
        return Path(path).expanduser()
    env_path = os.environ.get(EXPLORATION_FUSE_CONFIG_ENV)
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_EXPLORATION_FUSE_CONFIG_PATH


def _read_config_data(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = load_simple_yaml(path)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD",
    "DEFAULT_EXPLORATION_FUSE_CONFIG_PATH",
    "DEFAULT_UNLIMITED_HINT_ROUNDS",
    "EXPLORATION_FUSE_CONFIG_ENV",
    "ExplorationFuseConfig",
    "exploration_fuse_config",
    "exploration_fuse_hint_rounds",
    "exploration_fuse_used_percent",
    "load_exploration_fuse_config",
]
