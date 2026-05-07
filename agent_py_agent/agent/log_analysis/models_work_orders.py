# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Work-order and query DTOs exported by log_analysis.models."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from .models import EvidenceRef, JsonRoundTripMixin, _coerce_evidence_refs, utc_now_iso


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 SecurityCase 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SecurityCase 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class SecurityCase(JsonRoundTripMixin):
    """Security detector output used to seed a log-analysis work order."""

    case_id: str
    severity: str = "medium"
    event_class: str = "alert"
    trigger_entities: dict[str, list[str]] = field(default_factory=dict)
    initial_evidence: list[EvidenceRef] = field(default_factory=list)
    detector_id: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    attributes: dict[str, Any] = field(default_factory=dict)

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 from_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 from dict 需要的领域对象，统一缺省值和兼容字段。
    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> SecurityCase:
        allowed = {item.name for item in fields(cls)}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["initial_evidence"] = _coerce_evidence_refs(clean.get("initial_evidence", []))
        return cls(**clean)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 LogWorkOrder 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 LogWorkOrder 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class LogWorkOrder(JsonRoundTripMixin):
    """Bounded investigation request produced from a SecurityCase."""

    work_order_id: str
    case_id: str
    investigation_goal: str
    start_time: str
    end_time: str
    allowed_query_templates: list[str] = field(default_factory=list)
    max_results: int = 100
    evidence_budget: int = 1000
    created_at: str = field(default_factory=utc_now_iso)
    status: str = "OPEN"
    assigned_run_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 QueryResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 QueryResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class QueryResult(JsonRoundTripMixin):
    """Controlled-query result plus truncation and metadata."""

    query_template: str
    query_params: dict[str, Any] = field(default_factory=dict)
    time_window: dict[str, str] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    truncated: bool = False
    max_limit: int = 100
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["LogWorkOrder", "QueryResult", "SecurityCase"]
