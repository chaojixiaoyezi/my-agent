
"""Date and retention helpers for memory archive storage."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def _date_key(created_at: str | int | float | None) -> str:
    """Convert an optional timestamp-like value into a daily archive key."""
    parsed = _coerce_datetime(created_at)
    if parsed is None:
        return date.today().isoformat()
    return parsed.date().isoformat()


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


def _days_from_text(value: str) -> int | None:
    text = value.strip()
    if not text.isdecimal():
        return None
    return int(text)


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


def _date_from_filename(path: Path) -> date | None:
    """Parse `YYYY-MM-DD.jsonl` filenames for retention decisions."""
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None
