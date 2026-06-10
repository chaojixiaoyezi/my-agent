"""交付收尾的数据模型与配置：恢复账本数据类 + DeliveryCloseoutConfig（原 recovery_models.py / config.py 并入）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...settings.config_io import load_simple_yaml
from ...settings.runtime_guard_config import DEFAULT_RUNTIME_GUARD_CONFIG_PATH


@dataclass
class RecoveryActionLedger:
    actions: list[dict[str, object]]
    seen: set[str]


@dataclass(frozen=True)
class StagingActionContext:
    ledger: RecoveryActionLedger
    staging: dict[str, Any]
    artifact_exists: bool
    builder_tool: str
    source_ref: str
    output_ref: str
    source_path: Path | None
    required_columns: list[str]
    required_sheets_min: int
    source_shape_hint: str
    workspace_root: Path


@dataclass(frozen=True)
class CheckpointQualityActionRequest:
    ledger: RecoveryActionLedger
    checkpoint_ref: str
    checkpoint_path: Path
    required_columns: list[str] | None = None
    required_sheets_min: int = 0
    checkpoint_shape_hint: str = ""
    validation_contract: dict[str, object] | None = None


@dataclass(frozen=True)
class StagedEvidenceActionRequest:
    ledger: RecoveryActionLedger
    checkpoint_ref: str
    workspace_root: Path
    validation_contract: dict[str, object]


__all__ = [
    "CheckpointQualityActionRequest",
    "RecoveryActionLedger",
    "StagedEvidenceActionRequest",
    "StagingActionContext",
]


DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT = 0
DEFAULT_DELIVERY_CLOSEOUT_CONFIG_PATH = DEFAULT_RUNTIME_GUARD_CONFIG_PATH
DELIVERY_CLOSEOUT_CONFIG_ENV = "MY_AGENT_DELIVERY_CLOSEOUT_CONFIG"


@dataclass(frozen=True)
class DeliveryCloseoutConfig:
    """Config for delivery closeout rework attempts.

    invalid_artifacts_retry_limit:
        必交产物都已经能定位到，但内容、格式、字段、证据或质量门不合格时，
        同一失败且工作区无新进展最多允许几次验收返工。默认 0。
        0 表示不按次数阻断，只持续返回结构化返工单。
    missing_artifacts_retry_limit:
        必交产物缺失、路径无效或无法唯一定位时，同一失败且工作区无新进展
        最多允许几次验收返工。默认 0。
        0 表示不按次数阻断。
    """

    invalid_artifacts_retry_limit: int = DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT
    missing_artifacts_retry_limit: int = DEFAULT_DELIVERY_CLOSEOUT_RETRY_LIMIT


def delivery_closeout_config(agent: object | None = None) -> DeliveryCloseoutConfig:
    override = getattr(agent, "_delivery_closeout_config", None)
    if isinstance(override, DeliveryCloseoutConfig):
        return override
    return load_delivery_closeout_config()


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
