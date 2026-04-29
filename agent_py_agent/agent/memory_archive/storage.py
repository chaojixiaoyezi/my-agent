from __future__ import annotations

"""LLM: JSONL storage primitives for memory hook snapshots and raw archive events.

给人看的解释：
这个文件只管把快照和归档事件写到固定目录。
它不决定什么时候压缩、不调用模型，也不读取配置；以后接入真实流程时，外层负责判断时机，这里负责可靠落盘。
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .models import CompressionSnapshot, RawMemoryEvent


class MemoryArchiveError(RuntimeError):
    """LLM: raised when archive writes cannot be verified after append.

    给人看的解释：
    快照写完必须能读回来。
    如果写了却读不到，说明这次恢复锚点不可靠，要明确报错，不能假装“记住了”。
    """


def snapshot_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:
    """LLM: map a snapshot timestamp to `memory/hooks/YYYY-MM-DD.jsonl`.

    给人看的解释：
    压缩前 hook 快照每天一个文件。
    比如 2026-04-30 的快照都会放到 `memory/hooks/2026-04-30.jsonl`，以后按天排查会很直观。
    """

    return Path(root) / "memory" / "hooks" / f"{_date_key(created_at)}.jsonl"


def raw_event_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:
    """LLM: map a raw archive event timestamp to `memory/raw/YYYY-MM-DD.jsonl`.

    给人看的解释：
    冷归档事件也按天分文件，但和 hook 快照分开存。
    这样 daily memory、压缩快照、原始流水不会混在一起，排错时能先看对应功能目录。
    """

    return Path(root) / "memory" / "raw" / f"{_date_key(created_at)}.jsonl"


def append_snapshot(root: str | Path, snapshot: CompressionSnapshot) -> Path:
    """LLM: append a compression snapshot and verify it can be read back.

    给人看的解释：
    这是压缩前最关键的一步：先写快照，再确认磁盘里真的有这一条。
    如果 readback 找不到同一个 `snapshot_id`，函数会报错，避免系统嘴上说保存了、实际没落盘。
    """

    path = snapshot_path_for(root, snapshot.created_at)
    payload = snapshot.to_dict()
    append_jsonl(path, payload, sort_keys=True)
    _verify_record_exists(path, key="snapshot_id", value=snapshot.snapshot_id, expected=payload)
    return path


def append_raw_event(root: str | Path, event: RawMemoryEvent) -> Path:
    """LLM: append one raw memory event to the daily cold archive JSONL.

    给人看的解释：
    这是一条普通冷归档流水。
    它可以记录用户消息、助手动作、工具调用、派工结果等；文件路径由 `created_at` 自动落到当天 raw 文件。
    """

    path = raw_event_path_for(root, event.created_at)
    append_jsonl(path, event.to_dict(), sort_keys=True)
    return path


def enforce_retention(root: str | Path, retention_days: Any, today: date | str | None = None) -> list[Path]:
    """LLM: delete hook snapshot files older than the configured retention window.

    给人看的解释：
    这里只清理 `memory/hooks/`，不碰 raw 冷归档。
    `retention_days=7` 表示保留今天和往前 6 天；`retention_days=0` 表示不删除。
    如果用户把配置写成 `abcd`、负数或奇怪内容，这里会安全跳过，不会崩，也不会误删。
    """

    days = _coerce_retention_days(retention_days)
    if days is None or days == 0:
        return []

    hook_dir = Path(root) / "memory" / "hooks"
    if not hook_dir.exists():
        return []

    today_date = _coerce_today(today) or date.today()
    cutoff = today_date - timedelta(days=days - 1)
    deleted: list[Path] = []

    for path in sorted(hook_dir.glob("*.jsonl")):
        file_date = _date_from_filename(path)
        if file_date is None or file_date >= cutoff:
            continue
        path.unlink()
        deleted.append(path)

    return deleted


def _verify_record_exists(path: Path, *, key: str, value: str, expected: dict[str, Any]) -> None:
    normalized_expected = _normalized_json(expected)
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get(key) != value:
            continue
        if _normalized_json(payload) != normalized_expected:
            raise MemoryArchiveError(f"readback payload mismatch for {key}={value}")
        return
    raise MemoryArchiveError(f"readback failed for {key}={value} in {path}")


def _normalized_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _date_key(created_at: str | int | float | None) -> str:
    parsed = _coerce_datetime(created_at)
    if parsed is None:
        return date.today().isoformat()
    return parsed.date().isoformat()


def _coerce_datetime(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
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


def _coerce_retention_days(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        days = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            return None
        days = int(text)
    else:
        return None

    if days < 0:
        return None
    return days


def _coerce_today(value: date | str | None) -> date | None:
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


def _date_from_filename(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None
