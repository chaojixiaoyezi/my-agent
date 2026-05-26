# LLM: Exploration fuse config keeps long-research budgets configurable instead of hidden constants.
# 模块用途: 集中定义探索熔断次数、提示节点和 0=不限制语义，避免真实研究任务被写死阈值误伤。

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..settings.config_io import load_simple_yaml
from .runtime_guard_config import DEFAULT_RUNTIME_GUARD_CONFIG_PATH

DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD = 300
DEFAULT_UNLIMITED_HINT_ROUNDS = (50, 150, 250)
DEFAULT_LOCAL_PROGRESS_UNLIMITED_HINT_INTERVAL = 10
DEFAULT_EXPLORATION_FUSE_CONFIG_PATH = DEFAULT_RUNTIME_GUARD_CONFIG_PATH
EXPLORATION_FUSE_CONFIG_ENV = "MY_AGENT_EXPLORATION_FUSE_CONFIG"
_RATIO_HINT_NUMERATORS = (1, 2, 4)
_RATIO_HINT_DENOMINATOR = 5


# LLM: ExplorationFuseConfig is the runtime contract for read-only exploration budget behavior.
# 类用途: 保存探索额度上限和提示节点；round_threshold=0 表示不按次数阻断，只保留固定提醒。
@dataclass(frozen=True)
class ExplorationFuseConfig:
    """Config for exploration-only tool loop hints and blocking.

    round_threshold:
        连续只读/搜索/抓取多少轮后才阻断。默认 300。
        0 表示不按次数阻断；系统只在 50、150、250 轮做软提醒。
    unlimited_hint_rounds:
        round_threshold=0 时使用的固定提示轮次。
    local_progress_unlimited_hint_interval:
        closeout 失败后连续只读/无本地推进时，每隔多少轮做一次固定软提醒。默认 10。
    """

    round_threshold: int = DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD
    unlimited_hint_rounds: tuple[int, ...] = DEFAULT_UNLIMITED_HINT_ROUNDS
    local_progress_unlimited_hint_interval: int = DEFAULT_LOCAL_PROGRESS_UNLIMITED_HINT_INTERVAL


# LLM: exploration_fuse_config keeps exploration budgets outside the main AgentConfig.
# 函数用途: 从专门的探索预算配置读取阈值；缺失、非法或负数时回到默认 300，0 保留为不限制。
def exploration_fuse_config(agent: object) -> ExplorationFuseConfig:
    override = getattr(agent, "_exploration_fuse_config", None)
    if isinstance(override, ExplorationFuseConfig):
        return override
    return load_exploration_fuse_config()


# LLM: load_exploration_fuse_config is the single reader for exploration fields in the shared runtime guard config.
# 函数用途: 读取 agent_py_agent/config/runtime_guard_config.yaml；环境变量只作为部署覆盖入口。
def load_exploration_fuse_config(path: Path | str | None = None) -> ExplorationFuseConfig:
    config_path = _resolve_config_path(path)
    data = _read_config_data(config_path)
    return ExplorationFuseConfig(
        round_threshold=_non_negative_int(data.get("round_threshold"), DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD),
        unlimited_hint_rounds=_positive_int_tuple(
            data.get("unlimited_hint_rounds"),
            DEFAULT_UNLIMITED_HINT_ROUNDS,
        ),
        local_progress_unlimited_hint_interval=_positive_int(
            data.get("local_progress_unlimited_hint_interval"),
            DEFAULT_LOCAL_PROGRESS_UNLIMITED_HINT_INTERVAL,
        ),
    )


# LLM: exploration_fuse_hint_rounds returns ratio hints for finite budgets and fixed hints for unlimited budgets.
# 函数用途: 计算应该提示模型的探索轮次：有限额度按 1/5、2/5、4/5；0 额度按 50、150、250。
def exploration_fuse_hint_rounds(config: ExplorationFuseConfig) -> tuple[int, ...]:
    if config.round_threshold <= 0:
        return tuple(item for item in config.unlimited_hint_rounds if item > 0)
    hints = {
        max(1, (config.round_threshold * numerator) // _RATIO_HINT_DENOMINATOR)
        for numerator in _RATIO_HINT_NUMERATORS
    }
    return tuple(sorted(item for item in hints if item < config.round_threshold))


# LLM: exploration_fuse_used_percent renders stable percentages for the configured ratio hints.
# 函数用途: 给模型提示“已经消耗多少探索额度”；非比例提示返回 0，由调用方按固定提醒处理。
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


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


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
    "DEFAULT_LOCAL_PROGRESS_UNLIMITED_HINT_INTERVAL",
    "EXPLORATION_FUSE_CONFIG_ENV",
    "ExplorationFuseConfig",
    "exploration_fuse_config",
    "exploration_fuse_hint_rounds",
    "exploration_fuse_used_percent",
    "load_exploration_fuse_config",
]
