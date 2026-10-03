# LLM: 锁定 common/cache_freshness 的两个纯函数：指纹必须覆盖 dev/ino/size/mtime/ctime（原子替换
#   只换 inode 也要变），窗口判断必须“够老才可信”（数值边界 + 方向都不能反过来）。
# 模块用途: 文件代次指纹与粗 mtime 信任窗口的回归测试；这两个判断被 json_io、response_renderer
#   和子代理持久层共用，改语义必须让本文件一起红。
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_freshness
from agent_py_agent.agent.common.cache_freshness import (
    CACHE_TRUST_AGE_SECONDS,
    cache_entry_trustworthy,
    cache_stat_signature,
)

pytestmark = pytest.mark.integration


# 函数用途: 造一个只填了本模块关心的字段的假 stat 结果，避免每条用例都真建文件。
def _fake_stat(**overrides: int) -> SimpleNamespace:
    values = {"st_dev": 1, "st_ino": 2, "st_size": 3, "st_mtime_ns": 4, "st_ctime_ns": 5}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_stat_signature_covers_dev_ino_size_mtime_ctime() -> None:
    base = _fake_stat()
    assert cache_stat_signature(base) == (1, 2, 3, 4, 5)
    other_device = _fake_stat(st_dev=9)
    other_inode = _fake_stat(st_ino=99)
    other_ctime = _fake_stat(st_ctime_ns=999)
    for changed in (other_device, other_inode, other_ctime):
        assert cache_stat_signature(changed) != cache_stat_signature(base)


def test_atomic_replace_in_same_tick_changes_signature(tmp_path: Path) -> None:
    """同一 mtime、同一大小，只换 inode：指纹必须变（真的走一次原子替换）。"""
    target = tmp_path / "state.json"
    target.write_text('{"a": 1}\n', encoding="utf-8")
    before = cache_stat_signature(target.stat())

    replacement = tmp_path / "state.json.tmp"
    replacement.write_text('{"b": 2}\n', encoding="utf-8")
    os.utime(replacement, ns=(before[3], before[3]))
    os.replace(replacement, target)
    os.utime(target, ns=(before[3], before[3]))

    after = cache_stat_signature(target.stat())
    assert after[3] == before[3], "本用例要求 mtime 相同"
    assert after[2] == before[2], "本用例要求大小相同"
    assert after != before, "同 mtime 同大小的原子替换必须靠 inode/ctime 被发现"


def test_trust_window_requires_file_to_be_old_enough(monkeypatch) -> None:
    clock = [time.time()]
    monkeypatch.setattr(cache_freshness.time, "time", lambda: clock[0])
    fresh_mtime_ns = int((clock[0] - 0.5) * 1_000_000_000)
    old_mtime_ns = int((clock[0] - CACHE_TRUST_AGE_SECONDS - 0.5) * 1_000_000_000)

    assert cache_entry_trustworthy(fresh_mtime_ns) is False, "窗口内的文件不许被信任"
    assert cache_entry_trustworthy(old_mtime_ns) is True, "窗口外的文件应可入缓存"

    # 时间往后走：同一份 mtime 自然离开窗口，方向不能反过来。用整数纳秒差值避免浮点误差。
    clock[0] = fresh_mtime_ns / 1_000_000_000 + CACHE_TRUST_AGE_SECONDS + 0.001
    assert cache_entry_trustworthy(fresh_mtime_ns) is True
