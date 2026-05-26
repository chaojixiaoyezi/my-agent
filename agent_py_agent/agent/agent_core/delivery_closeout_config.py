# LLM: Delivery closeout retry config keeps acceptance rework budgets explicit and user-tunable.
# 模块用途: 集中定义交付验收失败后的返工次数预算；0 表示不按次数阻断，避免隐藏常量卡死模型。

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..settings.config_io import load_simple_yaml
from .runtime_guard_config import DEFAULT_RUNTIME_GUARD_CONFIG_PATH

DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT = 3
DEFAULT_DELIVERY_CLOSEOUT_CONFIG_PATH = DEFAULT_RUNTIME_GUARD_CONFIG_PATH
DELIVERY_CLOSEOUT_CONFIG_ENV = "MY_AGENT_DELIVERY_CLOSEOUT_CONFIG"


# LLM: DeliveryCloseoutConfig is intentionally generic; it does not know artifact kinds or task types.
# 类用途: 保存两类通用验收失败的返工预算：产物齐全但不合格、产物缺失或无法定位。
@dataclass(frozen=True)
class DeliveryCloseoutConfig:
    """Config for delivery closeout rework attempts.

    invalid_artifacts_retry_limit:
        必交产物都已经能定位到，但内容、格式、字段、证据或质量门不合格时，
        同一失败且工作区无新进展最多允许几次验收返工。默认 3。
        0 表示不按次数阻断，只持续返回结构化返工单。
    missing_artifacts_retry_limit:
        必交产物缺失、路径无效或无法唯一定位时，同一失败且工作区无新进展
        最多允许几次验收返工。默认 3。
        0 表示不按次数阻断。
    """

    invalid_artifacts_retry_limit: int = DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT
    missing_artifacts_retry_limit: int = DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT


# LLM: delivery_closeout_config keeps retry budgets outside task prompts and delivery contracts.
# 函数用途: 优先使用测试/运行时注入的配置；否则读取统一运行门 YAML 配置。
def delivery_closeout_config(agent: object | None = None) -> DeliveryCloseoutConfig:
    override = getattr(agent, "_delivery_closeout_config", None)
    if isinstance(override, DeliveryCloseoutConfig):
        return override
    return load_delivery_closeout_config()


# LLM: load_delivery_closeout_config is the single reader for delivery fields in the shared runtime guard config.
# 函数用途: 读取 agent_py_agent/config/runtime_guard_config.yaml；环境变量只作为部署覆盖入口。
def load_delivery_closeout_config(path: Path | str | None = None) -> DeliveryCloseoutConfig:
    data = _read_config_data(_resolve_config_path(path))
    return DeliveryCloseoutConfig(
        invalid_artifacts_retry_limit=_non_negative_int(
            data.get("invalid_artifacts_retry_limit"),
            DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT,
        ),
        missing_artifacts_retry_limit=_non_negative_int(
            data.get("missing_artifacts_retry_limit"),
            DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT,
        ),
    )


def _non_negative_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _resolve_config_path(path: Path | str | None) -> Path:
    if path:
        return Path(path).expanduser()
    env_path = os.environ.get(DELIVERY_CLOSEOUT_CONFIG_ENV)
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_DELIVERY_CLOSEOUT_CONFIG_PATH


def _read_config_data(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = load_simple_yaml(path)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "DEFAULT_DELIVERY_CLOSEOUT_CONFIG_PATH",
    "DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT",
    "DELIVERY_CLOSEOUT_CONFIG_ENV",
    "DeliveryCloseoutConfig",
    "delivery_closeout_config",
    "load_delivery_closeout_config",
]
