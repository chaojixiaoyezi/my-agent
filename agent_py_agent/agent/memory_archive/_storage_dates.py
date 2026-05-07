# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

"""Date and retention helpers for memory archive storage."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _date_key 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 date key 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _date_key(created_at: str | int | float | None) -> str:
    """Convert an optional timestamp-like value into a daily archive key."""
    parsed = _coerce_datetime(created_at)
    if parsed is None:
        return date.today().isoformat()
    return parsed.date().isoformat()


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_datetime 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce datetime 涉及的字段，让后续匹配和存储使用同一形态。
def _coerce_datetime(value: str | int | float | None) -> datetime | None:
    """Safely coerce a caller timestamp into a timezone-aware datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_retention_days 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce retention days 涉及的字段，让后续匹配和存储使用同一形态。
def _coerce_retention_days(value: Any) -> int | None:
    """Normalize retention-days config without throwing on bad user input."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    days = _days_from_text(value)
    if days is None or days < 0:
        return None
    return days


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _days_from_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 days from text 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _days_from_text(value: str) -> int | None:
    text = value.strip()
    if not text.isdecimal():
        return None
    return int(text)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_today 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce today 涉及的字段，让后续匹配和存储使用同一形态。
def _coerce_today(value: date | str | None) -> date | None:
    """Normalize an optional test override for today's date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _date_from_filename 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 date from filename 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _date_from_filename(path: Path) -> date | None:
    """Parse `YYYY-MM-DD.jsonl` filenames for retention decisions."""
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None
