# LLM: Spreadsheet builder models are shared by extraction and xlsx serialization.
# 模块用途: 保存通用表格构建的数据模型，避免工具入口和写入器互相复制结构。

from __future__ import annotations

from dataclasses import dataclass


# LLM: WorkbookSheet stores normalized table data before xlsx serialization.
# 类用途: 保存一个工作表的名字、列名和行数据，避免生成器读自然语言描述。
@dataclass(frozen=True)
class WorkbookSheet:
    name: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, object], ...]


__all__ = ["WorkbookSheet"]
