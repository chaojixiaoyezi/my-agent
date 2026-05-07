# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

from typing import Any


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 __getattr__ 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 getattr 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def __getattr__(name: str) -> Any:
    if name == "LocalEvidenceStore":
        from .evidence import LocalEvidenceStore

        return LocalEvidenceStore
    raise AttributeError(name)

__all__ = ["LocalEvidenceStore"]
