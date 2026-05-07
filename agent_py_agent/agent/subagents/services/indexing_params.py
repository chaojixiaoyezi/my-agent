# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

from dataclasses import dataclass


# LLM: LocalRecordParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存local记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class LocalRecordParams:
    """Bundle of log_local_record parameters."""
    source_type: str
    source_id: str
    title: str
    content: str
    event_type: str
    metadata: dict[str, object] | None = None


# LLM: IndexReportParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存index报告参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class IndexReportParams:
    """Bundle of report indexing fields."""

    # LLM: report indexing is one logical LocalStore event; keep it bundled.
    source_type: str
    source_id: str
    title: str
    report: object
    event_type: str


# LLM: DataclassRecordIndexParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存dataclass记录index参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class DataclassRecordIndexParams:
    """Bundle of dataclass record indexing fields."""

    source_type: str
    source_id: str
    title: str
    record: object
    event_type: str
