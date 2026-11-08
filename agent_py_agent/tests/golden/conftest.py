"""Shared golden data loading; capability enforcement remains in the existing root gate."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .replayer import replay_bounds


# LLM: The golden suite consumes the same versioned fixture bounds as the SLO suite.
# 函数用途: Load only test-owned process deadlines and model fixture parameters.
@pytest.fixture
def golden_parameters():
    path = Path(__file__).parent.parent / "slo" / "slo_targets.json"
    parameters = replay_bounds(json.loads(path.read_text())["fixture"])
    # 三次回放及重启共用同一天，不让宿主时区或 UTC 跨日改变目录分区。
    return {**parameters, "calendar_day": datetime.now(timezone.utc).date().isoformat()}
