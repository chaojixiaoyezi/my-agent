# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""rule dataclasses and enums for log analysis detectors.

新手说明:
这个文件放的是检测器规则相关的数据结构体和类型定义。
实际规则从 security_rules 模块加载，这里只定义数据结构。
"""

from dataclasses import dataclass, field
from typing import Any


# Dataclass for detector configuration
# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 DetectorConfig 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DetectorConfig 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class DetectorConfig:
    """configuration parameters for a soft detector."""

    window_minutes: int = 15
    failure_threshold: int = 5
    baselines: Any = None


# Dataclass for detector result
# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 DetectorResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DetectorResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class DetectorResult:
    """result from running a detector."""

    detector_id: str
    findings: list[Any]
    features: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
