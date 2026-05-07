# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Log analysis config services.

Facade sub-package exposing normalization and coercion services.
"""

from .coercion import (
    append_warning,
    coerce_bool,
    coerce_choice,
    coerce_int,
    coerce_path_string,
    lookup,
)
from .normalization import (
    normalize_all,
    normalize_choice_fields,
    normalize_core_bool_fields,
    normalize_dispatch_and_window_fields,
    normalize_path_and_version_fields,
    normalize_query_limit_fields,
    normalize_retention_and_preview_fields,
)

__all__ = [
    "append_warning",
    "coerce_bool",
    "coerce_choice",
    "coerce_int",
    "coerce_path_string",
    "lookup",
    "normalize_all",
    "normalize_core_bool_fields",
    "normalize_choice_fields",
    "normalize_dispatch_and_window_fields",
    "normalize_path_and_version_fields",
    "normalize_query_limit_fields",
    "normalize_retention_and_preview_fields",
]