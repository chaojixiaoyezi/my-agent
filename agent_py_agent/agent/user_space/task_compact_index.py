from __future__ import annotations

from pathlib import Path


def current_or_first_compact_index(compact_root: Path) -> int:
    latest = compact_root / "latest.txt"
    if latest.exists():
        parsed = compact_index_from_name(latest.read_text(encoding="utf-8").strip())
        if parsed:
            return parsed
    latest_link = compact_root / "latest"
    if latest_link.exists() or latest_link.is_symlink():
        try:
            parsed = compact_index_from_name(latest_link.resolve().name)
        except OSError:
            parsed = compact_index_from_name(latest_link.name)
        if parsed:
            return parsed
    existing = sorted(compact_root.glob("compact_[0-9][0-9][0-9][0-9]"))
    if existing:
        return compact_index_from_name(existing[-1].name) or 1
    return 1


def compact_index_from_name(value: str) -> int:
    text = str(value or "").strip()
    if not text.startswith("compact_"):
        return 0
    try:
        return int(text.rsplit("_", 1)[-1])
    except ValueError:
        return 0
