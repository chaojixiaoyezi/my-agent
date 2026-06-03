
from __future__ import annotations

"""Small DTOs for bounded log queries."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BoundedQueryError:
    """受控查询的错误。"""

    error_type: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"error_type": self.error_type, "message": self.message, "details": self.details}


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

    def __post_init__(self) -> None:
        if not self.allowed_base_paths:
            self.allowed_base_paths = [
                "agent_py_agent/data/log_fixtures",
                "validation/security_fixtures",
            ]


__all__ = ["BoundedQueryConfig", "BoundedQueryError"]
