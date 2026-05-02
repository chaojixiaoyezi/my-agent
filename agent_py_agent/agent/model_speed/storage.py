from __future__ import annotations

"""LLM: model speed profile storage.

给人看的解释：
负责速度配置文件的保存和加载。
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from .models import SpeedProfile

if TYPE_CHECKING:
    pass


def save_speed_profile(profile: SpeedProfile, path: str | Path) -> None:
    """保存速度配置文件。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def load_speed_profile(path: str | Path) -> SpeedProfile | None:
    """加载速度配置文件，文件不存在时返回 None。"""

    target = Path(path)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return SpeedProfile.from_dict(data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None
