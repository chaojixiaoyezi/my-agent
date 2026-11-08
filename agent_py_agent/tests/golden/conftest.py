"""Shared golden data loading; capability enforcement remains in the existing root gate."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .replayer import replay_bounds


@pytest.fixture
def tmp_path(tmp_path_factory):
    """固定回放根宽度，避免临时路径长度改变真实描述字段的裁剪位置。"""
    base = tmp_path_factory.mktemp("golden").resolve()
    # runner 的描述限长仍为真实的 300 字；128 字根让各平台沿原基线裁在时间戳内。
    padding = 128 - len(str(base)) - len("/run-0") - 1
    assert padding > 0, f"golden basetemp is too long for its fixed-width fixture: {base}"
    root = base / ("g" * padding)
    root.mkdir()
    return root


# LLM: The golden suite consumes the same versioned fixture bounds as the SLO suite.
# 函数用途: Load only test-owned process deadlines and model fixture parameters.
@pytest.fixture
def golden_parameters():
    path = Path(__file__).parent.parent / "slo" / "slo_targets.json"
    parameters = replay_bounds(json.loads(path.read_text())["fixture"])
    # 三次回放及重启共用同一天，不让宿主时区或 UTC 跨日改变目录分区。
    return {**parameters, "calendar_day": datetime.now(timezone.utc).date().isoformat()}
