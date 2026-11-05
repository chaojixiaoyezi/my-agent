# LLM: 仅提供可选扫描预算及续点存取合同；是否启用、持有何锁和何时提交由原留存服务决定。
# 模块用途: 为后续留存扫描提供条目、字节、时间计量与私有游标持久化。
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

# 留存扫描单片最多解析的候选条数。
RETENTION_SCAN_SLICE_ENTRIES = 128
# 留存扫描单片最多读取的字节数。
RETENTION_SCAN_SLICE_BYTES = 8 * 1024 * 1024
# 留存扫描单片墙钟预算毫秒数，目录枚举计入。
RETENTION_SCAN_SLICE_MS = 25


# LLM: 可变计量只属于本次扫描；不在构造时读取文件或启用维护。
# 类用途: 累计本片扫描消耗并检查三个预算上界。
@dataclass
class RetentionScanBudget:
    max_entries: int
    max_bytes: int
    deadline: float
    entries: int = 0
    size: int = 0

    # LLM: 消耗只能单调增加，负数或布尔输入不能恢复已用预算。
    # 函数用途: 累加本次读取的条目数与字节数。
    def charge(self, *, entries: int = 0, size: int = 0) -> None:
        if type(entries) is not int or entries < 0 or type(size) is not int or size < 0:
            raise ValueError("invalid_retention_scan_charge")
        self.entries += entries
        self.size += size

    # LLM: 任一维度到达上限即停止，时间使用单调时钟而非策略墙钟。
    # 函数用途: 判断本片扫描是否应当暂停。
    def exhausted(self) -> bool:
        return (
            self.entries >= self.max_entries
            or self.size >= self.max_bytes
            or time.monotonic() >= self.deadline
        )

    # LLM: 每次调用创建独立计量和截止时间，不共享上一片状态。
    # 函数用途: 使用冻结常量构造默认扫描预算。
    @classmethod
    def default(cls) -> RetentionScanBudget:
        return cls(
            RETENTION_SCAN_SLICE_ENTRIES,
            RETENTION_SCAN_SLICE_BYTES,
            time.monotonic() + RETENTION_SCAN_SLICE_MS / 1000,
        )


# LLM: 缺失、非法、过深JSON或不可读游标均从空续点重扫，不把坏字段伪造为已完成。
# 函数用途: 只读加载所有者的留存扫描续点。
def load_scan_cursor(home) -> dict:
    from ..common.json_io import read_json_object_report

    path = Path(home.owner_data_dir) / "retention_scan_cursor.json"
    try:
        report = read_json_object_report(path, context="retention_scan_budget.cursor")
    except (OSError, ValueError, RecursionError):
        return {}
    return report.payload if report.load_error is None else {}


# LLM: 沿用私有原子 JSON 写者；调用者负责在原留存锁或协调器提交门内调用。
# 函数用途: 原子保存本片执行后生成的扫描续点。
def save_scan_cursor(home, cursor: dict) -> None:
    from ..common.json_io import write_private_json_file_atomic

    if not isinstance(cursor, dict):
        raise ValueError("invalid_retention_scan_cursor")
    path = Path(home.owner_data_dir) / "retention_scan_cursor.json"
    write_private_json_file_atomic(path, cursor)
