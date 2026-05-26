# LLM: Open write-session config controls reminder cadence without making staged writes a hard fuse.
# 模块用途: 集中定义 file_write_session 未提交时的提醒周期；它只影响提醒频率，不决定任务失败。

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..settings.config_io import load_simple_yaml
from .runtime_guard_config import DEFAULT_RUNTIME_GUARD_CONFIG_PATH

DEFAULT_OPEN_WRITE_SESSION_REMINDER_INTERVAL = 3
DEFAULT_OPEN_WRITE_SESSION_CONFIG_PATH = DEFAULT_RUNTIME_GUARD_CONFIG_PATH
OPEN_WRITE_SESSION_CONFIG_ENV = "MY_AGENT_OPEN_WRITE_SESSION_CONFIG"


# LLM: OpenWriteSessionConfig stays task-agnostic and only knows about model-turn reminder cadence.
# 类用途: 保存 open file_write_session 的提醒周期；关键动作仍由事务保护门即时纠偏。
@dataclass(frozen=True)
class OpenWriteSessionConfig:
    """Config for unfinished file_write_session reminders.

    reminder_interval:
        当存在未 finish/abort/reset 的 file_write_session，且模型继续做非冲突工具调用时，
        每隔多少个“未处理该 session 的模型回合”给一次中文软提醒。默认 3。
        这个值不会触发 runtime blocked；它只决定提醒频率。
        小于等于 0 或非法值会回退默认值，避免配置写错导致完全无提醒。

    关键动作不受 reminder_interval 限制：
        模型想最终回答、submit_for_acceptance、读取未提交目标文件、覆盖写未提交目标文件时，
        系统会立即返回结构化返工提示，因为这些动作可能造成假完成或读旧文件。
    """

    reminder_interval: int = DEFAULT_OPEN_WRITE_SESSION_REMINDER_INTERVAL


# LLM: open_write_session_config keeps staged-write reminder knobs outside the main AgentConfig.
# 函数用途: 优先读取测试/运行时注入配置，否则读取统一运行门 YAML 配置。
def open_write_session_config(agent: object | None = None) -> OpenWriteSessionConfig:
    override = getattr(agent, "_open_write_session_config", None)
    if isinstance(override, OpenWriteSessionConfig):
        return override
    return load_open_write_session_config()


# LLM: load_open_write_session_config is the single reader for staged-write fields in the shared runtime guard config.
# 函数用途: 读取 agent_py_agent/config/runtime_guard_config.yaml；环境变量只作为部署覆盖入口。
def load_open_write_session_config(path: Path | str | None = None) -> OpenWriteSessionConfig:
    data = _read_config_data(_resolve_config_path(path))
    return OpenWriteSessionConfig(
        reminder_interval=_positive_int(
            data.get("open_write_session_reminder_interval", data.get("reminder_interval")),
            DEFAULT_OPEN_WRITE_SESSION_REMINDER_INTERVAL,
        )
    )


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _resolve_config_path(path: Path | str | None) -> Path:
    if path:
        return Path(path).expanduser()
    env_path = os.environ.get(OPEN_WRITE_SESSION_CONFIG_ENV)
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_OPEN_WRITE_SESSION_CONFIG_PATH


def _read_config_data(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = load_simple_yaml(path)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "DEFAULT_OPEN_WRITE_SESSION_CONFIG_PATH",
    "DEFAULT_OPEN_WRITE_SESSION_REMINDER_INTERVAL",
    "OPEN_WRITE_SESSION_CONFIG_ENV",
    "OpenWriteSessionConfig",
    "load_open_write_session_config",
    "open_write_session_config",
]
