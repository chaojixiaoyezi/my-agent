
from __future__ import annotations

"""rule dataclasses and enums for log analysis detectors.

新手说明:
这个文件放的是检测器规则相关的数据结构体和类型定义。
实际规则从 security_rules 模块加载，这里只定义数据结构。
"""

from dataclasses import dataclass, field
from typing import Any


# Dataclass for detector configuration
@dataclass
class DetectorConfig:
    """configuration parameters for a soft detector."""

    window_minutes: int = 15
    failure_threshold: int = 5
    baselines: Any = None


# Dataclass for detector result
@dataclass
class DetectorResult:
    """result from running a detector."""

    detector_id: str
    findings: list[Any]
    features: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
