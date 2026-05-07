# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Small DTOs for bounded log queries."""

from dataclasses import dataclass, field
from typing import Any


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 BoundedQueryError 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 BoundedQueryError 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class BoundedQueryError:
    """受控查询的错误。"""

    error_type: str
    message: str
    details: dict[str, Any]

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return {"error_type": self.error_type, "message": self.message, "details": self.details}


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 BoundedQueryConfig 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 BoundedQueryConfig 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class BoundedQueryConfig:
    """受控查询的配置。"""

    default_max_results: int = 100
    default_max_file_size_mb: float = 10.0
    default_max_lines: int = 10000
    default_tail_lines: int = 1000
    enforce_time_window: bool = True
    allow_absolute_paths: bool = False
    allowed_base_paths: list[str] = field(default_factory=list)

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 __post_init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 post init 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def __post_init__(self) -> None:
        if not self.allowed_base_paths:
            self.allowed_base_paths = [
                "agent_py_agent/data/log_fixtures",
                "validation/security_fixtures",
            ]


__all__ = ["BoundedQueryConfig", "BoundedQueryError"]
